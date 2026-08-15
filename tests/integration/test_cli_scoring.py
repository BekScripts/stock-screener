"""The Phase 2 commands, driven end to end against a temporary database.

The fixture carries what scoring actually needs — four years of quarters, a full
year of prices, and a benchmark series — because a company with four quarters of
history is `INSUFFICIENT_DATA`, which would make every assertion here vacuous.
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
    bars = []
    for offset in reversed(range(PRICE_DAYS)):
        price = end_price / daily**offset
        bars.append(
            {
                "date": (TODAY - timedelta(days=offset)).isoformat(),
                "open": price,
                "high": price * 1.001,
                "low": price * 0.999,
                "close": price,
                "volume": 1_000_000.0,
            }
        )
    return bars


def _periods(*, growth: float, base_revenue: float = 100_000_000.0) -> list[dict[str, Any]]:
    """Sixteen quarters growing at `growth` a year, with healthy economics."""
    periods = []
    for index in range(QUARTERS):
        revenue = base_revenue * (1 + growth) ** (index / 4)
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
        },
        {
            "ticker": "SLOW",
            "name": "Slow Industries Inc",
            "exchange": "NYSE",
            "sector": "Industrials",
            "industry": "Farm & Heavy Construction Machinery",
            "market_cap": 20_000_000_000,
            "average_volume": 1_000_000,
            "bars": _bars(end_price=90.0, annual_return=0.02),
            "periods": _periods(growth=0.03),
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
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary database and the scoring fixture."""
    fixture_path = tmp_path / "universe.json"
    fixture_path.write_text(json.dumps(FIXTURE), encoding="utf-8")

    database_path = tmp_path / "radar.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database_path}")
    monkeypatch.setenv("FIXTURE_PATH", str(fixture_path))
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()

    from data_access import create_all, create_engine_from_url

    engine = create_engine_from_url(f"sqlite:///{database_path}")
    create_all(engine)
    engine.dispose()
    return database_path


def _count(database_path: Path, table: str) -> int:
    """Return the row count of one table."""
    connection = sqlite3.connect(database_path)
    try:
        # The table name is a literal from the caller, not input — S608 cannot tell.
        return int(connection.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
    finally:
        connection.close()


def _ingest(runner: CliRunner) -> None:
    """Run the ingestion commands scoring depends on."""
    runner.invoke(app, ["update-universe"])
    runner.invoke(app, ["update-market"])
    runner.invoke(app, ["update-benchmark"])
    runner.invoke(app, ["update-fundamentals"])


@pytest.mark.integration
def test_the_benchmark_is_stored_without_entering_the_company_universe(cli_env: Path) -> None:
    runner = CliRunner()

    runner.invoke(app, ["update-universe"])
    result = runner.invoke(app, ["update-benchmark"])

    assert result.exit_code == 0, result.output
    connection = sqlite3.connect(cli_env)
    try:
        tickers = {row[0] for row in connection.execute("select ticker from companies")}
        latest = connection.execute("select max(date) from benchmark_prices").fetchone()[0]
    finally:
        connection.close()

    # Stored under its own symbol, and absent from the company universe: the
    # benchmark is an ETF, and a scan must never rank one.
    assert _count(cli_env, "benchmark_prices") > 0
    assert latest == TODAY.isoformat()
    assert "SPY" not in tickers


@pytest.mark.integration
def test_score_persists_one_snapshot_per_company(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)

    result = runner.invoke(app, ["score"])

    assert result.exit_code == 0, result.output
    assert "scored=2" in result.output
    assert _count(cli_env, "score_snapshots") == 2


@pytest.mark.integration
def test_running_score_twice_in_a_day_does_not_duplicate_snapshots(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)

    runner.invoke(app, ["score"])
    runner.invoke(app, ["score"])

    assert _count(cli_env, "score_snapshots") == 2


@pytest.mark.integration
def test_a_dry_run_scores_without_writing(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)

    result = runner.invoke(app, ["score", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert _count(cli_env, "score_snapshots") == 0


@pytest.mark.integration
def test_rankings_show_the_faster_growing_company_first(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["rankings"])

    assert result.exit_code == 0, result.output
    assert result.output.index("XYZ") < result.output.index("SLOW")
    assert "Top Opportunities: 2 companies" in result.output


@pytest.mark.integration
def test_rankings_can_be_filtered_by_score(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["rankings", "--min-score", "99"])

    assert "No companies matched." in result.output


@pytest.mark.integration
def test_rankings_export_to_csv(cli_env: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])
    output = tmp_path / "exports" / "rankings.csv"

    result = runner.invoke(app, ["rankings", "--output", str(output)])

    assert result.exit_code == 0, result.output
    contents = output.read_text(encoding="utf-8")
    assert "rank,ticker,company" in contents
    assert "XYZ" in contents


@pytest.mark.integration
def test_hidden_gems_finds_the_small_fast_grower(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["hidden-gems"])

    assert result.exit_code == 0, result.output
    assert "XYZ" in result.output
    assert "SLOW" not in result.output


@pytest.mark.integration
def test_improving_is_empty_without_score_history(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["improving"])

    assert result.exit_code == 0, result.output
    assert "No companies matched." in result.output


@pytest.mark.integration
def test_explain_prints_the_metric_by_metric_breakdown(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["explain", "xyz"])

    assert result.exit_code == 0, result.output
    assert "Growth:" in result.output
    assert "revenue_growth" in result.output
    assert "Risk:" in result.output


@pytest.mark.integration
def test_explaining_an_unscored_company_fails_clearly(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["explain", "NOPE"])

    assert result.exit_code == 1
    assert "No score stored for NOPE" in result.output


@pytest.mark.integration
def test_run_daily_ingests_scores_and_ranks_in_one_pass(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run-daily"])

    assert result.exit_code == 0, result.output
    assert "benchmark SPY" in result.output
    assert "scoring:" in result.output
    assert "XYZ" in result.output
    assert _count(cli_env, "score_snapshots") == 2


@pytest.mark.integration
def test_scoring_without_a_benchmark_leaves_nothing_scoreable(cli_env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["update-universe"])
    runner.invoke(app, ["update-market"])
    runner.invoke(app, ["update-fundamentals"])

    result = runner.invoke(app, ["score"])

    assert result.exit_code == 0, result.output
    assert "insufficient_data=2" in result.output


@pytest.mark.integration
def test_help_lists_the_scoring_commands() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("score", "rankings", "hidden-gems", "improving", "explain", "run-daily"):
        assert command in result.output


@pytest.mark.integration
def test_enrich_is_skipped_when_the_provider_has_nothing_to_verify(cli_env: Path) -> None:
    # The fixture provider supplies no vendor market cap or consolidated volume,
    # so spending a request per candidate would buy nothing.
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["enrich"])

    assert result.exit_code == 0, result.output
    assert "enrichment skipped" in result.output
    assert "PRELIMINARY" in result.output


@pytest.mark.integration
def test_rankings_are_produced_without_any_metered_provider(cli_env: Path) -> None:
    runner = CliRunner()
    _ingest(runner)
    runner.invoke(app, ["score"])

    result = runner.invoke(app, ["rankings"])

    assert result.exit_code == 0, result.output
    assert "XYZ" in result.output
