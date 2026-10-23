"""The first AlphaTrader strategy authority, and what it refuses.

The strategy itself is three rules — monthly, top decile by Compounder score,
equal weight — and almost everything here is about the fourth thing it does,
which is decline. A strategy is the first link in an authority chain that ends
at a real venue, so the interesting assertions are that it cannot leak future
evidence into a decision, cannot size around the risk engine, cannot reach a
broker, and cannot quietly turn a missing input into a selection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from stock_screener.execution import ExecutionCalendar
from stock_screener.portfolio_risk import RiskPolicy, SecurityIdentity
from stock_screener.strategies import (
    STRATEGY_ID,
    STRATEGY_VERSION,
    Code,
    CompounderMonthlyStrategy,
    StrategyDeploymentAuthorization,
    StrategyError,
    StrategyInputBundle,
    StrategyInputRecord,
    StrategyState,
    StrategyStateStore,
    decision_sessions,
    evaluate_session,
    is_decision_session,
    target_count,
)

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy.orm import Session

# A calendar spanning three month ends, including a Friday month end and a
# year boundary, so the timing rules are exercised against real shapes.
SESSIONS: tuple[str, ...] = (
    "2026-10-28", "2026-10-29", "2026-10-30",
    "2026-11-02", "2026-11-03", "2026-11-30",
    "2026-12-01", "2026-12-30", "2026-12-31",
    "2027-01-04", "2027-01-05",
)  # fmt: skip

#: 14:00 New York, comfortably before the 16:00 close of 2026-10-30.
BEFORE_CLOSE = "2026-10-30T18:00:00+00:00"

#: 17:00 New York on the same session. Expressed in a western offset on
#: purpose: it sorts *before* "16:00:00-04:00" as a string while being an hour
#: after it as an instant, which is the mistake this suite exists to catch.
AFTER_CLOSE = "2026-10-30T13:00:00-08:00"

DECISION_SESSION = "2026-10-30"


@pytest.fixture
def calendar() -> ExecutionCalendar:
    """The market calendar. Authoritative for month ends."""
    return ExecutionCalendar(sessions=SESSIONS, name="S1_TEST")


def record(
    identity_id: int,
    score: float | None,
    *,
    status: str = "SCORED",
    freshness: str = "CURRENT",
    calculated_at: str = BEFORE_CLOSE,
    symbol: str | None = None,
) -> StrategyInputRecord:
    """One evidence record."""
    return StrategyInputRecord(
        identity=SecurityIdentity(
            kind="COMPANY", identity_id=identity_id, symbol=symbol or f"T{identity_id}"
        ),
        final_score=score,
        scoring_status=status,
        freshness=freshness,
        score_date=DECISION_SESSION,
        score_version="COMPOUNDER_V1_2",
        calculated_at=calculated_at,
        source_fingerprint=f"fp-{identity_id}",
    )


def bundle(*records: StrategyInputRecord, version: str = "COMPOUNDER_V1_2") -> StrategyInputBundle:
    """A bundle over the given records."""
    return StrategyInputBundle(
        as_of=DECISION_SESSION,
        score_version=version,
        universe_fingerprint="universe-1",
        records=records,
    )


def scored(count: int, *, top: float = 90.0) -> tuple[StrategyInputRecord, ...]:
    """`count` selectable records with strictly descending scores."""
    return tuple(record(i, top - i) for i in range(1, count + 1))


# --------------------------------------------------------------------------
# Identity and the authority boundary
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_strategy_has_a_frozen_identity_and_fingerprint() -> None:
    strategy = CompounderMonthlyStrategy()

    assert strategy.strategy_id == STRATEGY_ID
    assert strategy.strategy_version == STRATEGY_VERSION
    assert len(strategy.fingerprint()) == 64


@pytest.mark.integration
def test_changing_a_rule_changes_the_fingerprint(monkeypatch: pytest.MonkeyPatch) -> None:
    # The grant binds the fingerprint, so this is what makes an edited strategy
    # stop being authorised instead of inheriting the old approval.
    before = CompounderMonthlyStrategy().fingerprint()
    monkeypatch.setattr("stock_screener.strategies.compounder_monthly.DECILE_FRACTION", 5)

    assert CompounderMonthlyStrategy().fingerprint() != before


@pytest.mark.integration
def test_the_policy_declares_it_sizes_nothing() -> None:
    policy = CompounderMonthlyStrategy().policy()

    assert policy["sizing_authority"].startswith("PORTFOLIO_RISK_ENGINE_V1_2")
    assert policy["reads_portfolio_cash"] is False
    assert policy["reads_broker_state"] is False
    assert policy["emits_orders"] is False


@pytest.mark.integration
def test_the_policy_makes_no_efficacy_claim() -> None:
    # A strategy authority is not a validated edge, and the artefact should not
    # let a later reader assume it was.
    policy = CompounderMonthlyStrategy().policy()
    text = str(policy).lower()

    assert policy["efficacy_claim"].startswith("NONE")
    for forbidden in ("profitable", "outperform", "validated alpha", "production-ready"):
        assert forbidden not in text


@pytest.mark.integration
def test_the_policy_declares_no_ai_decision_authority() -> None:
    assert CompounderMonthlyStrategy().policy()["ai_decision_authority"] == "NONE"


# --------------------------------------------------------------------------
# Cadence (§9, §36, §37, §38, §39)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_month_ends_come_from_the_calendar(calendar: ExecutionCalendar) -> None:
    # 2026-10-31 is a Saturday and 2026-11-30 a Monday: weekday arithmetic gets
    # both wrong, and the calendar gets both right.
    assert decision_sessions(calendar) == (
        "2026-10-30",
        "2026-11-30",
        "2026-12-31",
        "2027-01-05",
    )


@pytest.mark.integration
def test_a_mid_month_session_is_not_a_decision_session(calendar: ExecutionCalendar) -> None:
    assert is_decision_session("2026-11-03", calendar=calendar) is False


@pytest.mark.integration
def test_a_session_outside_the_calendar_is_never_a_decision(calendar: ExecutionCalendar) -> None:
    # 2026-10-31 is a Saturday. It is not a session, so it is not a month end.
    assert is_decision_session("2026-10-31", calendar=calendar) is False


@pytest.mark.integration
def test_a_mid_month_session_produces_no_decision_and_no_intents(
    calendar: ExecutionCalendar,
) -> None:
    report = evaluate_session(bundle(*scored(30)), session="2026-11-03", calendar=calendar)

    assert report.outcome == "NO_STRATEGY_DECISION_DUE"
    assert report.decision is None
    assert report.intents == ()


@pytest.mark.integration
def test_no_decision_due_never_implies_liquidation(calendar: ExecutionCalendar) -> None:
    # The failure this prevents: an operational caller reading "no decision" as
    # "exit everything" would liquidate the portfolio every day between month
    # ends.
    report = evaluate_session(bundle(*scored(30)), session="2026-11-03", calendar=calendar)

    assert report.implies_liquidation is False
    assert report.as_json()["implies_liquidation"] is False


@pytest.mark.integration
def test_the_month_end_session_decides_exactly_once(calendar: ExecutionCalendar) -> None:
    rows = bundle(*scored(30))

    first = evaluate_session(rows, session=DECISION_SESSION, calendar=calendar)
    second = evaluate_session(rows, session=DECISION_SESSION, calendar=calendar)

    assert first.decision is not None
    assert second.decision is not None
    assert first.decision.fingerprint() == second.decision.fingerprint()


@pytest.mark.integration
def test_the_decision_instant_is_the_session_close(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(10)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.decision_at == "2026-10-30T16:00:00-04:00"


@pytest.mark.integration
def test_execution_is_the_next_session_after_a_friday_month_end(
    calendar: ExecutionCalendar,
) -> None:
    # 2026-10-30 is a Friday; the next session is the following Monday.
    report = evaluate_session(bundle(*scored(10)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.first_execution_session == "2026-11-02"


@pytest.mark.integration
def test_execution_crosses_a_year_boundary(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(10)), session="2026-12-31", calendar=calendar)

    assert report.decision is not None
    assert report.decision.first_execution_session == "2027-01-04"


@pytest.mark.integration
def test_a_decision_is_never_executable_in_its_own_session(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(10)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.first_execution_session > report.decision.decision_session


@pytest.mark.integration
def test_a_decision_with_no_following_session_is_refused() -> None:
    # The calendar ends at the decision. Recording that as actionable would
    # produce a decision nothing could ever execute.
    ending = ExecutionCalendar(sessions=("2026-10-29", DECISION_SESSION), name="ENDS")

    with pytest.raises(StrategyError, match=Code.STRATEGY_NO_EXECUTION_SESSION):
        CompounderMonthlyStrategy().decide(
            bundle(*scored(10)), session=DECISION_SESSION, calendar=ending
        )


# --------------------------------------------------------------------------
# Selection, the decile rule and ties (§11, §12, §30, §31, §32, §33)
# --------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(
    ("eligible", "expected"),
    [(0, 0), (1, 1), (9, 1), (10, 1), (11, 2), (19, 2), (20, 2), (21, 3), (100, 10), (101, 11)],
)
def test_the_decile_rule_is_frozen_at_the_boundaries(eligible: int, expected: int) -> None:
    # Frozen rather than derived at read time: `floor` would select nothing from
    # any universe under ten, and a strategy that silently holds nothing when
    # its universe is small is one whose behaviour nobody tested.
    assert target_count(eligible) == expected


@pytest.mark.integration
def test_the_target_never_exceeds_the_eligible_count() -> None:
    for eligible in range(0, 40):
        assert target_count(eligible) <= max(eligible, 0)


@pytest.mark.integration
def test_selection_takes_the_highest_scores(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.target_count == 3
    assert [row.identity.identity_id for row in report.decision.selections] == [1, 2, 3]


@pytest.mark.integration
def test_equal_relative_exposure_sums_to_one(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    weights = [row.desired_weight for row in report.decision.selections]
    assert weights == [pytest.approx(1 / 3)] * 3
    assert sum(weights) == pytest.approx(1.0)


@pytest.mark.integration
def test_a_single_eligible_security_gets_full_relative_exposure(
    calendar: ExecutionCalendar,
) -> None:
    report = evaluate_session(bundle(record(7, 50.0)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.target_count == 1
    assert report.decision.selections[0].desired_weight == pytest.approx(1.0)


@pytest.mark.integration
def test_ties_resolve_on_durable_identity_not_ticker(calendar: ExecutionCalendar) -> None:
    # Identical scores, and the tickers sort opposite to the identity ids. A
    # ticker-based tie break would pick 30; the durable key picks 10.
    rows = (
        record(30, 50.0, symbol="AAA"),
        record(10, 50.0, symbol="ZZZ"),
        record(20, 50.0, symbol="MMM"),
    )
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.selections[0].identity.identity_id == 10


@pytest.mark.integration
def test_a_tie_is_resolved_identically_whatever_order_it_arrives_in(
    calendar: ExecutionCalendar,
) -> None:
    rows = (record(30, 50.0), record(10, 50.0), record(20, 50.0))
    forward = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)
    reverse = evaluate_session(bundle(*reversed(rows)), session=DECISION_SESSION, calendar=calendar)

    assert forward.decision is not None
    assert reverse.decision is not None
    assert forward.decision.fingerprint() == reverse.decision.fingerprint()


@pytest.mark.integration
def test_an_empty_universe_decides_nothing_without_dividing_by_zero(
    calendar: ExecutionCalendar,
) -> None:
    report = evaluate_session(bundle(), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.target_count == 0
    assert report.decision.selections == ()


@pytest.mark.integration
def test_a_universe_of_only_ineligible_records_selects_nothing(
    calendar: ExecutionCalendar,
) -> None:
    rows = (record(1, None, status="INSUFFICIENT_DATA"), record(2, 50.0, freshness="STALE"))
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.eligible_count == 0
    assert report.decision.selections == ()
    assert report.decision.considered_count == 2


# --------------------------------------------------------------------------
# Order invariance and idempotency (§28, §29)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_reversing_the_input_order_changes_nothing(calendar: ExecutionCalendar) -> None:
    rows = scored(37)
    forward = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)
    reverse = evaluate_session(bundle(*reversed(rows)), session=DECISION_SESSION, calendar=calendar)

    assert forward.decision is not None
    assert reverse.decision is not None
    assert forward.decision.selected_identities == reverse.decision.selected_identities
    assert forward.decision.fingerprint() == reverse.decision.fingerprint()
    assert forward.bundle_fingerprint == reverse.bundle_fingerprint


@pytest.mark.integration
def test_the_same_evidence_produces_the_same_decision_id(calendar: ExecutionCalendar) -> None:
    rows = bundle(*scored(25))

    first = evaluate_session(rows, session=DECISION_SESSION, calendar=calendar)
    second = evaluate_session(rows, session=DECISION_SESSION, calendar=calendar)

    assert first.decision is not None
    assert second.decision is not None
    assert first.decision.decision_id == second.decision.decision_id


@pytest.mark.integration
def test_different_evidence_produces_a_different_decision(calendar: ExecutionCalendar) -> None:
    one = evaluate_session(bundle(*scored(25)), session=DECISION_SESSION, calendar=calendar)
    two = evaluate_session(
        bundle(*scored(25, top=91.0)), session=DECISION_SESSION, calendar=calendar
    )

    assert one.decision is not None
    assert two.decision is not None
    assert one.decision.fingerprint() != two.decision.fingerprint()


@pytest.mark.integration
def test_no_clock_reading_enters_the_decision_identity(calendar: ExecutionCalendar) -> None:
    # The decision digests evidence and policy only. A wall-clock reading would
    # make the same decision unrecognisable as a retry of itself.
    rows = bundle(*scored(12))
    report = evaluate_session(rows, session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert "calculated_at" not in report.decision.body()
    assert report.decision.body()["decision_at"] == "2026-10-30T16:00:00-04:00"


# --------------------------------------------------------------------------
# Point in time (§7, §8, §34, §35, §44)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_evidence_computed_after_the_close_is_refused(calendar: ExecutionCalendar) -> None:
    rows = (*scored(20), record(99, 99.0, calculated_at=AFTER_CLOSE))

    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_AFTER_DECISION_TIME):
        evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)


@pytest.mark.integration
def test_the_look_ahead_check_compares_instants_not_strings(
    calendar: ExecutionCalendar,
) -> None:
    # AFTER_CLOSE is "2026-10-30T13:00:00-08:00" — 17:00 New York, an hour after
    # the close, but lexicographically *before* "16:00:00-04:00". A string
    # comparison admits it. This is the assertion that proves it does not.
    assert AFTER_CLOSE < "2026-10-30T16:00:00-04:00"

    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_AFTER_DECISION_TIME):
        evaluate_session(
            bundle(record(1, 50.0, calculated_at=AFTER_CLOSE)),
            session=DECISION_SESSION,
            calendar=calendar,
        )


@pytest.mark.integration
def test_evidence_in_a_different_offset_before_the_close_is_admissible(
    calendar: ExecutionCalendar,
) -> None:
    # The mirror case: "18:00:00+00:00" is 14:00 New York and sorts *after* the
    # close as a string. Rejecting it would withhold perfectly good evidence.
    assert BEFORE_CLOSE > "2026-10-30T16:00:00-04:00"

    report = evaluate_session(
        bundle(record(1, 50.0, calculated_at=BEFORE_CLOSE)),
        session=DECISION_SESSION,
        calendar=calendar,
    )

    assert report.decision is not None
    assert report.decision.eligible_count == 1


@pytest.mark.integration
def test_no_older_record_is_substituted_for_a_future_one(calendar: ExecutionCalendar) -> None:
    # A fallback to the last admissible record would be a different decision
    # wearing this one's fingerprint.
    rows = (record(1, 50.0, calculated_at=BEFORE_CLOSE), record(1, 99.0, calculated_at=AFTER_CLOSE))

    with pytest.raises(StrategyError, match="No older record is substituted"):
        evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)


@pytest.mark.integration
def test_a_record_without_a_timestamp_is_refused_not_dropped(
    calendar: ExecutionCalendar,
) -> None:
    # Dropping it would shrink the eligible count, move the decile boundary and
    # change the selection — a silent alteration rather than a refusal.
    rows = (*scored(20), record(99, 99.0, calculated_at=""))

    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_INCOMPLETE):
        evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)


@pytest.mark.integration
def test_a_naive_timestamp_is_refused(calendar: ExecutionCalendar) -> None:
    rows = (record(1, 50.0, calculated_at="2026-10-30T14:00:00"),)

    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_NOT_ADMISSIBLE):
        evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)


@pytest.mark.integration
def test_another_score_version_is_refused(calendar: ExecutionCalendar) -> None:
    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_VERSION_MISMATCH):
        evaluate_session(
            bundle(*scored(10), version="COMPOUNDER_V1_1"),
            session=DECISION_SESSION,
            calendar=calendar,
        )


# --------------------------------------------------------------------------
# Missingness (§14, §34)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_missing_score_is_not_selected(calendar: ExecutionCalendar) -> None:
    rows = (record(1, None, status="INSUFFICIENT_DATA"), record(2, 10.0))
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.selected_identities[0].identity_id == 2


@pytest.mark.integration
def test_a_missing_score_is_never_treated_as_zero_or_a_median(
    calendar: ExecutionCalendar,
) -> None:
    # If missing became zero it would still be ranked; if it became the median
    # it could be selected. Neither happens: it is not eligible at all.
    rows = (record(1, None, status="INSUFFICIENT_DATA"),)
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.eligible_count == 0
    assert report.decision.selections == ()


@pytest.mark.integration
def test_a_stale_score_is_not_rank_eligible(calendar: ExecutionCalendar) -> None:
    # The frozen V1.2 policy, consumed from domain.scores rather than re-derived.
    rows = (record(1, 99.0, freshness="STALE"), record(2, 10.0))
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.selected_identities[0].identity_id == 2


@pytest.mark.integration
def test_a_scored_status_with_no_number_is_reported_as_a_contradiction(
    calendar: ExecutionCalendar,
) -> None:
    # Folding it into INSUFFICIENT_DATA would make a data fault look like a
    # company's circumstances.
    rows = (record(1, None, status="SCORED"), record(2, 10.0))
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.excluded_counts["SCORED_WITHOUT_A_NUMBER"] == 1


@pytest.mark.integration
def test_an_unrecognised_status_is_not_selectable(calendar: ExecutionCalendar) -> None:
    rows = (record(1, 99.0, status="SOMETHING_NEW"),)
    report = evaluate_session(bundle(*rows), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None
    assert report.decision.eligible_count == 0


# --------------------------------------------------------------------------
# State, rotation and exits (§17, §19, §40, §41)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_a_rotation_exits_what_is_no_longer_selected(calendar: ExecutionCalendar) -> None:
    # Previously A, B, C; now B, C, D. A must reach desired zero.
    previous = StrategyState(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        decision_session="2026-09-30",
        decision_at="2026-09-30T16:00:00-04:00",
        decision_fingerprint="prior",
        desired={"COMPANY:1": 1 / 3, "COMPANY:2": 1 / 3, "COMPANY:3": 1 / 3},
        identities=tuple(SecurityIdentity(kind="COMPANY", identity_id=i) for i in (1, 2, 3)),
    )
    # 2, 3 and 4 score best now; 1 falls away.
    rows = (
        record(1, 10.0),
        record(2, 90.0),
        record(3, 89.0),
        record(4, 88.0),
        # 30 eligible in total, so the decile rule asks for exactly 3.
        *[record(i, 5.0) for i in range(5, 31)],
    )
    report = evaluate_session(
        bundle(*rows), session=DECISION_SESSION, calendar=calendar, previous=previous
    )

    assert report.decision is not None
    assert [row.identity.identity_id for row in report.decision.selections] == [2, 3, 4]
    assert [row.identity_id for row in report.decision.exits] == [1]


@pytest.mark.integration
def test_an_exit_is_an_intent_at_zero_not_an_order(calendar: ExecutionCalendar) -> None:
    previous = StrategyState(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        decision_session="2026-09-30",
        decision_at="2026-09-30T16:00:00-04:00",
        decision_fingerprint="prior",
        desired={"COMPANY:99": 1.0},
        identities=(SecurityIdentity(kind="COMPANY", identity_id=99),),
    )
    report = evaluate_session(
        bundle(record(1, 50.0)), session=DECISION_SESSION, calendar=calendar, previous=previous
    )

    exits = [row for row in report.intents if row.identity.identity_id == 99]
    assert len(exits) == 1
    assert exits[0].requested_weight == 0.0
    assert "no order implied" in (exits[0].reason or "")


@pytest.mark.integration
def test_a_name_just_outside_the_decile_is_not_retained(calendar: ExecutionCalendar) -> None:
    # No hysteresis. The retention rule that would keep it is
    # ONE_DECILE_HYSTERESIS_V1, which is H2 research property under a protocol
    # whose promotion ladder records broker-paper orders as not authorised.
    previous = StrategyState(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        decision_session="2026-09-30",
        decision_at="2026-09-30T16:00:00-04:00",
        decision_fingerprint="prior",
        desired={"COMPANY:3": 1.0},
        identities=(SecurityIdentity(kind="COMPANY", identity_id=3),),
    )
    # 20 eligible -> target 2. Company 3 ranks third: just outside.
    report = evaluate_session(
        bundle(*scored(20)), session=DECISION_SESSION, calendar=calendar, previous=previous
    )

    assert report.decision is not None
    assert report.decision.target_count == 2
    assert 3 not in [row.identity.identity_id for row in report.decision.selections]
    assert [row.identity_id for row in report.decision.exits] == [3]


@pytest.mark.integration
def test_state_round_trips_through_the_store(tmp_path: Path, calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None
    store = StrategyStateStore(tmp_path)

    store.write(StrategyState.from_decision(report.decision))
    reloaded = store.read()

    assert reloaded is not None
    assert reloaded.decision_fingerprint == report.decision.fingerprint()
    assert [row.identity_id for row in reloaded.desired_identities] == [1, 2, 3]


@pytest.mark.integration
def test_absent_state_is_none_rather_than_an_error(tmp_path: Path) -> None:
    # A first decision legitimately has no predecessor.
    assert StrategyStateStore(tmp_path).read() is None


@pytest.mark.integration
def test_corrupt_state_is_refused_not_defaulted(tmp_path: Path) -> None:
    # A defaulted previous selection would exit every holding.
    store = StrategyStateStore(tmp_path)
    store.path.write_text("{not json", encoding="utf-8")

    with pytest.raises(StrategyError, match=Code.STRATEGY_STATE_UNREADABLE):
        store.read()


@pytest.mark.integration
def test_state_from_another_contract_version_is_refused(tmp_path: Path) -> None:
    import json

    store = StrategyStateStore(tmp_path)
    store.path.write_text(json.dumps({"version": "SOMETHING_ELSE"}), encoding="utf-8")

    with pytest.raises(StrategyError, match=Code.STRATEGY_STATE_UNREADABLE):
        store.read()


@pytest.mark.integration
def test_state_carries_no_positions(tmp_path: Path, calendar: ExecutionCalendar) -> None:
    # Broker positions are deliberately not strategy state: a strategy that read
    # its holdings from the venue would conflate what it wants with what it has.
    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None
    state = StrategyState.from_decision(report.decision)

    text = str(state.as_json()).lower()
    for forbidden in ("quantity", "cash", "position", "cost_basis", "buying_power"):
        assert forbidden not in text


# --------------------------------------------------------------------------
# Deployment authorization (§22, §23, §53)
# --------------------------------------------------------------------------


def grant(
    *, fingerprint: str, environment: str = "EXTERNAL_PAPER"
) -> StrategyDeploymentAuthorization:
    """A deployment grant."""
    return StrategyDeploymentAuthorization(
        authorization_id="s1-grant",
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        strategy_fingerprint=fingerprint,
        code_commit="deadbeef",
        created_at="2026-10-30T12:00:00+00:00",
        environment=environment,
        approved_by="operator",
    )


@pytest.mark.integration
def test_the_exact_strategy_fingerprint_is_authorized_for_paper() -> None:
    strategy = CompounderMonthlyStrategy()
    authorization = grant(fingerprint=strategy.fingerprint())

    authorization.require(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        fingerprint=strategy.fingerprint(),
        environment="EXTERNAL_PAPER",
    )


@pytest.mark.integration
def test_a_changed_strategy_fingerprint_is_refused() -> None:
    # The case the fingerprint binding exists for: same name, same version,
    # edited rule. A version is only as honest as whoever remembered to bump it.
    authorization = grant(fingerprint="a" * 64)

    with pytest.raises(StrategyError, match=Code.STRATEGY_AUTHORIZATION_VERSION_MISMATCH):
        authorization.require(
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            fingerprint=CompounderMonthlyStrategy().fingerprint(),
            environment="EXTERNAL_PAPER",
        )


@pytest.mark.integration
@pytest.mark.parametrize("environment", ["LIVE", "EXTERNAL_LIVE", "PRODUCTION_LIVE", "live"])
def test_a_live_environment_cannot_be_granted(environment: str) -> None:
    with pytest.raises(StrategyError, match=Code.STRATEGY_LIVE_NOT_AUTHORIZED):
        grant(fingerprint="a" * 64, environment=environment)


@pytest.mark.integration
def test_live_is_refused_even_when_a_paper_grant_exists() -> None:
    authorization = grant(fingerprint="a" * 64)

    with pytest.raises(StrategyError, match=Code.STRATEGY_LIVE_NOT_AUTHORIZED):
        authorization.require(
            strategy_id=STRATEGY_ID,
            strategy_version=STRATEGY_VERSION,
            fingerprint="a" * 64,
            environment="LIVE",
        )


@pytest.mark.integration
def test_a_grant_cannot_omit_the_fingerprint() -> None:
    # An empty fingerprint would authorise every later edit of the strategy.
    with pytest.raises(StrategyError, match=Code.STRATEGY_NOT_AUTHORIZED):
        grant(fingerprint="   ")


@pytest.mark.integration
def test_a_grant_for_another_strategy_is_refused() -> None:
    authorization = grant(fingerprint="a" * 64)

    with pytest.raises(StrategyError, match=Code.STRATEGY_NOT_AUTHORIZED):
        authorization.require(
            strategy_id="SOMETHING_ELSE",
            strategy_version=STRATEGY_VERSION,
            fingerprint="a" * 64,
            environment="EXTERNAL_PAPER",
        )


@pytest.mark.integration
def test_a_grant_authorises_no_order_and_no_sizing() -> None:
    body = grant(fingerprint="a" * 64).body()

    assert body["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"
    assert any("order" in row for row in body["does_not_authorise"])
    assert any("sizing" in row for row in body["does_not_authorise"])


# --------------------------------------------------------------------------
# Isolation: what the package cannot reach (§42, §47, §48, §49, §50)
# --------------------------------------------------------------------------


def _package_source() -> str:
    """Every line of the strategy package, concatenated."""
    from pathlib import Path as _Path

    import stock_screener.strategies as package

    root = _Path(next(iter(package.__path__)))
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(root.glob("*.py")))


@pytest.mark.integration
def test_the_strategy_package_imports_no_broker() -> None:
    source = _package_source()

    for forbidden in (
        "execution.external",
        "AlpacaPaper",
        "alpaca",
        "httpx",
        "requests",
        "BrokerTransport",
    ):
        assert forbidden not in source, forbidden


@pytest.mark.integration
def test_the_strategy_package_submits_nothing() -> None:
    source = _package_source()

    for forbidden in ("submit_order", "place_order", ".post(", ".delete(", "cancel_order"):
        assert forbidden not in source, forbidden


@pytest.mark.integration
def test_the_strategy_package_mutates_no_accounting() -> None:
    source = _package_source()

    for forbidden in ("portfolio_accounting", "AccountingEvent", "admit_fill", "post_fill"):
        assert forbidden not in source, forbidden


@pytest.mark.integration
def test_the_strategy_package_reads_no_cash_or_buying_power() -> None:
    # The structural reason it cannot size around the risk engine.
    source = _package_source()

    for forbidden in ("buying_power", "PortfolioSnapshot", "available_cash", ".cash"):
        assert forbidden not in source, forbidden


@pytest.mark.integration
def test_the_strategy_package_calls_no_model() -> None:
    source = _package_source()

    for forbidden in ("anthropic", "openai", "ResearchProvider", "llm", "completion"):
        assert forbidden not in source.lower(), forbidden


@pytest.mark.integration
def test_no_credential_is_referenced_anywhere_in_the_package() -> None:
    # "authorization" is deliberately absent from this list: it is the
    # package's own vocabulary for a deployment grant, which carries no secret.
    source = _package_source().lower()

    for forbidden in (
        "api_key",
        "api-key",
        "secret_key",
        "apca",
        "bearer ",
        "password",
        "access_token",
        "os.environ",
    ):
        assert forbidden not in source, forbidden


@pytest.mark.integration
def test_the_strategy_package_imports_no_h2_module() -> None:
    # H2 is frozen and isolated. Its retention rule is research property under a
    # protocol that records broker-paper orders as not authorised.
    #
    # Only import statements are examined. The package *names*
    # ONE_DECILE_HYSTERESIS_V1 in a comment explaining why it is not used, and a
    # naive text scan would read that explanation as a dependency — which would
    # push the next author towards deleting the explanation rather than the
    # dependency.
    imports = [
        line
        for line in _package_source().splitlines()
        if line.lstrip().startswith(("import ", "from "))
    ]

    for forbidden in (
        "membership_hypothesis",
        "prospective_protocol",
        "prospective_runner",
        "prospective_capture",
        "prospective_source",
        "hysteresis",
        "turnover_forensics",
    ):
        offenders = [line for line in imports if forbidden in line.lower()]
        assert offenders == [], offenders


@pytest.mark.integration
def test_the_strategy_declares_no_hysteresis() -> None:
    assert CompounderMonthlyStrategy().policy()["hysteresis"] == "NONE"


@pytest.mark.integration
def test_evaluation_opens_no_socket(
    calendar: ExecutionCalendar, monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def refuse(*args: object, **kwargs: object) -> None:
        msg = "the strategy must not reach a network"
        raise AssertionError(msg)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)

    assert report.decision is not None


# --------------------------------------------------------------------------
# Downstream integration: risk stays the sizing authority (§51, §52)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_intents_are_accepted_by_the_risk_engine(calendar: ExecutionCalendar) -> None:
    from stock_screener.portfolio_risk import PortfolioSnapshot, evaluate

    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None

    decision = evaluate(
        list(report.intents),
        PortfolioSnapshot(as_of=report.decision.decision_at, cash=100_000.0, positions=()),
        _permissive_policy(),
    )

    assert decision.approved is not None


@pytest.mark.integration
def test_risk_can_cut_the_strategy_exposure_and_the_strategy_cannot_object(
    calendar: ExecutionCalendar,
) -> None:
    # The strategy asks for 1/3 each. A 5% position cap must win, and nothing in
    # the strategy's output can restore the difference.
    from stock_screener.portfolio_risk import PortfolioSnapshot, evaluate

    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None
    assert all(row.requested_weight == pytest.approx(1 / 3) for row in report.intents)

    decision = evaluate(
        list(report.intents),
        PortfolioSnapshot(as_of=report.decision.decision_at, cash=100_000.0, positions=()),
        _permissive_policy(max_position_weight=0.05),
    )

    approved = decision.approved
    assert approved is not None
    assert approved.targets != {}
    assert all(round(weight, 9) <= 0.05 for weight in approved.targets.values())


@pytest.mark.integration
def test_the_strategy_output_contains_no_order_or_quantity(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None

    import re

    # Word-boundary matching, because "considered_count" contains "side" and a
    # substring scan would fail on a field that is entirely innocent.
    text = str(report.decision.as_json()).lower()
    for forbidden in ("quantity", "order_id", "side", "limit_price", "shares", "notional"):
        assert re.search(rf"\b{forbidden}\b", text) is None, forbidden


def _permissive_policy(*, max_position_weight: float = 1.0) -> RiskPolicy:
    """A risk policy wide enough that only the named limit binds."""
    return RiskPolicy(
        policy_id="S1_TEST",
        policy_version="V1",
        max_gross_exposure=1.0,
        max_net_exposure=1.0,
        max_position_weight=max_position_weight,
        max_positions=50,
        min_cash_weight=0.0,
    )


# --------------------------------------------------------------------------
# Golden fixtures (§42, §43, §44)
# --------------------------------------------------------------------------


def golden_bundle() -> StrategyInputBundle:
    """A fixture exercising every selection path at once.

    Twenty eligible names so the decile rule asks for two; a tie at the
    boundary so the durable-identity tie-break decides it; a missing score; a
    stale score; and a previously held name that is no longer selected.
    """
    rows = [
        record(10, 90.0, symbol="ZZZ"),
        record(11, 88.0, symbol="AAA"),
        record(12, 88.0, symbol="BBB"),
        *[record(i, 50.0 - i * 0.1) for i in range(20, 37)],
        record(80, None, status="INSUFFICIENT_DATA"),
        record(81, 99.0, freshness="STALE"),
        record(82, None, status="UNSUPPORTED_SECTOR"),
    ]
    return bundle(*rows)


def golden_previous() -> StrategyState:
    """The previous decision: it held 10 and 99."""
    return StrategyState(
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        decision_session="2026-09-30",
        decision_at="2026-09-30T16:00:00-04:00",
        decision_fingerprint="prior-decision",
        desired={"COMPANY:10": 0.5, "COMPANY:99": 0.5},
        identities=(
            SecurityIdentity(kind="COMPANY", identity_id=10),
            SecurityIdentity(kind="COMPANY", identity_id=99),
        ),
    )


@pytest.mark.integration
def test_the_golden_decision_is_exactly_as_expected(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(
        golden_bundle(),
        session=DECISION_SESSION,
        calendar=calendar,
        previous=golden_previous(),
    )

    decision = report.decision
    assert decision is not None
    assert decision.considered_count == 23
    assert decision.eligible_count == 20
    assert decision.target_count == 2
    # 10 scores highest; 11 and 12 tie at 88.0 and the lower identity wins.
    assert [row.identity.identity_id for row in decision.selections] == [10, 11]
    assert [row.desired_weight for row in decision.selections] == [0.5, 0.5]
    # 10 is held and re-selected; 99 is held and gone.
    assert [row.identity_id for row in decision.exits] == [99]
    assert decision.first_execution_session == "2026-11-02"
    assert decision.excluded_counts == {
        "INSUFFICIENT_DATA": 1,
        "NOT_CURRENT_STALE": 1,
        "UNSUPPORTED_SECTOR": 1,
    }


@pytest.mark.integration
def test_the_golden_decision_produces_the_expected_intents(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(
        golden_bundle(),
        session=DECISION_SESSION,
        calendar=calendar,
        previous=golden_previous(),
    )

    assert [(row.identity.label, row.requested_weight) for row in report.intents] == [
        ("COMPANY:10", 0.5),
        ("COMPANY:11", 0.5),
        ("COMPANY:99", 0.0),
    ]
    assert {row.decision_id for row in report.intents} == {report.decision.decision_id}  # type: ignore[union-attr]
    assert {row.as_of for row in report.intents} == {"2026-10-30T16:00:00-04:00"}


@pytest.mark.integration
def test_the_golden_decision_fingerprint_is_stable(calendar: ExecutionCalendar) -> None:
    # Pinned so a change to any selection rule, weighting or field has to be
    # noticed and justified rather than absorbed.
    report = evaluate_session(
        golden_bundle(),
        session=DECISION_SESSION,
        calendar=calendar,
        previous=golden_previous(),
    )

    assert report.decision is not None
    assert (
        report.decision.fingerprint()
        == "1c64906b9f597fbc18241c784c29bf4a3b4c3082a110cd75713e4869972a96b8"
    )


@pytest.mark.integration
def test_the_golden_no_decision_fixture_produces_nothing(calendar: ExecutionCalendar) -> None:
    report = evaluate_session(
        golden_bundle(),
        session="2026-11-03",
        calendar=calendar,
        previous=golden_previous(),
    )

    assert report.outcome == "NO_STRATEGY_DECISION_DUE"
    assert report.intents == ()
    assert report.decision is None
    assert report.implies_liquidation is False


@pytest.mark.integration
def test_the_golden_pit_refusal_emits_nothing(calendar: ExecutionCalendar) -> None:
    rows = (*golden_bundle().records, record(70, 100.0, calculated_at=AFTER_CLOSE))

    with pytest.raises(StrategyError) as caught:
        evaluate_session(
            bundle(*rows),
            session=DECISION_SESSION,
            calendar=calendar,
            previous=golden_previous(),
        )

    assert caught.value.code == Code.STRATEGY_INPUT_AFTER_DECISION_TIME


# --------------------------------------------------------------------------
# Execution-planner compatibility (§52)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_approved_portfolio_reaches_the_execution_planner(
    calendar: ExecutionCalendar,
) -> None:
    # Offline throughout: no broker, no venue, no network. This proves the chain
    # is technically compatible end to end, not that any order is authorised.
    #
    # The real link is `PortfolioDecision.execution_intents` — the risk engine's
    # inert description of the changes its approved targets imply. That is what
    # `plan_execution` consumes, and the strategy never sees it.
    from stock_screener.portfolio_risk import Action, PortfolioSnapshot, evaluate

    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    assert report.decision is not None
    risk = evaluate(
        list(report.intents),
        PortfolioSnapshot(as_of=report.decision.decision_at, cash=100_000.0, positions=()),
        _permissive_policy(max_position_weight=0.34),
    )

    assert risk.approved is not None
    assert risk.execution_intents != ()
    # From an empty book, every approved target is an entry and nothing else.
    assert {row.action for row in risk.execution_intents} == {Action.ENTER}
    # And the identities reaching execution are exactly the ones risk approved,
    # never the ones the strategy asked for.
    assert {row.identity.label for row in risk.execution_intents} == set(risk.approved.targets)


# --------------------------------------------------------------------------
# Reading the real stored record (§5, §48)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_bundle_is_read_from_the_stored_score_snapshots(session: Session) -> None:
    from datetime import UTC, date, datetime

    from data_access import Company, ScoreSnapshot
    from stock_screener.strategies import load_bundle

    day = date(2026, 10, 30)
    computed = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)
    for index, (score, status, freshness) in enumerate(
        [
            (90.0, "SCORED", "CURRENT"),
            (80.0, "SCORED", "CURRENT"),
            (None, "INSUFFICIENT_DATA", "CURRENT"),
            (70.0, "SCORED", "STALE"),
        ],
        start=1,
    ):
        session.add(Company(id=index, ticker=f"T{index}", name=f"Co {index}"))
        session.add(
            ScoreSnapshot(
                company_id=index,
                score_date=day,
                score_version="COMPOUNDER_V1_2",
                scoring_status=status,
                final_score=score,
                freshness=freshness,
                calculated_at=computed,
                coverage="MARKET",
            )
        )
    session.flush()

    result = load_bundle(session, score_date=day)

    assert result.score_version == "COMPOUNDER_V1_2"
    assert len(result.records) == 4
    # The whole day is read, excluded companies included: that is what makes
    # `considered_count` meaningful rather than tautological.
    assert sum(1 for row in result.records if row.is_selectable) == 2


@pytest.mark.integration
def test_a_stored_naive_timestamp_is_read_as_an_explicit_instant(session: Session) -> None:
    # SQLite stores no offset and hands back a naive datetime. Recovering the
    # writer's UTC at the boundary means the rest of the strategy only ever
    # sees offset-bearing timestamps.
    from datetime import date, datetime

    from data_access import Company, ScoreSnapshot
    from stock_screener.strategies import load_bundle

    day = date(2026, 10, 30)
    session.add(Company(id=1, ticker="T1", name="Co 1"))
    session.add(
        ScoreSnapshot(
            company_id=1,
            score_date=day,
            score_version="COMPOUNDER_V1_2",
            scoring_status="SCORED",
            final_score=50.0,
            freshness="CURRENT",
            calculated_at=datetime(2026, 10, 30, 18, 0),  # noqa: DTZ001 - the SQLite shape
            coverage="MARKET",
        )
    )
    session.flush()

    result = load_bundle(session, score_date=day)

    assert result.records[0].calculated_at.endswith("+00:00")


@pytest.mark.integration
def test_a_day_with_no_snapshot_is_a_missing_input_not_an_empty_market(session: Session) -> None:
    from datetime import date

    from stock_screener.strategies import load_bundle

    with pytest.raises(StrategyError, match=Code.STRATEGY_INPUT_INCOMPLETE):
        load_bundle(session, score_date=date(2026, 10, 30))


@pytest.mark.integration
def test_the_identity_comes_from_the_company_id_never_the_ticker(session: Session) -> None:
    from datetime import UTC, date, datetime

    from data_access import Company, ScoreSnapshot
    from stock_screener.strategies import load_bundle

    day = date(2026, 10, 30)
    session.add(Company(id=77, ticker="REUSED", name="Co"))
    session.add(
        ScoreSnapshot(
            company_id=77,
            score_date=day,
            score_version="COMPOUNDER_V1_2",
            scoring_status="SCORED",
            final_score=50.0,
            freshness="CURRENT",
            calculated_at=datetime(2026, 10, 30, 18, 0, tzinfo=UTC),
            coverage="MARKET",
        )
    )
    session.flush()

    result = load_bundle(session, score_date=day)

    assert result.records[0].identity.key == ("COMPANY", 77)


# --------------------------------------------------------------------------
# The entrypoint (§27)
# --------------------------------------------------------------------------


@pytest.mark.integration
def test_the_strategy_cli_exposes_no_live_flag() -> None:
    from typer.testing import CliRunner

    from stock_screener.cli import app

    runner = CliRunner()
    helps = [
        runner.invoke(app, ["strategy", name, "--help"]).stdout
        for name in ("show-policy", "evaluate")
    ]

    for text in helps:
        assert "--live" not in text


@pytest.mark.integration
def test_the_strategy_cli_prints_the_policy_and_its_fingerprint() -> None:
    from typer.testing import CliRunner

    from stock_screener.cli import app

    result = CliRunner().invoke(app, ["strategy", "show-policy"])

    assert result.exit_code == 0
    assert CompounderMonthlyStrategy().fingerprint() in result.stdout
    assert "NOT_IMPLEMENTED_NOT_AUTHORIZED" in result.stdout


@pytest.mark.integration
def test_the_manifest_declares_zero_network_broker_and_accounting_activity(
    calendar: ExecutionCalendar,
) -> None:
    from stock_screener.strategies import strategy_manifest

    report = evaluate_session(bundle(*scored(21)), session=DECISION_SESSION, calendar=calendar)
    manifest = strategy_manifest(report=report, calendar=calendar, code_commit="abc123")

    assert manifest["network_calls"] == 0
    assert manifest["broker_calls"] == 0
    assert manifest["accounting_mutations"] == 0
    assert manifest["orders_emitted"] == 0
    assert manifest["ai_decision_authority"] == "NONE"
    assert manifest["live_trading"] == "NOT_IMPLEMENTED_NOT_AUTHORIZED"
    assert manifest["manifest_version"] == "STRATEGY_MANIFEST_V1"
