"""Adversarial tests for the R1.0 backtest harness.

Each test names a way a historical study could lie — a binding that no longer
holds, a boundary moved after a result was seen, a filing read before it was
accepted, a future bar reaching backwards, a label reaching a decision, a
portfolio spending cash it does not have, a delisted name quietly vanishing —
and asserts the harness refuses it or records it.

The world is small and explicit. It is not a market; it is the smallest set of
rows on which each property is decidable.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from data_access import (
    BenchmarkPriceRepository,
    CompanyRepository,
    FundamentalObservationRepository,
    PriceHistoryRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import (
    ForwardReturnRecord,
    HistoricalObservation,
    HistoricalPrice,
    HistoricalSecurity,
    PitCaptureRun,
    PitFeatureSnapshot,
    PitSignalSnapshot,
    ResearchUniverseMonth,
)
from domain import CompanyProfile, PriceBar
from stock_screener.backtest_contract import (
    CONTRACT_VERSION,
    DATASET_ID,
    ContractBindings,
    build_contract,
    write_contract,
)
from stock_screener.backtest_harness import (
    COVERAGE_FILE,
    GROSS,
    OUTCOMES_FILE,
    SIMPLE_10BPS_PER_SIDE,
    BindingError,
    CalendarError,
    CrossSectionRow,
    DecisionDate,
    PitUniverse,
    PreregistrationError,
    PriceBook,
    Rebalance,
    ResearchSplit,
    Stage,
    TerminalLeg,
    TerminalState,
    assign_deciles,
    build_split,
    cross_section,
    decile_diagnostics,
    decision_calendar,
    first_holdout_access,
    load_labels,
    load_split,
    performance,
    rank_information_coefficient,
    run_manifest,
    simulate,
    validate_manifest,
    verify_bindings,
    write_split,
)
from stock_screener.backtest_harness import crosssection as crosssection_module
from stock_screener.backtest_harness import portfolio as portfolio_module
from stock_screener.backtest_harness import universe as universe_module
from stock_screener.backtest_harness.diagnostics import contrast_series
from stock_screener.backtest_harness.registry import preregistration, write_document
from stock_screener.config import Settings
from stock_screener.pit import FEATURE_VERSION

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
BENCHMARK = "SPY"
SIGNAL_VERSION = "MARKET_SIGNALS_V1"

PIT_SUMMARY = {"pre_open": 3, "newly_resolved": 2, "still_open": 1, "regressed": 0}
AUDIT_SUMMARY = {"sufficiency": {"SEC_MORE_MAY_HELP": 1}}


# ----------------------------------------------------------------------------
# A small world
# ----------------------------------------------------------------------------


def _sessions(first: date, count: int) -> list[date]:
    """`count` consecutive weekdays from `first`."""
    days: list[date] = []
    day = first
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


#: Three calendar months, so a decision date exists in each.
CALENDAR = _sessions(date(2024, 1, 1), 65)
MONTH_ENDS = sorted(
    {
        max(d for d in CALENDAR if d.strftime("%Y-%m") == m)
        for m in {d.strftime("%Y-%m") for d in CALENDAR}
    }
)


def _coverage_row(
    kind: str,
    identity_id: int,
    symbol: str,
    month: str,
    *,
    state: str = "UNKNOWN",
    status: str = "OPEN",
    evidence: str | None = None,
    listing_observed: bool = True,
) -> dict[str, object]:
    return {
        "kind": kind,
        "identity_id": identity_id,
        "symbol": symbol,
        "observation_month": f"{month}-01",
        "month_end": month,
        "universe_state": state,
        "status": status,
        "previous_observation": evidence,
        "listing_observed": listing_observed,
    }


def _write_frozen(
    directory: Path, coverage: Sequence[dict[str, object]], outcomes: Sequence[dict[str, object]]
) -> tuple[str, str]:
    """Write the two frozen point-in-time files and return their digests."""
    for name, rows in ((COVERAGE_FILE, coverage), (OUTCOMES_FILE, outcomes)):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8"
        )
    return (
        hashlib.sha256((directory / COVERAGE_FILE).read_bytes()).hexdigest(),
        hashlib.sha256((directory / OUTCOMES_FILE).read_bytes()).hexdigest(),
    )


def bindings(**overrides: object) -> ContractBindings:
    base: dict[str, object] = {
        "dataset_id": DATASET_ID,
        "contract_version": CONTRACT_VERSION,
        "universe_sha256": "b8" * 32,
        "evidence_manifest_sha256": "a5" * 32,
        "post_a3_index_sha256": "e6" * 32,
        "pit_marker_sha256": "e5" * 32,
        "planning_inputs_sha256": "79" * 32,
        "a3_plan_sha256": "0e" * 32,
        "pit_audit_marker_sha256": "ae" * 32,
        "database_sha256": "20" * 32,
        "fingerprints": {
            name: {"rows": 1, "sha256": f"{index:02x}" * 32}
            for index, name in enumerate(
                (
                    "historical_universe",
                    "price_history",
                    "historical_prices",
                    "forward_return_labels",
                    "signal_observations",
                )
            )
        },
    }
    base.update(overrides)
    return ContractBindings(**base)  # type: ignore[arg-type]


@pytest.fixture
def contract_dir(tmp_path: Path) -> Path:
    files = build_contract(
        bindings(), pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
    )
    write_contract(tmp_path / "contract", files)
    return tmp_path / "contract"


@pytest.fixture
def world(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'research.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    try:
        yield maker
    finally:
        engine.dispose()


def _profile(ticker: str) -> CompanyProfile:
    return CompanyProfile(
        ticker=ticker,
        name=f"{ticker} Inc",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        market_cap=None,
    )


def _bars(days: Sequence[date], price: float) -> list[PriceBar]:
    return [
        PriceBar(date=day, open=price, high=price, low=price, close=price, volume=1_000_000)
        for day in days
    ]


def _capture(
    session: Session,
    when: date,
    *,
    company_id: int | None = None,
    historical_security_id: int | None = None,
    ticker: str = "AAA",
    tradability: str = "TRADABLE",
    adv: float | None = 5_000_000.0,
) -> None:
    """Write one frozen feature and signal snapshot for one identity on one session."""
    run = session.query(PitCaptureRun).filter_by(snapshot_date=when).one_or_none()
    if run is None:
        run = PitCaptureRun(
            snapshot_date=when,
            status="COMPLETE",
            score_version="COMPOUNDER_V1_2",
            feature_version=FEATURE_VERSION,
            scores_captured=0,
            features_captured=0,
            universe_events_recorded=0,
            completed_at=datetime(2026, 1, 1, 12, 0, 0),  # noqa: DTZ001 - a stored naive stamp
        )
        session.add(run)
        session.flush()
    session.add(
        PitFeatureSnapshot(
            run_id=run.id,
            company_id=company_id,
            historical_security_id=historical_security_id,
            ticker=ticker,
            snapshot_date=when,
            feature_version=FEATURE_VERSION,
            feature_as_of=when,
            bars_available=200,
            benchmark_symbol=BENCHMARK,
            venue_dollar_volume_median=adv,
        )
    )
    session.add(
        PitSignalSnapshot(
            run_id=run.id,
            company_id=company_id,
            historical_security_id=historical_security_id,
            ticker=ticker,
            snapshot_date=when,
            signal_version=SIGNAL_VERSION,
            feature_version=FEATURE_VERSION,
            feature_as_of=when,
            bars_available=200,
            peer_population=40,
            trend_direction="UP",
            setups="NONE",
            tradability=tradability,
            liquidity_floor_proven=tradability == "TRADABLE",
        )
    )
    session.flush()


def _read_only(database: Path) -> closing[sqlite3.Connection]:
    """A read-only handle on the world, as the harness opens one."""
    return closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True))


def _company(session: Session, ticker: str) -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(_profile(ticker))
    session.flush()
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return int(stored.id)


def _universe_month(session: Session, company_id: int, month: date, symbol: str) -> None:
    """One row of the frozen monthly universe, which defines the calendar's months."""
    session.add(
        ResearchUniverseMonth(
            company_id=company_id,
            symbol=symbol,
            observation_month=month,
            venue="NASDAQ",
            state="UNKNOWN",
            security_class="UNKNOWN",
            class_evidence_mode="ABSENT",
            reason="fixture",
        )
    )
    session.flush()


def _historical(session: Session, ticker: str, *, last_trading_date: date | None = None) -> int:
    """Store one security that has left the market and return its primary key."""
    security = HistoricalSecurity(
        name=f"{ticker} Corp",
        primary_ticker=ticker,
        status="UNKNOWN",
        last_trading_date=last_trading_date,
    )
    session.add(security)
    session.flush()
    return int(security.id)


def _settings(database: Path) -> Settings:
    return Settings(database_url=f"sqlite:///{database}", _env_file=None)  # type: ignore[call-arg]


# ----------------------------------------------------------------------------
# Binding: fail closed, with no way round it
# ----------------------------------------------------------------------------


class TestTheContractGate:
    def test_a_matching_run_is_admitted(self, tmp_path: Path, contract_dir: Path) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"not a database, but a file that hashes")

        proven = verify_bindings(
            contract_dir,
            generation,
            expect=bindings(),
            expect_coverage_sha=coverage_sha,
            expect_outcomes_sha=outcomes_sha,
            database_path=database,
        )

        assert proven.dataset_id == DATASET_ID
        assert proven.contract_version == CONTRACT_VERSION
        assert proven.coverage_sha256 == coverage_sha
        assert (
            proven.label_fingerprint == bindings().fingerprints["forward_return_labels"]["sha256"]
        )

    def test_a_run_presenting_another_dataset_is_refused(
        self, tmp_path: Path, contract_dir: Path
    ) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")

        with pytest.raises(BindingError, match="must present PIT_RESEARCH_DATASET_V2"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(dataset_id="SOMETHING_ELSE"),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_a_run_presenting_another_contract_version_is_refused(
        self, tmp_path: Path, contract_dir: Path
    ) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")

        with pytest.raises(BindingError, match="BACKTEST_DATA_CONTRACT_V1"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(contract_version="BACKTEST_DATA_CONTRACT_V2"),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_a_universe_digest_the_contract_does_not_know_is_refused(
        self, tmp_path: Path, contract_dir: Path
    ) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")

        with pytest.raises(BindingError, match="bound to other inputs"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(universe_sha256="cc" * 32),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_a_price_fingerprint_that_moved_is_refused(
        self, tmp_path: Path, contract_dir: Path
    ) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")
        moved = dict(bindings().fingerprints)
        moved["price_history"] = {"rows": 1, "sha256": "ff" * 32}

        with pytest.raises(BindingError, match="bound to other inputs"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(fingerprints=moved),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_an_edited_coverage_file_is_refused(self, tmp_path: Path, contract_dir: Path) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")
        (generation / COVERAGE_FILE).write_text("{}\n", encoding="utf-8")

        with pytest.raises(BindingError, match="refusing to run"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_a_contract_missing_a_required_fingerprint_is_refused(self, tmp_path: Path) -> None:
        thin = bindings(fingerprints={"historical_universe": {"rows": 1, "sha256": "00" * 32}})
        files = build_contract(
            thin, pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        write_contract(tmp_path / "thin", files)
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")

        with pytest.raises(BindingError, match="no price_history fingerprint"):
            verify_bindings(
                tmp_path / "thin",
                generation,
                expect=thin,
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_a_fingerprint_that_is_not_a_digest_is_refused(self, tmp_path: Path) -> None:
        broken = bindings(
            fingerprints={
                name: {"rows": 1, "sha256": "not-a-digest"}
                for name in (
                    "historical_universe",
                    "price_history",
                    "historical_prices",
                    "forward_return_labels",
                    "signal_observations",
                )
            }
        )
        files = build_contract(
            broken, pit_summary=PIT_SUMMARY, audit_summary=AUDIT_SUMMARY, repo_commit="abc"
        )
        write_contract(tmp_path / "broken", files)
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])
        database = tmp_path / "research.db"
        database.write_bytes(b"x")

        with pytest.raises(BindingError, match="is not a digest"):
            verify_bindings(
                tmp_path / "broken",
                generation,
                expect=broken,
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=database,
            )

    def test_an_absent_database_is_refused(self, tmp_path: Path, contract_dir: Path) -> None:
        generation = tmp_path / "gen"
        coverage_sha, outcomes_sha = _write_frozen(generation, [], [])

        with pytest.raises(BindingError, match="no dataset to read"):
            verify_bindings(
                contract_dir,
                generation,
                expect=bindings(),
                expect_coverage_sha=coverage_sha,
                expect_outcomes_sha=outcomes_sha,
                database_path=tmp_path / "absent.db",
            )

    def test_there_is_no_force_escape_hatch(self) -> None:
        parameters = set(inspect.signature(verify_bindings).parameters)

        assert not {name for name in parameters if "force" in name.lower()}


# ----------------------------------------------------------------------------
# The calendar and the frozen split
# ----------------------------------------------------------------------------


class TestTheCalendarAndSplit:
    def test_a_month_without_both_identity_classes_is_not_a_decision_month(
        self, world: sessionmaker[Session], tmp_path: Path
    ) -> None:
        with session_scope(world) as session:
            BenchmarkPriceRepository(session).upsert_bars(BENCHMARK, _bars(CALENDAR, 500.0))
            company_id = _company(session, "AAA")
            security_id = _historical(session, "DDD")
            for month in {d.replace(day=1) for d in MONTH_ENDS}:
                _universe_month(session, company_id, month, "AAA")
            # January has both classes; February has only the company.
            _capture(session, MONTH_ENDS[0], company_id=company_id, ticker="AAA")
            _capture(session, MONTH_ENDS[0], historical_security_id=security_id, ticker="DDD")
            _capture(session, MONTH_ENDS[1], company_id=company_id, ticker="AAA")

        with _read_only(tmp_path / "research.db") as db:
            calendar = decision_calendar(db)

        assert [entry.month for entry in calendar] == [MONTH_ENDS[0].strftime("%Y-%m")]

    def test_overlapping_blocks_are_refused(self) -> None:
        with pytest.raises(CalendarError, match="overlap"):
            ResearchSplit(
                version="RESEARCH_SPLIT_V1",
                development=("2022-01", "2022-02"),
                validation=("2022-02",),
                holdout=("2022-03",),
            )

    def test_blocks_out_of_order_are_refused(self) -> None:
        with pytest.raises(CalendarError, match="chronological"):
            ResearchSplit(
                version="RESEARCH_SPLIT_V1",
                development=("2024-01",),
                validation=("2023-01",),
                holdout=("2025-01",),
            )

    def test_the_split_round_trips_and_assigns_every_month(self, tmp_path: Path) -> None:
        dates = [
            DecisionDate(
                month=month,
                decision=date(int(month[:4]), int(month[5:]), 28),
                execution=date(int(month[:4]), int(month[5:]), 28) + timedelta(days=1),
                company_features=5,
                historical_features=3,
            )
            for month in ("2022-01", "2023-12", "2024-06", "2025-03")
        ]
        split = build_split(dates)

        path = write_split(tmp_path, split, dates)
        again = load_split(path)

        assert split.development == ("2022-01", "2023-12")
        assert split.validation == ("2024-06",)
        assert split.holdout == ("2025-03",)
        assert again == split
        assert [split.assign(d.month) for d in dates] == [
            "DEVELOPMENT",
            "DEVELOPMENT",
            "VALIDATION",
            "HOLDOUT",
        ]

    def test_a_split_missing_a_block_is_refused(self, tmp_path: Path) -> None:
        (tmp_path / "research_split_v1.json").write_text(
            json.dumps({"version": "RESEARCH_SPLIT_V1", "blocks": {}}), encoding="utf-8"
        )

        with pytest.raises(CalendarError, match="three-block"):
            load_split(tmp_path / "research_split_v1.json")


# ----------------------------------------------------------------------------
# The universe: what the evidence admits, and what it does not
# ----------------------------------------------------------------------------


class TestTheFrozenUniverse:
    def test_evidence_accepted_on_the_decision_session_is_not_yet_available(
        self, tmp_path: Path
    ) -> None:
        _write_frozen(
            tmp_path,
            [_coverage_row("COMPANY", 1, "AAA", "2024-01")],
            [
                {
                    "kind": "COMPANY",
                    "identity_id": 1,
                    "month_end": "2024-01-31",
                    "post_status": "COVERED",
                    "evidence_date": "2024-01-31",
                }
            ],
        )
        universe = PitUniverse.load(tmp_path)

        members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert members == []
        assert excluded == {"EVIDENCE_NOT_YET_ACCEPTED": 1}

    def test_evidence_accepted_before_the_decision_session_admits_the_month(
        self, tmp_path: Path
    ) -> None:
        _write_frozen(
            tmp_path,
            [_coverage_row("COMPANY", 1, "AAA", "2024-01")],
            [
                {
                    "kind": "COMPANY",
                    "identity_id": 1,
                    "month_end": "2024-01-31",
                    "post_status": "COVERED",
                    "evidence_date": "2024-01-30",
                }
            ],
        )
        universe = PitUniverse.load(tmp_path)

        members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert [m.key for m in members] == [("COMPANY", 1)]
        assert members[0].source == "A3_EVIDENCE"
        assert excluded == {}

    def test_a_non_common_verdict_never_enters_the_universe(self, tmp_path: Path) -> None:
        _write_frozen(
            tmp_path,
            [
                _coverage_row(
                    "COMPANY",
                    1,
                    "AAAW",
                    "2024-01",
                    state="NON_COMMON_OR_EXCLUDED",
                    status="DECIDED",
                )
            ],
            [],
        )
        universe = PitUniverse.load(tmp_path)

        members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert members == []
        assert excluded == {"NON_COMMON_OR_EXCLUDED": 1}

    def test_an_unestablished_class_is_counted_not_assumed(self, tmp_path: Path) -> None:
        _write_frozen(tmp_path, [_coverage_row("COMPANY", 1, "AAA", "2024-01")], [])
        universe = PitUniverse.load(tmp_path)

        _members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert excluded == {"CLASS_UNESTABLISHED": 1}

    def test_a_ticker_two_securities_held_stays_two_identities(self, tmp_path: Path) -> None:
        _write_frozen(
            tmp_path,
            [
                _coverage_row("COMPANY", 7, "AAA", "2024-01", state="ELIGIBLE", status="DECIDED"),
                _coverage_row(
                    "HISTORICAL", 7, "AAA", "2024-01", state="ELIGIBLE", status="DECIDED"
                ),
            ],
            [],
        )
        universe = PitUniverse.load(tmp_path)

        members, _excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert [m.key for m in members] == [("COMPANY", 7), ("HISTORICAL", 7)]
        assert len({m.key for m in members}) == 2

    def test_two_classes_under_one_issuer_are_credited_separately(self, tmp_path: Path) -> None:
        _write_frozen(
            tmp_path,
            [
                _coverage_row("COMPANY", 11, "BRK.A", "2024-01"),
                _coverage_row("COMPANY", 12, "BRK.B", "2024-01"),
            ],
            [
                {
                    "kind": "COMPANY",
                    "identity_id": 12,
                    "month_end": "2024-01-31",
                    "post_status": "COVERED",
                    "evidence_date": "2024-01-10",
                }
            ],
        )
        universe = PitUniverse.load(tmp_path)

        members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert [m.key for m in members] == [("COMPANY", 12)]
        assert excluded == {"CLASS_UNESTABLISHED": 1}

    def test_a_delisted_security_keeps_the_months_it_qualified_for(self, tmp_path: Path) -> None:
        _write_frozen(
            tmp_path,
            [
                _coverage_row(
                    "HISTORICAL", 4, "DDD", "2024-01", state="ELIGIBLE", status="DECIDED"
                ),
                _coverage_row(
                    "HISTORICAL", 4, "DDD", "2024-02", state="ELIGIBLE", status="DECIDED"
                ),
            ],
            [],
        )
        universe = PitUniverse.load(tmp_path)

        january, _ = universe.eligible("2024-01", date(2024, 1, 31))
        february, _ = universe.eligible("2024-02", date(2024, 2, 29))

        assert [m.key for m in january] == [("HISTORICAL", 4)]
        assert [m.key for m in february] == [("HISTORICAL", 4)]

    def test_a_month_the_listing_record_never_observed_is_not_admitted(
        self, tmp_path: Path
    ) -> None:
        _write_frozen(
            tmp_path,
            [_coverage_row("COMPANY", 1, "AAA", "2024-01", listing_observed=False)],
            [
                {
                    "kind": "COMPANY",
                    "identity_id": 1,
                    "month_end": "2024-01-31",
                    "post_status": "COVERED",
                    "evidence_date": "2024-01-02",
                }
            ],
        )
        universe = PitUniverse.load(tmp_path)

        members, excluded = universe.eligible("2024-01", date(2024, 1, 31))

        assert members == []
        assert excluded == {"LISTING_UNOBSERVED": 1}


# ----------------------------------------------------------------------------
# The cross-section: what a decision saw, and what it could not see
# ----------------------------------------------------------------------------


def _decision(month: str, decision: date, execution: date) -> DecisionDate:
    return DecisionDate(
        month=month,
        decision=decision,
        execution=execution,
        company_features=1,
        historical_features=1,
    )


@pytest.fixture
def cross_world(
    world: sessionmaker[Session], tmp_path: Path
) -> tuple[Path, Path, DecisionDate, dict[str, int]]:
    """One month, four identities, each stopping at a different stage."""
    decision, execution = MONTH_ENDS[0], next(d for d in CALENDAR if d > MONTH_ENDS[0])
    keys: dict[str, int] = {}
    with session_scope(world) as session:
        BenchmarkPriceRepository(session).upsert_bars(BENCHMARK, _bars(CALENDAR, 500.0))
        for ticker in ("AAA", "BBB", "CCC"):
            keys[ticker] = _company(session, ticker)
            _universe_month(session, keys[ticker], decision.replace(day=1), ticker)
        keys["DDD"] = _historical(session, "DDD")
        prices = PriceHistoryRepository(session)
        for ticker in ("AAA", "BBB"):
            prices.upsert_bars(keys[ticker], _bars(CALENDAR, 20.0))
        # CCC is tradable but has no bar on the execution session.
        prices.upsert_bars(keys["CCC"], _bars([d for d in CALENDAR if d <= decision], 20.0))
        for day in CALENDAR:
            session.add(
                HistoricalPrice(
                    historical_security_id=keys["DDD"],
                    date=day,
                    open=8.0,
                    high=8.0,
                    low=8.0,
                    close=8.0,
                    volume=1000.0,
                )
            )
        _capture(session, decision, company_id=keys["AAA"], ticker="AAA")
        _capture(session, decision, company_id=keys["BBB"], ticker="BBB", tradability="UNVERIFIED")
        _capture(session, decision, company_id=keys["CCC"], ticker="CCC")
        _capture(session, decision, historical_security_id=keys["DDD"], ticker="DDD")
        # DDD filed on the decision session itself: the conservative lag excludes it.
        session.add(
            HistoricalObservation(
                historical_security_id=keys["DDD"],
                observation_fingerprint="f1",
                taxonomy="us-gaap",
                concept="Revenues",
                value=1.0,
                unit="USD",
                period_end=date(2023, 12, 31),
                filed_date=decision,
                accession="0000000000-24-000001",
                form="10-K",
                source="test",
            )
        )
    generation = tmp_path / "gen"
    rows = [
        _coverage_row("COMPANY", keys[t], t, "2024-01", state="ELIGIBLE", status="DECIDED")
        for t in ("AAA", "BBB", "CCC")
    ]
    rows.append(
        _coverage_row(
            "HISTORICAL", keys["DDD"], "DDD", "2024-01", state="ELIGIBLE", status="DECIDED"
        )
    )
    _write_frozen(generation, rows, [])
    return tmp_path / "research.db", generation, _decision("2024-01", decision, execution), keys


class TestTheCrossSection:
    def test_every_identity_carries_the_stage_it_reached_and_why_it_stopped(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, generation, when, keys = cross_world
        universe = PitUniverse.load(generation)

        with session_scope(world) as session, _read_only(database) as db:
            rows, coverage = cross_section(
                session, db, _settings(database), universe, when, split="DEVELOPMENT"
            )

        by_id = {row.identity_id: row for row in rows if row.kind == "COMPANY"}
        historical = next(row for row in rows if row.kind == "HISTORICAL")
        assert coverage.universe == 4
        assert coverage.feature_ready == 3
        assert historical.exclusion == "SAME_DAY_HISTORICAL_FILING"
        assert by_id[keys["BBB"]].tradability == "UNVERIFIED"
        assert coverage.exclusions["SAME_DAY_HISTORICAL_FILING"] == 1
        assert all(row.stage != str(Stage.EXECUTABLE) for row in rows)

    def test_a_missing_execution_price_is_never_backfilled(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, generation, when, keys = cross_world
        universe = PitUniverse.load(generation)

        with session_scope(world) as session, _read_only(database) as db:
            rows, _coverage = cross_section(
                session, db, _settings(database), universe, when, split="DEVELOPMENT"
            )

        missing = next(row for row in rows if row.identity_id == keys["CCC"])
        priced = next(row for row in rows if row.identity_id == keys["AAA"])
        assert missing.execution_price is None
        assert priced.execution_price == 20.0

    def test_a_bar_after_the_execution_session_changes_nothing(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, generation, when, keys = cross_world
        universe = PitUniverse.load(generation)
        settings = _settings(database)

        with session_scope(world) as session, _read_only(database) as db:
            before, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")
        later = next(d for d in CALENDAR if d > when.execution)
        with session_scope(world) as session:
            PriceHistoryRepository(session).upsert_bars(keys["AAA"], _bars([later], 999.0))
        with session_scope(world) as session, _read_only(database) as db:
            after, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")

        assert [row.as_json() for row in before] == [row.as_json() for row in after]

    def test_the_execution_session_open_is_the_only_future_price_a_decision_uses(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, generation, when, keys = cross_world
        universe = PitUniverse.load(generation)
        settings = _settings(database)

        with session_scope(world) as session, _read_only(database) as db:
            before, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")
        with session_scope(world) as session:
            PriceHistoryRepository(session).upsert_bars(keys["AAA"], _bars([when.execution], 31.0))
        with session_scope(world) as session, _read_only(database) as db:
            after, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")

        changed = [
            (a.identity_id, key)
            for a, b in zip(before, after, strict=True)
            for key, value in a.as_json().items()
            if b.as_json()[key] != value
        ]
        assert changed == [(keys["AAA"], "execution_price")]

    def test_changing_a_forward_label_changes_no_decision(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, generation, when, keys = cross_world
        universe = PitUniverse.load(generation)
        settings = _settings(database)

        with session_scope(world) as session, _read_only(database) as db:
            before, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")
        with session_scope(world) as session:
            session.add(
                ForwardReturnRecord(
                    company_id=keys["AAA"],
                    observation_date=when.decision,
                    horizon_sessions=20,
                    status="REALISED",
                    entry_date=when.execution,
                    entry_price=20.0,
                    exit_date=CALENDAR[-1],
                    exit_price=40.0,
                    total_return=1.0,
                )
            )
        with session_scope(world) as session, _read_only(database) as db:
            after, _ = cross_section(session, db, settings, universe, when, split="DEVELOPMENT")

        assert [row.as_json() for row in before] == [row.as_json() for row in after]

    def test_the_frozen_labels_are_read_only_by_the_diagnostic(
        self,
        world: sessionmaker[Session],
        cross_world: tuple[Path, Path, DecisionDate, dict[str, int]],
    ) -> None:
        database, _generation, when, keys = cross_world
        with session_scope(world) as session:
            session.add(
                ForwardReturnRecord(
                    company_id=keys["AAA"],
                    observation_date=when.decision,
                    horizon_sessions=20,
                    status="REALISED",
                    entry_date=when.execution,
                    entry_price=20.0,
                    exit_date=CALENDAR[-1],
                    exit_price=40.0,
                    total_return=1.0,
                )
            )

        with _read_only(database) as db:
            labels = load_labels(db, [when.decision.isoformat()], horizons=(20,))

        assert labels[(when.decision.isoformat(), "COMPANY", keys["AAA"], 20)] == (
            "REALISED",
            1.0,
        )


# ----------------------------------------------------------------------------
# The portfolio: cash, shares and terminal legs
# ----------------------------------------------------------------------------


AAA = ("COMPANY", 1)
BBB = ("COMPANY", 2)
DDD = ("HISTORICAL", 3)
SYMBOLS = {AAA: "AAA", BBB: "BBB", DDD: "DDD"}


def _book(sessions: Sequence[date], series: dict[tuple[str, int], list[float | None]]) -> PriceBook:
    return PriceBook(sessions, series, series)


class TestPortfolioAccounting:
    def test_nav_is_cash_plus_market_value_at_every_session(self) -> None:
        sessions = CALENDAR[:6]
        book = _book(
            sessions,
            {AAA: [10.0] * 6, BBB: [20.0, 20.0, 22.0, 22.0, 22.0, 22.0]},
        )
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 0.5, BBB: 0.5},
                split="DEVELOPMENT",
            )
        ]

        result = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)

        assert result.violations == []
        for position, session in enumerate(result.sessions):
            index = sessions.index(session)
            market = sum(
                holding * (book.close_at(key, index) or 0.0)
                for key, holding in _positions(result, session).items()
            )
            assert result.nav[position] == pytest.approx(result.cash[position] + market)

    def test_shares_reconcile_from_the_trades_that_produced_them(self) -> None:
        sessions = CALENDAR[:8]
        book = _book(sessions, {AAA: [10.0] * 8, BBB: [20.0] * 8})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 1.0},
                split="DEVELOPMENT",
            ),
            Rebalance(
                month="2024-02",
                decision=sessions[4],
                execution=sessions[5],
                targets={BBB: 1.0},
                split="DEVELOPMENT",
            ),
        ]

        result = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)

        net: dict[tuple[str, int], float] = {}
        for trade in result.trades:
            key = (trade.kind, trade.identity_id)
            net[key] = net.get(key, 0.0) + (trade.shares if trade.side == "BUY" else -trade.shares)
        final = {
            (holding.kind, holding.identity_id): holding.shares
            for holding in result.holdings
            if holding.execution == sessions[5].isoformat()
        }
        assert net[AAA] == pytest.approx(0.0, abs=1e-9)
        assert final[BBB] == pytest.approx(net[BBB])

    def test_the_portfolio_never_spends_cash_it_does_not_hold(self) -> None:
        sessions = CALENDAR[:5]
        book = _book(sessions, {AAA: [10.0] * 5, BBB: [20.0] * 5})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 0.5, BBB: 0.5},
                split="DEVELOPMENT",
            )
        ]

        result = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)

        assert min(result.cash) >= -1e-6
        assert result.violations == []

    def test_target_weights_never_exceed_one_and_no_position_is_short(self) -> None:
        sessions = CALENDAR[:5]
        book = _book(sessions, {AAA: [10.0] * 5})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 1.0},
                split="DEVELOPMENT",
            )
        ]

        result = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)

        assert all(0.0 <= holding.target_weight <= 1.0 for holding in result.holdings)
        assert all(holding.shares >= 0 for holding in result.holdings)

    def test_a_settled_terminal_leg_is_liquidated_at_the_stated_consideration(self) -> None:
        sessions = CALENDAR[:6]
        series: dict[tuple[str, int], list[float | None]] = {
            DDD: [10.0, 10.0, 10.0, None, None, None]
        }
        book = _book(sessions, series)
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={DDD: 1.0},
                split="DEVELOPMENT",
            )
        ]
        legs = {
            DDD: TerminalLeg(
                key=DDD,
                state=TerminalState.SETTLED,
                effective_date=sessions[3],
                value_per_share=15.0,
                reason="ACQUIRED",
                source="test:MERGER",
            )
        }

        result = simulate("TEST", plan, book, terminal=legs, symbols=SYMBOLS)

        terminal_trades = [trade for trade in result.trades if trade.side == "TERMINAL"]
        assert len(terminal_trades) == 1
        assert terminal_trades[0].price == 15.0
        assert result.nav[-1] == pytest.approx(result.cash[-1])
        assert result.diagnostics["settled_identities"] == 1

    def test_an_unknown_terminal_leg_is_carried_and_bounded_rather_than_guessed(self) -> None:
        sessions = CALENDAR[:6]
        book = _book(sessions, {DDD: [10.0, 10.0, 10.0, None, None, None]})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={DDD: 1.0},
                split="DEVELOPMENT",
            )
        ]
        legs = {
            DDD: TerminalLeg(
                key=DDD,
                state=TerminalState.UNKNOWN,
                effective_date=sessions[3],
                value_per_share=None,
                reason="DELISTED",
                source=None,
            )
        }

        result = simulate("TEST", plan, book, terminal=legs, symbols=SYMBOLS)

        assert [trade.side for trade in result.trades] == ["BUY"]
        assert result.nav[-1] > 0
        assert result.floor_nav[-1] < result.nav[-1]
        assert result.diagnostics["unknown_terminal_identities"] == 1

    def test_costs_reduce_the_path_and_gross_is_the_upper_bound(self) -> None:
        sessions = CALENDAR[:8]
        book = _book(sessions, {AAA: [10.0] * 8, BBB: [10.0] * 8})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 1.0},
                split="DEVELOPMENT",
            ),
            Rebalance(
                month="2024-02",
                decision=sessions[4],
                execution=sessions[5],
                targets={BBB: 1.0},
                split="DEVELOPMENT",
            ),
        ]

        gross = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS, cost=GROSS)
        net = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS, cost=SIMPLE_10BPS_PER_SIDE)

        assert net.nav[-1] < gross.nav[-1]
        assert sum(trade.cost for trade in gross.trades) == 0.0
        assert sum(trade.cost for trade in net.trades) > 0.0

    def test_two_simulations_of_one_plan_are_identical(self) -> None:
        sessions = CALENDAR[:8]
        book = _book(sessions, {AAA: [10.0] * 8, BBB: [12.0] * 8})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 0.5, BBB: 0.5},
                split="DEVELOPMENT",
            )
        ]

        first = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)
        second = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)

        assert first.nav == second.nav
        assert [vars(t) for t in first.trades] == [vars(t) for t in second.trades]
        assert [vars(h) for h in first.holdings] == [vars(h) for h in second.holdings]

    def test_a_benchmark_measured_over_the_same_sessions_has_the_same_length(self) -> None:
        sessions = CALENDAR[:10]
        book = _book(sessions, {AAA: [10.0 + index for index in range(10)]})
        plan = [
            Rebalance(
                month="2024-01",
                decision=sessions[0],
                execution=sessions[1],
                targets={AAA: 1.0},
                split="DEVELOPMENT",
            )
        ]

        result = simulate("TEST", plan, book, terminal={}, symbols=SYMBOLS)
        metrics = performance(result.sessions, result.nav)

        assert metrics.sessions == len(result.sessions)
        assert metrics.total_return == pytest.approx(result.nav[-1] / result.nav[0] - 1.0)


def _positions(result: object, session: date) -> dict[tuple[str, int], float]:
    """Reconstruct the position book at a session from the trade ledger."""
    shares: dict[tuple[str, int], float] = {}
    for trade in result.trades:  # type: ignore[attr-defined]
        if date.fromisoformat(trade.execution) > session:
            continue
        key = (trade.kind, trade.identity_id)
        if trade.side == "BUY":
            shares[key] = shares.get(key, 0.0) + trade.shares
        else:
            shares[key] = shares.get(key, 0.0) - trade.shares
    return {key: held for key, held in shares.items() if held > 1e-12}


# ----------------------------------------------------------------------------
# Diagnostics, preregistration and the static guarantees
# ----------------------------------------------------------------------------


def _row(identity_id: int, month: str, decision: str, score: float) -> CrossSectionRow:
    return CrossSectionRow(
        kind="COMPANY",
        identity_id=identity_id,
        symbol=f"S{identity_id}",
        month=month,
        decision=decision,
        execution=decision,
        universe_source="FROZEN_VERDICT",
        evidence_date=None,
        tradability="TRADABLE",
        score_status="SCORED",
        final_score=score,
        freshness="CURRENT",
        rank_eligible=True,
        execution_price=10.0,
        adv_dollar=1_000_000.0,
        stage=str(Stage.EXECUTABLE),
        exclusion=None,
        classification_is_current=True,
    )


class TestTheSignalDiagnostic:
    def test_deciles_run_low_score_to_high(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-31", float(index)) for index in range(1, 101)]

        assigned = assign_deciles(rows)

        lowest = [row.final_score or 0.0 for row, bucket in assigned if bucket == 1]
        highest = [row.final_score or 0.0 for row, bucket in assigned if bucket == 10]
        assert max(lowest) < min(highest)

    def test_a_month_below_the_minimum_cross_section_is_not_cut(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-31", float(index)) for index in range(1, 20)]

        stats, skipped, months = decile_diagnostics(rows, {}, {"2024-01": "DEVELOPMENT"})

        assert stats == []
        assert skipped == {"BELOW_MINIMUM_CROSS_SECTION": 1}
        assert months == [{"month": "2024-01", "observations": 19, "cut": False}]

    def test_only_realised_labels_enter_a_return_statistic(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-31", float(index)) for index in range(1, 101)]
        labels: dict[tuple[str, str, int, int], tuple[str, float | None]] = {}
        for index in range(1, 101):
            status = "REALISED" if index % 2 else "INTERRUPTED"
            labels[("2024-01-31", "COMPANY", index, 20)] = (
                status,
                0.10 if status == "REALISED" else None,
            )

        stats, _skipped, _months = decile_diagnostics(
            rows, labels, {"2024-01": "DEVELOPMENT"}, horizons=(20,)
        )

        for stat in stats:
            assert stat.realised == stat.statuses.get("REALISED", 0)
            assert stat.mean in (None, pytest.approx(0.10))
            assert stat.statuses.get("INTERRUPTED", 0) > 0

    def test_a_perfectly_ordered_month_has_a_rank_ic_of_one(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-31", float(index)) for index in range(1, 41)]
        labels = {
            ("2024-01-31", "COMPANY", index, 20): ("REALISED", float(index) / 100.0)
            for index in range(1, 41)
        }

        summaries, monthly = rank_information_coefficient(
            rows, labels, {"2024-01": "DEVELOPMENT"}, horizons=(20,)
        )

        assert monthly[0]["ic"] == pytest.approx(1.0)
        assert all(summary.mean == pytest.approx(1.0) for summary in summaries)

    def test_the_monthly_contrast_carries_an_interval_measured_across_months(self) -> None:
        rows = [
            _row(index, month, f"{month}-28", float(index))
            for month in ("2024-01", "2024-02", "2024-03", "2024-04")
            for index in range(1, 101)
        ]
        labels = {
            (f"{month}-28", "COMPANY", index, 20): ("REALISED", index / 100.0)
            for month in ("2024-01", "2024-02", "2024-03", "2024-04")
            for index in range(1, 101)
        }

        monthly, summary = contrast_series(rows, labels, {})

        assert [entry["month"] for entry in monthly] == [
            "2024-01",
            "2024-02",
            "2024-03",
            "2024-04",
        ]
        assert summary["ALL"]["months"] == 4
        assert summary["ALL"]["positive_months"] == 1.0
        assert summary["ALL"]["mean"] is not None and summary["ALL"]["mean"] > 0

    def test_a_month_below_the_minimum_has_no_contrast(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-28", float(index)) for index in range(1, 20)]

        monthly, summary = contrast_series(rows, {}, {})

        assert monthly == []
        assert summary == {}

    def test_changing_the_labels_changes_the_diagnostic(self) -> None:
        rows = [_row(index, "2024-01", "2024-01-31", float(index)) for index in range(1, 101)]
        rising = {
            ("2024-01-31", "COMPANY", index, 20): ("REALISED", index / 100.0)
            for index in range(1, 101)
        }
        falling = {
            ("2024-01-31", "COMPANY", index, 20): ("REALISED", -index / 100.0)
            for index in range(1, 101)
        }

        up, _s, _m = decile_diagnostics(rows, rising, {}, horizons=(20,))
        down, _s2, _m2 = decile_diagnostics(rows, falling, {}, horizons=(20,))

        assert [stat.mean for stat in up] != [stat.mean for stat in down]


class TestYearLabelling:
    def test_a_year_the_study_spans_completely_is_not_labelled_partial(self) -> None:
        from stock_screener.backtest_harness.run import _partial

        sessions = [date(2022, 2, 1), date(2023, 1, 3), date(2023, 12, 29), date(2024, 6, 3)]

        assert _partial(2023, sessions) is False
        assert _partial(2022, sessions) is True
        assert _partial(2024, sessions) is True

    def test_an_empty_path_is_partial(self) -> None:
        from stock_screener.backtest_harness.run import _partial

        assert _partial(2024, []) is True


class TestHoldoutDiscipline:
    def test_the_holdout_may_not_be_read_before_a_preregistration_exists(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(PreregistrationError, match="before a preregistration exists"):
            first_holdout_access(tmp_path, preregistration_sha256="00" * 32, strategies=["X"])

    def test_a_preregistration_that_does_not_hash_as_presented_is_refused(
        self, tmp_path: Path
    ) -> None:
        document = preregistration(
            dataset_id=DATASET_ID,
            contract_version=CONTRACT_VERSION,
            contract_sha256="f7" * 32,
            split_sha256="ab" * 32,
            split={"blocks": {}},
            registry_sha256="cd" * 32,
            benchmark=BENCHMARK,
            code_commit="a" * 40,
            bindings={"universe": "b8" * 32},
        )
        write_document(tmp_path / "r1_0_preregistration.json", document)

        with pytest.raises(PreregistrationError, match="refusing to read the holdout"):
            first_holdout_access(tmp_path, preregistration_sha256="00" * 32, strategies=["X"])

    def test_the_access_event_proves_the_preregistration_came_first(self, tmp_path: Path) -> None:
        document = preregistration(
            dataset_id=DATASET_ID,
            contract_version=CONTRACT_VERSION,
            contract_sha256="f7" * 32,
            split_sha256="ab" * 32,
            split={"blocks": {}},
            registry_sha256="cd" * 32,
            benchmark=BENCHMARK,
            code_commit="a" * 40,
            bindings={"universe": "b8" * 32},
        )
        digest = write_document(tmp_path / "r1_0_preregistration.json", document)

        event = first_holdout_access(tmp_path, preregistration_sha256=digest, strategies=["B", "A"])

        assert event["event"] == "FIRST_HOLDOUT_ACCESS"
        assert event["strategies"] == ["A", "B"]
        assert (
            json.loads((tmp_path / "holdout_access.json").read_text(encoding="utf-8"))[
                "preregistration_sha256"
            ]
            == digest
        )

    def test_the_preregistration_forbids_optimisation_in_writing(self) -> None:
        document = preregistration(
            dataset_id=DATASET_ID,
            contract_version=CONTRACT_VERSION,
            contract_sha256="f7" * 32,
            split_sha256="ab" * 32,
            split={"blocks": {}},
            registry_sha256="cd" * 32,
            benchmark=BENCHMARK,
            code_commit="a" * 40,
            bindings={},
        )

        assert document["variants_tried"] == 1
        assert document["parameter_sweeps"] == []
        assert "threshold search" in document["prohibited"]  # type: ignore[operator]


class TestRunManifests:
    def _manifest(self, **overrides: object) -> dict[str, object]:
        manifest = run_manifest(
            run_id="R1_0-X",
            strategy="X",
            code_commit="a" * 40,
            dataset_id=DATASET_ID,
            contract_version=CONTRACT_VERSION,
            universe_fingerprint="b8" * 32,
            feature_fingerprints={"signal_observations": "cc" * 32},
            price_fingerprint="dd" * 32,
            label_fingerprint="ee" * 32,
            rebalance_rule="monthly",
            cost={"model": "GROSS"},
            date_range={"start": "2022-02-01", "end": "2026-04-30"},
            split={"DEVELOPMENT": {}},
            parameters={"weighting": "equal"},
            benchmark=BENCHMARK,
            outputs={"nav": "ff" * 32},
            metrics={"total_return": 0.1},
        )
        manifest.update(overrides)
        return manifest

    def test_a_complete_manifest_validates(self) -> None:
        schema = json.loads(
            (
                REPO
                / ".evidence"
                / "contracts"
                / "backtest_v1"
                / "backtest_run_manifest_schema.json"
            ).read_text(encoding="utf-8")
        )

        assert validate_manifest(self._manifest(), schema) == []

    def test_a_manifest_missing_a_required_field_is_rejected(self) -> None:
        schema = json.loads(
            (
                REPO
                / ".evidence"
                / "contracts"
                / "backtest_v1"
                / "backtest_run_manifest_schema.json"
            ).read_text(encoding="utf-8")
        )
        manifest = self._manifest()
        del manifest["selection_criterion"]

        problems = validate_manifest(manifest, schema)

        assert problems == ["missing required field selection_criterion"]

    def test_a_manifest_naming_another_dataset_is_rejected(self) -> None:
        schema = json.loads(
            (
                REPO
                / ".evidence"
                / "contracts"
                / "backtest_v1"
                / "backtest_run_manifest_schema.json"
            ).read_text(encoding="utf-8")
        )

        problems = validate_manifest(self._manifest(dataset_id="OTHER"), schema)

        assert any("dataset_id" in problem for problem in problems)

    def test_a_manifest_with_another_execution_rule_is_rejected(self) -> None:
        schema = json.loads(
            (
                REPO
                / ".evidence"
                / "contracts"
                / "backtest_v1"
                / "backtest_run_manifest_schema.json"
            ).read_text(encoding="utf-8")
        )

        problems = validate_manifest(self._manifest(execution_rule="SAME_CLOSE"), schema)

        assert any("execution_rule" in problem for problem in problems)


class TestTheStaticGuarantees:
    def test_the_run_verifies_the_label_boundary_itself(self) -> None:
        from stock_screener.backtest_harness.audit import label_reader_in_decision_path

        assert label_reader_in_decision_path() == []

    def test_no_decision_module_imports_the_label_layer(self) -> None:
        for module in (crosssection_module, portfolio_module, universe_module):
            source = Path(module.__file__ or "").read_text(encoding="utf-8")
            assert "forward_returns" not in source
            assert "ForwardReturn" not in source
            assert "load_labels" not in source

    def test_no_harness_module_ranks_or_constrains_on_sector(self) -> None:
        directory = Path(crosssection_module.__file__ or "").parent
        offending = []
        for path in sorted(directory.glob("*.py")):
            body = path.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                if ".sector" in stripped or ".industry" in stripped:
                    offending.append(f"{path.name}: {stripped}")
        assert offending == []

    def test_the_harness_states_the_execution_rule_in_one_place(self) -> None:
        from stock_screener.backtest_harness import EXECUTION_RULE

        assert EXECUTION_RULE == "CLOSE_SIGNAL_NEXT_SESSION_OPEN"


# ----------------------------------------------------------------------------
# The whole phase, end to end, over a world small enough to reason about
# ----------------------------------------------------------------------------


FLOW_CONCEPTS = {
    "Revenues": 1_000_000_000.0,
    "GrossProfit": 600_000_000.0,
    "OperatingIncomeLoss": 250_000_000.0,
    "NetIncomeLoss": 200_000_000.0,
    "NetCashProvidedByUsedInOperatingActivities": 300_000_000.0,
    "PaymentsToAcquirePropertyPlantAndEquipment": 50_000_000.0,
}
INSTANT_CONCEPTS = {
    "Assets": 5_000_000_000.0,
    "Liabilities": 2_000_000_000.0,
    "StockholdersEquity": 3_000_000_000.0,
    "CashAndCashEquivalentsAtCarryingValue": 500_000_000.0,
    "LongTermDebtNoncurrent": 800_000_000.0,
    "CommonStockSharesOutstanding": 100_000_000.0,
    "AssetsCurrent": 1_500_000_000.0,
    "LiabilitiesCurrent": 700_000_000.0,
}

#: Long enough that a decision in late 2023 has real price history behind it.
LONG_CALENDAR = _sessions(date(2022, 1, 3), 870)

#: One month per block, so the split has all three and none of them is empty.
END_TO_END_MONTHS = ("2023-11", "2023-12", "2024-06", "2025-03")


def _fundamentals(session: Session, company_id: int, scale: float) -> None:
    """Twelve quarters of filings, growing, enough for the frozen formula to score."""
    rows: list[dict[str, object]] = []
    for quarter in range(12):
        period_end = date(2022, 3, 31) + timedelta(days=91 * quarter)
        filed = period_end + timedelta(days=45)
        growth = 1.0 + 0.05 * quarter
        for concept, value in FLOW_CONCEPTS.items():
            rows.append(
                {
                    "observation_fingerprint": hashlib.sha256(
                        f"{company_id}|{concept}|{quarter}".encode()
                    ).hexdigest(),
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "value": value * growth * scale,
                    "unit": "USD",
                    "period_start": period_end - timedelta(days=90),
                    "period_end": period_end,
                    "filed_date": filed,
                    "accession": f"acc-{company_id}-{quarter}",
                    "form": "10-Q",
                    "source": "test",
                }
            )
        for concept, value in INSTANT_CONCEPTS.items():
            rows.append(
                {
                    "observation_fingerprint": hashlib.sha256(
                        f"{company_id}|{concept}|{quarter}|i".encode()
                    ).hexdigest(),
                    "taxonomy": "us-gaap",
                    "concept": concept,
                    "value": value * growth,
                    "unit": "shares" if "Shares" in concept else "USD",
                    "period_start": None,
                    "period_end": period_end,
                    "filed_date": filed,
                    "accession": f"acc-{company_id}-{quarter}",
                    "form": "10-Q",
                    "source": "test",
                }
            )
    FundamentalObservationRepository(session).add_all(company_id, rows)
    session.flush()


@pytest.fixture
def phase_world(world: sessionmaker[Session], tmp_path: Path) -> tuple[Path, Path, Path]:
    """Thirty-two scoring companies over four decision months, one per block."""
    decisions = [
        max(day for day in LONG_CALENDAR if day.strftime("%Y-%m") == month)
        for month in END_TO_END_MONTHS
    ]
    with session_scope(world) as session:
        BenchmarkPriceRepository(session).upsert_bars(
            BENCHMARK,
            [
                PriceBar(
                    date=day,
                    open=400.0 + index * 0.05,
                    high=401.0 + index * 0.05,
                    low=399.0 + index * 0.05,
                    close=400.0 + index * 0.05,
                    volume=1_000_000,
                )
                for index, day in enumerate(LONG_CALENDAR)
            ],
        )
        prices = PriceHistoryRepository(session)
        identities: list[int] = []
        for number in range(32):
            ticker = f"C{number:02d}"
            company_id = _company(session, ticker)
            identities.append(company_id)
            _fundamentals(session, company_id, 1.0 + number / 20.0)
            drift = 0.005 + number * 0.0004
            prices.upsert_bars(
                company_id,
                [
                    PriceBar(
                        date=day,
                        open=20.0 + index * drift,
                        high=21.0 + index * drift,
                        low=19.0 + index * drift,
                        close=20.0 + index * drift,
                        volume=3_000_000,
                    )
                    for index, day in enumerate(LONG_CALENDAR)
                ],
            )
            for month in END_TO_END_MONTHS:
                _universe_month(
                    session, company_id, date(int(month[:4]), int(month[5:]), 1), ticker
                )
            for when in decisions:
                _capture(session, when, company_id=company_id, ticker=ticker)
        # One security that has left the market, so the historical class is
        # represented in the calendar and in the universe.
        security_id = _historical(session, "GONE")
        for when in decisions:
            _capture(session, when, historical_security_id=security_id, ticker="GONE")
        for index, day in enumerate(LONG_CALENDAR):
            session.add(
                HistoricalPrice(
                    historical_security_id=security_id,
                    date=day,
                    open=9.0 + index * 0.001,
                    high=9.5 + index * 0.001,
                    low=8.5 + index * 0.001,
                    close=9.0 + index * 0.001,
                    volume=500_000,
                )
            )
        session.flush()

    generation = tmp_path / "gen"
    coverage = [
        _coverage_row(
            "COMPANY", company_id, f"C{number:02d}", month, state="ELIGIBLE", status="DECIDED"
        )
        for number, company_id in enumerate(identities)
        for month in END_TO_END_MONTHS
    ]
    coverage.extend(
        _coverage_row("HISTORICAL", security_id, "GONE", month, state="ELIGIBLE", status="DECIDED")
        for month in END_TO_END_MONTHS
    )
    _write_frozen(generation, coverage, [])
    return tmp_path / "research.db", generation, tmp_path / "out"


def _run(database: Path, generation: Path, contract: Path, out: Path) -> object:
    from stock_screener.backtest_harness import run_r1_0

    coverage_sha = hashlib.sha256((generation / COVERAGE_FILE).read_bytes()).hexdigest()
    outcomes_sha = hashlib.sha256((generation / OUTCOMES_FILE).read_bytes()).hexdigest()
    proven = verify_bindings(
        contract,
        generation,
        expect=bindings(),
        expect_coverage_sha=coverage_sha,
        expect_outcomes_sha=outcomes_sha,
        database_path=database,
    )
    return run_r1_0(
        out,
        _settings(database),
        proven,
        generation,
        contract,
        contract_sha256="f7" * 32,
        code_commit="a" * 40,
    )


class TestThePhaseEndToEnd:
    def test_it_produces_every_artifact_in_the_required_order(
        self, phase_world: tuple[Path, Path, Path], contract_dir: Path
    ) -> None:
        database, generation, out = phase_world

        result = _run(database, generation, contract_dir, out)

        assert result.violations == []  # type: ignore[attr-defined]
        assert result.manifest_problems == {}  # type: ignore[attr-defined]
        for name in (
            "research_split_v1.json",
            "baseline_registry.json",
            "r1_0_preregistration.json",
            "holdout_access.json",
            "cross_sections.jsonl",
            "monthly_coverage.json",
            "signal_diagnostics_compounder_v1.json",
            "spot_audit.json",
            "bias_audit.json",
            "research_ledger.jsonl",
            "r1_0_summary.json",
            "r1_0_complete.json",
            "r1_0_fingerprints.json",
        ):
            assert (out / name).is_file(), name
        marker = json.loads((out / "r1_0_complete.json").read_text(encoding="utf-8"))
        assert marker["stage"] == "R1_0_COMPLETE"
        assert marker["holdout_evaluated_after_preregistration"] is True
        assert marker["accounting_violations"] == 0
        assert sorted(marker["strategy_versions"]) == [
            "BASELINE_PASSIVE_V1",
            "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1",
            "COMPOUNDER_DECILES_V1",
            "COMPOUNDER_TOP_DECILE_V1",
        ]

    def test_the_portfolios_execute_and_their_accounting_reconciles(
        self, phase_world: tuple[Path, Path, Path], contract_dir: Path
    ) -> None:
        database, generation, out = phase_world

        _run(database, generation, contract_dir, out)

        summary = json.loads((out / "r1_0_summary.json").read_text(encoding="utf-8"))
        assert summary["decision_dates"] == 4
        assert summary["sample"]["executable"] >= 30
        equal = json.loads(
            (out / "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1" / "metrics.json").read_text(
                encoding="utf-8"
            )
        )
        assert equal["rebalances"] == 4
        assert equal["window"]["total_return"] is not None
        trades = (
            (out / "run_BASELINE_UNIVERSE_EQUAL_WEIGHT_V1" / "trades.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        )
        assert trades
        assert summary["accounting_violations"] == []

    def test_two_executions_produce_identical_fingerprints(
        self, phase_world: tuple[Path, Path, Path], contract_dir: Path
    ) -> None:
        database, generation, out = phase_world

        first = _run(database, generation, contract_dir, out)
        second = _run(database, generation, contract_dir, out.with_name("out2"))

        assert first.fingerprints == second.fingerprints  # type: ignore[attr-defined]
        assert first.fingerprints  # type: ignore[attr-defined]

    def test_the_cost_sensitivity_never_beats_the_gross_path(
        self, phase_world: tuple[Path, Path, Path], contract_dir: Path
    ) -> None:
        database, generation, out = phase_world

        _run(database, generation, contract_dir, out)

        run_dir = out / "run_COMPOUNDER_TOP_DECILE_V1"
        gross = json.loads((run_dir / "metrics.json").read_text(encoding="utf-8"))
        net = json.loads(
            (run_dir / "metrics.SIMPLE_10BPS_PER_SIDE_V1.json").read_text(encoding="utf-8")
        )
        assert net["window"]["total_return"] <= gross["window"]["total_return"]

    def test_the_spot_audit_re_derives_its_sample_without_failures(
        self, phase_world: tuple[Path, Path, Path], contract_dir: Path
    ) -> None:
        database, generation, out = phase_world

        _run(database, generation, contract_dir, out)

        audit = json.loads((out / "spot_audit.json").read_text(encoding="utf-8"))
        assert audit["failures"] == []
        assert sorted(audit["months"]) == ["DEVELOPMENT", "HOLDOUT", "VALIDATION"]
        assert (
            audit["label_access_during_decisions"]["modules_that_can_reach_the_label_layer"] == []
        )
        assert all(check["score_reproduced"] for check in audit["checks"])
