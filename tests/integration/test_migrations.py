"""Migrations must work from a clean database, and match the models.

The second test is the one that earns its keep. `create_all` and Alembic are two
independent descriptions of the same schema, and they drift the moment someone
adds a column to a model without generating a revision. Autogenerate comparing
them and finding nothing is what proves they still agree.
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
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
    # Phase 8B's point-in-time layer. Listed so `test_downgrade_removes_every_table`
    # actually exercises 0021's downgrade rather than assuming it works.
    "pit_capture_runs",
    "pit_score_snapshots",
    "pit_feature_snapshots",
    "fundamental_observations",
    "universe_events",
    "corporate_actions",
    # Phase 8H's historical-security layer, keyed by CIK and separate from
    # `companies` so a delisted name is addressable without polluting the
    # investable set.
    "historical_securities",
    "historical_observations",
    "historical_prices",
    "historical_corporate_actions",
    # Phase 9A's identity layer: dated identifiers against a stable security id,
    # and the fails-to-deliver evidence they are built from.
    "security_identifiers",
    "ftd_source_files",
    "ftd_identity_observations",
    # Phase 9C's membership layer: effective-dated verdicts against both
    # identity kinds, so a delisted security can hold membership at all.
    "universe_membership",
}

#: Phase 9D gave both point-in-time snapshot tables a dual identity; they are
#: already in `_TABLES` above under their Phase 8B entries.

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
            connection.execute(
                text(
                    "INSERT INTO filings (company_id, accession, form, filed, url, source) "
                    "VALUES (1, 'a-1', '10-K', '2026-01-02', 'http://x', 'test')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO filing_excerpts "
                    "(company_id, accession, form, section, text, filed, url, source) "
                    "VALUES (1, 'a-1', '10-K', 'business', 'body', '2026-01-02', "
                    "'http://x', 'test')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO research_reports (company_id, score_version, score_date, "
                    "brief_fingerprint, contract_version, prompt_version, model_id, status, "
                    "generated_at, report) VALUES (1, 'COMPOUNDER_V1_1', '2026-01-02', 'f', "
                    "'1', '1', 'm', 'COMPLETE', '2026-01-02 00:00:00', '{}')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO deep_research_reports (company_id, ticker, as_of, "
                    "score_version, deterministic_fingerprint, evidence_fingerprint, "
                    "contract_version, prompt_version, model_id, status, confidence, "
                    "generated_at, validated_report_json) VALUES (1, 'AAA', '2026-01-02', "
                    "'COMPOUNDER_V1_1', 'd', 'e', '1', '1', 'm', 'COMPLETE', 'MEDIUM', "
                    "'2026-01-02 00:00:00', '{}')"
                )
            )

        command.upgrade(_alembic_config(url), "head")

        with engine.begin() as connection:
            # Every table declaring ON DELETE CASCADE against `companies`. Those
            # are exactly the tables a rebuild of that parent empties; the two
            # that survived it, `benchmark_prices` and `jobs`, are the two with
            # no foreign key to it.
            counts = {
                "companies": text("SELECT COUNT(*) FROM companies"),
                "price_history": text("SELECT COUNT(*) FROM price_history"),
                "financial_snapshots": text("SELECT COUNT(*) FROM financial_snapshots"),
                "score_snapshots": text("SELECT COUNT(*) FROM score_snapshots"),
                "filings": text("SELECT COUNT(*) FROM filings"),
                "filing_excerpts": text("SELECT COUNT(*) FROM filing_excerpts"),
                "research_reports": text("SELECT COUNT(*) FROM research_reports"),
                "deep_research_reports": text("SELECT COUNT(*) FROM deep_research_reports"),
                "watchlist": text("SELECT COUNT(*) FROM watchlist"),
            }
            for table, statement in counts.items():
                assert connection.execute(statement).scalar() == 1, (
                    f"{table} lost its rows during the upgrade"
                )
    finally:
        engine.dispose()


@pytest.mark.integration
@pytest.mark.slow
def test_the_identity_migration_preserves_a_populated_historical_security(
    tmp_path: Path,
) -> None:
    """0027 rebuilds `historical_securities`; its three children must survive.

    Making `cik` nullable needs a table rebuild on SQLite — create, copy, drop,
    rename — and `historical_securities` has three children declaring
    `ON DELETE CASCADE`. Dropping a parent under enforced foreign keys is exactly
    what `0017` did to `companies`, deleting about 1.6 million rows.

    The migration disables enforcement for the rebuild and verifies the pragma
    took effect, because `PRAGMA foreign_keys` is silently ignored inside an open
    transaction and Alembic normally has one open. This test is the proof that
    the whole arrangement works on the real upgrade path: a schema-only check
    would pass against an emptied database.
    """
    url = f"sqlite:///{tmp_path / 'identity.db'}"
    engine = create_engine_from_url(url)
    try:
        command.upgrade(_alembic_config(url), "0026")

        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO companies (id, ticker, name, is_active) VALUES (1, 'AAA', 'A', 1)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO historical_securities "
                    "(id, cik, name, primary_ticker, status) "
                    "VALUES (1, 42, 'Gone Inc', 'GONE', 'ACQUIRED')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO historical_prices "
                    "(historical_security_id, date, open, high, low, close, volume) "
                    "VALUES (1, '2020-01-02', 1, 2, 0.5, 1.5, 100)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO historical_observations (historical_security_id, "
                    "observation_fingerprint, taxonomy, concept, value, unit, period_end, source) "
                    "VALUES (1, 'fp', 'us-gaap', 'Revenues', 100, 'USD', '2019-12-31', 'test')"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO historical_corporate_actions (historical_security_id, "
                    "action_fingerprint, ticker, action_type, ex_date, source) "
                    "VALUES (1, 'af', 'GONE', 'REVERSE_SPLIT', '2020-01-03', 'test')"
                )
            )
            for horizon in (1, 5, 20, 60, 120):
                connection.execute(
                    text(
                        "INSERT INTO forward_returns (company_id, observation_date, "
                        "horizon_sessions, status, entry_date, entry_price, exit_date, "
                        "exit_price, total_return) VALUES (1, '2024-06-28', :horizon, "
                        "'REALISED', '2024-07-01', 10.0, '2024-07-02', 11.0, 0.1)"
                    ),
                    {"horizon": horizon},
                )

        command.upgrade(_alembic_config(url), "head")

        with engine.begin() as connection:
            children = {
                "historical_securities": text("SELECT COUNT(*) FROM historical_securities"),
                "historical_prices": text("SELECT COUNT(*) FROM historical_prices"),
                "historical_observations": text("SELECT COUNT(*) FROM historical_observations"),
                "historical_corporate_actions": text(
                    "SELECT COUNT(*) FROM historical_corporate_actions"
                ),
            }
            for table, statement in children.items():
                assert connection.execute(statement).scalar() == 1, (
                    f"{table} lost its rows when historical_securities was rebuilt"
                )

            # The CIK is nullable on the other side, and a security without one
            # is now storable — the whole point of the revision.
            nullable = [
                row
                for row in connection.execute(text("PRAGMA table_info(historical_securities)"))
                if row[1] == "cik"
            ]
            assert nullable and nullable[0][3] == 0

            # Every forward return keeps its company and its values, and gains a
            # NULL historical identity. Not one row is migrated.
            rows = list(
                connection.execute(
                    text(
                        "SELECT company_id, historical_security_id, status, entry_price, "
                        "exit_price, total_return FROM forward_returns ORDER BY horizon_sessions"
                    )
                )
            )
            assert len(rows) == 5
            assert {row[0] for row in rows} == {1}
            assert {row[1] for row in rows} == {None}
            assert {row[2] for row in rows} == {"REALISED"}
            assert {row[3] for row in rows} == {10.0}
            assert {row[5] for row in rows} == {0.1}
    finally:
        engine.dispose()


class TestAConfigUrlCannotSilentlyRedirectAMigration:
    """The guard in `migrations/env.py`, asserted.

    Setting `sqlalchemy.url` on an Alembic `Config` reads like it targets that
    database. It does not — `env.py` resolves the URL from application settings —
    so the silent version of this ran four revisions against the production
    database while the caller believed it was working on a staging copy. The
    override must be `-x url=`, and a config URL must fail loudly.
    """

    def test_setting_sqlalchemy_url_raises_instead_of_migrating_the_configured_database(
        self, tmp_path: Path
    ) -> None:
        config = Config(str(_PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
        config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'decoy.db'}")

        with pytest.raises(RuntimeError, match="ignored here"):
            command.upgrade(config, "head")

    def test_the_x_url_override_is_the_supported_way_to_target_another_database(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "explicit.db"
        config = Config(str(_PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(_PROJECT_ROOT / "migrations"))
        config.cmd_opts = Namespace(x=[f"url=sqlite:///{target}"])

        command.upgrade(config, "head")

        # `sqlite3.connect` as a context manager commits; it does not close.
        # A leaked connection surfaces later as an unraisable exception in
        # whichever test the collection happens to reach next.
        connection = sqlite3.connect(target)
        try:
            version = connection.execute("select version_num from alembic_version").fetchone()
        finally:
            connection.close()
        assert version is not None
