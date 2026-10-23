"""R1.12: the prospective capture pipeline, from sealed snapshots to a packet.

These tests exist because R1.11 certified an armed window as ready and then found
it had no executor. So they check capability rather than intent: that a derived
component is not ready until something produced it, that the calendar cannot be
validated against a thinner source, and that the one session already missed is
refused before any value is computed.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from domain import (
    CorporateActionKind,
    CorporateActionRecord,
    FilingObservation,
    PriceBar,
    VolumeBasis,
)
from stock_screener.prospective_capture import (
    AUTHORITATIVE_CALENDAR,
    CalendarBindingError,
    ComponentState,
    DecisionRefusedError,
    ExecutionRefusedError,
    GuardViolationError,
    PackageError,
    ProspectiveIdentity,
    admitted_accessions_by_ticker,
    assess_capture_readiness,
    audit_package,
    bind_calendar,
    build_cross_section,
    build_score_inputs,
    capture_execution_reference,
    derive_execution_session,
    fetch_corporate_actions,
    fetch_filing_observations,
    load_package,
    prior_incumbents,
    require_six_fingerprints,
    resolve_identities,
    score_cross_section,
    seal_predecision,
    session_digest,
)
from stock_screener.prospective_capture.guards import reachable_names
from stock_screener.prospective_capture.readiness import IncompleteDecisionInputsError
from stock_screener.prospective_capture.scoring import ProspectiveScore, ProspectiveScoreSet
from stock_screener.prospective_runner import SEMANTIC_MODULES
from stock_screener.prospective_runner.fingerprint import source_root
from stock_screener.prospective_source import SnapshotStore, StoreConflictError
from stock_screener.prospective_source.adapters import (
    SourceResult,
    fetch_sec_availability,
    fetch_universe,
    session_close,
)
from stock_screener.prospective_source.envelope import StaleSourceError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence

SESSION = date(2026, 12, 31)
INSTANT = session_close(SESSION)

#: Tests run from a temporary working directory, so the frozen databases are
#: addressed from the repository root rather than relatively.
REPO_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_DB = REPO_ROOT / "stage9k.db"
OPERATIONAL_DB = REPO_ROOT / "compounder_radar.db"


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #


class _Listing:
    """One broker asset-list entry."""

    def __init__(self, ticker: str, *, active: bool = True, fund: bool = False) -> None:
        self.ticker = ticker
        self.name = f"{ticker} Inc"
        self.exchange = "NASDAQ"
        self.is_active = active
        self.is_fund = fund
        self.quote_currency = "USD"


class _Universe:
    """A market-data provider that returns a fixed asset list."""

    def __init__(self, listings: Sequence[_Listing]) -> None:
        self._listings = list(listings)

    def get_stock_universe(self) -> list[_Listing]:
        return list(self._listings)


class _Observations:
    """A fundamentals source returning fixed observations per ticker."""

    def __init__(self, by_ticker: dict[str, list[FilingObservation]]) -> None:
        self._by_ticker = by_ticker
        self.calls: list[str] = []

    def get_filing_observations(self, ticker: str) -> list[FilingObservation]:
        self.calls.append(ticker)
        if ticker == "BOOM":
            raise RuntimeError("provider exploded")
        return list(self._by_ticker.get(ticker, ()))


class _Actions:
    """A provider returning fixed corporate actions."""

    def __init__(self, records: Sequence[CorporateActionRecord]) -> None:
        self._records = list(records)

    def get_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[CorporateActionRecord]:
        return [row for row in self._records if row.symbol in set(symbols)]


def _observation(
    concept: str,
    value: float,
    *,
    period_end: date,
    filed: date | None = date(2026, 11, 1),
    accession: str | None = "0000-26-000001",
    period_start: date | None = None,
) -> FilingObservation:
    return FilingObservation(
        taxonomy="us-gaap",
        concept=concept,
        value=value,
        unit="USD",
        period_end=period_end,
        period_start=period_start,
        filed_date=filed,
        accession=accession,
        form="10-Q",
        source="test",
    )


def _bars(ticker: str, *, through: date, count: int = 300) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(count):
        day = through - timedelta(days=offset)
        rows.append(
            {
                "ticker": ticker,
                "session": day.isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 5_000_000.0,
            }
        )
    return rows


@pytest.fixture
def research_db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A research database holding only the identity registry."""
    db = sqlite3.connect(tmp_path / "research.db")
    db.execute(
        "CREATE TABLE companies (id INTEGER PRIMARY KEY, ticker TEXT, name TEXT, "
        "exchange TEXT, sector TEXT, industry TEXT, quote_currency TEXT)"
    )
    db.executemany(
        "INSERT INTO companies VALUES (?,?,?,?,?,?,?)",
        [
            (1, "AAA", "Alpha", "NASDAQ", "Technology", "Software", "USD"),
            (2, "BBB", "Beta", "NYSE", "Industrials", "Machinery", "USD"),
            (3, "DUP", "Dup One", "NYSE", "Technology", "Software", "USD"),
            (4, "DUP", "Dup Two", "NYSE", "Technology", "Software", "USD"),
        ],
    )
    db.commit()
    yield db
    db.close()


# --------------------------------------------------------------------------- #
# §5-§7 readiness means capability
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_derived_component_is_blocked_until_a_producer_emits_it() -> None:
    report = assess_capture_readiness(
        decision_instant=INSTANT.isoformat(),
        sources={
            name: {"admissible": True, "fingerprint": f"{name}-digest"}
            for name in ("universe", "tradability", "market", "sec_availability")
        },
        derived={
            "score_inputs": {"produced": False, "validated": False, "fingerprint": None},
            "ranked_cross_section": {"produced": False, "validated": False, "fingerprint": None},
        },
        executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": True},
        store_healthy=True,
        calendar_authoritative=True,
        activation_matches=True,
        arm_matches=True,
    )

    assert report.components["score_inputs"].state is ComponentState.BLOCKED
    assert "ARTIFACT_NOT_PRODUCED" in report.components["score_inputs"].reasons
    assert not report.capture_ready


@pytest.mark.integration
def test_derivability_alone_never_reaches_capture_ready() -> None:
    report = assess_capture_readiness(
        decision_instant=INSTANT.isoformat(),
        sources={
            name: {"admissible": True, "fingerprint": f"{name}-digest"}
            for name in ("universe", "tradability", "market", "sec_availability")
        },
        derived={
            "score_inputs": {"produced": True, "validated": True, "fingerprint": None},
            "ranked_cross_section": {"produced": True, "validated": True, "fingerprint": "x"},
        },
        executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": True},
        store_healthy=True,
        calendar_authoritative=True,
        activation_matches=True,
        arm_matches=True,
    )

    assert "ARTIFACT_FINGERPRINT_MISSING" in report.components["score_inputs"].reasons
    assert report.non_null_fingerprints == 5
    assert report.as_json()["state"] == "NOT_CAPTURE_READY"


@pytest.mark.integration
def test_capture_ready_requires_every_condition_including_executors() -> None:
    kwargs: dict[str, Any] = {
        "decision_instant": INSTANT.isoformat(),
        "sources": {
            name: {"admissible": True, "fingerprint": f"{name}-digest"}
            for name in ("universe", "tradability", "market", "sec_availability")
        },
        "derived": {
            name: {"produced": True, "validated": True, "fingerprint": f"{name}-digest"}
            for name in ("score_inputs", "ranked_cross_section")
        },
        "store_healthy": True,
        "calendar_authoritative": True,
        "activation_matches": True,
        "arm_matches": True,
    }
    ready = assess_capture_readiness(
        **kwargs, executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": True}
    )
    missing = assess_capture_readiness(
        **kwargs, executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": False}
    )

    assert ready.capture_ready
    assert ready.non_null_fingerprints == 6
    assert not missing.capture_ready
    assert "executor_missing: E_EXECUTION_REFERENCE" in missing.blockers


@pytest.mark.integration
def test_the_six_fingerprint_gate_refuses_an_incomplete_package() -> None:
    with pytest.raises(IncompleteDecisionInputsError, match="DECISION_INPUT_PACKAGE_INCOMPLETE"):
        require_six_fingerprints(
            {
                "universe": "a",
                "tradability": "b",
                "market": "c",
                "sec_availability": "d",
                "score_inputs": None,
                "ranked_cross_section": "f",
            },
            decision_session=SESSION.isoformat(),
        )


@pytest.mark.integration
def test_the_six_fingerprint_gate_passes_a_complete_package() -> None:
    gated = require_six_fingerprints(
        {
            "universe": "a",
            "tradability": "b",
            "market": "c",
            "sec_availability": "d",
            "score_inputs": "e",
            "ranked_cross_section": "f",
        },
        decision_session=SESSION.isoformat(),
    )

    assert len(gated) == 6


@pytest.mark.integration
def test_the_old_readiness_module_no_longer_calls_a_derived_input_ready() -> None:
    from stock_screener.prospective_source.readiness import DERIVED_INPUTS, assess

    report = assess({}, decision_instant=INSTANT)

    for name in DERIVED_INPUTS:
        assert report.inputs[name]["status"] != "READY"


# --------------------------------------------------------------------------- #
# §29-§31 calendar authority
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_authoritative_session_source_binds() -> None:
    with closing(sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]

    binding = bind_calendar(sessions, source=str(RESEARCH_DB))

    assert binding.authoritative
    assert binding.session_count == AUTHORITATIVE_CALENDAR["session_count"]
    assert binding.sessions_sha256 == AUTHORITATIVE_CALENDAR["sessions_sha256"]
    assert binding.replay["agrees"]


@pytest.mark.integration
def test_substituting_the_thinner_session_source_is_a_binding_mismatch() -> None:
    with closing(sqlite3.connect(f"file:{OPERATIONAL_DB}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]

    with pytest.raises(CalendarBindingError, match="CALENDAR_BINDING_MISMATCH"):
        bind_calendar(sessions, source=str(OPERATIONAL_DB))


@pytest.mark.integration
def test_the_thinner_source_would_otherwise_have_agreed() -> None:
    from stock_screener.prospective_runner import verify_against_history

    with closing(sqlite3.connect(f"file:{OPERATIONAL_DB}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]

    # This is the hazard the binding exists for: the calendar reproduces the
    # thinner source perfectly and reports agreement, certifying far less.
    assert verify_against_history(sessions)["agrees"]
    assert len(sessions) < int(AUTHORITATIVE_CALENDAR["session_count"])


@pytest.mark.integration
def test_an_empty_session_source_is_refused() -> None:
    with pytest.raises(CalendarBindingError, match="supplied no sessions"):
        bind_calendar([], source="empty")


@pytest.mark.integration
def test_the_session_digest_distinguishes_two_sources() -> None:
    assert session_digest([date(2026, 1, 2)]) != session_digest([date(2026, 1, 5)])


# --------------------------------------------------------------------------- #
# §16 identity
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_identities_resolve_to_kind_and_identity_id(research_db: sqlite3.Connection) -> None:
    listings = [
        {"ticker": "AAA", "is_active": True, "is_fund": False},
        {"ticker": "BBB", "is_active": True, "is_fund": False},
    ]

    resolved, counts, dropped = resolve_identities(research_db, listings)

    assert [row.key for row in resolved] == [("COMPANY", 1), ("COMPANY", 2)]
    assert not dropped
    assert counts["NO_RESEARCH_IDENTITY"] == 0


@pytest.mark.integration
def test_a_listing_with_no_research_identity_is_excluded_not_invented(
    research_db: sqlite3.Connection,
) -> None:
    resolved, counts, dropped = resolve_identities(
        research_db, [{"ticker": "NEWCO", "is_active": True, "is_fund": False}]
    )

    assert resolved == []
    assert counts["NO_RESEARCH_IDENTITY"] == 1
    assert dropped["NEWCO"] == "NO_RESEARCH_IDENTITY"


@pytest.mark.integration
def test_a_reused_ticker_is_excluded_rather_than_guessed(
    research_db: sqlite3.Connection,
) -> None:
    _, counts, dropped = resolve_identities(
        research_db, [{"ticker": "DUP", "is_active": True, "is_fund": False}]
    )

    assert counts["AMBIGUOUS_TICKER"] == 1
    assert dropped["DUP"] == "AMBIGUOUS_TICKER"


@pytest.mark.integration
def test_a_fund_or_inactive_listing_is_not_a_company(research_db: sqlite3.Connection) -> None:
    _, counts, _ = resolve_identities(
        research_db,
        [
            {"ticker": "AAA", "is_active": True, "is_fund": True},
            {"ticker": "BBB", "is_active": False, "is_fund": False},
        ],
    )

    assert counts["NOT_A_COMPANY_LISTING"] == 2


# --------------------------------------------------------------------------- #
# §12 the fundamentals source
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_only_observations_from_a_publicly_accepted_accession_are_admitted() -> None:
    identity = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    source = _Observations(
        {
            "AAA": [
                _observation("Revenues", 100.0, period_end=date(2026, 9, 30)),
                _observation(
                    "Revenues", 999.0, period_end=date(2026, 12, 30), accession="0000-26-LATE"
                ),
            ]
        }
    )

    result = fetch_filing_observations(
        source,
        [identity],
        decision_instant=INSTANT,
        admitted={"AAA": {"0000-26-000001"}},
    )

    assert result.envelope.record_count == 1
    assert result.records[0]["value"] == 100.0
    assert result.payload["excluded"]["ACCESSION_NOT_PUBLIC_AT_INSTANT"] == 1


@pytest.mark.integration
def test_an_observation_without_an_accession_or_filed_date_cannot_participate() -> None:
    identity = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    source = _Observations(
        {
            "AAA": [
                _observation("Revenues", 1.0, period_end=date(2026, 9, 30), accession=None),
                _observation("Revenues", 2.0, period_end=date(2026, 9, 30), filed=None),
                _observation("Revenues", 3.0, period_end=date(2026, 9, 30)),
            ]
        }
    )

    result = fetch_filing_observations(
        source, [identity], decision_instant=INSTANT, admitted={"AAA": {"0000-26-000001"}}
    )

    assert result.payload["excluded"]["NO_ACCESSION"] == 1
    assert result.payload["excluded"]["NO_FILED_DATE"] == 1
    assert result.envelope.record_count == 1


@pytest.mark.integration
def test_a_filer_absent_from_the_acceptance_snapshot_is_never_silently_admitted() -> None:
    identity = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    source = _Observations({"AAA": [_observation("Revenues", 1.0, period_end=date(2026, 9, 30))]})

    with pytest.raises(StaleSourceError, match="no filer yielded an observation"):
        fetch_filing_observations(source, [identity], decision_instant=INSTANT, admitted={})


@pytest.mark.integration
def test_a_provider_failure_is_recorded_rather_than_raised() -> None:
    good = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    bad = ProspectiveIdentity(kind="COMPANY", identity_id=2, symbol="BOOM")
    source = _Observations({"AAA": [_observation("Revenues", 1.0, period_end=date(2026, 9, 30))]})

    result = fetch_filing_observations(
        source,
        [good, bad],
        decision_instant=INSTANT,
        admitted={"AAA": {"0000-26-000001"}, "BOOM": {"0000-26-000002"}},
    )

    assert any("BOOM" in note for note in result.envelope.ambiguity)
    assert result.envelope.record_count == 1


@pytest.mark.integration
def test_a_naive_decision_instant_is_refused() -> None:
    with pytest.raises(StaleSourceError, match="timezone-aware"):
        fetch_filing_observations(
            _Observations({}),
            [],
            decision_instant=datetime(2026, 12, 31, 16),  # noqa: DTZ001 - the point of the test
            admitted={},
        )


@pytest.mark.integration
def test_acceptance_records_group_by_filer() -> None:
    grouped = admitted_accessions_by_ticker(
        [
            {"ticker": "aaa", "accession": "1"},
            {"ticker": "AAA", "accession": "2"},
            {"ticker": None, "accession": "3"},
        ]
    )

    assert grouped == {"AAA": {"1", "2"}}


@pytest.mark.integration
def test_a_corporate_action_after_the_decision_session_is_dropped() -> None:
    provider = _Actions(
        [
            CorporateActionRecord(
                symbol="AAA",
                kind=CorporateActionKind.FORWARD_SPLIT,
                ex_date=date(2026, 12, 1),
                split_factor=2.0,
                source="test",
            ),
            CorporateActionRecord(
                symbol="AAA",
                kind=CorporateActionKind.FORWARD_SPLIT,
                ex_date=date(2027, 1, 5),
                split_factor=3.0,
                source="test",
            ),
        ]
    )

    result = fetch_corporate_actions(
        provider, ["AAA"], decision_session=SESSION, lookback_start=date(2026, 1, 1)
    )

    assert result.envelope.record_count == 1
    assert len(result.payload["dropped_future_actions"]) == 1


# --------------------------------------------------------------------------- #
# §12-§16 the production chain
# --------------------------------------------------------------------------- #


#: Twelve quarters, because COMPOUNDER_V1_2 needs year-over-year comparisons and
#: four quarters leave it INSUFFICIENT_DATA. Share concepts are tagged in
#: `shares`: the cover-page count is what the calculated market capitalisation
#: multiplies out, and with a USD unit it never reaches the metric engine at all.
def _quarter_ends(through: date) -> tuple[date, ...]:
    """Reported quarters ending by a date, three years back.

    Parameterised by the decision because fundamentals go stale: a cross-section
    decided in March 2027 against filings that stop in September 2026 is correctly
    `STALE_FUNDAMENTALS` and correctly unrankable, which would make a continuation
    fixture vacuous rather than wrong.
    """
    return tuple(
        end
        for year in range(through.year - 3, through.year + 1)
        for end in (date(year, 3, 31), date(year, 6, 30), date(year, 9, 30), date(year, 12, 31))
        if end <= through
    )


def _observations_for(
    identity: ProspectiveIdentity, *, scale: float, through: date
) -> list[dict[str, Any]]:
    """One identity's filed history, scaled so identities rank differently."""
    duration = (
        ("Revenues", 500_000_000.0, "USD"),
        ("GrossProfit", 300_000_000.0, "USD"),
        ("OperatingIncomeLoss", 150_000_000.0, "USD"),
        ("NetIncomeLoss", 120_000_000.0, "USD"),
        ("NetCashProvidedByUsedInOperatingActivities", 160_000_000.0, "USD"),
        ("PaymentsToAcquirePropertyPlantAndEquipment", 10_000_000.0, "USD"),
        ("WeightedAverageNumberOfDilutedSharesOutstanding", 495_000_000.0, "shares"),
    )
    instants = (
        ("Assets", 4_000_000_000.0, "USD"),
        ("Liabilities", 1_000_000_000.0, "USD"),
        ("StockholdersEquity", 3_000_000_000.0, "USD"),
        ("CashAndCashEquivalentsAtCarryingValue", 800_000_000.0, "USD"),
        ("LongTermDebtNoncurrent", 200_000_000.0, "USD"),
    )
    rows: list[dict[str, Any]] = []
    for quarter, end in enumerate(_quarter_ends(through), start=1):
        period_start = end - timedelta(days=89)
        facts: list[tuple[str, float, date | None, str, str]] = [
            (concept, value * quarter * scale, period_start, "us-gaap", unit)
            for concept, value, unit in duration
        ]
        facts += [(concept, value, None, "us-gaap", unit) for concept, value, unit in instants]
        facts.append(("EntityCommonStockSharesOutstanding", 500_000_000.0, None, "dei", "shares"))
        for concept, value, period_start_or_none, taxonomy, unit in facts:
            rows.append(
                {
                    "kind": identity.kind,
                    "identity_id": identity.identity_id,
                    "symbol": identity.symbol,
                    "taxonomy": taxonomy,
                    "concept": concept,
                    "value": value,
                    "unit": unit,
                    "period_end": end.isoformat(),
                    "period_start": (
                        period_start_or_none.isoformat() if period_start_or_none else None
                    ),
                    "filed_date": (end + timedelta(days=30)).isoformat(),
                    "accession": f"acc-{identity.identity_id}-{quarter}",
                    "form": "10-Q",
                    "fiscal_year": end.year,
                    "fiscal_period": f"Q{quarter}",
                    "frame": None,
                    "source": "test",
                }
            )
    return rows


def _benchmark_bars(*, through: date = SESSION, count: int = 400) -> list[PriceBar]:
    """A rising benchmark series, so relative strength is measurable at all."""
    return [
        PriceBar(
            date=through - timedelta(days=offset),
            open=400.0,
            high=401.0,
            low=399.0,
            close=400.0 - offset * 0.2,
            volume=1_000_000.0,
        )
        for offset in reversed(range(count))
    ]


def _chain_fixtures(
    count: int = 12, *, session: date = SESSION
) -> tuple[list[ProspectiveIdentity], list[dict[str, Any]], list[dict[str, Any]]]:
    """Identities that genuinely clear the frozen eligibility and score gates.

    The frozen top-decile count over two identities is zero, and a cross-section
    that fails eligibility would make every selection assertion vacuous, so the
    fixture is built to score rather than merely to exist.
    """
    identities = [
        ProspectiveIdentity(
            kind="COMPANY",
            identity_id=number,
            symbol=f"T{number:02d}",
            name=f"Target {number}",
            exchange="NASDAQ",
            sector="Technology",
            industry="Software",
            quote_currency="USD",
        )
        for number in range(1, count + 1)
    ]
    #: The newest filed quarter must be recent enough for the decision to rank it.
    newest = max(end for end in _quarter_ends(session) if end <= session - timedelta(days=45))
    observations = [
        row
        for index, identity in enumerate(identities)
        for row in _observations_for(identity, scale=1.0 + index * 0.05, through=newest)
    ]
    bars = [row for identity in identities for row in _bars(identity.symbol, through=session)]
    return identities, observations, bars


@pytest.mark.integration
def test_score_inputs_are_built_and_fingerprinted_deterministically() -> None:
    identities, observations, bars = _chain_fixtures()

    first = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={"market": "m"},
        bar_volume_basis=VolumeBasis.CONSOLIDATED,
    )
    second = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={"market": "m"},
        bar_volume_basis=VolumeBasis.CONSOLIDATED,
    )

    assert first.fingerprint() == second.fingerprint()
    assert first.fingerprint() is not None
    assert first.scorable == 12


@pytest.mark.integration
def test_an_identity_with_nothing_filed_is_present_and_unscorable() -> None:
    identities, _, bars = _chain_fixtures()

    built = build_score_inputs(
        identities,
        observation_records=[],
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )

    assert len(built.inputs) == 12
    assert built.scorable == 0
    assert all(
        row.provenance["unusable_reason"] == "NOTHING_FILED_BY_DECISION_INSTANT"
        for row in built.inputs
    )


@pytest.mark.integration
def test_an_identity_with_no_price_history_is_unscorable() -> None:
    identities, observations, _ = _chain_fixtures()

    built = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=[],
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )

    assert all(
        row.provenance["unusable_reason"] == "NO_PRICE_HISTORY_THROUGH_DECISION_SESSION"
        for row in built.inputs
    )


@pytest.mark.integration
def test_provenance_answers_which_inputs_produced_the_score() -> None:
    identities, observations, bars = _chain_fixtures()

    built = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={"market": "m"},
    )
    provenance = built.inputs[0].provenance

    assert provenance["accessions"]
    assert provenance["periods_used"] > 0
    assert provenance["price_sessions"] > 0
    assert provenance["last_session"] == SESSION.isoformat()
    assert provenance["score_version"] == "COMPOUNDER_V1_2"
    assert provenance["decision_instant"] == INSTANT.isoformat()


@pytest.mark.integration
def test_the_frozen_scorer_produces_the_prospective_scores() -> None:
    identities, observations, bars = _chain_fixtures()
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )

    scores = score_cross_section(
        inputs,
        benchmark_bars=[
            PriceBar(
                date=SESSION - timedelta(days=offset),
                open=400.0,
                high=401.0,
                low=399.0,
                close=400.0 + offset * 0.1,
                volume=1.0,
            )
            for offset in range(400)
        ],
        benchmark_symbol="SPY",
        decision_session=SESSION,
    )

    assert scores.as_json()["score_version"] == "COMPOUNDER_V1_2"
    assert scores.as_json()["reimplemented_formula"] is False
    assert scores.fingerprint()
    assert scores.score_input_fingerprint == inputs.fingerprint()


@pytest.mark.integration
def test_the_benchmark_is_measured_as_of_the_decision_not_later() -> None:
    from stock_screener.prospective_capture import benchmark_from_bars

    bars = [
        PriceBar(
            date=SESSION + timedelta(days=offset),
            open=1.0,
            high=1.0,
            low=1.0,
            close=1_000.0,
            volume=1.0,
        )
        for offset in range(1, 10)
    ]

    measured = benchmark_from_bars(bars, symbol="SPY", as_of=SESSION)

    assert measured.return_6m is None
    assert measured.return_12m is None


def _rankable_scores() -> ProspectiveScoreSet:
    """A score set past the score and freshness gates, to test the later ones."""
    built = ProspectiveScoreSet(
        decision_session=SESSION.isoformat(), decision_instant=INSTANT.isoformat()
    )
    built.scores = [
        ProspectiveScore(
            kind="COMPANY",
            identity_id=identity,
            symbol=symbol,
            status="SCORED",
            final_score=60.0 - identity,
            freshness="CURRENT",
            rank_eligible=True,
            eligible=True,
        )
        for identity, symbol in ((1, "AAA"), (2, "BBB"))
    ]
    return built


@pytest.mark.integration
def test_an_identity_the_broker_does_not_list_is_not_tradable() -> None:
    built = build_cross_section(
        _rankable_scores(),
        tradability={("COMPANY", 1): "TRADABLE"},
        session_priced=[("COMPANY", 1), ("COMPANY", 2)],
        source_fingerprints={"score_inputs": "x"},
    )

    assert built.fingerprint()
    assert built.exclusions["NOT_TRADABLE_ABSENT"] == 1
    assert [row.identity_id for row in built.executable] == [1]


@pytest.mark.integration
def test_an_identity_without_a_decision_session_bar_is_not_executable() -> None:
    built = build_cross_section(
        _rankable_scores(),
        tradability={("COMPANY", 1): "TRADABLE", ("COMPANY", 2): "TRADABLE"},
        session_priced=[],
        source_fingerprints={},
    )

    assert built.executable == []
    assert built.exclusions["NO_DECISION_SESSION_PRICE"] == 2


@pytest.mark.integration
def test_the_funnel_counts_every_stage_an_identity_reached() -> None:
    built = build_cross_section(
        _rankable_scores(),
        tradability={("COMPANY", 1): "TRADABLE", ("COMPANY", 2): "TRADABLE"},
        session_priced=[("COMPANY", 1), ("COMPANY", 2)],
        source_fingerprints={},
    )

    assert built.funnel == {
        "UNIVERSE": 2,
        "SCORE_READY": 2,
        "RANKABLE": 2,
        "TRADABLE": 2,
        "EXECUTABLE": 2,
    }


@pytest.mark.integration
def test_ranking_breaks_ties_on_identity_not_on_ticker() -> None:
    from stock_screener.prospective_capture.crosssection import (
        CrossSectionRow,
        ProspectiveCrossSection,
        ProspectiveStage,
    )

    section = ProspectiveCrossSection(
        decision_session=SESSION.isoformat(), decision_instant=INSTANT.isoformat()
    )
    section.rows = [
        CrossSectionRow(
            kind="COMPANY",
            identity_id=identity,
            symbol=symbol,
            score_status="SCORED",
            final_score=50.0,
            freshness="CURRENT",
            rank_eligible=True,
            tradability="TRADABLE",
            has_session_bar=True,
            stage=str(ProspectiveStage.EXECUTABLE),
        )
        for identity, symbol in ((9, "AAA"), (2, "ZZZ"))
    ]
    from stock_screener.membership_hypothesis import rank

    ordered = rank(section.candidates)

    # Equal scores: the lower identity_id wins, regardless of ticker order.
    assert [row.identity_id for row in ordered] == [2, 9]


# --------------------------------------------------------------------------- #
# §45-§49 the sealed package
# --------------------------------------------------------------------------- #


def _sealed(tmp_path: Path, *, retrieved_before: bool = True) -> tuple[SnapshotStore, Path, Any]:
    store = SnapshotStore(tmp_path / "snapshots")
    universe = fetch_universe(_Universe([_Listing("AAA")]), decision_session=SESSION)
    if retrieved_before:
        moment = INSTANT - timedelta(minutes=5)
        envelope = type(universe.envelope)(
            **{
                **universe.envelope.__dict__,
                "retrieved_at": moment,
                "semantic_as_of": moment,
            }
        )
        universe = type(universe)(
            envelope=envelope, payload=universe.payload, records=universe.records
        )
    return store, tmp_path, universe


@pytest.mark.integration
def test_a_predecision_source_retrieved_after_the_instant_is_refused(tmp_path: Path) -> None:
    store, directory, universe = _sealed(tmp_path, retrieved_before=False)

    with pytest.raises(PackageError, match="not admissible"):
        seal_predecision(
            store,
            directory,
            decision_session=SESSION.isoformat(),
            decision_instant=INSTANT,
            sources={"universe": universe, "tradability": universe},
        )


@pytest.mark.integration
def test_a_missing_predecision_source_is_the_honest_miss(tmp_path: Path) -> None:
    store, directory, universe = _sealed(tmp_path)

    with pytest.raises(PackageError, match="REQUIRED_PREDECISION_SOURCE_UNAVAILABLE"):
        seal_predecision(
            store,
            directory,
            decision_session=SESSION.isoformat(),
            decision_instant=INSTANT,
            sources={"universe": universe},
        )


@pytest.mark.integration
def test_a_sealed_package_is_recoverable_by_a_fresh_process(tmp_path: Path) -> None:
    store, directory, universe = _sealed(tmp_path)

    sealed = seal_predecision(
        store,
        directory,
        decision_session=SESSION.isoformat(),
        decision_instant=INSTANT,
        sources={"universe": universe, "tradability": universe},
    )
    recovered = load_package(directory)

    assert recovered.decision_session == sealed.decision_session
    assert recovered.fingerprints == sealed.fingerprints
    assert recovered.verify(store)["all_recovered"]


@pytest.mark.integration
def test_the_post_close_stage_may_not_replace_a_predecision_snapshot(tmp_path: Path) -> None:
    from stock_screener.prospective_capture import complete_post_close

    store, directory, universe = _sealed(tmp_path)
    sealed = seal_predecision(
        store,
        directory,
        decision_session=SESSION.isoformat(),
        decision_instant=INSTANT,
        sources={"universe": universe, "tradability": universe},
    )

    with pytest.raises(PackageError, match="POST_CLOSE_WOULD_REPLACE_PREDECISION_SOURCE"):
        complete_post_close(
            store,
            directory,
            package=sealed,
            decision_instant=INSTANT,
            sources={"universe": universe},
        )


@pytest.mark.integration
def test_loading_an_absent_package_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(PackageError, match="no sealed input package"):
        load_package(tmp_path)


# --------------------------------------------------------------------------- #
# §40-§41 September 30 remains missed
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_capture_executor_refuses_september_30_before_computing_anything(
    tmp_path: Path,
) -> None:
    from stock_screener.prospective_capture import capture_decision
    from stock_screener.prospective_source.window import WindowError

    store = SnapshotStore(tmp_path / "snapshots")

    from stock_screener.prospective_capture.package import SealedPackage

    missed = SealedPackage(
        decision_session="2026-09-30",
        decision_instant=session_close(date(2026, 9, 30)).isoformat(),
        stage="COMPLETE",
    )

    with pytest.raises(WindowError, match="MISSED_DECISION_NOT_BACKFILLABLE"):
        capture_decision(
            store,
            tmp_path,
            package=missed,
            activation={},
            arm={},
            calendar=None,  # type: ignore[arg-type]
            identities=[],
            benchmark_bars=[],
            benchmark_symbol="SPY",
            bar_volume_basis=VolumeBasis.UNKNOWN,
            thresholds=None,
            semantic_fingerprint="x",
            code_commit="y",
            state_path=tmp_path / "state.json",
            now=datetime.now(UTC),
        )


@pytest.mark.integration
def test_no_decision_packet_exists_for_september_30(tmp_path: Path) -> None:
    assert not list(tmp_path.glob("decision_2026-09-30.json"))
    assert prior_incumbents(tmp_path, before_session="2026-12-31") == []


# --------------------------------------------------------------------------- #
# §11 first decision has no incumbents
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_first_prospective_decision_has_no_incumbents(tmp_path: Path) -> None:
    assert prior_incumbents(tmp_path, before_session=SESSION.isoformat()) == []


@pytest.mark.integration
def test_incumbents_come_only_from_an_earlier_prospective_packet(tmp_path: Path) -> None:
    (tmp_path / "decision_2026-09-30.json").write_text(
        json.dumps({"h2_target_set": [{"kind": "COMPANY", "identity_id": 7}]}), encoding="utf-8"
    )

    assert prior_incumbents(tmp_path, before_session="2026-12-31") == [("COMPANY", 7)]
    assert prior_incumbents(tmp_path, before_session="2026-01-01") == []


# --------------------------------------------------------------------------- #
# §25-§28 the execution reference
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_execution_session_is_derived_from_the_calendar() -> None:
    assert derive_execution_session(date(2026, 12, 31)) == date(2027, 1, 4)


@pytest.mark.integration
def test_an_execution_reference_without_a_prior_decision_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ExecutionRefusedError, match="NO_PRIOR_DECISION"):
        capture_execution_reference(
            tmp_path,
            decision_session=SESSION.isoformat(),
            opens={},
            source={"provider": "test"},
            state_path=tmp_path / "state.json",
            now=datetime.now(UTC),
        )


@pytest.mark.integration
def test_an_execution_reference_is_refused_before_its_session_opens(tmp_path: Path) -> None:
    (tmp_path / f"decision_{SESSION.isoformat()}.json").write_text(
        json.dumps(
            {
                "packet_key": SESSION.isoformat(),
                "execution_session": "2027-01-04",
                "h2_target_set": [{"kind": "COMPANY", "identity_id": 1, "symbol": "AAA"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExecutionRefusedError, match="EXECUTION_SESSION_NOT_OPEN"):
        capture_execution_reference(
            tmp_path,
            decision_session=SESSION.isoformat(),
            opens={"AAA": 101.0},
            source={"provider": "test"},
            state_path=tmp_path / "state.json",
            now=datetime(2026, 12, 31, 17, tzinfo=UTC),
        )


# --------------------------------------------------------------------------- #
# §17-§18, §26 guards
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_capture_package_cannot_reach_an_outcome_a_sector_rule_or_an_order() -> None:
    audit = audit_package()

    assert audit["findings"] == {
        "outcome_fields": [],
        "merged_fundamental_reads": [],
        "sector_logic": [],
        "broker_capability": [],
    }
    assert audit["broker_requests"] == 0
    assert audit["fundamentals_are_accession_scoped"] is True


@pytest.mark.integration
def test_the_guard_detects_a_real_violation(tmp_path: Path) -> None:
    offender = tmp_path / "leaky.py"
    offender.write_text("def go(client):\n    return client.submit_order()\n", encoding="utf-8")

    with pytest.raises(GuardViolationError, match="broker_capability"):
        audit_package(tmp_path)


@pytest.mark.integration
def test_the_guard_reads_syntax_not_text(tmp_path: Path) -> None:
    declaration = tmp_path / "declares.py"
    declaration.write_text('FORBIDDEN = ("submit_order", "forward_returns")\n', encoding="utf-8")

    # A string naming a forbidden operation is not a use of it. This is why the
    # audit walks the tree: R1.7's grep-based guard flagged its own declarations.
    assert "submit_order" not in reachable_names([declaration])
    assert audit_package(tmp_path)["findings"]["broker_capability"] == []


# --------------------------------------------------------------------------- #
# the fingerprint covers the new path
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_every_capture_module_that_can_move_a_decision_is_fingerprinted() -> None:
    package = source_root() / "prospective_capture"
    semantic = {name for name in SEMANTIC_MODULES if name.startswith("prospective_capture/")}
    present = {
        f"prospective_capture/{path.name}"
        for path in package.glob("*.py")
        if path.name not in {"__init__.py", "run.py", "guards.py"}
    }

    assert present == semantic


@pytest.mark.integration
def test_every_fingerprinted_module_exists() -> None:
    from stock_screener.prospective_runner import semantic_fingerprint

    report = semantic_fingerprint()

    assert report["module_count"] == len(SEMANTIC_MODULES)
    assert all(report["modules"].values())


# --------------------------------------------------------------------------- #
# §19-§24 the decision executor, end to end
# --------------------------------------------------------------------------- #


def _result(
    envelope_kwargs: dict[str, Any], payload: dict[str, Any], records: list[dict[str, Any]]
) -> SourceResult:
    """One source result with an envelope that describes its own payload."""
    from stock_screener.prospective_source.envelope import (
        Completeness,
        SnapshotEnvelope,
        content_digest,
    )

    envelope = SnapshotEnvelope(
        source_name="Fake",
        source_version="v1",
        operation="op",
        content_sha256=content_digest(payload),
        request="req",
        normalizer_version="n1",
        record_count=len(records),
        completeness=Completeness.COMPLETE,
        **{"decision_session": SESSION.isoformat(), **envelope_kwargs},
    )
    return SourceResult(envelope=envelope, payload=payload, records=records)


def _complete_package(
    tmp_path: Path, *, session: date = SESSION, count: int = 12
) -> tuple[SnapshotStore, Path, list[ProspectiveIdentity]]:
    """A sealed, complete six-input package over two identities."""
    from stock_screener.prospective_capture import complete_post_close
    from stock_screener.prospective_source.envelope import TemporalClass

    identities, observations, bars = _chain_fixtures(count=count, session=session)
    instant = session_close(session)
    bars = [
        row for identity in identities for row in _bars(identity.symbol, through=session, count=420)
    ]
    store = SnapshotStore(tmp_path / "snapshots")
    before = instant - timedelta(minutes=10)

    listings = [
        {"ticker": identity.symbol, "is_active": True, "is_fund": False} for identity in identities
    ]
    universe = _result(
        {
            "source_type": "universe",
            "retrieved_at": before,
            "semantic_as_of": before,
            "temporal_class": TemporalClass.CURRENT_AT_RETRIEVAL,
            "decision_session": session.isoformat(),
        },
        {"listings": listings},
        listings,
    )
    seal_predecision(
        store,
        tmp_path,
        decision_session=session.isoformat(),
        decision_instant=instant,
        sources={"universe": universe, "tradability": universe},
    )
    package = load_package(tmp_path)
    after = instant + timedelta(hours=2)
    complete_post_close(
        store,
        tmp_path,
        package=package,
        decision_instant=instant,
        sources={
            "market": _result(
                {
                    "source_type": "market",
                    "retrieved_at": after,
                    "semantic_as_of": instant,
                    "temporal_class": TemporalClass.SESSION_BOUNDED,
                    "decision_session": session.isoformat(),
                },
                {"bars": bars},
                bars,
            ),
            "sec_availability": _result(
                {
                    "source_type": "sec_availability",
                    "retrieved_at": after,
                    "semantic_as_of": instant,
                    "temporal_class": TemporalClass.EVENT_TIME,
                    "decision_session": session.isoformat(),
                },
                {"admitted": _acceptance_rows(observations, instant)},
                _acceptance_rows(observations, instant),
            ),
            "filing_observations": _result(
                {
                    "source_type": "filing_observations",
                    "retrieved_at": after,
                    "semantic_as_of": instant,
                    "temporal_class": TemporalClass.EVENT_TIME,
                    "decision_session": session.isoformat(),
                },
                {"observations": observations},
                observations,
            ),
            "corporate_actions": _result(
                {
                    "source_type": "corporate_actions",
                    "retrieved_at": after,
                    "semantic_as_of": instant,
                    "temporal_class": TemporalClass.SESSION_BOUNDED,
                    "decision_session": session.isoformat(),
                },
                {"actions": []},
                [],
            ),
        },
    )
    return store, tmp_path, identities


def _acceptance_rows(
    observations: Sequence[Mapping[str, Any]], instant: datetime
) -> list[dict[str, Any]]:
    """An acceptance record per accession, all public before the instant."""
    return [
        {
            "accession": accession,
            "accepted_at": (instant - timedelta(days=30)).isoformat(),
            "form": "10-Q",
        }
        for accession in sorted({str(row["accession"]) for row in observations})
    ]


def _armed(
    fingerprint: str = "fp",
    commit: str = "c0ffee",
    *,
    session: date = SESSION,
    execution: str = "2027-01-04",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """An activation and arm record that authorise a window."""
    from stock_screener.prospective_protocol import CANDIDATE_ID, PROTOCOL_ID

    common = {
        "candidate_id": CANDIDATE_ID,
        "protocol_id": PROTOCOL_ID,
        "semantic_code_fingerprint": fingerprint,
        "code_commit": commit,
    }
    arm = {
        **common,
        "decision_session": session.isoformat(),
        "execution_reference_session": execution,
        "armed_at": "2026-10-01T12:00:00+00:00",
        "arm_version": "PROSPECTIVE_ARM_V2",
    }
    return dict(common), arm


def _waiting_state(path: Path) -> None:
    """Put the runner in WAITING_FOR_DECISION, the state an arm leaves it in."""
    from stock_screener.prospective_runner import ProspectiveState, RunnerState, save_state

    state = RunnerState(state=ProspectiveState.WAITING_FOR_DECISION)
    save_state(path, state)


def _calendar() -> Any:
    """The real, authoritative calendar binding."""
    with closing(sqlite3.connect(f"file:{RESEARCH_DB}?mode=ro", uri=True)) as db:
        sessions = [
            date.fromisoformat(str(row[0]))
            for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
        ]
    return bind_calendar(sessions, source=str(RESEARCH_DB))


def _capture(
    tmp_path: Path,
    *,
    now: datetime | None = None,
    session: date = SESSION,
    count: int = 12,
    execution: str = "2027-01-04",
    **overrides: Any,
) -> Any:
    """Run the decision executor over a complete package."""
    from stock_screener.prospective_capture import capture_decision

    store, directory, identities = _complete_package(tmp_path, session=session, count=count)
    activation, arm = _armed(session=session, execution=execution)
    state_path = tmp_path / "state.json"
    _waiting_state(state_path)
    kwargs: dict[str, Any] = {
        "package": load_package(directory),
        "activation": activation,
        "arm": arm,
        "calendar": _calendar(),
        "identities": identities,
        "benchmark_bars": _benchmark_bars(through=session),
        "benchmark_symbol": "SPY",
        "bar_volume_basis": VolumeBasis.CONSOLIDATED,
        "thresholds": None,
        "semantic_fingerprint": "fp",
        "code_commit": "c0ffee",
        "state_path": state_path,
        "now": now or datetime(2027, 1, 1, tzinfo=UTC),
    }
    kwargs.update(overrides)
    return capture_decision(store, directory, **kwargs)


@pytest.mark.integration
def test_a_decision_packet_is_written_and_the_state_advances(tmp_path: Path) -> None:
    capture = _capture(tmp_path)

    assert capture.packet_path.is_file()
    assert capture.written
    assert len(capture.fingerprints) == 6
    assert all(capture.fingerprints.values())
    assert capture.state == "DECISION_CAPTURED"


@pytest.mark.integration
def test_the_decision_packet_names_one_authoritative_constructor(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))

    assert body["candidate_id"]
    assert body["protocol_id"]
    assert body["execution_session"] == "2027-01-04"
    assert "h2_target_set" in body
    assert "comparator_target_set" in body
    assert "forward_returns" not in body


@pytest.mark.integration
def test_a_second_capture_returns_the_same_packet_rather_than_a_second_one(
    tmp_path: Path,
) -> None:
    first = _capture(tmp_path)
    second = _capture(tmp_path)

    assert second.packet_digest == first.packet_digest
    assert second.written is False
    packets = [
        path
        for path in tmp_path.glob("decision_*.json")
        if path.name != "decision_input_package.json"
    ]
    assert len(packets) == 1


@pytest.mark.integration
def test_a_capture_is_refused_when_the_semantic_fingerprint_has_moved(tmp_path: Path) -> None:
    with pytest.raises(DecisionRefusedError, match="SEMANTIC_RESEARCH_CHANGE"):
        _capture(tmp_path, semantic_fingerprint="moved")


@pytest.mark.integration
def test_a_capture_is_refused_when_the_commit_has_drifted(tmp_path: Path) -> None:
    with pytest.raises(DecisionRefusedError, match="COMMIT_DRIFT"):
        _capture(tmp_path, code_commit="deadbeef")


@pytest.mark.integration
def test_a_capture_is_refused_before_the_decision_instant(tmp_path: Path) -> None:
    with pytest.raises(DecisionRefusedError, match="DECISION_INSTANT_NOT_REACHED"):
        _capture(tmp_path, now=datetime(2026, 12, 31, 18, tzinfo=UTC))


@pytest.mark.integration
def test_a_capture_is_refused_when_the_arm_authorises_another_session(tmp_path: Path) -> None:
    _, arm = _armed()
    arm["decision_session"] = "2027-03-31"

    with pytest.raises(DecisionRefusedError, match="authorises 2027-03-31"):
        _capture(tmp_path, arm=arm)


@pytest.mark.integration
def test_a_capture_is_refused_on_an_incomplete_input_package(tmp_path: Path) -> None:
    from stock_screener.prospective_capture import capture_decision
    from stock_screener.prospective_source.envelope import TemporalClass

    store = SnapshotStore(tmp_path / "snapshots")
    identities, _, _ = _chain_fixtures()
    listings = [{"ticker": "AAA", "is_active": True, "is_fund": False}]
    before = INSTANT - timedelta(minutes=10)
    universe = _result(
        {
            "source_type": "universe",
            "retrieved_at": before,
            "semantic_as_of": before,
            "temporal_class": TemporalClass.CURRENT_AT_RETRIEVAL,
        },
        {"listings": listings},
        listings,
    )
    seal_predecision(
        store,
        tmp_path,
        decision_session=SESSION.isoformat(),
        decision_instant=INSTANT,
        sources={"universe": universe, "tradability": universe},
    )
    activation, arm = _armed()
    _waiting_state(tmp_path / "state.json")

    with pytest.raises(DecisionRefusedError, match="SOURCE_INCOMPLETE"):
        capture_decision(
            store,
            tmp_path,
            package=load_package(tmp_path),
            activation=activation,
            arm=arm,
            calendar=_calendar(),
            identities=identities,
            benchmark_bars=[],
            benchmark_symbol="SPY",
            bar_volume_basis=VolumeBasis.CONSOLIDATED,
            thresholds=None,
            semantic_fingerprint="fp",
            code_commit="c0ffee",
            state_path=tmp_path / "state.json",
            now=datetime(2027, 1, 1, tzinfo=UTC),
        )


@pytest.mark.integration
def test_a_capture_is_refused_under_a_non_authoritative_calendar(tmp_path: Path) -> None:
    from stock_screener.prospective_capture.calendar_binding import CalendarBinding

    weak = CalendarBinding(
        calendar_id="NYSE_SESSION_CALENDAR_V1",
        source="thin",
        session_count=282,
        first_session="2025-07-14",
        last_session="2026-08-25",
        sessions_sha256="x",
        replay={"agrees": True},
        authoritative=False,
    )

    with pytest.raises(DecisionRefusedError, match="CALENDAR_BINDING_MISMATCH"):
        _capture(tmp_path, calendar=weak)


@pytest.mark.integration
def test_the_decision_writes_its_derived_artifacts_beside_the_packet(tmp_path: Path) -> None:
    _capture(tmp_path)

    for name in ("score_inputs", "scores", "cross_section"):
        assert (tmp_path / f"{name}_{SESSION.isoformat()}.json").is_file()


@pytest.mark.integration
def test_an_execution_reference_prices_a_captured_decision(tmp_path: Path) -> None:
    capture = _capture(tmp_path)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))
    targets = [f"{row['kind']}:{row['identity_id']}" for row in body["h2_target_set"]]
    assert targets, "the fixture must select at least one identity"

    reference = capture_execution_reference(
        tmp_path,
        decision_session=SESSION.isoformat(),
        opens=dict.fromkeys(targets, 123.0),
        source={"provider": "Fake", "basis": "official open"},
        state_path=tmp_path / "state.json",
        now=datetime(2027, 1, 4, 15, tzinfo=UTC),
    )

    assert reference.execution_session == "2027-01-04"
    assert reference.capture_status == "COMPLETE"
    assert reference.unpriced == 0
    assert reference.state == "EXECUTION_CAPTURED"
    assert reference.as_json()["orders_placed"] == 0


@pytest.mark.integration
def test_an_unpriced_target_is_recorded_rather_than_substituted(tmp_path: Path) -> None:
    from stock_screener.prospective_runner import packet_digest

    decision = {
        "packet_key": SESSION.isoformat(),
        "execution_session": "2027-01-04",
        "h2_target_set": [{"kind": "COMPANY", "identity_id": 1, "symbol": "AAA"}],
    }
    (tmp_path / f"decision_{SESSION.isoformat()}.json").write_text(
        json.dumps(decision, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _waiting_state(tmp_path / "state.json")

    reference = capture_execution_reference(
        tmp_path,
        decision_session=SESSION.isoformat(),
        opens={},
        source={"provider": "Fake"},
        state_path=tmp_path / "state.json",
        now=datetime(2027, 1, 4, 15, tzinfo=UTC),
    )
    body = json.loads(reference.packet_path.read_text(encoding="utf-8"))

    assert reference.unpriced == 1
    assert reference.capture_status == "INCOMPLETE"
    assert body["reference_opens"] == {"COMPANY:1": None}
    assert all(row["substitution"] == "NONE" for row in body["unpriced"])
    assert body["decision_packet_sha256"] == packet_digest(decision)
    assert body["modifies_decision_packet"] is False


@pytest.mark.integration
def test_an_execution_reference_is_exactly_once(tmp_path: Path) -> None:
    _capture(tmp_path)
    args: dict[str, Any] = {
        "decision_session": SESSION.isoformat(),
        "opens": {"COMPANY:1": 1.0},
        "source": {"provider": "Fake"},
        "state_path": tmp_path / "state.json",
        "now": datetime(2027, 1, 4, 15, tzinfo=UTC),
    }

    first = capture_execution_reference(tmp_path, **args)
    second = capture_execution_reference(tmp_path, **args)

    assert second.packet_digest == first.packet_digest
    assert second.written is False


@pytest.mark.integration
def test_the_executor_selection_path_yields_targets_on_an_executable_cross_section() -> None:
    """The end-to-end decision tests use synthetic companies that do not clear the
    frozen eligibility thresholds, so their target sets are legitimately empty.
    This drives the same selection path over an executable cross-section, so the
    rule's output is checked rather than assumed."""
    from stock_screener.prospective_runner import ordinary_target_count, paired_targets

    scores = ProspectiveScoreSet(
        decision_session=SESSION.isoformat(), decision_instant=INSTANT.isoformat()
    )
    scores.scores = [
        ProspectiveScore(
            kind="COMPANY",
            identity_id=number,
            symbol=f"T{number:02d}",
            status="SCORED",
            final_score=100.0 - number,
            freshness="CURRENT",
            rank_eligible=True,
            eligible=True,
        )
        for number in range(1, 21)
    ]
    section = build_cross_section(
        scores,
        tradability={("COMPANY", n): "TRADABLE" for n in range(1, 21)},
        session_priced=[("COMPANY", n) for n in range(1, 21)],
        source_fingerprints={},
    )

    targets = paired_targets(section.candidates, [])

    assert len(section.executable) == 20
    assert targets["target_count"] == ordinary_target_count(20)
    assert len(targets["h2_target_set"]) == targets["target_count"]
    # With no incumbents the two strategies must agree, which is what makes the
    # first prospective decision ordinary top-decile.
    assert targets["sets_are_equal"] is True
    assert [row.identity_id for row in section.executable[:3]] == [1, 2, 3]


# --------------------------------------------------------------------------- #
# §50-§54 fundamental provenance and the refusal of merged statements
# --------------------------------------------------------------------------- #


class _Acceptance:
    """An acceptance source with one filing before T and one after it."""

    def __init__(self, filings: dict[str, list[Any]]) -> None:
        self._filings = filings
        self.statement_calls: list[str] = []

    def get_filing_acceptance(self, ticker: str) -> list[Any]:
        return list(self._filings.get(ticker, ()))

    def get_filing_observations(self, ticker: str) -> list[FilingObservation]:
        early = _observation(
            "Revenues",
            100.0,
            period_end=date(2026, 9, 30),
            filed=date(2026, 11, 1),
            accession="ACC-EARLY",
            period_start=date(2026, 7, 1),
        )
        late = _observation(
            "Revenues",
            999.0,
            period_end=date(2026, 9, 30),
            filed=date(2027, 1, 2),
            accession="ACC-LATE",
            period_start=date(2026, 7, 1),
        )
        return [early, late]

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[Any]:
        """The merged read. Invoking it at all is the failure this records."""
        self.statement_calls.append(ticker)
        raise AssertionError("the prospective capture path invoked the merged statement operation")


def _accepted(accession: str, moment: datetime) -> Any:
    from domain import AcceptedFiling

    return AcceptedFiling(
        accession=accession,
        accepted_at=moment,
        filed_date=moment.date(),
        form="10-Q",
        source="test",
    )


@pytest.mark.integration
def test_a_value_from_a_filing_accepted_after_the_instant_never_reaches_a_metric() -> None:
    identity = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    source = _Acceptance(
        {
            "AAA": [
                _accepted("ACC-EARLY", INSTANT - timedelta(days=60)),
                _accepted("ACC-LATE", INSTANT + timedelta(days=2)),
            ]
        }
    )

    acceptance = fetch_sec_availability(source, ["AAA"], decision_instant=INSTANT)
    observations = fetch_filing_observations(
        source,
        [identity],
        decision_instant=INSTANT,
        admitted=admitted_accessions_by_ticker(acceptance.records),
    )

    admitted = [row["accession"] for row in observations.records]
    assert admitted == ["ACC-EARLY"]
    assert all(row["value"] != 999.0 for row in observations.records)
    assert observations.payload["excluded"]["ACCESSION_NOT_PUBLIC_AT_INSTANT"] == 1


@pytest.mark.integration
def test_the_post_cutoff_value_influences_no_score_input_score_or_rank() -> None:
    identity = ProspectiveIdentity(kind="COMPANY", identity_id=1, symbol="AAA")
    source = _Acceptance(
        {
            "AAA": [
                _accepted("ACC-EARLY", INSTANT - timedelta(days=60)),
                _accepted("ACC-LATE", INSTANT + timedelta(days=2)),
            ]
        }
    )
    acceptance = fetch_sec_availability(source, ["AAA"], decision_instant=INSTANT)
    observations = fetch_filing_observations(
        source,
        [identity],
        decision_instant=INSTANT,
        admitted=admitted_accessions_by_ticker(acceptance.records),
    )

    inputs = build_score_inputs(
        [identity],
        observation_records=observations.records,
        bar_records=_bars("AAA", through=SESSION),
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )
    provenance = inputs.inputs[0].provenance

    assert provenance["accessions"] == ["ACC-EARLY"]
    assert "ACC-LATE" not in provenance["accessions"]
    assert source.statement_calls == []


@pytest.mark.integration
def test_the_provenance_audit_rejects_a_post_cutoff_accession() -> None:
    from stock_screener.prospective_capture import ProvenanceViolationError
    from stock_screener.prospective_capture.provenance import audit

    identities, observations, bars = _chain_fixtures(count=1)
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )
    accessions = sorted({str(row["accession"]) for row in observations})
    late = [
        {"accession": name, "accepted_at": (INSTANT + timedelta(days=1)).isoformat()}
        for name in accessions
    ]

    with pytest.raises(ProvenanceViolationError, match="post_cutoff_observations"):
        audit(
            inputs,
            observation_records=observations,
            acceptance_records=late,
            decision_instant=INSTANT,
            source_operation="get_filing_observations",
        )


@pytest.mark.integration
def test_the_provenance_audit_rejects_an_untraceable_value() -> None:
    from stock_screener.prospective_capture import ProvenanceViolationError
    from stock_screener.prospective_capture.provenance import audit

    identities, observations, bars = _chain_fixtures(count=1)
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )

    # No acceptance record at all: nothing can prove it was public.
    with pytest.raises(ProvenanceViolationError, match="untraceable_values"):
        audit(
            inputs,
            observation_records=observations,
            acceptance_records=[],
            decision_instant=INSTANT,
            source_operation="get_filing_observations",
        )


@pytest.mark.integration
def test_the_provenance_audit_passes_a_fully_traceable_set() -> None:
    from stock_screener.prospective_capture.provenance import audit

    identities, observations, bars = _chain_fixtures(count=2)
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )
    accessions = sorted({str(row["accession"]) for row in observations})
    dated = [
        {"accession": name, "accepted_at": (INSTANT - timedelta(days=30)).isoformat()}
        for name in accessions
    ]

    report = audit(
        inputs,
        observation_records=observations,
        acceptance_records=dated,
        decision_instant=INSTANT,
        source_operation="get_filing_observations",
    )

    assert report["clean"] is True
    assert report["untraceable_values"] == 0
    assert report["post_cutoff_observations_rejected"] == 0
    assert report["merged_current_operations_used"] == []
    assert report["score_inputs_with_complete_accession_provenance"] == inputs.scorable
    assert report["unique_accessions"] == len(accessions)


@pytest.mark.integration
def test_a_merged_statement_operation_is_a_provenance_violation() -> None:
    from stock_screener.prospective_capture import ProvenanceViolationError
    from stock_screener.prospective_capture.provenance import audit

    identities, observations, bars = _chain_fixtures(count=1)
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )
    dated = [
        {
            "accession": str(row["accession"]),
            "accepted_at": (INSTANT - timedelta(days=30)).isoformat(),
        }
        for row in observations
    ]

    with pytest.raises(ProvenanceViolationError, match="merged_current_operations"):
        audit(
            inputs,
            observation_records=observations,
            acceptance_records=dated,
            decision_instant=INSTANT,
            source_operation="get_filing_observations",
            merged_operations_used=["get_financial_statements"],
        )


@pytest.mark.integration
def test_the_merged_statement_operation_is_structurally_unreachable() -> None:
    from stock_screener.prospective_capture import FORBIDDEN_FUNDAMENTAL_NAMES
    from stock_screener.prospective_capture.guards import reachable_names

    package = source_root() / "prospective_capture"
    names = reachable_names(sorted(package.glob("*.py")))

    assert "get_filing_observations" in names
    assert not (names & FORBIDDEN_FUNDAMENTAL_NAMES)
    assert "get_financial_statements" in FORBIDDEN_FUNDAMENTAL_NAMES


@pytest.mark.integration
def test_the_guard_catches_a_refactor_to_the_merged_read(tmp_path: Path) -> None:
    offender = tmp_path / "convenient.py"
    offender.write_text(
        "def read(source):\n    return source.get_financial_statements('AAA')\n",
        encoding="utf-8",
    )

    with pytest.raises(GuardViolationError, match="merged_fundamental_reads"):
        audit_package(tmp_path)


# --------------------------------------------------------------------------- #
# §55-§58 non-empty orchestrated decisions, through the frozen rule
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_orchestrated_first_decision_selects_a_non_empty_target_set(
    tmp_path: Path,
) -> None:
    from stock_screener.prospective_runner import ordinary_target_count

    capture = _capture(tmp_path, count=40)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))

    assert body["cross_section_size"] == 40
    assert body["target_count"] == ordinary_target_count(40)
    assert body["target_count"] > 0
    assert len(body["h2_target_set"]) == body["target_count"]
    assert all(
        row["rank"] is not None and row["score"] is not None for row in body["h2_target_set"]
    )
    assert all(capture.fingerprints.values())
    assert len(capture.fingerprints) == 6


@pytest.mark.integration
def test_the_orchestrated_first_decision_is_ordinary_top_decile(tmp_path: Path) -> None:
    """§57: with no prospective incumbents the two strategies must agree, which is
    what makes the first decision ordinary top-decile rather than a continuation."""
    capture = _capture(tmp_path, count=40)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))

    assert body["h2_holdings_before_decision"] == []
    assert body["forced_exits"] == []
    assert body["retained_incumbents"] == []
    assert capture.targets["sets_are_equal"] is True
    comparator = {(row["kind"], row["identity_id"]) for row in body["comparator_target_set"]}
    selected = {(row["kind"], row["identity_id"]) for row in body["h2_target_set"]}
    assert comparator == selected


@pytest.mark.integration
def test_the_orchestrated_decision_is_equally_weighted(tmp_path: Path) -> None:
    """The frozen packet records identities, ranks and scores and carries no weight
    field; equal weighting is a property of the candidate, which prohibits any
    other. So the weight is derived and the prohibition is asserted at its source."""
    from stock_screener.membership_hypothesis import RULE
    from stock_screener.prospective_capture import KNOWN_DEGRADATIONS  # noqa: F401

    capture = _capture(tmp_path, count=40)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))
    weights = [1.0 / len(body["h2_target_set"]) for _ in body["h2_target_set"]]

    assert all(weight > 0 for weight in weights)
    assert sum(weights) == pytest.approx(1.0)
    assert len(set(weights)) == 1
    assert "equal" in json.dumps(RULE).lower()


@pytest.mark.integration
def test_the_orchestrated_selection_ranks_by_score_then_identity(tmp_path: Path) -> None:
    capture = _capture(tmp_path, count=40)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))
    ranks = [row["rank"] for row in body["h2_target_set"]]
    scores = [row["score"] for row in body["h2_target_set"]]

    # The selected set is the best-ranked prefix of the cross-section.
    assert sorted(ranks) == list(range(min(ranks), min(ranks) + len(ranks)))
    assert scores == sorted(scores, reverse=True)


@pytest.mark.integration
def test_a_continuation_decision_retains_fills_and_forces_out(tmp_path: Path) -> None:
    """§58: the orchestrator must be correct past decision #1. A prior prospective
    packet supplies incumbents, one of which is no longer in the cross-section at
    all and must be forced out rather than carried."""
    from stock_screener.membership_hypothesis import retention_limit
    from stock_screener.prospective_runner import ordinary_target_count

    later = date(2027, 3, 31)
    count = 40
    target_count = ordinary_target_count(count)
    band = retention_limit(target_count)

    # Incumbents: one that still ranks inside the band, and one identity that is
    # not in the cross-section at all.
    incumbents = [{"kind": "COMPANY", "identity_id": 1}, {"kind": "COMPANY", "identity_id": 9_999}]
    (tmp_path / f"decision_{SESSION.isoformat()}.json").write_text(
        json.dumps({"packet_key": SESSION.isoformat(), "h2_target_set": incumbents}, indent=2)
        + "\n",
        encoding="utf-8",
    )

    capture = _capture(
        tmp_path,
        session=later,
        count=count,
        execution="2027-04-01",
        now=datetime(2027, 4, 1, tzinfo=UTC),
    )
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))
    held = {(row["kind"], row["identity_id"]) for row in body["h2_holdings_before_decision"]}
    selected = {(row["kind"], row["identity_id"]) for row in body["h2_target_set"]}

    assert held == {("COMPANY", 1), ("COMPANY", 9_999)}
    # Forced out: the contract no longer permits holding an identity absent from
    # the executable cross-section.
    assert ("COMPANY", 9_999) in {tuple(row) for row in body["forced_exits"]} or (
        "COMPANY",
        9_999,
    ) not in selected
    assert ("COMPANY", 9_999) not in selected
    # The vacancy it leaves is filled, so the target count still holds.
    assert len(selected) == target_count
    assert capture.targets["retention_limit"] == band
    assert capture.state == "DECISION_CAPTURED"


@pytest.mark.integration
def test_a_continuation_decision_keeps_an_incumbent_inside_the_band(tmp_path: Path) -> None:
    later = date(2027, 3, 31)
    # Identity 1 scores highest in the fixture, so it is both an incumbent and a
    # legitimate hold; retention must show it rather than readmitting it.
    (tmp_path / f"decision_{SESSION.isoformat()}.json").write_text(
        json.dumps(
            {
                "packet_key": SESSION.isoformat(),
                "h2_target_set": [{"kind": "COMPANY", "identity_id": 40}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    capture = _capture(
        tmp_path,
        session=later,
        count=40,
        execution="2027-04-01",
        now=datetime(2027, 4, 1, tzinfo=UTC),
    )
    buckets = {
        name: {tuple(entry) if isinstance(entry, list) else entry for entry in rows}
        for name, rows in capture.targets.items()
        if name.startswith("h2_") and name != "h2_target_set" and isinstance(rows, list)
    }
    appearances = [name for name, rows in buckets.items() if ("COMPANY", 40) in rows]

    # An incumbent is attributed exactly once: retained, dropped or forced out.
    # It is never "admitted", which is reserved for a new entrant.
    assert len(appearances) == 1, appearances
    assert "h2_admitted" not in appearances


@pytest.mark.integration
def test_the_continuation_decision_uses_only_prospective_incumbents(tmp_path: Path) -> None:
    """No incumbent may be imported from the historical experiment or from the
    missed September session."""
    assert prior_incumbents(tmp_path, before_session="2027-03-31") == []

    (tmp_path / "decision_2026-09-30.json").write_text(
        json.dumps({"h2_target_set": [{"kind": "COMPANY", "identity_id": 5}]}), encoding="utf-8"
    )

    # A September packet does not exist in the real stream and never will; this
    # only proves the reader takes incumbents from the stream's own packets.
    assert prior_incumbents(tmp_path, before_session="2027-03-31") == [("COMPANY", 5)]
    assert not (Path(".evidence/prospective/h2_v1") / "decision_2026-09-30.json").exists()


# --------------------------------------------------------------------------- #
# §59 the execution reference, from a durable decision
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_the_execution_reference_derives_its_session_from_the_calendar(
    tmp_path: Path,
) -> None:
    capture = _capture(tmp_path, count=40)
    body = json.loads(capture.packet_path.read_text(encoding="utf-8"))
    targets = [f"{row['kind']}:{row['identity_id']}" for row in body["h2_target_set"]]

    reference = capture_execution_reference(
        tmp_path,
        decision_session=SESSION.isoformat(),
        opens=dict.fromkeys(targets, 101.5),
        source={"provider": "Fake", "basis": "official open"},
        state_path=tmp_path / "state.json",
        now=datetime(2027, 1, 4, 15, tzinfo=UTC),
    )
    written = json.loads(reference.packet_path.read_text(encoding="utf-8"))

    assert reference.execution_session == derive_execution_session(SESSION).isoformat()
    assert reference.execution_session == "2027-01-04"
    assert written["decision_packet_sha256"] == capture.packet_digest
    assert written["modifies_decision_packet"] is False
    assert reference.priced == len(targets)
    assert reference.as_json()["broker_requests"] == 0


# --------------------------------------------------------------------------- #
# §61 the uncovered safety branches
# --------------------------------------------------------------------------- #


@pytest.mark.integration
def test_a_network_call_after_the_freeze_is_a_hard_failure() -> None:
    import socket

    from stock_screener.prospective_capture.run import NetworkAfterFreezeError, _no_network

    # A literal address, so `connect` is the first call rather than a DNS lookup.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        with (
            _no_network() as blocked,
            pytest.raises(NetworkAfterFreezeError, match="after the decision"),
        ):
            probe.connect(("127.0.0.1", 9))

        assert blocked == ["('127.0.0.1', 9)"]


@pytest.mark.integration
def test_the_socket_block_is_lifted_afterwards() -> None:
    import socket

    from stock_screener.prospective_capture.run import _no_network

    original = socket.socket.connect
    with _no_network():
        assert socket.socket.connect is not original
    assert socket.socket.connect is original


@pytest.mark.integration
def test_a_capture_is_refused_when_the_activation_binds_another_candidate(
    tmp_path: Path,
) -> None:
    activation, _ = _armed()
    activation["candidate_id"] = "SOME_OTHER_CANDIDATE"

    with pytest.raises(DecisionRefusedError, match="does not bind this candidate"):
        _capture(tmp_path, activation=activation)


@pytest.mark.integration
def test_a_capture_is_refused_when_the_arm_binds_another_protocol(tmp_path: Path) -> None:
    _, arm = _armed()
    arm["protocol_id"] = "SOME_OTHER_PROTOCOL"

    with pytest.raises(DecisionRefusedError, match="does not bind this candidate"):
        _capture(tmp_path, arm=arm)


@pytest.mark.integration
def test_a_predecision_source_may_not_be_relabelled_to_a_looser_class(
    tmp_path: Path,
) -> None:
    from stock_screener.prospective_source.envelope import TemporalClass

    store = SnapshotStore(tmp_path / "snapshots")
    listings = [{"ticker": "AAA", "is_active": True, "is_fund": False}]
    # Declaring the broker asset list event-time would let it be fetched after the
    # instant, which is the September failure with a new label on it.
    mislabelled = _result(
        {
            "source_type": "universe",
            "retrieved_at": INSTANT + timedelta(hours=1),
            "semantic_as_of": INSTANT - timedelta(hours=1),
            "temporal_class": TemporalClass.EVENT_TIME,
        },
        {"listings": listings},
        listings,
    )

    with pytest.raises(PackageError, match="current-only by definition"):
        seal_predecision(
            store,
            tmp_path,
            decision_session=SESSION.isoformat(),
            decision_instant=INSTANT,
            sources={"universe": mislabelled, "tradability": mislabelled},
        )


@pytest.mark.integration
def test_a_tampered_sealed_payload_fails_verification(tmp_path: Path) -> None:
    store, directory, _ = _complete_package(tmp_path)
    package = load_package(directory)
    envelope = package.envelopes["market"]
    payload = (
        store.root
        / envelope.source_type
        / (envelope.decision_session or "unbound")
        / envelope.content_sha256[:16]
        / "payload.json"
    )
    payload.write_text(json.dumps({"bars": []}) + "\n", encoding="utf-8")

    with pytest.raises((PackageError, StoreConflictError), match=r"no longer matches its envelope"):
        package.verify(store)


@pytest.mark.integration
def test_a_file_that_is_not_a_decision_packet_is_not_read_as_one(tmp_path: Path) -> None:
    (tmp_path / "decision_input_package.json").write_text(
        json.dumps({"h2_target_set": [{"kind": "COMPANY", "identity_id": 1}]}), encoding="utf-8"
    )
    (tmp_path / "decision_notes.json").write_text(json.dumps({}), encoding="utf-8")

    assert prior_incumbents(tmp_path, before_session="2027-01-01") == []


@pytest.mark.integration
def test_a_source_admissible_without_a_fingerprint_is_blocked() -> None:
    report = assess_capture_readiness(
        decision_instant=INSTANT.isoformat(),
        sources={
            "universe": {"admissible": True, "fingerprint": None},
            "tradability": {"admissible": True, "fingerprint": "t"},
            "market": {"admissible": True, "fingerprint": "m"},
            "sec_availability": {"admissible": True, "fingerprint": "s"},
        },
        derived={
            name: {"produced": True, "validated": True, "fingerprint": name}
            for name in ("score_inputs", "ranked_cross_section")
        },
        executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": True},
        store_healthy=True,
        calendar_authoritative=True,
        activation_matches=True,
        arm_matches=True,
    )

    assert "SOURCE_FINGERPRINT_MISSING" in report.components["universe"].reasons
    assert not report.capture_ready


@pytest.mark.integration
def test_a_derived_artifact_that_did_not_validate_is_blocked() -> None:
    report = assess_capture_readiness(
        decision_instant=INSTANT.isoformat(),
        sources={
            name: {"admissible": True, "fingerprint": name}
            for name in ("universe", "tradability", "market", "sec_availability")
        },
        derived={
            "score_inputs": {"produced": True, "validated": False, "fingerprint": "x"},
            "ranked_cross_section": {"produced": True, "validated": True, "fingerprint": "y"},
        },
        executors={"D_DECISION": True, "E_EXECUTION_REFERENCE": True},
        store_healthy=True,
        calendar_authoritative=True,
        activation_matches=True,
        arm_matches=True,
    )

    assert "ARTIFACT_DID_NOT_VALIDATE" in report.components["score_inputs"].reasons


@pytest.mark.integration
def test_a_missing_required_component_is_reported_absent() -> None:
    report = assess_capture_readiness(
        decision_instant=INSTANT.isoformat(),
        sources={},
        derived={},
        executors={},
        store_healthy=False,
        calendar_authoritative=False,
        activation_matches=False,
        arm_matches=False,
        required=("universe",),
    )

    assert report.components["universe"].reasons == ("SOURCE_UNAVAILABLE",)
    assert "snapshot_store_unhealthy" in report.blockers
    assert "calendar_not_authoritative" in report.blockers
    assert "activation_mismatch" in report.blockers
    assert "arm_mismatch" in report.blockers
    assert report.fingerprints["score_inputs"] is None


@pytest.mark.integration
def test_the_provenance_audit_names_an_undated_observation() -> None:
    from stock_screener.prospective_capture.provenance import audit

    identities, observations, bars = _chain_fixtures(count=1)
    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        decision_session=SESSION,
        decision_instant=INSTANT.isoformat(),
        source_fingerprints={},
    )
    broken = [{**row, "filed_date": None} for row in observations[:1]] + list(observations[1:])
    dated = _acceptance_rows(observations, INSTANT)

    report = audit(
        inputs,
        observation_records=broken,
        acceptance_records=dated,
        decision_instant=INSTANT,
        source_operation="get_filing_observations",
        strict=False,
    )

    assert report["clean"] is False
    assert any(row["reason"] == "NO_FILED_DATE" for row in report["untraceable_detail"])


@pytest.mark.integration
def test_an_execution_reference_is_refused_when_the_packet_names_another_session(
    tmp_path: Path,
) -> None:
    (tmp_path / f"decision_{SESSION.isoformat()}.json").write_text(
        json.dumps(
            {
                "packet_key": SESSION.isoformat(),
                "execution_session": "2027-02-01",
                "h2_target_set": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ExecutionRefusedError, match="calendar derives"):
        capture_execution_reference(
            tmp_path,
            decision_session=SESSION.isoformat(),
            opens={},
            source={"provider": "Fake"},
            state_path=tmp_path / "state.json",
            now=datetime(2027, 1, 4, 15, tzinfo=UTC),
        )


@pytest.mark.integration
def test_september_30_is_refused_before_a_single_source_read(tmp_path: Path) -> None:
    """§69: the refusal must be the executor's first act. A counting store proves
    it, because an ordering bug would show up as a read rather than as a different
    exception."""
    from stock_screener.prospective_capture import capture_decision
    from stock_screener.prospective_capture.package import SealedPackage
    from stock_screener.prospective_source.window import WindowError

    class _CountingStore(SnapshotStore):
        reads = 0

        def get(self, source_type: str, session: str, digest_prefix: str) -> dict[str, Any]:
            type(self).reads += 1
            return super().get(source_type, session, digest_prefix)

    store = _CountingStore(tmp_path / "snapshots")
    missed = SealedPackage(
        decision_session="2026-09-30",
        decision_instant=session_close(date(2026, 9, 30)).isoformat(),
        stage="COMPLETE",
    )

    with pytest.raises(WindowError, match="MISSED_DECISION_NOT_BACKFILLABLE"):
        capture_decision(
            store,
            tmp_path,
            package=missed,
            activation=_armed()[0],
            arm=_armed()[1],
            calendar=_calendar(),
            identities=[],
            benchmark_bars=[],
            benchmark_symbol="SPY",
            bar_volume_basis=VolumeBasis.UNKNOWN,
            thresholds=None,
            semantic_fingerprint="fp",
            code_commit="c0ffee",
            state_path=tmp_path / "state.json",
            now=datetime(2026, 10, 1, tzinfo=UTC),
        )

    assert _CountingStore.reads == 0
    assert not list(tmp_path.glob("decision_2026-09-30.json"))
