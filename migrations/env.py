"""Alembic environment.

The database URL comes from application settings rather than `alembic.ini`, so
there is exactly one place a connection string is configured and no chance of a
migration running against a different database than the app. `alembic.ini` is
left without a `sqlalchemy.url` deliberately — a URL there would be a second
source of truth, and the wrong one.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from data_access import Base, create_engine_from_url
from stock_screener.config import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares the live database against this. A model that does not
# inherit from data_access.Base is invisible here.
target_metadata = Base.metadata


def _database_url() -> str:
    """Return the URL to migrate, preferring an explicit -x url= override."""
    override = context.get_x_argument(as_dictionary=True).get("url")
    return override or get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect and apply migrations.

    `render_as_batch` is enabled for SQLite, which cannot `ALTER TABLE` in place.
    Without it a future column change works against PostgreSQL and fails against
    a local SQLite database.
    """
    engine = create_engine_from_url(_database_url())

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=connection.dialect.name == "sqlite",
        )

        with context.begin_transaction():
            context.run_migrations()

    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
