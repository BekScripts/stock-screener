"""The daily capture: immutable, atomic, idempotent, and never backfilled.

These run against a real database because every property under test is a
property of the transaction and the schema. What they pin down is the promise
the whole phase rests on — that a row recording what was believed on a date
cannot later be quietly replaced by what is believed now.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    OBSERVATION_BASELINE,
    OBSERVATION_OBSERVED,
    RUN_COMPLETE,
    RUN_FAILED,
    Company,
    CompanyRepository,
    ImmutableSnapshotError,
    PitCaptureRunRepository,
    PitFeatureSnapshotRepository,
    PitScoreSnapshotRepository,
    UniverseEventRepository,
)
from domain import CURRENT_SCORE_VERSION
from stock_screener.config import Settings
from stock_screener.pit import FEATURE_VERSION, capture_day, record_capture_failure

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Seed

SNAPSHOT = date(2026, 6, 30)


def _stored(session: Session, ticker: str) -> Company:
    """Return a stored company, asserting it is there."""
    company = CompanyRepository(session).get_by_ticker(ticker)
    assert company is not None
    return company


@pytest.fixture
def settings() -> Settings:
    """Settings with the benchmark the seed fixture writes."""
    return Settings(benchmark_symbol="SPY")


@pytest.mark.integration
def test_a_capture_records_the_current_score(
    session: Session, settings: Settings, scored_company: str
) -> None:
    result = capture_day(session, settings, snapshot_date=SNAPSHOT)

    assert result.already_captured is False
    assert result.scores == 1
    stored = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )
    assert [row.ticker for row in stored] == [scored_company]
    assert stored[0].final_score == 80.0


@pytest.mark.integration
def test_the_score_version_travels_with_the_snapshot(
    session: Session, settings: Settings, scored_company: str
) -> None:
    capture_day(session, settings, snapshot_date=SNAPSHOT)

    stored = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )
    assert stored[0].score_version == CURRENT_SCORE_VERSION


@pytest.mark.integration
def test_every_component_is_preserved(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """A snapshot must reproduce the decision, not just its headline number."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)

    row = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )[0]
    assert row.growth_score is not None
    assert row.quality_score is not None
    assert row.valuation_score is not None
    assert row.momentum_score is not None
    assert row.scoring_status == "SCORED"
    assert row.coverage == "MARKET"


@pytest.mark.integration
def test_freshness_and_rank_eligibility_are_preserved(
    session: Session, settings: Settings, scored_company: str
) -> None:
    capture_day(session, settings, snapshot_date=SNAPSHOT)

    row = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )[0]
    assert row.freshness == "CURRENT"
    assert row.rank_eligible is True


@pytest.mark.integration
def test_market_features_are_captured_with_their_version(
    session: Session, settings: Settings, scored_company: str
) -> None:
    result = capture_day(session, settings, snapshot_date=SNAPSHOT)

    assert result.features == 1
    row = PitFeatureSnapshotRepository(session).for_date(SNAPSHOT, feature_version=FEATURE_VERSION)[
        0
    ]
    assert row.feature_version == FEATURE_VERSION
    assert row.bars_available > 0
    assert row.last_close is not None


@pytest.mark.integration
def test_the_snapshot_date_and_the_capture_time_are_separate(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """A capture that runs after midnight still describes the day it was for."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)

    row = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )[0]
    assert row.snapshot_date == SNAPSHOT
    # The capture ran later than the day it describes, which is the normal case
    # and exactly why the two are separate columns. (SQLite does not persist
    # tzinfo; the value written is aware UTC.)
    assert row.captured_at.date() >= SNAPSHOT
    assert row.captured_at.date() != row.snapshot_date


@pytest.mark.integration
def test_the_feature_watermark_records_the_newest_bar_used(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """`feature_as_of` is the data watermark, not the day asked for."""
    capture_day(session, settings, snapshot_date=date(2026, 7, 31))

    row = PitFeatureSnapshotRepository(session).for_date(
        date(2026, 7, 31), feature_version=FEATURE_VERSION
    )[0]
    assert row.snapshot_date == date(2026, 7, 31)
    assert row.feature_as_of <= date(2026, 7, 31)


@pytest.mark.integration
def test_rerunning_the_same_day_writes_nothing(
    session: Session, settings: Settings, scored_company: str
) -> None:
    first = capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    second = capture_day(session, settings, snapshot_date=SNAPSHOT)

    assert first.already_captured is False
    assert second.already_captured is True
    assert second.scores == 0
    assert PitScoreSnapshotRepository(session).count() == first.scores


@pytest.mark.integration
def test_a_completed_day_cannot_be_changed_by_a_later_score(
    session: Session,
    settings: Settings,
    scored_company: str,
    seed: type[Seed],
) -> None:
    """The property the phase exists for: hindsight does not rewrite the record."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    captured = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )[0]
    original = captured.final_score

    # The operational table is upserted with a better score for the same day.
    company_id = _stored(session, scored_company).id
    seed.score(session, company_id, scored_company, final=99.0, score_date=SNAPSHOT)
    session.flush()

    again = capture_day(session, settings, snapshot_date=SNAPSHOT)
    assert again.already_captured is True
    still = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )[0]
    assert still.final_score == original


@pytest.mark.integration
def test_a_partial_run_leaves_no_day_behind(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """A run that never completed is not a captured day, and blocks nothing."""
    runs = PitCaptureRunRepository(session)
    runs.start(SNAPSHOT, score_version=CURRENT_SCORE_VERSION, feature_version=FEATURE_VERSION)
    session.flush()

    assert runs.completed_run(SNAPSHOT) is None
    result = capture_day(session, settings, snapshot_date=SNAPSHOT)
    assert result.already_captured is False
    assert result.scores == 1


@pytest.mark.integration
def test_writing_a_day_twice_in_one_transaction_is_refused(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """Belt and braces: even without the run guard, the rows refuse to move."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    run = PitCaptureRunRepository(session).completed_run(SNAPSHOT)
    assert run is not None
    run.status = RUN_FAILED
    session.flush()

    with pytest.raises(ImmutableSnapshotError):
        capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.rollback()


@pytest.mark.integration
def test_the_run_records_what_it_wrote(
    session: Session, settings: Settings, scored_company: str
) -> None:
    result = capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()

    run = PitCaptureRunRepository(session).completed_run(SNAPSHOT)
    assert run is not None
    assert run.status == RUN_COMPLETE
    assert run.scores_captured == result.scores
    assert run.features_captured == result.features
    assert run.score_version == CURRENT_SCORE_VERSION
    assert run.feature_version == FEATURE_VERSION


@pytest.mark.integration
def test_a_failure_is_recorded_without_a_snapshot(session: Session) -> None:
    record_capture_failure(session, SNAPSHOT, "ProviderError: boom")
    session.flush()

    runs = PitCaptureRunRepository(session)
    assert runs.completed_run(SNAPSHOT) is None
    assert runs.count() == 1
    assert PitScoreSnapshotRepository(session).count() == 0


@pytest.mark.integration
def test_the_first_capture_records_a_baseline_not_a_dated_event(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """Nothing is known about when membership began, and nothing is invented."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()

    company_id = _stored(session, scored_company).id
    history = UniverseEventRepository(session).history(company_id)
    assert len(history) == 1
    assert history[0].observation_kind == OBSERVATION_BASELINE
    assert history[0].effective_date == SNAPSHOT
    assert history[0].is_active is True


@pytest.mark.integration
def test_an_unchanged_universe_writes_no_second_event(
    session: Session, settings: Settings, scored_company: str
) -> None:
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    second = capture_day(session, settings, snapshot_date=date(2026, 7, 1))
    session.flush()

    assert second.universe_events == 0
    company_id = _stored(session, scored_company).id
    assert len(UniverseEventRepository(session).history(company_id)) == 1


@pytest.mark.integration
def test_a_delisting_is_recorded_as_an_observed_transition(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """Only a change the system watched happen gets a dated OBSERVED row."""
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()

    company = _stored(session, scored_company)
    company.is_active = False
    session.flush()

    later = date(2026, 7, 1)
    result = capture_day(session, settings, snapshot_date=later)
    session.flush()

    assert result.universe_events == 1
    history = UniverseEventRepository(session).history(company.id)
    assert [row.observation_kind for row in history] == [
        OBSERVATION_BASELINE,
        OBSERVATION_OBSERVED,
    ]
    assert history[1].event_type == "DELISTED"
    assert history[1].effective_date == later


@pytest.mark.integration
def test_a_reactivation_is_recorded_too(
    session: Session, settings: Settings, scored_company: str
) -> None:
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()
    company = _stored(session, scored_company)
    company.is_active = False
    session.flush()
    capture_day(session, settings, snapshot_date=date(2026, 7, 1))
    session.flush()

    company.is_active = True
    session.flush()
    capture_day(session, settings, snapshot_date=date(2026, 7, 2))
    session.flush()

    history = UniverseEventRepository(session).history(company.id)
    assert [row.event_type for row in history] == ["ACTIVE", "DELISTED", "REACTIVATED"]


@pytest.mark.integration
def test_nothing_is_captured_for_a_date_before_any_scoring_run(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """A capture must never import a score computed after the day it describes."""
    result = capture_day(session, settings, snapshot_date=date(2026, 1, 1))

    assert result.scores == 0
    assert PitScoreSnapshotRepository(session).count() == 0


@pytest.mark.integration
def test_no_scoring_formula_runs_during_a_capture(
    session: Session, settings: Settings, scored_company: str
) -> None:
    """The captured number is the stored one, copied verbatim."""
    from data_access import ScoreSnapshotRepository

    stored = ScoreSnapshotRepository(session).list_for_date(
        score_date=SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )
    capture_day(session, settings, snapshot_date=SNAPSHOT)
    session.flush()

    captured = PitScoreSnapshotRepository(session).for_date(
        SNAPSHOT, score_version=CURRENT_SCORE_VERSION
    )
    assert [row.final_score for row in captured] == [row.final_score for row in stored]
    assert captured[0].source_score_date == SNAPSHOT
