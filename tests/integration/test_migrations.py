"""Migrations must work from a clean database, and match the models.

The second test is the one that earns its keep. `create_all` and Alembic are two
independent descriptions of the same schema, and they drift the moment someone
adds a column to a model without generating a revision. Autogenerate comparing
them and finding nothing is what proves they still agree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from data_access import Base, create_engine_from_url

_TABLES = {"companies", "financial_snapshots", "price_history"}

# The autouse `isolated_env` fixture chdirs into a tmp_path, so both paths are
# resolved from this file rather than from the working directory.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config(database_url: str) -> Config:
    """Build an Alembic config pointed at a throwaway database."""
    config = Config(str(_PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
    config.cmd_opts = None
    config.attributes["configure_logger"] = False
    # `-x url=` is how env.py takes an override without touching the environment.
    config.cmd_opts = type("Opts", (), {"x": [f"url={database_url}"]})()
    return config


@pytest.mark.integration
@pytest.mark.slow
def test_upgrade_creates_every_table_from_an_empty_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"

    command.upgrade(_alembic_config(url), "head")

    engine = create_engine_from_url(url)
    try:
        assert set(inspect(engine).get_table_names()) >= _TABLES
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_the_migrated_schema_matches_the_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    command.upgrade(_alembic_config(url), "head")

    engine = create_engine_from_url(url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()

    assert differences == []


@pytest.mark.integration
@pytest.mark.slow
def test_downgrade_removes_every_table(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")

    engine = create_engine_from_url(url)
    try:
        assert _TABLES.isdisjoint(set(inspect(engine).get_table_names()))
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_upgrading_an_already_current_database_is_a_no_op(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'fresh.db'}"
    config = _alembic_config(url)

    command.upgrade(config, "head")
    command.upgrade(config, "head")

    engine = create_engine_from_url(url)
    try:
        assert set(inspect(engine).get_table_names()) >= _TABLES
    finally:
        engine.dispose()
