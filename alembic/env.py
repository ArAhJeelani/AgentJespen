from alembic import context

from app.db import database_url, postgres_engine


def migrate(connection):
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    raise RuntimeError("Run migrations against PostgreSQL")
elif connection := context.config.attributes.get("connection"):
    migrate(connection)
else:
    engine = postgres_engine(database_url())
    try:
        with engine.connect() as connection:
            migrate(connection)
    finally:
        engine.dispose()
