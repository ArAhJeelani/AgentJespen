import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url


def database_url() -> str:
    return os.environ["DATABASE_URL"]


def postgres_engine(url: str) -> Engine:
    if make_url(url).drivername != "postgresql+psycopg":
        raise ValueError("Use a postgresql+psycopg database URL")
    return create_engine(url, isolation_level="READ COMMITTED", pool_pre_ping=True, hide_parameters=True)
