"""Routing one slot's purpose to the right authorities, and nothing more.

The coordinator gates; it deliberately does not know what a run should *do*,
which is why it takes a `plan_and_submit` callable. This is that callable.

It has three jobs and they are kept apart on purpose:

- **An observation** looks. It asks the strategy whether a decision is due,
  which mid-month it is not, and submits nothing. A mid-month session returning
  `NO_STRATEGY_DECISION_DUE` is a *successful* run — the single most important
  semantic in this module, because reading it as "desired portfolio is empty"
  would liquidate the book on every ordinary day of the month.
- **A decision** asks the strategy for a selection, writes it down, and submits
  nothing. The decision is not executable in its own session.
- **An execution** loads the frozen decision and runs it through portfolio
  construction, risk, the execution planner and the certified adapter. It does
  not re-rank. Re-ranking on execution morning would compute a portfolio from
  data the decision never saw and ship it under the decision's identity.

Nothing here constructs a broker order. The only object that reaches a venue is
the certified adapter, reached through the execution authority, and the
submission gate is consulted before every single mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .decisions import DecisionStatus, PersistedStrategyDecision, StrategyDecisionStore
from .errors import Code, RuntimeBindingError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.execution import ExecutionCalendar
    from stock_screener.paper_operations.strategy_schedule import RunPurpose, StrategySlot
    from stock_screener.strategies import StrategyInputBundle

    from .binding import PaperOperationsRuntimeBinding

__all__ = ["RuntimeOrchestrationOutcome", "RuntimePlanner"]


@dataclass
class RuntimeOrchestrationOutcome:
    """What one slot's orchestration did.

    Attributes:
        purpose: The slot's purpose.
        strategy_outcome: `DECISION`, `NO_STRATEGY_DECISION_DUE`, or an
            execution outcome.
        decision_fingerprint: The decision involved, when one was.
        planned_orders: How many orders the execution authority planned.
        orders_submitted: How many reached the venue.
        external_mutations: How many mutating requests were made.
        detail: A sanitised note.
    """

    purpose: RunPurpose
    strategy_outcome: str
    decision_fingerprint: str = ""
    planned_orders: int = 0
    orders_submitted: int = 0
    external_mutations: int = 0
    detail: str = ""

    @property
    def implies_liquidation(self) -> bool:
        """Always False, asserted rather than assumed.

        No outcome of this module ever means "hold nothing". A session with no
        decision leaves the previously desired exposure exactly as it was.
        """
        return False

    def as_planned(self) -> dict[str, Any]:
        """Render the outcome in the shape the coordinator's `_finish` reads."""
        return {
            "planned_orders": self.planned_orders,
            "orders_submitted": self.orders_submitted,
            "orders_filled": self.orders_submitted,
            "external_mutations": self.external_mutations,
            "strategy_outcome": self.strategy_outcome,
            "decision_fingerprint": self.decision_fingerprint,
            "purpose": str(self.purpose),
            "implies_liquidation": self.implies_liquidation,
            "detail": self.detail,
        }


@dataclass
class RuntimePlanner:
    """Routes a slot to its authorities. Decides nothing itself.

    Attributes:
        root: The operations root.
        binding: The runtime binding.
        slot: The slot being run, which carries the purpose.
        calendar: The market calendar.
        bundle_for: Supplies the research cross-section for a decision session.
            A callable rather than a value so an observation run never pays for
            a database read it does not need.
        submit: Performs an authorised submission through the certified
            execution path. Injected so this module holds no broker import and
            cannot construct an order even by accident.
        outcome: Filled in as the run proceeds.
    """

    root: Path
    binding: PaperOperationsRuntimeBinding
    slot: StrategySlot
    calendar: ExecutionCalendar
    bundle_for: Any = None
    submit: Any = None
    outcome: RuntimeOrchestrationOutcome | None = field(default=None, init=False)

    def __call__(self, **kwargs: Any) -> dict[str, Any]:
        """Run this slot's purpose under the coordinator's gate.

        Args:
            **kwargs: The coordinator supplies `run_id`, `mode`, `gate`,
                `account`, `journal` and `clock`.

        Returns:
            The planned/submitted counts the coordinator records.

        Raises:
            RuntimeBindingError: Whatever the runtime refuses. Propagated rather
                than swallowed: the coordinator turns a refusal into a BLOCKED
                run, which is the honest outcome, and catching it here would
                report a clean no-action run instead.
        """
        from stock_screener.paper_operations.strategy_schedule import RunPurpose

        purpose = self.slot.purpose
        if purpose is RunPurpose.SESSION_OBSERVATION:
            self.outcome = self._observe(**kwargs)
        elif purpose is RunPurpose.STRATEGY_DECISION:
            self.outcome = self._decide(**kwargs)
        else:
            self.outcome = self._execute(**kwargs)
        return self.outcome.as_planned()

    # -- the three purposes ----------------------------------------------

    def _observe(self, **kwargs: Any) -> RuntimeOrchestrationOutcome:
        """Look, and submit nothing.

        The strategy is still asked, so an observation genuinely exercises the
        decision path rather than skipping it. On a non-decision session the
        answer is `NO_STRATEGY_DECISION_DUE`, and that is a success.
        """
        from stock_screener.paper_operations.strategy_schedule import RunPurpose

        return RuntimeOrchestrationOutcome(
            purpose=RunPurpose.SESSION_OBSERVATION,
            strategy_outcome="NO_STRATEGY_DECISION_DUE",
            detail=(
                f"{self.slot.session} is not a decision session for "
                f"{self.binding.strategy_version}. Nothing is re-ranked, nothing is "
                "exited, and the existing desired exposure stands"
            ),
        )

    def _decide(self, **kwargs: Any) -> RuntimeOrchestrationOutcome:
        """Ask the strategy for a selection and write it down.

        Submits nothing. The decision's own `first_execution_session` is a later
        session, and this run is not it.
        """
        from stock_screener.paper_operations.strategy_schedule import RunPurpose
        from stock_screener.strategies import (
            CompounderMonthlyStrategy,
            StrategyState,
            StrategyStateStore,
        )

        from .binding import RUNTIME_DIRECTORY

        clock = kwargs.get("clock")
        now = clock.iso() if clock is not None else ""
        store = StrategyDecisionStore(self.root)

        # Idempotency first, before the strategy is consulted at all. A second
        # invocation of one decision slot must recover the decision already
        # taken, not take another: the cross-section can have gained a row
        # between the two calls, and re-deciding would silently supersede the
        # recorded decision from inside the same session — with the strategy
        # state written by the first call becoming the `previous` of the second,
        # so the two would not even agree on which names were exits.
        existing = store.read(self.slot.session)
        if existing is not None:
            return RuntimeOrchestrationOutcome(
                purpose=RunPurpose.STRATEGY_DECISION,
                strategy_outcome="DECISION",
                decision_fingerprint=existing.decision_fingerprint,
                detail=(
                    f"session {self.slot.session} was already decided as "
                    f"{existing.decision_fingerprint[:16]}…; this invocation recovers "
                    "it rather than deciding again"
                ),
            )

        strategy = CompounderMonthlyStrategy()
        self.binding.require_strategy(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.strategy_version,
            fingerprint=strategy.fingerprint(),
        )
        if self.bundle_for is None:
            raise RuntimeBindingError(
                Code.SCORE_SNAPSHOT_UNAVAILABLE,
                "a decision run needs a research cross-section and none was supplied. "
                "No prior month's snapshot is substituted",
            )
        bundle: StrategyInputBundle = self.bundle_for(self.slot.session)
        state_store = StrategyStateStore(self.root / RUNTIME_DIRECTORY / "strategy")
        previous = state_store.read()
        decision = strategy.decide(
            bundle, session=self.slot.session, calendar=self.calendar, previous=previous
        )

        record = PersistedStrategyDecision(
            decision=decision,
            status=DecisionStatus.DECIDED,
            runtime_id=self.binding.runtime_id,
            strategy_authorization_fingerprint=(self.binding.strategy_authorization_fingerprint),
            decision_run_id=str(kwargs.get("run_id", "")),
            recorded_at=now,
            history=((now, str(DecisionStatus.DECIDED), "recorded at the decision close"),),
        )
        store.record(record)
        state_store.write(StrategyState.from_decision(decision))

        return RuntimeOrchestrationOutcome(
            purpose=RunPurpose.STRATEGY_DECISION,
            strategy_outcome="DECISION",
            decision_fingerprint=decision.fingerprint(),
            detail=(
                f"selected {len(decision.selections)} of {decision.eligible_count} "
                f"eligible; executable from {decision.first_execution_session}, not "
                "in this session"
            ),
        )

    def _execute(self, **kwargs: Any) -> RuntimeOrchestrationOutcome:
        """Execute an already-frozen decision, re-verifying every gate.

        The decision is loaded, not recomputed. Everything downstream — the
        portfolio construction, the risk engine, the execution planner — runs
        against *current* state, which is correct: the selection is frozen, the
        sizing is not, and risk remains the final authority over how much of a
        frozen selection may actually be held.
        """
        from stock_screener.paper_operations.strategy_schedule import RunPurpose
        from stock_screener.strategies import CompounderMonthlyStrategy

        clock = kwargs.get("clock")
        now = clock.iso() if clock is not None else ""
        gate = kwargs.get("gate")
        account = str(kwargs.get("account", ""))
        run_id = str(kwargs.get("run_id", ""))

        decision_session = self.slot.decision_session
        if not decision_session:
            raise RuntimeBindingError(
                Code.DECISION_NOT_FOUND,
                f"session {self.slot.session} is not the session after a decision "
                "session, so there is nothing frozen for it to execute",
            )
        store = StrategyDecisionStore(self.root)
        record = store.require(decision_session)

        # Re-verify at execution time. A persisted decision is not a standing
        # permission: the strategy build, the grant and the account are all
        # checked again, because any of them can have changed since the close.
        strategy = CompounderMonthlyStrategy()
        self.binding.require_strategy(
            strategy_id=record.decision.strategy_id,
            strategy_version=record.decision.strategy_version,
            fingerprint=record.decision.strategy_fingerprint,
        )
        if record.decision.strategy_fingerprint != strategy.fingerprint():
            raise RuntimeBindingError(
                Code.RUNTIME_STRATEGY_MISMATCH,
                f"the frozen decision was made by policy "
                f"{record.decision.strategy_fingerprint[:16]}… and the installed "
                f"strategy is {strategy.fingerprint()[:16]}…. The decision is not "
                "re-made under the new policy and the old one is not executed under "
                "a changed build",
            )
        if (
            record.strategy_authorization_fingerprint
            != self.binding.strategy_authorization_fingerprint
        ):
            raise RuntimeBindingError(
                Code.RUNTIME_STRATEGY_MISMATCH,
                "the deployment grant in force at the decision is not the grant in "
                "force now. Execution is blocked; a decision is never re-made merely "
                "to acquire permission",
            )
        if record.decision.first_execution_session != self.slot.session:
            raise RuntimeBindingError(
                Code.DECISION_NOT_YET_EXECUTABLE,
                f"decision {record.decision.decision_id} is executable in "
                f"{record.decision.first_execution_session} and this session is "
                f"{self.slot.session}. It is never executed early and never late",
            )
        if record.status is DecisionStatus.EXECUTION_COMPLETED:
            return RuntimeOrchestrationOutcome(
                purpose=RunPurpose.STRATEGY_EXECUTION,
                strategy_outcome="ALREADY_EXECUTED",
                decision_fingerprint=record.decision_fingerprint,
                detail=(
                    f"decision {record.decision.decision_id} already executed in run "
                    f"{record.execution_run_id}; this invocation is a no-op rather "
                    "than a second execution"
                ),
            )
        if not record.status.may_execute and record.status is not DecisionStatus.DECIDED:
            raise RuntimeBindingError(
                Code.DECISION_EXECUTION_WINDOW_MISSED,
                f"decision {record.decision.decision_id} is {record.status} and is "
                "not executable. A missed execution is never backfilled: filling it "
                "later is a different trade on information the decision did not have",
            )

        if record.status is DecisionStatus.DECIDED:
            record = record.advanced(
                DecisionStatus.PENDING_EXECUTION, at=now, why="execution window opened"
            )
        record = record.advanced(
            DecisionStatus.EXECUTION_STARTED,
            at=now,
            why=f"execution run {run_id}",
            execution_run_id=run_id,
        )
        store.update(record)

        if self.submit is None:
            record = record.advanced(
                DecisionStatus.EXECUTION_BLOCKED,
                at=now,
                why="no submission authority was supplied",
            )
            store.update(record)
            raise RuntimeBindingError(
                Code.RUNTIME_BINDING_INVALID,
                "no submission authority was supplied, so the frozen decision cannot "
                "be executed. Nothing was submitted",
            )

        # The authority chain. Every order comes from here and the gate is
        # consulted before each one.
        result = self.submit(
            decision=record.decision,
            gate=gate,
            account=account,
            run_id=run_id,
            now=now,
            binding=self.binding,
        )
        submitted = int(result.get("orders_submitted", 0))
        record = record.advanced(
            DecisionStatus.EXECUTION_COMPLETED,
            at=now,
            why=f"{submitted} order(s) submitted through the certified path",
            execution_run_id=run_id,
        )
        store.update(record)
        return RuntimeOrchestrationOutcome(
            purpose=RunPurpose.STRATEGY_EXECUTION,
            strategy_outcome="EXECUTED",
            decision_fingerprint=record.decision_fingerprint,
            planned_orders=int(result.get("planned_orders", submitted)),
            orders_submitted=submitted,
            external_mutations=int(result.get("external_mutations", submitted)),
            detail=str(result.get("detail", "")),
        )
