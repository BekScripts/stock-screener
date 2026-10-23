"""The evidence CLI, and the deterministic batch it reads.

Every test points `SEC_EVIDENCE_DIR` at a directory in `tmp_path`, so the real
`.env` is never consulted and no test can reach the external drive. Nothing here
touches the network: the only command that would is exercised as a dry run.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from typer.testing import CliRunner

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import HistoricalSecurity
from domain import CompanyProfile
from stock_screener.cli import app
from stock_screener.config import get_settings
from stock_screener.sec_evidence import controlled_sample

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session

pytestmark = pytest.mark.integration


@pytest.fixture
def environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Point the CLI at a temporary database and a temporary evidence root."""
    database = tmp_path / "cli.db"
    engine = create_engine_from_url(f"sqlite:///{database}")
    create_all(engine)
    with session_scope(build_session_factory(engine)) as session:
        _seed(session)
    engine.dispose()

    root = tmp_path / "sec"
    root.mkdir()
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{database}")
    monkeypatch.setenv("SEC_EVIDENCE_DIR", str(root))
    monkeypatch.setenv("SEC_USER_AGENT", "compounder-radar-tests test@example.com")
    monkeypatch.setenv("ENVIRONMENT", "test")
    get_settings.cache_clear()
    yield root
    get_settings.cache_clear()


def _seed(session: Session) -> None:
    """A small roster spanning every sampling bucket."""
    for ticker in ("AAPL", "MSFT", "GOOGL", "BRKB", "T"):
        CompanyRepository(session).upsert_profile(
            CompanyProfile(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ")
        )
    session.add(
        HistoricalSecurity(primary_ticker="BBBY", name="Bed Bath", cik=886158, status="UNKNOWN")
    )
    session.add(
        HistoricalSecurity(primary_ticker="SGEN", name="Seagen", cik=1060736, status="UNKNOWN")
    )
    session.add(
        HistoricalSecurity(primary_ticker="ZZZZ", name="No Filer", cik=None, status="UNKNOWN")
    )
    session.flush()


def _batch(output: str) -> list[str]:
    """The selection lines, without the timestamped log output around them."""
    return [line for line in output.splitlines() if "cik=" in line]


class TestStatusReportsWithoutRaising:
    def test_a_configured_root_reports_available(self, environment: Path) -> None:
        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 0
        assert str(environment) in result.stdout
        assert "filings" in result.stdout

    def test_an_unconfigured_root_exits_non_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SEC_EVIDENCE_DIR", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "test")
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 1
        assert "UNCONFIGURED" in result.stdout
        get_settings.cache_clear()

    def test_a_missing_root_exits_non_zero_and_creates_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        absent = tmp_path / "Volumes" / "BEK" / "sec"
        monkeypatch.setenv("SEC_EVIDENCE_DIR", str(absent))
        monkeypatch.setenv("ENVIRONMENT", "test")
        get_settings.cache_clear()

        result = CliRunner().invoke(app, ["evidence", "status"])

        assert result.exit_code == 1
        assert not (tmp_path / "Volumes").exists()
        get_settings.cache_clear()


class TestTheSampleIsDeterministic:
    def test_the_same_database_yields_the_same_batch(self, environment: Path) -> None:
        """The selection, not the log line beside it, is what must repeat."""
        first = CliRunner().invoke(app, ["evidence", "sample"])
        second = CliRunner().invoke(app, ["evidence", "sample"])

        assert first.exit_code == 0
        assert _batch(first.stdout) == _batch(second.stdout)
        assert _batch(first.stdout)

    def test_the_batch_spans_live_and_delisted_securities(self, environment: Path) -> None:
        result = CliRunner().invoke(app, ["evidence", "sample"])

        assert "LIVE_SHORT_TICKER" in result.stdout
        assert "DELISTED_WITH_CIK" in result.stdout
        assert "DELISTED_WITHOUT_CIK" in result.stdout

    def test_a_security_without_a_cik_is_not_excluded_from_the_batch(
        self, environment: Path
    ) -> None:
        """The hard cases are the point of the sample."""
        result = CliRunner().invoke(app, ["evidence", "sample"])

        assert "ZZZZ" in result.stdout

    def test_the_limit_caps_the_printed_batch(self, environment: Path) -> None:
        result = CliRunner().invoke(app, ["evidence", "sample", "--limit", "2"])

        assert "2 securities" in result.stdout


class TestSampleSelectionRules:
    def test_short_and_long_tickers_are_bucketed_apart(self, tmp_path: Path) -> None:
        engine = create_engine_from_url(f"sqlite:///{tmp_path / 'buckets.db'}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            _seed(session)

            selections = controlled_sample(session)

        engine.dispose()
        short = {s.symbol for s in selections if s.bucket == "LIVE_SHORT_TICKER"}
        long_ = {s.symbol for s in selections if s.bucket == "LIVE_LONG_TICKER"}
        assert "T" in short
        assert "GOOGL" in long_

    def test_the_batch_spans_the_pool_rather_than_its_first_letters(self, tmp_path: Path) -> None:
        """Taking the first N alphabetically sampled the letter A, not the market."""
        engine = create_engine_from_url(f"sqlite:///{tmp_path / 'stride.db'}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            for index in range(60):
                session.add(
                    HistoricalSecurity(
                        primary_ticker=f"{chr(ord('A') + index // 3)}{index:02d}",
                        name=f"Security {index}",
                        cik=None,
                        status="UNKNOWN",
                    )
                )
            session.flush()

            selections = controlled_sample(session)

        engine.dispose()
        picked = [s.symbol for s in selections if s.bucket == "DELISTED_WITHOUT_CIK"]
        assert len(picked) == 25
        assert len({symbol[0] for symbol in picked}) > 5

    def test_a_pool_smaller_than_its_quota_is_taken_whole(self, tmp_path: Path) -> None:
        """A quota is a ceiling, never padding."""
        engine = create_engine_from_url(f"sqlite:///{tmp_path / 'small.db'}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            _seed(session)

            selections = controlled_sample(session)

        engine.dispose()
        assert len([s for s in selections if s.bucket == "DELISTED_WITH_CIK"]) == 2

    def test_no_security_is_selected_twice(self, tmp_path: Path) -> None:
        engine = create_engine_from_url(f"sqlite:///{tmp_path / 'dupes.db'}")
        create_all(engine)
        with session_scope(build_session_factory(engine)) as session:
            _seed(session)

            selections = controlled_sample(session)

        engine.dispose()
        keys = [(s.symbol, s.company_id, s.historical_security_id) for s in selections]
        assert len(keys) == len(set(keys))


class TestHarvestDryRun:
    def test_a_dry_run_writes_nothing_at_all(self, environment: Path) -> None:
        result = CliRunner().invoke(app, ["evidence", "harvest", "--dry-run", "--limit", "5"])

        assert result.exit_code == 0
        assert not (environment / "manifests" / "evidence.jsonl").exists()
        assert list((environment / "submissions").iterdir()) == []

    def test_a_dry_run_reports_the_configured_root_and_budget(self, environment: Path) -> None:
        result = CliRunner().invoke(app, ["evidence", "harvest", "--dry-run", "--limit", "5"])

        assert str(environment) in result.stdout
        assert "remaining" in result.stdout

    def test_the_budget_arguments_are_accepted(self, environment: Path) -> None:
        result = CliRunner().invoke(
            app,
            ["evidence", "harvest", "--dry-run", "--max-requests", "5", "--max-bytes", "1024"],
        )

        assert result.exit_code == 0
