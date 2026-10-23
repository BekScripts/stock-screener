"""The point-in-time tables, and the three properties that make them worth having.

`integration`, not `unit`: what these verify is that these tables append where
the operational ones upsert, that a stored row cannot be replaced, and that
selection reads `filed_date` rather than `period_end`. All three are properties
of the schema and the SQL, not of any Python object.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import pytest

from data_access import (
    OBSERVATION_BASELINE,
    OBSERVATION_OBSERVED,
    RUN_COMPLETE,
    RUN_FAILED,
    CompanyRepository,
    CorporateActionRepository,
    FundamentalObservationRepository,
    ImmutableSnapshotError,
    PitCaptureRunRepository,
    PitFeatureSnapshotRepository,
    PitScoreSnapshotRepository,
    UniverseEventRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from domain import CompanyProfile

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session

SNAPSHOT = date(2026, 8, 26)


@pytest.fixture
def session() -> Iterator[Session]:
    """Yield a session against a fresh in-memory database."""
    engine = create_engine_from_url("sqlite://")
    create_all(engine)
    factory = build_session_factory(engine)
    with session_scope(factory) as open_session:
        yield open_session
    engine.dispose()


def _company(session: Session, ticker: str = "ACME") -> int:
    """Store one company and return its primary key."""
    companies = CompanyRepository(session)
    companies.upsert_profile(CompanyProfile(ticker=ticker, name=f"{ticker} Inc"))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _run(session: Session) -> int:
    """Open a capture run and return its id, as the capture pass does."""
    run = PitCaptureRunRepository(session).start(
        SNAPSHOT, score_version="COMPOUNDER_V1_2", feature_version="MARKET_FEATURES_V1"
    )
    return run.id


def _score_row(company_id: int, run_id: int, *, ticker: str = "ACME", final: float = 80.0) -> dict:
    """A minimal captured score."""
    return {
        "run_id": run_id,
        "company_id": company_id,
        "ticker": ticker,
        "snapshot_date": SNAPSHOT,
        "captured_at": datetime(2026, 8, 26, 23, 55, tzinfo=UTC),
        "source_score_date": SNAPSHOT,
        "score_version": "COMPOUNDER_V1_2",
        "scoring_status": "SCORED",
        "final_score": final,
        "coverage": "MARKET",
        "rank_eligible": True,
    }


def _feature_row(company_id: int, run_id: int, *, ticker: str = "ACME", atr: float = 3.5) -> dict:
    """A minimal captured feature set."""
    return {
        "run_id": run_id,
        "company_id": company_id,
        "ticker": ticker,
        "snapshot_date": SNAPSHOT,
        "captured_at": datetime(2026, 8, 26, 23, 55, tzinfo=UTC),
        "feature_version": "MARKET_FEATURES_V1",
        "feature_as_of": SNAPSHOT,
        "bars_available": 282,
        "atr_pct_14": atr,
    }


def _observation(
    *,
    fingerprint: str,
    value: float,
    filed: date | None,
    period_end: date = date(2025, 12, 31),
    concept: str = "Revenues",
) -> dict:
    """One reported figure as one filing stated it."""
    return {
        "observation_fingerprint": fingerprint,
        "taxonomy": "us-gaap",
        "concept": concept,
        "value": value,
        "unit": "USD",
        "period_start": date(2025, 10, 1),
        "period_end": period_end,
        "filed_date": filed,
        "source": "sec-edgar",
    }


# --------------------------------------------------------------------------
# Immutability
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_stored_score_snapshot_cannot_be_replaced(session: Session) -> None:
    """The whole value of the table is that a row records what was believed."""
    company_id = _company(session)
    run_id = _run(session)
    repo = PitScoreSnapshotRepository(session)
    repo.add_all([_score_row(company_id, run_id, final=80.0)])
    session.flush()

    with pytest.raises(ImmutableSnapshotError):
        repo.add_all([_score_row(company_id, run_id, final=91.0)])

    session.rollback()


@pytest.mark.integration
def test_the_original_value_survives_a_refused_rewrite(session: Session) -> None:
    company_id = _company(session)
    run_id = _run(session)
    repo = PitScoreSnapshotRepository(session)
    repo.add_all([_score_row(company_id, run_id, final=80.0)])
    session.flush()

    with pytest.raises(ImmutableSnapshotError):
        repo.add_all([_score_row(company_id, run_id, final=91.0)])

    stored = repo.for_date(SNAPSHOT, score_version="COMPOUNDER_V1_2")
    assert [row.final_score for row in stored] == [80.0]


@pytest.mark.integration
def test_a_stored_feature_snapshot_cannot_be_replaced(session: Session) -> None:
    company_id = _company(session)
    run_id = _run(session)
    repo = PitFeatureSnapshotRepository(session)
    repo.add_all([_feature_row(company_id, run_id, atr=3.5)])
    session.flush()

    with pytest.raises(ImmutableSnapshotError):
        repo.add_all([_feature_row(company_id, run_id, atr=9.9)])

    session.rollback()


@pytest.mark.integration
def test_a_different_day_is_a_new_row_not_a_conflict(session: Session) -> None:
    """Immutability is per day. Tomorrow is a fresh observation."""
    company_id = _company(session)
    run_id = _run(session)
    repo = PitScoreSnapshotRepository(session)
    repo.add_all([_score_row(company_id, run_id, final=80.0)])
    tomorrow = _score_row(company_id, run_id, final=81.0)
    tomorrow["snapshot_date"] = date(2026, 8, 27)
    repo.add_all([tomorrow])
    session.flush()

    assert repo.count() == 2
    history = repo.history(company_id, score_version="COMPOUNDER_V1_2")
    assert [row.final_score for row in history] == [80.0, 81.0]


@pytest.mark.integration
def test_the_repositories_expose_no_update_path() -> None:
    """Immutability is enforced by there being no way to write an update."""
    for repository in (PitScoreSnapshotRepository, PitFeatureSnapshotRepository):
        methods = {name for name in dir(repository) if not name.startswith("_")}
        assert not methods & {"update", "upsert", "replace", "delete", "save"}


# --------------------------------------------------------------------------
# Capture runs
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_only_a_completed_run_counts_as_a_captured_day(session: Session) -> None:
    runs = PitCaptureRunRepository(session)
    runs.start(SNAPSHOT, score_version="COMPOUNDER_V1_2", feature_version="MARKET_FEATURES_V1")
    session.flush()

    assert runs.completed_run(SNAPSHOT) is None


@pytest.mark.integration
def test_a_completed_run_is_found(session: Session) -> None:
    runs = PitCaptureRunRepository(session)
    run = runs.start(
        SNAPSHOT, score_version="COMPOUNDER_V1_2", feature_version="MARKET_FEATURES_V1"
    )
    runs.complete(run, scores=3, features=4, universe_events=5)
    session.flush()

    found = runs.completed_run(SNAPSHOT)
    assert found is not None
    assert found.status == RUN_COMPLETE
    assert (found.scores_captured, found.features_captured) == (3, 4)
    assert found.completed_at is not None


@pytest.mark.integration
def test_a_failed_run_is_recorded_without_blocking_a_retry(session: Session) -> None:
    """Attempts are history. A failure must not make the day uncapturable."""
    runs = PitCaptureRunRepository(session)
    runs.fail(
        SNAPSHOT,
        detail="ProviderError: boom",
        score_version="COMPOUNDER_V1_2",
        feature_version="MARKET_FEATURES_V1",
    )
    session.flush()

    assert runs.completed_run(SNAPSHOT) is None
    run = runs.start(
        SNAPSHOT, score_version="COMPOUNDER_V1_2", feature_version="MARKET_FEATURES_V1"
    )
    runs.complete(run, scores=1, features=1, universe_events=1)
    session.flush()

    assert runs.count() == 2
    found = runs.completed_run(SNAPSHOT)
    assert found is not None
    assert found.status != RUN_FAILED


# --------------------------------------------------------------------------
# Fundamental observations: append-only and point-in-time
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_two_filings_for_one_period_both_survive(session: Session) -> None:
    """The requirement, stated exactly: a revision must not destroy the original."""
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)

    repo.add_all(
        company_id,
        [_observation(fingerprint="original", value=100_000_000.0, filed=date(2026, 2, 15))],
    )
    repo.add_all(
        company_id,
        [_observation(fingerprint="restated", value=104_000_000.0, filed=date(2026, 4, 30))],
    )
    session.flush()

    revisions = repo.revisions(company_id, date(2025, 12, 31), "Revenues")
    assert [row.value for row in revisions] == [100_000_000.0, 104_000_000.0]
    assert [row.filed_date for row in revisions] == [date(2026, 2, 15), date(2026, 4, 30)]


@pytest.mark.integration
def test_re_reading_the_same_filing_appends_nothing(session: Session) -> None:
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)
    row = _observation(fingerprint="original", value=100_000_000.0, filed=date(2026, 2, 15))

    assert repo.add_all(company_id, [row]) == 1
    session.flush()
    assert repo.add_all(company_id, [row]) == 0
    session.flush()

    assert repo.count() == 1


@pytest.mark.integration
def test_point_in_time_selection_hides_a_filing_that_had_not_happened(
    session: Session,
) -> None:
    """A decision in March sees the original figure, not April's restatement."""
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)
    repo.add_all(
        company_id,
        [
            _observation(fingerprint="original", value=100_000_000.0, filed=date(2026, 2, 15)),
            _observation(fingerprint="restated", value=104_000_000.0, filed=date(2026, 4, 30)),
        ],
    )
    session.flush()

    march = repo.as_of(company_id, date(2026, 3, 1))
    may = repo.as_of(company_id, date(2026, 5, 1))

    assert [row.value for row in march] == [100_000_000.0]
    assert [row.value for row in may] == [104_000_000.0]


@pytest.mark.integration
def test_period_end_is_never_treated_as_the_availability_date(session: Session) -> None:
    """A December quarter filed in February was not knowable in January."""
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)
    repo.add_all(
        company_id,
        [_observation(fingerprint="original", value=100_000_000.0, filed=date(2026, 2, 15))],
    )
    session.flush()

    # After the period ended, before it was filed.
    assert repo.as_of(company_id, date(2026, 1, 15)) == []
    assert len(repo.as_of(company_id, date(2026, 2, 15))) == 1


@pytest.mark.integration
def test_an_observation_without_a_filing_date_is_excluded_not_guessed(
    session: Session,
) -> None:
    """Unknown availability must not fall back to the period end."""
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)
    repo.add_all(
        company_id,
        [_observation(fingerprint="undated", value=90_000_000.0, filed=None)],
    )
    session.flush()

    assert repo.count() == 1
    assert repo.as_of(company_id, date(2030, 1, 1)) == []


@pytest.mark.integration
def test_selection_returns_one_row_per_concept_and_period(session: Session) -> None:
    company_id = _company(session)
    repo = FundamentalObservationRepository(session)
    repo.add_all(
        company_id,
        [
            _observation(fingerprint="rev-q4", value=100.0, filed=date(2026, 2, 15)),
            _observation(
                fingerprint="rev-q3",
                value=90.0,
                filed=date(2025, 11, 1),
                period_end=date(2025, 9, 30),
            ),
            _observation(
                fingerprint="gp-q4", value=40.0, filed=date(2026, 2, 15), concept="GrossProfit"
            ),
        ],
    )
    session.flush()

    rows = repo.as_of(company_id, date(2026, 6, 1))
    assert {(row.concept, row.period_end) for row in rows} == {
        ("Revenues", date(2025, 12, 31)),
        ("Revenues", date(2025, 9, 30)),
        ("GrossProfit", date(2025, 12, 31)),
    }


# --------------------------------------------------------------------------
# Universe events
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_baseline_is_distinguishable_from_an_observed_transition(
    session: Session,
) -> None:
    """The column that stops a first sighting being read as a dated event."""
    company_id = _company(session)
    repo = UniverseEventRepository(session)
    repo.add_all(
        [
            {
                "company_id": company_id,
                "event_fingerprint": "baseline",
                "ticker": "ACME",
                "event_type": "ACTIVE",
                "observation_kind": OBSERVATION_BASELINE,
                "effective_date": SNAPSHOT,
                "is_active": True,
                "source": "pit-capture",
            },
            {
                "company_id": company_id,
                "event_fingerprint": "delisted",
                "ticker": "ACME",
                "event_type": "DELISTED",
                "observation_kind": OBSERVATION_OBSERVED,
                "effective_date": date(2026, 9, 30),
                "is_active": False,
                "source": "pit-capture",
            },
        ]
    )
    session.flush()

    history = repo.history(company_id)
    assert [row.observation_kind for row in history] == [
        OBSERVATION_BASELINE,
        OBSERVATION_OBSERVED,
    ]
    assert [row.is_active for row in history] == [True, False]


@pytest.mark.integration
def test_universe_events_are_append_only(session: Session) -> None:
    company_id = _company(session)
    repo = UniverseEventRepository(session)
    row = {
        "company_id": company_id,
        "event_fingerprint": "baseline",
        "ticker": "ACME",
        "event_type": "ACTIVE",
        "observation_kind": OBSERVATION_BASELINE,
        "effective_date": SNAPSHOT,
        "is_active": True,
        "source": "pit-capture",
    }

    assert repo.add_all([row]) == 1
    session.flush()
    assert repo.add_all([row]) == 0
    session.flush()

    assert repo.count() == 1


# --------------------------------------------------------------------------
# Corporate actions
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_corporate_action_keeps_its_factor_and_ex_date(session: Session) -> None:
    company_id = _company(session)
    repo = CorporateActionRepository(session)
    repo.add_all(
        [
            {
                "company_id": company_id,
                "action_fingerprint": "split-2026-09-01",
                "ticker": "ACME",
                "action_type": "SPLIT",
                "ex_date": date(2026, 9, 1),
                "split_factor": 2.0,
                "source": "test",
                "source_id": "abc-123",
            }
        ]
    )
    session.flush()

    stored = repo.for_company(company_id)
    assert len(stored) == 1
    assert stored[0].split_factor == 2.0
    assert stored[0].ex_date == date(2026, 9, 1)
    assert stored[0].source_id == "abc-123"


@pytest.mark.integration
def test_ingesting_the_same_action_twice_is_idempotent(session: Session) -> None:
    company_id = _company(session)
    repo = CorporateActionRepository(session)
    row = {
        "company_id": company_id,
        "action_fingerprint": "split-2026-09-01",
        "ticker": "ACME",
        "action_type": "SPLIT",
        "ex_date": date(2026, 9, 1),
        "split_factor": 2.0,
        "source": "test",
    }

    assert repo.add_all([row]) == 1
    session.flush()
    assert repo.add_all([row]) == 0
    session.flush()

    assert repo.count() == 1


@pytest.mark.integration
def test_actions_can_be_bounded_to_what_had_already_happened(session: Session) -> None:
    """A price series rebuilt for a past day must not apply a later factor."""
    company_id = _company(session)
    repo = CorporateActionRepository(session)
    repo.add_all(
        [
            {
                "company_id": company_id,
                "action_fingerprint": "early",
                "ticker": "ACME",
                "action_type": "SPLIT",
                "ex_date": date(2026, 3, 1),
                "split_factor": 2.0,
                "source": "test",
            },
            {
                "company_id": company_id,
                "action_fingerprint": "late",
                "ticker": "ACME",
                "action_type": "SPLIT",
                "ex_date": date(2026, 9, 1),
                "split_factor": 3.0,
                "source": "test",
            },
        ]
    )
    session.flush()

    assert len(repo.for_company(company_id)) == 2
    assert [row.split_factor for row in repo.for_company(company_id, until=date(2026, 6, 1))] == [
        2.0
    ]


@pytest.mark.integration
def test_no_corporate_action_source_leaves_an_empty_table_not_a_guess(
    session: Session,
) -> None:
    """Phase 8B wires no source. Absence is recorded as absence."""
    _company(session)

    assert CorporateActionRepository(session).count() == 0
