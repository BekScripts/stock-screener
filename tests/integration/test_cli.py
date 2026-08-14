"""CLI tests, driving real commands against a temporary SQLite database.

Each test points `DATABASE_URL` at a file in `tmp_path` and `FIXTURE_PATH` at a
small fixture, so the commands do everything they do in production except reach
the network.
"""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import get_settings

if TYPE_CHECKING:
    from pathlib import Path

FIXTURE = {
    "companies": [
        {
            "ticker": "XYZ",
            "name": "Example Corp",
            "exchange": "NASDAQ",
            "sector": "Technology",
            "market_cap": 1_200_000_000,
            "bars": [
                {
                    "date": f"2026-06-{day:02d}",
                    "open": 25.0,
                    "high": 25.5,
                    "low": 24.5,
                    "close": 25.0,
                    "volume": 500_000,
                }
                for day in range(1, 26)
            ],
            "periods": [
                {"period_end": "2025-09-30", "revenue": 80_000_000, "gross_profit": 30_000_000},
                {"period_end": "2025-12-31", "revenue": 85_000_000, "gross_profit": 32_000_000},
                {"period_end": "2026-03-31", "revenue": 90_000_000, "gross_profit": 34_000_000},
                {"period_end": "2026-06-30", "revenue": 100_000_000, "gross_profit": 38_000_000},
            ],
        },
        {
            "ticker": "PENY",
            "name": "Pennywise Logistics Inc",
            "exchange": "NASDAQ",
            "market_cap": 140_000_000,
            "bars": [
                {
                    "date": f"2026-06-{day:02d}",
                    "open": 1.5,
                    "high": 1.6,
                    "low": 1.4,
                    "close": 1.55,
                    "volume": 900_000,
                }
                for day in range(1, 26)
            ],
            "periods": [],
        },
    ]
}


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI at a temporary database and fixture file."""
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


def _row_counts(database_path: Path) -> tuple[int, int, int]:
    """Return row counts for the three tables."""
    connection = sqlite3.connect(database_path)
    try:
        counts = [
            # The table names are literals below, not input — S608 cannot tell.
            int(connection.execute(f"select count(*) from {table}").fetchone()[0])  # noqa: S608
            for table in ("companies", "financial_snapshots", "price_history")
        ]
    finally:
        connection.close()
    return counts[0], counts[1], counts[2]


@pytest.mark.integration
def test_run_scan_ingests_and_prints_an_eligible_company(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run-scan"])

    assert result.exit_code == 0, result.output
    assert "XYZ" in result.output
    assert "Eligible: 1" in result.output


@pytest.mark.integration
def test_run_scan_excludes_the_sub_two_dollar_stock(cli_env: Path) -> None:
    result = CliRunner().invoke(app, ["run-scan"])

    # PENY trades at $1.55, so it must not appear in the eligible table.
    assert "PENY" not in result.output


@pytest.mark.integration
def test_running_run_scan_twice_does_not_duplicate_rows(cli_env: Path) -> None:
    runner = CliRunner()

    runner.invoke(app, ["run-scan"])
    first = _row_counts(cli_env)
    runner.invoke(app, ["run-scan"])

    assert _row_counts(cli_env) == first


@pytest.mark.integration
def test_scan_all_shows_excluded_companies(cli_env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["run-scan"])

    result = runner.invoke(app, ["scan", "--all"])

    assert "PENY" in result.output
    assert "Excluded: 1" in result.output


@pytest.mark.integration
def test_scan_writes_every_company_to_the_csv(cli_env: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["run-scan"])
    output = tmp_path / "out" / "scan.csv"

    result = runner.invoke(app, ["scan", "--output", str(output)])

    assert result.exit_code == 0, result.output
    assert output.is_file()
    # The export always includes exclusions, even when the table does not.
    assert "PENY" in output.read_text(encoding="utf-8")


@pytest.mark.integration
def test_the_commands_can_be_run_one_at_a_time(cli_env: Path) -> None:
    runner = CliRunner()

    assert runner.invoke(app, ["update-universe"]).exit_code == 0
    assert runner.invoke(app, ["update-market"]).exit_code == 0
    assert runner.invoke(app, ["update-fundamentals"]).exit_code == 0
    assert runner.invoke(app, ["scan"]).exit_code == 0
    assert _row_counts(cli_env) == (2, 4, 50)


@pytest.mark.integration
def test_a_command_can_be_restricted_to_one_ticker(cli_env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["update-universe"])

    runner.invoke(app, ["update-market", "--ticker", "XYZ"])

    assert _row_counts(cli_env)[2] == 25


@pytest.mark.integration
def test_the_limit_option_caps_the_printed_rows(cli_env: Path) -> None:
    runner = CliRunner()
    runner.invoke(app, ["run-scan"])

    result = runner.invoke(app, ["scan", "--all", "--limit", "1"])

    assert "PENY" in result.output
    assert "XYZ" not in result.output.split("Processed")[0].split("PENY")[1]


@pytest.mark.integration
def test_selecting_alpaca_without_credentials_reports_the_missing_variables(
    cli_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "alpaca")
    get_settings.cache_clear()

    result = CliRunner().invoke(app, ["update-universe"])

    assert result.exit_code != 0
    assert isinstance(result.exception, Exception)
    assert "ALPACA_API_KEY" in str(result.exception)


@pytest.mark.integration
def test_help_lists_every_phase_1_command() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("update-universe", "update-market", "update-fundamentals", "scan", "run-scan"):
        assert command in result.output
