"""The `research` command group, driven end to end against a temporary database.

Inspection commands, so the assertions are about what an operator can see: which
companies would be researched, what evidence a brief carries, and — the one that
matters most before any of this costs money — that a brief with no score behind
it fails loudly rather than printing something plausible.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

TODAY = date(2026, 6, 30)
QUARTERS = 16
PRICE_DAYS = 400


def _bars(*, end_price: float, annual_return: float) -> list[dict[str, Any]]:
    """Daily bars compounding to `annual_return` over a year."""
    daily = (1 + annual_return) ** (1 / 365)
    return [
        {
            "date": (TODAY - timedelta(days=offset)).isoformat(),
            "open": (price := end_price / daily**offset),
            "high": price * 1.001,
            "low": price * 0.999,
            "close": price,
            "volume": 1_000_000.0,
        }
        for offset in reversed(range(PRICE_DAYS))
    ]


def _periods(*, growth: float) -> list[dict[str, Any]]:
    """Sixteen quarters growing at `growth` a year, with healthy economics."""
    periods = []
    for index in range(QUARTERS):
        revenue = 100_000_000.0 * (1 + growth) ** (index / 4)
        periods.append(
            {
                "period_end": (TODAY - timedelta(days=91 * (QUARTERS - 1 - index))).isoformat(),
                "revenue": revenue,
                "gross_profit": revenue * 0.62,
                "operating_income": revenue * 0.10,
                "free_cash_flow": revenue * 0.15,
                "cash": 400_000_000.0,
                "total_debt": 50_000_000.0,
                "shares_outstanding": 100_000_000.0 * 1.01 ** (index / 4),
            }
        )
    return periods


FIXTURE = {
    "companies": [
        {
            "ticker": "XYZ",
            "name": "Example Corp",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "industry": "Software - Application",
            "market_cap": 1_200_000_000,
            "average_volume": 1_000_000,
            "bars": _bars(end_price=40.0, annual_return=0.45),
            "periods": _periods(growth=0.45),
            "filings": [
                {
                    "accession": "0000000000-26-000001",
                    "form": "10-K",
                    "filed": (TODAY - timedelta(days=150)).isoformat(),
                    "period_end": (TODAY - timedelta(days=180)).isoformat(),
                    "primary_document": "xyz-10k.htm",
                    "url": "https://www.sec.gov/Archives/xyz-10k.htm",
                },
                {
                    "accession": "0000000000-26-000002",
                    "form": "10-Q",
                    "filed": (TODAY - timedelta(days=40)).isoformat(),
                    "period_end": (TODAY - timedelta(days=60)).isoformat(),
                    "primary_document": "xyz-10q.htm",
                    "url": "https://www.sec.gov/Archives/xyz-10q.htm",
                },
            ],
        },
        {
            "ticker": "SPY",
            "name": "SPDR S&P 500 ETF Trust",
            "exchange": "ARCA",
            "market_cap": 600_000_000_000,
            "bars": _bars(end_price=500.0, annual_return=0.10),
            "periods": [],
        },
    ]
}


@pytest.fixture
def cli_database(tmp_path: Path) -> Path:
    """Where the CLI's temporary database lives."""
    return tmp_path / "radar.db"


def _count(database_path: Path, table: str) -> int:
    """Return the row count of one table."""
    connection = sqlite3.connect(database_path)
    try:
        # The table name is a literal from the caller, not input — S608 cannot tell.
        return int(connection.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
    finally:
        connection.close()


@pytest.fixture
def scored_cli(tmp_path: Path, cli_database: Path, monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    """Point the CLI at a temporary database and score the fixture into it."""
    fixture_path = tmp_path / "universe.json"
    fixture_path.write_text(json.dumps(FIXTURE), encoding="utf-8")

    database_path = cli_database
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

    from data_access import create_all, create_engine_from_url

    engine = create_engine_from_url(f"sqlite:///{database_path}")
    create_all(engine)
    engine.dispose()

    runner = CliRunner()
    for command in (
        ["update-universe"],
        ["update-market"],
        ["update-benchmark"],
        ["update-fundamentals"],
        ["score"],
    ):
        result = runner.invoke(app, command)
        assert result.exit_code == 0, f"{command} failed: {result.output}"
    return runner


@pytest.mark.integration
@pytest.mark.slow
def test_candidates_lists_the_scored_company(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "candidates"])

    assert result.exit_code == 0
    assert "XYZ" in result.output
    assert "TOP_RANKED" in result.output
    assert "1 selected" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_candidates_respects_a_limit(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "candidates", "--limit", "1"])

    assert result.exit_code == 0
    assert "1 selected" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_brief_shows_what_the_evidence_contains(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "brief", "XYZ"])

    assert result.exit_code == 0
    assert "XYZ — Example Corp" in result.output
    assert "fingerprint" in result.output
    assert "COMPOUNDER_V1_1" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_brief_accepts_a_lowercase_ticker(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "brief", "xyz"])

    assert result.exit_code == 0
    assert "XYZ" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_brief_emits_the_whole_brief_as_json(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "brief", "XYZ", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["ticker"] == "XYZ"
    assert payload["contract_version"] == "RESEARCH_V1"
    assert payload["facts"]


@pytest.mark.integration
@pytest.mark.slow
def test_brief_fails_for_a_company_that_was_never_scored(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "brief", "NOPE"])

    assert result.exit_code == 1
    assert "No brief for NOPE" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_update_filings_stores_the_fixture_index(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "update-filings", "-t", "XYZ"])

    assert result.exit_code == 0
    assert "succeeded=1" in result.output
    assert "rows=2" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_a_brief_cites_filings_once_they_are_ingested(scored_cli: CliRunner) -> None:
    before = scored_cli.invoke(app, ["research", "brief", "XYZ"])
    assert "filings      0" in before.output

    scored_cli.invoke(app, ["research", "update-filings", "-t", "XYZ"])
    after = scored_cli.invoke(app, ["research", "brief", "XYZ"])

    assert after.exit_code == 0
    assert "filings      2" in after.output
    assert "10-Kx1" in after.output
    assert "10-Qx1" in after.output


@pytest.mark.integration
@pytest.mark.slow
def test_run_dry_run_prints_the_prompt_and_calls_nothing(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "run", "XYZ", "--dry-run"])

    assert result.exit_code == 0
    assert "You are a research assistant for Compounder Radar" in result.output
    assert "# CompounderScore (given — do not recompute)" in result.output
    assert "XYZ" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_run_dry_run_needs_a_ticker(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "run", "--dry-run"])

    assert result.exit_code == 1
    assert "needs a ticker" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_an_accidental_default_run_is_refused_and_writes_nothing(
    scored_cli: CliRunner, cli_database: Path
) -> None:
    # RESEARCH_PROVIDER defaults to `mock` with nothing loaded. That is somebody
    # running the real command before configuring a model, and it must cost
    # nothing: no call, and no table full of FAILED rows that say nothing about
    # any company.
    result = scored_cli.invoke(app, ["research", "run", "XYZ"])

    assert result.exit_code == 2
    assert "RESEARCH_PROVIDER" in result.output
    assert _count(cli_database, "research_reports") == 0


@pytest.mark.integration
@pytest.mark.slow
def test_a_real_provider_without_a_key_is_refused_before_anything_runs(
    scored_cli: CliRunner, cli_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESEARCH_PROVIDER", "anthropic")
    get_settings.cache_clear()

    result = scored_cli.invoke(app, ["research", "run", "XYZ"])

    assert result.exit_code == 2
    assert "credential" in result.output
    assert _count(cli_database, "research_reports") == 0


@pytest.mark.integration
@pytest.mark.slow
def test_a_real_provider_with_a_key_gets_past_preflight(
    scored_cli: CliRunner, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Preflight is local: a syntactically present key passes it. Whether the
    # vendor accepts the key is a runtime question, and answering it costs a
    # request — so preflight does not ask.
    monkeypatch.setenv("RESEARCH_PROVIDER", "anthropic")
    monkeypatch.setenv("RESEARCH_API_KEY", "sk-ant-not-a-real-key")
    get_settings.cache_clear()

    result = scored_cli.invoke(app, ["research", "run", "NOPE"])

    assert result.exit_code == 0
    assert "NO_BRIEF" in result.output


@pytest.mark.integration
@pytest.mark.slow
def test_update_filings_can_restrict_itself_to_candidates(scored_cli: CliRunner) -> None:
    result = scored_cli.invoke(app, ["research", "update-filings", "--candidates"])

    assert result.exit_code == 0
    assert "research candidate(s)" in result.output
