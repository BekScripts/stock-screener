"""`PAPER_SHADOW_SOAK_PROTOCOL_V2` — scored by session, not by run.

V1 was halted at 0 of 20 because no concrete operations environment existed.
V2 is a new protocol rather than a repair of that one, and the counting unit is
the thing that changed.

**One eligible market session is one observation.** V1 could not have expressed
that, because when it was written a session held at most one run. Now a single
session can legitimately hold several: an execution near the open consuming last
month's decision, an ordinary observation at midday, a strategy decision at the
close, a recovery run after a crash. Those are separate operational runs with
separate derived ids — and counting them as separate soak sessions would let one
busy day look like four days of evidence. The scorer counts the session.

The other change V2 needs is a promotion gate V1 had no reason to have. A soak
of a control plane could pass on twenty quiet sessions. A soak of a *strategy
pipeline* cannot: twenty sessions where the strategy was never asked to decide
would prove the scheduler works and nothing about the decision path. So
`successful_strategy_decision_cycles >= 1` is required for an A — and it is
emphatically **not** a requirement that any order be placed, because the risk
engine reducing everything to zero is a correct outcome and P2.2E already
certified the mutation boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.paper_operations.errors import Code, OperationsError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.execution import ExecutionCalendar

__all__ = [
    "PAPER_SHADOW_SOAK_ACTIVATION_V2",
    "PAPER_SHADOW_SOAK_PROTOCOL_V2",
    "REQUIRED_SESSIONS_V2",
    "SOAK_V2_DIRECTORY",
    "PaperShadowSessionObservationV2",
    "PaperShadowSoakActivationV2",
    "PaperShadowSoakProtocolV2",
    "SessionClassification",
    "SoakV2Store",
    "complete_cycle_in",
    "window_for",
]

#: The protocol contract. A new version, never an edit of V1.
PAPER_SHADOW_SOAK_PROTOCOL_V2 = "PAPER_SHADOW_SOAK_PROTOCOL_V2"

#: The activation contract.
PAPER_SHADOW_SOAK_ACTIVATION_V2 = "PAPER_SHADOW_SOAK_ACTIVATION_V2"

#: Required prospective evidence, in eligible market sessions.
REQUIRED_SESSIONS_V2 = 20

#: V2's own namespace. V1's files are never touched.
SOAK_V2_DIRECTORY = "v2"


class SessionClassification(StrEnum):
    """What one eligible market session amounted to, operationally."""

    OBSERVED_NO_ACTION = "OBSERVED_NO_ACTION"
    """Observed; the strategy had no decision due. The ordinary day."""

    STRATEGY_DECISION = "STRATEGY_DECISION"
    """A month-end decision was taken. No order is expected from it."""

    PAPER_EXECUTION = "PAPER_EXECUTION"
    """A frozen decision was executed and reached the venue."""

    EXECUTION_NO_ACTION = "EXECUTION_NO_ACTION"
    """An execution purpose ran and correctly produced no order — risk
    constrained it to zero, or the book already held the approved targets."""

    BLOCKED = "BLOCKED"
    """A gate refused. Correct behaviour, and evidence of it."""

    KILLED = "KILLED"
    """The control state forbade activity."""

    RECOVERED = "RECOVERED"
    """A run resumed rather than starting a competitor."""

    MISSED = "MISSED"
    """No valid observation for an eligible session. Permanent."""


@dataclass(frozen=True)
class PaperShadowSoakProtocolV2:
    """The frozen V2 rules, binding the whole real runtime.

    Attributes:
        protocol_id: A stable name.
        qualifying_commit: The exact build being judged. Observations from
            another build do not count.
        runtime_binding_fingerprint: Which deployed runtime.
        account_binding_fingerprint: Which paper account.
        strategy_fingerprint: Which exact strategy policy.
        strategy_authorization_fingerprint: Which deployment grant.
        risk_policy_fingerprint: Which risk policy. Bound so a policy change
            cannot silently take effect inside an armed soak.
        operations_policy_fingerprint: Which operational rules.
        calendar_fingerprint: Which forward operational calendar.
        schedule_fingerprint: Which V1.1 schedule.
        score_availability_fingerprint: The declared score timing.
        execution_timing_fingerprint: The declared execution timing.
        accounting_generation: The ledger lineage.
        execution_journal_generation: The journal lineage.
        operations_engine_version: The control plane version.
        required_sessions: How many eligible sessions are needed.
    """

    protocol_id: str
    qualifying_commit: str
    runtime_binding_fingerprint: str
    account_binding_fingerprint: str
    strategy_fingerprint: str
    strategy_authorization_fingerprint: str
    risk_policy_fingerprint: str
    operations_policy_fingerprint: str
    calendar_fingerprint: str
    schedule_fingerprint: str
    score_availability_fingerprint: str
    execution_timing_fingerprint: str
    accounting_generation: str
    execution_journal_generation: str
    operations_generation: str = ""
    required_sessions: int = REQUIRED_SESSIONS_V2
    version: str = PAPER_SHADOW_SOAK_PROTOCOL_V2

    @property
    def promotion_gates(self) -> dict[str, Any]:
        """Every gate, inside the body so a changed threshold changes the digest.

        Returns:
            The gates, split by what a failure means. A zero-tolerance failure
            is a safety breach. A non-zero-tolerance failure blocks an `A`
            without being one — missing evidence and a correct refusal to trade
            are the system working, and reporting either as a breach is both
            wrong and the kind of noise that gets real breaches ignored.
        """
        return {
            "counting": {
                "unit": "ONE_ELIGIBLE_MARKET_SESSION",
                "not": "ONE_OPERATIONAL_RUN",
                "multiple_runs_per_session": "one observation, still +1",
                "zero_mutations": "still +1 if the session is validly observed",
                "required_sessions": self.required_sessions,
                "prospective_only": True,
                "backfill": "NONE",
            },
            "zero_tolerance": {
                "live_mutations": 0,
                "duplicate_remote_orders": 0,
                "duplicate_admitted_fills": 0,
                "duplicate_accounting_events": 0,
                "duplicate_counted_sessions": 0,
                "stale_data_trades": 0,
                "missing_data_trades": 0,
                "missed_run_backfills": 0,
                "ignored_reconciliation_breaks": 0,
                "ignored_remote_state_unknown": 0,
                "kill_bypasses": 0,
                "unknown_remote_order_auto_cancels": 0,
                "automatic_accounting_repairs": 0,
                "reranked_executions": 0,
                "risk_authority_expansions": 0,
            },
            "required_not_zero_tolerance": {
                "qualifying_sessions_observed": self.required_sessions,
                "successful_strategy_decision_cycles": 1,
                "execution_eligible_decisions_accounted_for": "ALL",
                "unresolved_critical_incidents": 0,
                "unresolved_blocking_incidents": 0,
                "required_drills_passed": "ALL",
                "pending_executions_at_completion": 0,
            },
            "not_gated_on": [
                "any return, profit, Sharpe, win rate, alpha or drawdown",
                "a minimum order count — risk may correctly permit nothing",
                "a minimum fill count — P2.2E certified the mutation boundary",
                "agreement with any backtest, which the timing profiles forbid",
            ],
        }

    def body(self) -> dict[str, Any]:
        """Return the protocol's semantic content."""
        return {
            "version": self.version,
            "protocol_id": self.protocol_id,
            "qualifying_commit": self.qualifying_commit,
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "strategy_fingerprint": self.strategy_fingerprint,
            "strategy_authorization_fingerprint": self.strategy_authorization_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "schedule_fingerprint": self.schedule_fingerprint,
            "score_availability_fingerprint": self.score_availability_fingerprint,
            "execution_timing_fingerprint": self.execution_timing_fingerprint,
            "accounting_generation": self.accounting_generation,
            "execution_journal_generation": self.execution_journal_generation,
            "operations_generation": self.operations_generation,
            "promotion_gates": self.promotion_gates,
            "timing_disclosure": {
                "strategy_decision": "the actual declared close of the month's last session",
                "score_admissibility": "calculated_at <= decision cutoff",
                "score_underlying_data": "latest price bar may precede the decision session",
                "execution": "09:35 ET, first eligible session strictly after the decision",
                "opening_auction_equivalence": "NONE",
                "backtest_performance_equivalence": "NONE",
            },
            "supersedes": "PAPER_SHADOW_SOAK_PROTOCOL_V1 (HALTED at 0/20, never resumed)",
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the protocol."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the protocol with its fingerprint."""
        return {**self.body(), "protocol_fingerprint": self.fingerprint()}


@dataclass(frozen=True)
class PaperShadowSoakActivationV2:
    """When V2 began observing, and what it bound.

    Attributes:
        protocol_fingerprint: Which rules.
        qualifying_commit: Which build.
        activated_at: The prospective boundary. No session closing before this
            can ever count.
        first_eligible_session: Derived from the calendar and this instant,
            never typed.
        window: The twenty sessions this soak will be scored on.
        decision_session: The month-end decision inside the window.
        execution_session: That decision's execution session, also inside it.
        starting_* : The heads and verdicts at activation.
    """

    protocol_fingerprint: str
    qualifying_commit: str
    activated_at: str
    first_eligible_session: str
    window: tuple[str, ...]
    decision_session: str
    execution_session: str
    runtime_binding_fingerprint: str = ""
    account_binding_fingerprint: str = ""
    starting_operations_head: str = ""
    starting_execution_head: str = ""
    starting_accounting_head: str = ""
    starting_reconciliation: str = ""
    starting_health: str = ""
    required_sessions: int = REQUIRED_SESSIONS_V2
    version: str = PAPER_SHADOW_SOAK_ACTIVATION_V2

    def body(self) -> dict[str, Any]:
        """Return the activation's semantic content. Carries no credential."""
        return {
            "version": self.version,
            "protocol_fingerprint": self.protocol_fingerprint,
            "qualifying_commit": self.qualifying_commit,
            "activated_at": self.activated_at,
            "first_eligible_session": self.first_eligible_session,
            "required_sessions": self.required_sessions,
            "window": list(self.window),
            "window_last_session": self.window[-1] if self.window else "",
            "decision_session": self.decision_session,
            "execution_session": self.execution_session,
            "complete_strategy_cycle_in_window": bool(
                self.decision_session and self.execution_session
            ),
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "starting_operations_head": self.starting_operations_head,
            "starting_execution_head": self.starting_execution_head,
            "starting_accounting_head": self.starting_accounting_head,
            "starting_reconciliation": self.starting_reconciliation,
            "starting_health": self.starting_health,
            "counts_historical_evidence": False,
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    def fingerprint(self) -> str:
        """Digest the activation."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the activation with its fingerprint."""
        return {**self.body(), "activation_fingerprint": self.fingerprint()}


def window_for(
    *, calendar: ExecutionCalendar, now: str, sessions: int = REQUIRED_SESSIONS_V2
) -> tuple[str, ...]:
    """Derive the prospective window from the calendar and an instant.

    Args:
        calendar: The forward operational calendar.
        now: The activation instant, ISO-8601 with offset.
        sessions: How many sessions the window holds.

    Returns:
        The window, earliest first. The first session is the first whose close
        is still in the future — derived, never typed, so the window cannot
        disagree with the calendar it came from.
    """
    from stock_screener.paper_operations.clock import parse_instant
    from stock_screener.paper_runtime.operational_calendar import session_calendar

    instant = parse_instant(now)
    rows = session_calendar()
    eligible = [
        session
        for session in calendar.sessions
        if session in rows and rows[session].closes_at() > instant
    ]
    return tuple(eligible[:sessions])


def complete_cycle_in(
    window: tuple[str, ...], *, calendar: ExecutionCalendar
) -> tuple[str, str] | None:
    """Find a decision session whose execution session is also in the window.

    Args:
        window: The prospective window.
        calendar: The calendar.

    Returns:
        The `(decision_session, execution_session)` pair, or `None` when the
        window holds no complete cycle — in which case the window must not be
        armed, because twenty sessions that never ask the strategy to decide
        prove the scheduler works and nothing about the decision path.
    """
    from stock_screener.strategies import is_decision_session

    for session in window:
        if not is_decision_session(session, calendar=calendar):
            continue
        following = calendar.next_session(session)
        if following is not None and following in window:
            return (session, following)
    return None


@dataclass(frozen=True)
class PaperShadowSessionObservationV2:
    """One eligible market session, aggregated from its runs.

    Derived from the runs and journals rather than assembled by hand. The
    distinction matters: a daily JSON that somebody filled in from the latest
    state would be a summary asserting itself as authority, and the same session
    would observe differently depending on when it was written. Given the same
    frozen underlying evidence this produces the same observation, which is what
    `observation_id` being content-addressed relies on.

    Attributes:
        protocol_fingerprint: Which rules.
        session: The market session.
        qualifying_commit: The build the runs were made under. An observation
            from another build does not count.
        session_opens_at: The declared open.
        session_closes_at: The declared close. 13:00 on an early close, which
            is why finalisation never waits for 16:00.
        early_close: Whether the session ended early.
        run_ids: Every operational run in this session, with its purpose.
        classification: What the session amounted to.
        decision_due / decision_created / decision_fingerprint: The strategy
            events, where any.
        execution_due / execution_outcome / executed_decision_fingerprint: The
            execution events.
        orders_submitted / fills_admitted / accounting_events: What reached the
            venue and the book.
        external_mutations: Mutating requests. Zero on most sessions.
        reconciliation_before / reconciliation_after: The three-way verdicts.
        health_before: The gate's verdict.
        incidents_opened: Incidents raised in this session.
        kill_state: The control state.
        journal heads: Where each record stood afterwards.
        missed_purposes: Expected run purposes that did not happen, named
            rather than omitted — a session can be validly observed while one
            expected run was missed, and pretending it happened is the thing
            this field exists to prevent.
    """

    protocol_fingerprint: str
    session: str
    qualifying_commit: str
    observed_at: str
    session_opens_at: str = ""
    session_closes_at: str = ""
    early_close: bool = False
    run_ids: tuple[tuple[str, str], ...] = ()
    classification: SessionClassification = SessionClassification.OBSERVED_NO_ACTION
    runtime_binding_fingerprint: str = ""
    strategy_fingerprint: str = ""
    risk_policy_fingerprint: str = ""
    account_binding_fingerprint: str = ""
    decision_due: bool = False
    decision_created: bool = False
    decision_fingerprint: str = ""
    execution_due: bool = False
    execution_outcome: str = ""
    executed_decision_fingerprint: str = ""
    risk_transformation_fingerprint: str = ""
    orders_submitted: int = 0
    fills_admitted: int = 0
    accounting_events: int = 0
    external_mutations: int = 0
    live_requests: int = 0
    health_before: str = ""
    reconciliation_before: str = ""
    reconciliation_after: str = ""
    incidents_opened: tuple[str, ...] = ()
    kill_state: str = ""
    operations_head: str = ""
    execution_head: str = ""
    accounting_head: str = ""
    missed_purposes: tuple[str, ...] = ()
    superseded_by: str | None = None

    @property
    def exercised_decision_cycle(self) -> bool:
        """Whether this session completed part of a real decision cycle.

        A decision cycle is exercised when the strategy was genuinely asked and
        produced a decision, or when a frozen decision was genuinely processed.
        Neither requires an order: risk reducing everything to zero is a correct
        outcome of a cycle that did happen.
        """
        return bool(self.decision_created) or bool(self.executed_decision_fingerprint)

    def body(self) -> dict[str, Any]:
        """Return the observation's semantic content. Carries no credential."""
        return {
            "version": "PAPER_SHADOW_SESSION_OBSERVATION_V2",
            "protocol_fingerprint": self.protocol_fingerprint,
            "session": self.session,
            "qualifying_commit": self.qualifying_commit,
            "session_opens_at": self.session_opens_at,
            "session_closes_at": self.session_closes_at,
            "early_close": self.early_close,
            "run_ids": [list(row) for row in sorted(self.run_ids)],
            "run_count": len(self.run_ids),
            "counts_as_sessions": 1,
            "classification": str(self.classification),
            "runtime_binding_fingerprint": self.runtime_binding_fingerprint,
            "strategy_fingerprint": self.strategy_fingerprint,
            "risk_policy_fingerprint": self.risk_policy_fingerprint,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "decision_due": self.decision_due,
            "decision_created": self.decision_created,
            "decision_fingerprint": self.decision_fingerprint,
            "execution_due": self.execution_due,
            "execution_outcome": self.execution_outcome,
            "executed_decision_fingerprint": self.executed_decision_fingerprint,
            "risk_transformation_fingerprint": self.risk_transformation_fingerprint,
            "orders_submitted": self.orders_submitted,
            "fills_admitted": self.fills_admitted,
            "accounting_events": self.accounting_events,
            "external_mutations": self.external_mutations,
            "live_requests": self.live_requests,
            "health_before": self.health_before,
            "reconciliation_before": self.reconciliation_before,
            "reconciliation_after": self.reconciliation_after,
            "incidents_opened": list(self.incidents_opened),
            "kill_state": self.kill_state,
            "operations_head": self.operations_head,
            "execution_head": self.execution_head,
            "accounting_head": self.accounting_head,
            "missed_purposes": list(self.missed_purposes),
            "exercised_decision_cycle": self.exercised_decision_cycle,
        }

    def fingerprint(self) -> str:
        """Digest what was observed.

        `observed_at` and `superseded_by` are both excluded, for the same
        reason: they are facts about the *record*, not about the session. If
        `superseded_by` were inside the digest, marking an observation
        superseded would change its own content-addressed id — so the file
        would no longer be findable under the id it was stored as, and a
        correction would orphan the thing it was correcting rather than point
        at it.
        """
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    @property
    def observation_id(self) -> str:
        """A content-addressed id for this observation.

        Includes the observation's own digest, so recording the identical
        observation twice is idempotent while a *corrected* one gets its own
        file and cannot silently replace what it corrects — the V1 lesson,
        carried forward.
        """
        import hashlib

        digest = hashlib.sha256(
            f"{self.protocol_fingerprint}|{self.session}|{self.fingerprint()}".encode()
        ).hexdigest()
        return f"{self.session}-{digest[:16]}"

    def as_json(self) -> dict[str, Any]:
        """Return the observation with its id and fingerprint."""
        return {
            **self.body(),
            "observed_at": self.observed_at,
            "superseded_by": self.superseded_by,
            "observation_id": self.observation_id,
            "observation_fingerprint": self.fingerprint(),
        }


@dataclass
class SoakV2Store:
    """Where V2's protocol, activation and session observations live.

    Its own namespace under the operations root. V1's files are never read,
    written or touched.

    Attributes:
        root: The operations root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root and locate V2's namespace."""
        from pathlib import Path as _Path

        from stock_screener.paper_operations import guard_operations_root
        from stock_screener.paper_operations.soak import SOAK_DIRECTORY

        self.root = _Path(self.root)
        guard_operations_root(self.root)
        self.directory = self.root / SOAK_DIRECTORY / SOAK_V2_DIRECTORY
        self.protocol_path = self.directory / "protocol.json"
        self.activation_path = self.directory / "activation.json"
        self.observations_directory = self.directory / "observations"

    def freeze_protocol(self, protocol: PaperShadowSoakProtocolV2) -> Path:
        """Write the protocol, refusing to change one already frozen.

        Args:
            protocol: The rules.

        Returns:
            Where it was written.

        Raises:
            OperationsError: `SOAK_PROTOCOL_DRIFT` for a different protocol.
                Re-freezing the identical one is a no-op.
        """
        from stock_screener.paper_operations import atomic_write_verified

        existing = self.protocol()
        if existing is not None and existing.fingerprint() != protocol.fingerprint():
            raise OperationsError(
                Code.SOAK_PROTOCOL_DRIFT,
                f"this root already holds V2 protocol {existing.fingerprint()[:16]}… "
                f"and this one is {protocol.fingerprint()[:16]}…. The rules are frozen "
                "before the first observation and are never edited afterwards",
            )
        atomic_write_verified(self.protocol_path, protocol.as_json())
        return self.protocol_path

    def protocol(self) -> PaperShadowSoakProtocolV2 | None:
        """Read the frozen protocol, or `None`."""
        import json

        if not self.protocol_path.exists():
            return None
        body = json.loads(self.protocol_path.read_text(encoding="utf-8"))
        fields = {
            key: body[key]
            for key in (
                "protocol_id",
                "qualifying_commit",
                "runtime_binding_fingerprint",
                "account_binding_fingerprint",
                "strategy_fingerprint",
                "strategy_authorization_fingerprint",
                "risk_policy_fingerprint",
                "operations_policy_fingerprint",
                "calendar_fingerprint",
                "schedule_fingerprint",
                "score_availability_fingerprint",
                "execution_timing_fingerprint",
                "accounting_generation",
                "execution_journal_generation",
            )
        }
        return PaperShadowSoakProtocolV2(
            **fields,
            operations_generation=str(body.get("operations_generation", "")),
            required_sessions=int(
                body.get("promotion_gates", {})
                .get("counting", {})
                .get("required_sessions", REQUIRED_SESSIONS_V2)
            ),
        )

    def activate(self, activation: PaperShadowSoakActivationV2) -> Path:
        """Record activation, refusing to re-activate.

        Args:
            activation: The activation artefact.

        Returns:
            Where it was written.

        Raises:
            OperationsError: `SOAK_ALREADY_ACTIVATED`. One armed soak, not a
                series of attempts at one.
        """
        import json

        from stock_screener.paper_operations import atomic_write_verified

        if self.activation_path.exists():
            existing = json.loads(self.activation_path.read_text(encoding="utf-8"))
            if existing.get("activation_fingerprint") != activation.fingerprint():
                raise OperationsError(
                    Code.SOAK_ALREADY_ACTIVATED,
                    f"V2 was already activated at {existing.get('activated_at')}. The "
                    "prospective boundary is set once; re-activating would move it",
                )
        atomic_write_verified(self.activation_path, activation.as_json())
        return self.activation_path

    def activation(self) -> dict[str, Any] | None:
        """Read the activation artefact, or `None`."""
        import json

        if not self.activation_path.exists():
            return None
        body: dict[str, Any] = json.loads(self.activation_path.read_text(encoding="utf-8"))
        return body

    def record_observation(
        self, observation: PaperShadowSessionObservationV2
    ) -> PaperShadowSessionObservationV2:
        """Record one session's observation, never overwriting another.

        Args:
            observation: The session.

        Returns:
            The observation.

        Raises:
            OperationsError: `SOAK_RETROSPECTIVE_OBSERVATION` for a session
                before the prospective boundary; `SOAK_OBSERVATION_INVALID`
                for a session from another build, or when a *different*
                observation already exists for the same protocol and session
                and no supersession was declared. Two run records on one
                session are not duplicates; two counted observations for one
                session are, and that is the gate.
        """
        from stock_screener.paper_operations import atomic_write_verified

        activation = self.activation()
        if activation is None:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                "V2 is not activated, so nothing can be observed against it",
            )
        if observation.qualifying_commit != str(activation.get("qualifying_commit")):
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"this observation was made under build "
                f"{observation.qualifying_commit[:12]} and the soak judges "
                f"{str(activation.get('qualifying_commit'))[:12]}. A semantic code "
                "change restarts the count; it does not inherit the sessions already "
                "collected",
            )
        if observation.session < str(activation.get("first_eligible_session")):
            raise OperationsError(
                Code.SOAK_RETROSPECTIVE_OBSERVATION,
                f"session {observation.session} precedes the first eligible session "
                f"{activation.get('first_eligible_session')}. No historical evidence "
                "counts, including the bootstrap, the smoke and every V1 record",
            )
        existing = self.observations_for(observation.session)
        conflicting = [
            row
            for row in existing
            if row.fingerprint() != observation.fingerprint() and row.superseded_by is None
        ]
        if conflicting:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"session {observation.session} already holds a different observation "
                f"({conflicting[0].observation_id}). It is not overwritten: a "
                "correction supersedes explicitly and both records are kept",
            )
        atomic_write_verified(
            self.observations_directory / f"{observation.observation_id}.json",
            observation.as_json(),
        )
        return observation

    def supersede(
        self,
        original: PaperShadowSessionObservationV2,
        correction: PaperShadowSessionObservationV2,
        *,
        reason: str,
    ) -> PaperShadowSessionObservationV2:
        """Correct a session's observation, keeping both records.

        A correction is not an edit. The original is rewritten only to point at
        what superseded it, and both files remain — because the history of what
        was observed is part of the evidence, and a soak whose records could be
        quietly replaced would be a soak nobody could check.

        Args:
            original: The observation being corrected. Must already be recorded.
            correction: The replacement, for the same protocol and session.
            reason: Why, recorded on the correction.

        Returns:
            The correction, as recorded.

        Raises:
            OperationsError: `SOAK_OBSERVATION_INVALID` if the two describe
                different sessions, if the original is not recorded, or if the
                correction is identical to the original — in which case there is
                nothing to correct and the original stands.
        """
        import dataclasses

        from stock_screener.paper_operations import atomic_write_verified

        if original.session != correction.session:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"a correction is for one session: {original.session} and "
                f"{correction.session} are different days",
            )
        if original.fingerprint() == correction.fingerprint():
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"the correction for {original.session} is identical to what is "
                "already recorded; there is nothing to correct",
            )
        recorded = {row.observation_id for row in self.observations_for(original.session)}
        if original.observation_id not in recorded:
            raise OperationsError(
                Code.SOAK_OBSERVATION_INVALID,
                f"{original.observation_id} is not recorded, so it cannot be superseded",
            )
        marked = dataclasses.replace(original, superseded_by=correction.observation_id)
        atomic_write_verified(
            self.observations_directory / f"{original.observation_id}.json",
            {**marked.as_json(), "superseded_reason": reason},
        )
        atomic_write_verified(
            self.observations_directory / f"{correction.observation_id}.json",
            {**correction.as_json(), "supersedes": original.observation_id},
        )
        return correction

    def observations(self) -> tuple[PaperShadowSessionObservationV2, ...]:
        """Every observation recorded, by session."""
        import json

        if not self.observations_directory.exists():
            return ()
        rows = [
            _observation_from_body(json.loads(path.read_text(encoding="utf-8")))
            for path in sorted(self.observations_directory.glob("*.json"))
        ]
        return tuple(sorted(rows, key=lambda row: (row.session, row.observation_id)))

    def observations_for(self, session: str) -> tuple[PaperShadowSessionObservationV2, ...]:
        """Every observation for one session, corrections included."""
        return tuple(row for row in self.observations() if row.session == session)

    def counted_sessions(self) -> tuple[str, ...]:
        """Each session counted exactly once, regardless of run count.

        Returns:
            The distinct sessions holding a live (non-superseded) observation.
            This is the soak counter: several runs in a session still contribute
            one, and a session with zero mutations contributes one.
        """
        return tuple(
            sorted({row.session for row in self.observations() if row.superseded_by is None})
        )


def _observation_from_body(body: dict[str, Any]) -> PaperShadowSessionObservationV2:
    """Rebuild an observation from its stored form.

    Args:
        body: What was written.

    Returns:
        The observation.
    """
    return PaperShadowSessionObservationV2(
        protocol_fingerprint=str(body["protocol_fingerprint"]),
        session=str(body["session"]),
        qualifying_commit=str(body["qualifying_commit"]),
        observed_at=str(body.get("observed_at", "")),
        session_opens_at=str(body.get("session_opens_at", "")),
        session_closes_at=str(body.get("session_closes_at", "")),
        early_close=bool(body.get("early_close", False)),
        run_ids=tuple((str(row[0]), str(row[1])) for row in body.get("run_ids", ())),
        classification=SessionClassification(str(body.get("classification"))),
        runtime_binding_fingerprint=str(body.get("runtime_binding_fingerprint", "")),
        strategy_fingerprint=str(body.get("strategy_fingerprint", "")),
        risk_policy_fingerprint=str(body.get("risk_policy_fingerprint", "")),
        account_binding_fingerprint=str(body.get("account_binding_fingerprint", "")),
        decision_due=bool(body.get("decision_due", False)),
        decision_created=bool(body.get("decision_created", False)),
        decision_fingerprint=str(body.get("decision_fingerprint", "")),
        execution_due=bool(body.get("execution_due", False)),
        execution_outcome=str(body.get("execution_outcome", "")),
        executed_decision_fingerprint=str(body.get("executed_decision_fingerprint", "")),
        risk_transformation_fingerprint=str(body.get("risk_transformation_fingerprint", "")),
        orders_submitted=int(body.get("orders_submitted", 0)),
        fills_admitted=int(body.get("fills_admitted", 0)),
        accounting_events=int(body.get("accounting_events", 0)),
        external_mutations=int(body.get("external_mutations", 0)),
        live_requests=int(body.get("live_requests", 0)),
        health_before=str(body.get("health_before", "")),
        reconciliation_before=str(body.get("reconciliation_before", "")),
        reconciliation_after=str(body.get("reconciliation_after", "")),
        incidents_opened=tuple(str(row) for row in body.get("incidents_opened", ())),
        kill_state=str(body.get("kill_state", "")),
        operations_head=str(body.get("operations_head", "")),
        execution_head=str(body.get("execution_head", "")),
        accounting_head=str(body.get("accounting_head", "")),
        missed_purposes=tuple(str(row) for row in body.get("missed_purposes", ())),
        superseded_by=body.get("superseded_by"),
    )
