"""The first AlphaTrader strategy authority: monthly top-decile Compounder.

This is the layer the system was missing. Research computes evidence, the
portfolio layer authorises exposure, execution produces orders and accounting
records fills — but until now nothing turned evidence into a *desire*. A
`StrategyIntent` was constructed in exactly one production location, a JSON
deserialiser, which meant the system could authorise, size, route, fill and book
a decision it had no way to make.

What this strategy is, deliberately in full:

- **Monthly.** One selection decision, on the last market session of the month,
  at its close. Operations runs daily; that is not a reason to re-rank daily. A
  daily top-decile rank would churn the portfolio on noise in the ranking's tail
  and would make turnover an artefact of the operational schedule.
- **Top decile of the eligible universe, by `COMPOUNDER_V1_2` total score.** No
  new alpha formula, no re-weighting of components, no tuning. The simple
  existing research selection convention, used because it is already the frozen
  one and because a strategy authority is the thing being defined here, not a
  strategy's merit.
- **Equal relative exposure**, `1 / K`. A preference, not a size.
- **Long only.**

What it is not, and these are the load-bearing absences:

- It reads no cash, no buying power and no portfolio value, so it cannot size
  around the risk engine. `PORTFOLIO_RISK_ENGINE_V1_2` remains the only sizing
  authority, and it may cut any exposure here to zero.
- It produces no order, no quantity and no fill. It expresses desire.
- It has no hysteresis. A name that falls just outside the new decile leaves the
  target. The retention rule that would keep it is `ONE_DECILE_HYSTERESIS_V1`,
  which is H2 research property under a protocol whose own promotion ladder
  records broker-paper orders as not authorised. Borrowing it would contradict
  the record that makes it evidence.
- It uses no market-signal overlay. `MARKET_SIGNALS_V1` has a declared role in
  the research *data* class — peer percentiles over feature snapshots — but no
  frozen contract gives it a selection or market-timing role for a strategy. So
  the first strategy ranks on the Compounder score alone, and introducing a
  regime overlay here would be a new, untested rule wearing an existing
  version's authority.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from domain import CURRENT_SCORE_VERSION

from .errors import Code, StrategyError
from .models import StrategyDecision, StrategySelection

if TYPE_CHECKING:
    from stock_screener.execution import ExecutionCalendar
    from stock_screener.portfolio_risk import StrategyIntent

    from .models import StrategyInputBundle, StrategyInputRecord
    from .state import StrategyState

__all__ = [
    "CADENCE",
    "DECILE_FRACTION",
    "SELECTION_RULE",
    "STRATEGY_ID",
    "STRATEGY_VERSION",
    "TARGET_COUNT_RULE",
    "CompounderMonthlyStrategy",
    "decision_sessions",
    "is_decision_session",
    "target_count",
]

#: The strategy's identity. One name, frozen with its policy.
STRATEGY_ID = "ALPHATRADER_COMPOUNDER_MONTHLY"

#: Its frozen version. A changed rule is a new version, never an edit — the
#: convention `CURRENT_SCORE_VERSION` already establishes for the score.
STRATEGY_VERSION = "ALPHATRADER_COMPOUNDER_MONTHLY_V1"

#: How often a selection decision is made.
CADENCE = "MONTHLY_LAST_SESSION_CLOSE"

#: What the ranking is on.
SELECTION_RULE = "TOP_DECILE_BY_COMPOUNDER_FINAL_SCORE_V1"

#: The decile.
DECILE_FRACTION = 10

#: The exact rounding rule, named so it cannot drift into an implementation
#: detail. `ceil` with a floor of one, over the eligible count:
#:
#:     N=0 -> 0     N=1 -> 1     N=9 -> 1     N=10 -> 1
#:     N=11 -> 2    N=19 -> 2    N=20 -> 2    N=21 -> 3
#:
#: `ceil` rather than `round` or `floor` for two reasons. It never selects zero
#: names from a non-empty eligible universe, which `floor` would do for every
#: N below ten — a strategy that silently holds nothing whenever its universe is
#: small is a strategy whose behaviour nobody tested. And it is monotone in N
#: with no plateau at the boundary, so the target moves when the universe moves.
#: It can never exceed N.
TARGET_COUNT_RULE = "CEIL_ELIGIBLE_OVER_TEN_FLOOR_ONE_V1"


def target_count(eligible: int) -> int:
    """How many names the decile rule asks for.

    Args:
        eligible: How many securities are selectable. Never negative.

    Returns:
        `ceil(eligible / 10)`, at least one when anything is eligible, zero when
        nothing is. Never more than `eligible`.
    """
    if eligible <= 0:
        return 0
    return min(eligible, max(1, math.ceil(eligible / DECILE_FRACTION)))


def is_decision_session(session: str, *, calendar: ExecutionCalendar) -> bool:
    """Whether a session is this strategy's monthly decision session.

    Month-end comes from the calendar, never from weekday arithmetic. The last
    session of a month is not the last weekday of it: a holiday on the 31st
    moves it, and the rule has to agree with the exchange rather than with a
    date library.

    Args:
        session: An ISO date.
        calendar: The market calendar. Authoritative.

    Returns:
        True when `session` is the last session of its month in this calendar.
    """
    if session not in calendar.sessions:
        return False
    month = session[:7]
    later = [row for row in calendar.sessions if row[:7] == month and row > session]
    return not later


def decision_sessions(calendar: ExecutionCalendar) -> tuple[str, ...]:
    """Every monthly decision session in a calendar, ascending.

    Args:
        calendar: The market calendar.

    Returns:
        The last session of each month the calendar covers. A partial trailing
        month yields its last *known* session, which is correct for a forward
        calendar and is why a caller must not read the final entry as a
        confirmed month end.
    """
    return tuple(
        session for session in calendar.sessions if is_decision_session(session, calendar=calendar)
    )


class CompounderMonthlyStrategy:
    """The frozen strategy authority.

    Stateless with respect to markets: every input arrives in a bundle and
    every output is derived from it, so the same evidence and the same session
    always produce the same decision.
    """

    strategy_id = STRATEGY_ID
    strategy_version = STRATEGY_VERSION

    def policy(self) -> dict[str, Any]:
        """Return the frozen policy, exactly as it decides.

        Returns:
            The policy. Its digest is the strategy fingerprint, so any change to
            a rule here changes the fingerprint, and a deployment grant bound to
            the old fingerprint stops covering the new strategy.
        """
        return {
            "strategy_id": STRATEGY_ID,
            "strategy_version": STRATEGY_VERSION,
            "cadence": CADENCE,
            "selection_rule": SELECTION_RULE,
            "target_count_rule": TARGET_COUNT_RULE,
            "decile_fraction": DECILE_FRACTION,
            "score_version": CURRENT_SCORE_VERSION,
            "eligibility": "domain.scores.is_rank_eligible — consumed, not re-derived",
            "ranking_key": "final_score descending, then durable identity ascending",
            "tie_break": "SecurityIdentity (kind, identity_id) ascending",
            "weighting": "EQUAL_RELATIVE_ONE_OVER_K",
            "direction": "LONG_ONLY",
            "hysteresis": "NONE",
            "market_signal_overlay": "NONE",
            "missing_score": "NOT_SELECTABLE — never zero, median or carried forward",
            "decision_time": "close of the decision session, 16:00 America/New_York",
            "execution_eligibility": "first calendar session strictly after the decision",
            "sizing_authority": "PORTFOLIO_RISK_ENGINE_V1_2 — this strategy sizes nothing",
            "reads_portfolio_cash": False,
            "reads_broker_state": False,
            "emits_orders": False,
            "ai_decision_authority": "NONE",
            "environments_authorised": ["EXTERNAL_PAPER"],
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
            "efficacy_claim": (
                "NONE. This defines a strategy authority, not a validated edge. No "
                "backtest result justified any rule here and none was tuned."
            ),
        }

    def fingerprint(self) -> str:
        """Digest the frozen policy."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.policy())

    def decision_instant_for(self, session: str) -> str:
        """The instant a decision for this session is made.

        One definition, so a subclass that must check its inputs before
        narrowing them cannot drift to a different close.

        Args:
            session: An ISO date.

        Returns:
            The session's close, ISO-8601 with offset.
        """
        from datetime import date as _date

        from stock_screener.pit.decision_time import DecisionPoint, decision_instant

        return decision_instant(_date.fromisoformat(session), DecisionPoint.CLOSE).isoformat()

    def decide(
        self,
        bundle: StrategyInputBundle,
        *,
        session: str,
        calendar: ExecutionCalendar,
        previous: StrategyState | None = None,
    ) -> StrategyDecision:
        """Decide for one session, or refuse.

        Args:
            bundle: The research evidence.
            session: The market session to decide on.
            calendar: The market calendar. Authoritative for month ends and for
                the execution session.
            previous: The last decision's state, for computing exits.

        Returns:
            The decision.

        Raises:
            StrategyError: `STRATEGY_SESSION_NOT_IN_CALENDAR` if the session is
                not a market session; `STRATEGY_NO_DECISION_DUE` if it is not a
                month end, which is an ordinary outcome rather than a fault;
                `STRATEGY_INPUT_VERSION_MISMATCH` if the bundle is not the
                frozen score version; `STRATEGY_NO_EXECUTION_SESSION` if the
                calendar has no session after the decision, because a decision
                nothing can execute must not be recorded as actionable; and
                whatever `require_admissible` raises.
        """
        if session not in calendar.sessions:
            raise StrategyError(
                Code.STRATEGY_SESSION_NOT_IN_CALENDAR,
                f"{session} is not a session in calendar {calendar.name!r}; month ends "
                "are read from the calendar, never computed from weekdays",
            )
        if not is_decision_session(session, calendar=calendar):
            raise StrategyError(
                Code.STRATEGY_NO_DECISION_DUE,
                f"{session} is not the last session of its month, so this strategy "
                "makes no selection. The existing desired exposure stands; nothing "
                "is re-ranked and nothing is liquidated",
            )
        if bundle.score_version != CURRENT_SCORE_VERSION:
            raise StrategyError(
                Code.STRATEGY_INPUT_VERSION_MISMATCH,
                f"this strategy decides on {CURRENT_SCORE_VERSION} and the bundle "
                f"carries {bundle.score_version!r}. No comparison crosses versions",
            )

        decision_at = self.decision_instant_for(session)
        bundle.require_admissible(decision_at=decision_at)

        execution = calendar.next_session(session)
        if execution is None:
            raise StrategyError(
                Code.STRATEGY_NO_EXECUTION_SESSION,
                f"calendar {calendar.name!r} ends at {session}, so no session exists "
                "to execute this decision in. A decision nothing can act on is "
                "refused rather than recorded as actionable",
            )

        records = bundle.ordered
        selectable = [row for row in records if row.is_selectable]
        wanted = target_count(len(selectable))
        # Score descending, then durable identity ascending. The identity is the
        # whole tie-break: `SecurityIdentity` orders on (kind, identity_id) and
        # excludes the symbol from comparison, so a reassigned ticker cannot
        # reorder the ranking or merge two companies.
        ranked = sorted(
            selectable,
            key=lambda row: (-float(row.final_score or 0.0), row.identity.key),
        )
        chosen = ranked[:wanted]
        weight = 1.0 / len(chosen) if chosen else 0.0
        selections = tuple(
            StrategySelection(
                identity=row.identity,
                final_score=float(row.final_score or 0.0),
                rank=index + 1,
                desired_weight=weight,
            )
            for index, row in enumerate(chosen)
        )

        held = previous.desired_identities if previous else ()
        selected_keys = {row.identity.key for row in selections}
        exits = tuple(identity for identity in held if identity.key not in selected_keys)

        return StrategyDecision(
            # Read from the instance, not the module constants: the values are
            # identical for V1, and a subclass that narrows the universe must
            # stamp its own identity rather than claim V1 decided.
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            strategy_fingerprint=self.fingerprint(),
            decision_session=session,
            decision_at=decision_at,
            first_execution_session=execution,
            input_fingerprint=bundle.fingerprint(),
            universe_fingerprint=bundle.universe_fingerprint,
            calendar_fingerprint=calendar.fingerprint(),
            eligible_count=len(selectable),
            considered_count=len(records),
            target_count=wanted,
            selections=selections,
            exits=exits,
            previous_decision_fingerprint=(previous.decision_fingerprint if previous else None),
            excluded_counts=_excluded_counts(records),
        )

    def intents(self, decision: StrategyDecision) -> tuple[StrategyIntent, ...]:
        """Turn a decision into the existing `StrategyIntent` contract.

        Exits are included at weight zero. That is the strategy saying it no
        longer wants the exposure — not an instruction to sell. Whether, when
        and how much to sell is the risk engine's and then execution's to decide.

        Args:
            decision: The decision.

        Returns:
            The intents: every selection at its relative weight, then every exit
            at zero, each ordered by durable identity within its group so the
            sequence is reproducible.

        Raises:
            StrategyError: `STRATEGY_NO_DECISION_DUE` if asked for intents from
                a decision belonging to another strategy version.
        """
        from stock_screener.portfolio_risk import Direction, StrategyIntent

        if decision.strategy_version != self.strategy_version:
            raise StrategyError(
                Code.STRATEGY_INPUT_VERSION_MISMATCH,
                f"decision was made by {decision.strategy_version!r}, not "
                f"{self.strategy_version!r}; intents are never re-derived across versions",
            )
        digest = decision.fingerprint()
        rows: list[StrategyIntent] = [
            StrategyIntent(
                strategy_id=decision.strategy_id,
                strategy_version=decision.strategy_version,
                decision_id=decision.decision_id,
                as_of=decision.decision_at,
                identity=row.identity,
                direction=Direction.LONG,
                requested_weight=row.desired_weight,
                # Higher is better, and the rank is the conviction order the
                # ranking already established. Deterministic, never a score
                # rescaled into something that looks like confidence.
                priority=float(decision.target_count - row.rank + 1),
                reason=(
                    f"top-decile {decision.strategy_version} rank {row.rank} of "
                    f"{decision.target_count} from {decision.eligible_count} eligible"
                ),
                source_fingerprint=digest,
            )
            for row in decision.selections
        ]
        rows.extend(
            StrategyIntent(
                strategy_id=decision.strategy_id,
                strategy_version=decision.strategy_version,
                decision_id=decision.decision_id,
                as_of=decision.decision_at,
                identity=identity,
                direction=Direction.LONG,
                requested_weight=0.0,
                priority=0.0,
                reason="no longer selected; desired exposure zero, no order implied",
                source_fingerprint=digest,
            )
            for identity in sorted(decision.exits, key=lambda row: row.key)
        )
        return tuple(rows)


def _excluded_counts(records: tuple[StrategyInputRecord, ...]) -> dict[str, int]:
    """Count why records were not selectable.

    Args:
        records: Every record offered.

    Returns:
        Reason to count, for the diagnostics. A record with a status of `SCORED`
        that still carries no number is counted separately rather than folded
        into `INSUFFICIENT_DATA`: it is a contradiction in the record, and
        hiding it would make a data fault look like a company's circumstances.
    """
    counts: dict[str, int] = {}
    for row in records:
        if row.is_selectable:
            continue
        if row.final_score is None and row.scoring_status == "SCORED":
            reason = "SCORED_WITHOUT_A_NUMBER"
        elif row.final_score is None:
            reason = row.scoring_status or "UNKNOWN_STATUS"
        elif row.freshness != "CURRENT":
            reason = f"NOT_CURRENT_{row.freshness or 'UNKNOWN'}"
        else:
            reason = row.scoring_status or "UNKNOWN_STATUS"
        counts[reason] = counts.get(reason, 0) + 1
    return counts
