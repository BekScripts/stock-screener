"""The production entrypoint: read the record, decide, report.

This module is the only place in the package that performs I/O, and it performs
none over a network. It reads stored score snapshots and hands them to a pure
decision function, which is what makes a decision reproducible from its
fingerprints rather than merely plausible.

Acquiring the data is somebody else's job — `score`, `run-daily`, `capture`
already do it. A strategy that fetched its own inputs could not be replayed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import TYPE_CHECKING, Any

from domain import CURRENT_SCORE_VERSION
from domain.scores import Freshness

from .compounder_monthly import (
    CADENCE,
    SELECTION_RULE,
    STRATEGY_ID,
    STRATEGY_VERSION,
    TARGET_COUNT_RULE,
    CompounderMonthlyStrategy,
    is_decision_session,
)
from .errors import Code, StrategyError
from .models import StrategyInputBundle, StrategyInputRecord

if TYPE_CHECKING:
    from datetime import date, datetime

    from sqlalchemy.orm import Session

    from stock_screener.execution import ExecutionCalendar
    from stock_screener.portfolio_risk import StrategyIntent

    from .models import StrategyDecision
    from .state import StrategyState

__all__ = [
    "STRATEGY_MANIFEST_VERSION",
    "StrategyRunReport",
    "evaluate_session",
    "load_bundle",
    "strategy_manifest",
]

#: The manifest contract.
STRATEGY_MANIFEST_VERSION = "STRATEGY_MANIFEST_V1"

#: The identity kind a scored company maps to. Companies only: a historical
#: identity has no current score and could not be bought today.
COMPANY_KIND = "COMPANY"


def load_bundle(
    session: Session,
    *,
    score_date: date,
    score_version: str = CURRENT_SCORE_VERSION,
    universe_fingerprint: str = "",
) -> StrategyInputBundle:
    """Read one day's complete cross-section from the stored record.

    The whole day is read, not just the scored companies. An excluded company is
    evidence: it establishes that the strategy considered it and why it could not
    be selected, and it is what makes `considered_count` meaningful rather than
    tautological.

    Args:
        session: An open database session.
        score_date: The cross-section date.
        score_version: The formula version. Defaults to the current one.
        universe_fingerprint: The digest of the population, where the caller
            has one. Recorded as given.

    Returns:
        The bundle.

    Raises:
        StrategyError: `STRATEGY_INPUT_INCOMPLETE` if the day holds no rows at
            all. An empty read is a missing input rather than an empty market,
            and deciding on it would select nothing for the wrong reason.
    """
    from data_access import ScoreSnapshotRepository
    from stock_screener.execution import fingerprint_of
    from stock_screener.portfolio_risk import SecurityIdentity

    rows = ScoreSnapshotRepository(session).list_for_date(
        score_date=score_date, score_version=score_version
    )
    if not rows:
        raise StrategyError(
            Code.STRATEGY_INPUT_INCOMPLETE,
            f"no {score_version} snapshot exists for {score_date.isoformat()}. An "
            "absent cross-section is a missing input, not an empty market",
        )
    records = tuple(
        StrategyInputRecord(
            identity=SecurityIdentity(kind=COMPANY_KIND, identity_id=row.company_id),
            final_score=row.final_score,
            scoring_status=str(row.scoring_status),
            freshness=str(row.freshness or Freshness.CURRENT),
            score_date=row.score_date.isoformat(),
            score_version=str(row.score_version),
            calculated_at=_instant(row.calculated_at),
            source_fingerprint=fingerprint_of(
                {
                    "company_id": row.company_id,
                    "score_date": row.score_date.isoformat(),
                    "score_version": str(row.score_version),
                    "final_score": row.final_score,
                    "scoring_status": str(row.scoring_status),
                    "freshness": str(row.freshness or ""),
                }
            ),
        )
        for row in rows
    )
    return StrategyInputBundle(
        as_of=score_date.isoformat(),
        score_version=score_version,
        universe_fingerprint=universe_fingerprint,
        records=records,
    )


def _instant(value: datetime | None) -> str:
    """Render a stored timestamp as an explicit instant.

    `score_snapshots.calculated_at` is declared `DateTime(timezone=True)` and
    the scoring run writes UTC, but SQLite does not store offsets and hands back
    a naive datetime. Reading that as UTC is therefore recovering what the
    writer meant rather than guessing — and it is done here, once, at the
    boundary, so the rest of the strategy only ever sees offset-bearing
    timestamps and the admissibility check can refuse anything that is not.

    Args:
        value: The stored timestamp, possibly naive, possibly absent.

    Returns:
        An ISO-8601 instant with an offset, or `""` when absent — which
        `require_admissible` refuses rather than treats as admissible.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.isoformat()


@dataclass(frozen=True)
class StrategyRunReport:
    """What one evaluation did, safe to print and to keep.

    Attributes:
        session: The session evaluated.
        decision_due: Whether it was a decision session.
        outcome: `DECISION` or `NO_STRATEGY_DECISION_DUE`.
        decision: The decision, when one was made.
        intents: The intents, when a decision was made.
        bundle_fingerprint: The evidence digest.
        detail: Why, when nothing was decided.
    """

    session: str
    decision_due: bool
    outcome: str
    bundle_fingerprint: str
    decision: StrategyDecision | None = None
    intents: tuple[StrategyIntent, ...] = ()
    detail: str = ""

    @property
    def implies_liquidation(self) -> bool:
        """Always False, and asserted rather than assumed.

        A session with no decision is not an instruction to hold nothing. The
        previously desired exposure stands untouched, and an operational caller
        that read "no decision" as "exit everything" would liquidate a portfolio
        every single day between month ends.
        """
        return False

    def body(self) -> dict[str, Any]:
        """Return the report's semantic content."""
        return {
            "session": self.session,
            "decision_due": self.decision_due,
            "outcome": self.outcome,
            "bundle_fingerprint": self.bundle_fingerprint,
            "decision": self.decision.as_json() if self.decision else None,
            "intent_count": len(self.intents),
            "implies_liquidation": self.implies_liquidation,
            "detail": self.detail,
            "efficacy_claim": "NONE — this reports a decision, not an expected return",
        }

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal. Carries no credential."""
        lines = [
            f"strategy        : {STRATEGY_ID} / {STRATEGY_VERSION}",
            f"session         : {self.session}",
            f"outcome         : {self.outcome}",
        ]
        if self.decision is None:
            lines += [
                f"detail          : {self.detail}",
                "intents         : 0",
                "implies exit    : no — existing desired exposure stands",
            ]
            return tuple(lines)
        decision = self.decision
        lines += [
            f"decision at     : {decision.decision_at}",
            f"first execution : {decision.first_execution_session}",
            f"considered      : {decision.considered_count}",
            f"eligible        : {decision.eligible_count}",
            f"target (decile) : {decision.target_count}",
            f"selected        : {len(decision.selections)}",
            f"exits           : {len(decision.exits)}",
            f"intents         : {len(self.intents)}",
            "selections      :",
        ]
        lines += [
            f"    {row.rank:>3}. {row.identity.label:<16} score {row.final_score:>8.3f}  "
            f"desired {row.desired_weight:.6f}"
            for row in decision.selections
        ]
        if decision.exits:
            lines.append("exits (desired 0, no order implied):")
            lines += [f"    {identity.label}" for identity in decision.exits]
        if decision.excluded_counts:
            lines.append("excluded        :")
            lines += [
                f"    {reason:<28} {count}"
                for reason, count in sorted(decision.excluded_counts.items())
            ]
        lines += [
            f"input fp        : {decision.input_fingerprint[:32]}…",
            f"decision fp     : {decision.fingerprint()[:32]}…",
            "sizing          : PORTFOLIO_RISK_ENGINE_V1_2 — this strategy sizes nothing",
            "orders          : none; a strategy expresses desire",
            "live trading    : NOT_IMPLEMENTED, NOT_AUTHORIZED",
        ]
        return tuple(lines)

    def as_json(self) -> dict[str, Any]:
        """Return the report as a JSON-safe mapping."""
        return self.body()


def evaluate_session(
    bundle: StrategyInputBundle,
    *,
    session: str,
    calendar: ExecutionCalendar,
    previous: StrategyState | None = None,
) -> StrategyRunReport:
    """Evaluate one session, deciding only when one is due.

    A non-decision session is an ordinary, successful outcome and is reported
    rather than raised. Every other refusal propagates: a stale or future input
    on a decision session is a fault, and swallowing it would turn a leakage
    guard into a no-action day nobody investigated.

    Args:
        bundle: The research evidence.
        session: The session to evaluate.
        calendar: The market calendar.
        previous: The last decision's state.

    Returns:
        The report.

    Raises:
        StrategyError: Any refusal other than `STRATEGY_NO_DECISION_DUE`.
    """
    strategy = CompounderMonthlyStrategy()
    digest = bundle.fingerprint()
    if session not in calendar.sessions or not is_decision_session(session, calendar=calendar):
        return StrategyRunReport(
            session=session,
            decision_due=False,
            outcome="NO_STRATEGY_DECISION_DUE",
            bundle_fingerprint=digest,
            detail=(
                f"{session} is not the last market session of its month in calendar "
                f"{calendar.name!r}. Nothing is re-ranked and nothing is exited"
            ),
        )
    decision = strategy.decide(bundle, session=session, calendar=calendar, previous=previous)
    return StrategyRunReport(
        session=session,
        decision_due=True,
        outcome="DECISION",
        bundle_fingerprint=digest,
        decision=decision,
        intents=strategy.intents(decision),
    )


def strategy_manifest(
    *,
    report: StrategyRunReport,
    calendar: ExecutionCalendar,
    code_commit: str = "",
    authorization_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Describe what decided, on what, under which approval.

    Args:
        report: The evaluation.
        calendar: The calendar used.
        code_commit: The build that evaluated.
        authorization_fingerprint: The deployment grant, where one was required.

    Returns:
        The manifest.
    """
    strategy = CompounderMonthlyStrategy()
    decision = report.decision
    return {
        "manifest_version": STRATEGY_MANIFEST_VERSION,
        "strategy_id": STRATEGY_ID,
        "strategy_version": STRATEGY_VERSION,
        "strategy_fingerprint": strategy.fingerprint(),
        "cadence": CADENCE,
        "selection_rule": SELECTION_RULE,
        "target_count_rule": TARGET_COUNT_RULE,
        "score_version": CURRENT_SCORE_VERSION,
        "session": report.session,
        "outcome": report.outcome,
        "decision_fingerprint": decision.fingerprint() if decision else None,
        "input_fingerprint": report.bundle_fingerprint,
        "universe_fingerprint": decision.universe_fingerprint if decision else None,
        "calendar_fingerprint": calendar.fingerprint(),
        "authorization_fingerprint": authorization_fingerprint,
        "code_commit": code_commit,
        "ai_decision_authority": "NONE",
        "network_calls": 0,
        "broker_calls": 0,
        "accounting_mutations": 0,
        "orders_emitted": 0,
        "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
    }
