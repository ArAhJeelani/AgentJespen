import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.db import postgres_engine
from app.main import create_app


@pytest.fixture(scope="session")
def database():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.fail("Set TEST_DATABASE_URL to a PostgreSQL test database (see README)")
    engine = postgres_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.dialect.name == "postgresql"
            connection.execute(text("SHOW server_version"))
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def engine(database):
    schema = f"test_{uuid4().hex}"
    with database.begin() as connection:
        connection.execute(text(f"CREATE SCHEMA {schema}"))
    scoped = create_engine(
        database.url,
        isolation_level="READ COMMITTED",
        connect_args={"options": f"-c search_path={schema} -c statement_timeout=15000"},
    )
    try:
        config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
        with scoped.connect() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        yield scoped
    finally:
        scoped.dispose()
        with database.begin() as connection:
            connection.execute(text(f"DROP SCHEMA {schema} CASCADE"))


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine)) as client:
        yield client
