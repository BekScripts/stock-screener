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
from sqlalchemy import inspect, text

from data_access import Base, create_engine_from_url

_TABLES = {
    "companies",
    "financial_snapshots",
    "price_history",
    "score_snapshots",
    "benchmark_prices",
    "research_reports",
    "filings",
}

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


@pytest.mark.integration
@pytest.mark.slow
def test_migrating_a_populated_database_preserves_its_rows(tmp_path: Path) -> None:
    """Rows must survive an upgrade, not merely the tables holding them.

    This is not hypothetical. Migration `0017` renamed one column on `companies`
    with `batch_alter_table`, which on SQLite rebuilds the table — create, copy,
    drop, rename. Dropping `companies` under enforced foreign keys cascaded
    through every child declaring `ON DELETE CASCADE` and deleted about 1.6
    million rows: all price history, all fundamentals, all scores, every filing
    and excerpt, both research tables and the user's watchlist. Only
    `benchmark_prices` and `jobs` survived, being the two tables with no foreign
    key to `companies`.

    Every table above therefore gets a row here before the upgrade and is
    counted after it. A schema-only migration test cannot catch this, because
    the schema was perfect on the other side.
    """
    url = f"sqlite:///{tmp_path / 'populated.db'}"
    engine = create_engine_from_url(url)
    try:
        command.upgrade(_alembic_config(url), "0015")

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO companies (id, ticker, name, is_active) VALUES (1, 'AAA', 'A', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO price_history (company_id, date, open, high, low, close, volume) "
                    "VALUES (1, '2026-01-02', 1, 1, 1, 1, 100)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO financial_snapshots (company_id, period_end, revenue, source) "
                    "VALUES (1, '2025-12-31', 500, 'test')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO score_snapshots "
                    "(company_id, score_date, score_version, scoring_status, coverage) "
                    "VALUES (1, '2026-01-02', 'COMPOUNDER_V1_1', 'SCORED', 'MARKET')"
                )
            )
            connection.execute(text("INSERT INTO watchlist (company_id) VALUES (1)"))

        command.upgrade(_alembic_config(url), "head")

        with engine.begin() as connection:
            counts = {
                "companies": text("SELECT COUNT(*) FROM companies"),
                "price_history": text("SELECT COUNT(*) FROM price_history"),
                "financial_snapshots": text("SELECT COUNT(*) FROM financial_snapshots"),
                "score_snapshots": text("SELECT COUNT(*) FROM score_snapshots"),
                "watchlist": text("SELECT COUNT(*) FROM watchlist"),
            }
            for table, statement in counts.items():
                assert connection.execute(statement).scalar() == 1, (
                    f"{table} lost its rows during the upgrade"
                )
    finally:
        engine.dispose()
