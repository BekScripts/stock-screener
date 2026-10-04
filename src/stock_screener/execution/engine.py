"""Running a plan: the two-phase fill handoff, drift detection and recovery.

This module owns the one sequence that has to be exactly right, because both
ways of getting it wrong are expensive. If the journal records a fill the book
never received, a trade is lost. If a crash causes a fill to be applied twice, a
trade is invented. The sequence that avoids both:

    the broker produces fill evidence
    -> the fill event is durable in the journal         (fill occurred)
    -> the accounting FillEvent is admitted and durable (fill is in the book)
    -> a FILL_APPLIED event is durable in the journal   (handoff confirmed)

Each step is durable before the next begins, so a crash anywhere leaves a state
recovery can read unambiguously. A fill durable in the journal with no
`FILL_APPLIED` is `PENDING`: recovery admits it, and the accounting layer's own
idempotency — keyed on an event id *derived* from the fill id — makes a second
admission a no-op rather than a second trade. That is why the id is derived and
not generated.

**Drift.** The book changes constantly while a plan runs, because that is the
plan working. What must not be tolerated is an *unrelated* change: an event
arriving from outside this plan means the portfolio the plan was authorised
against no longer exists. The conservative response is to stop submitting new
orders and leave every admitted fill exactly where it is. Rolling them back would
mean un-trading something that traded, which nothing here can do and nothing
should pretend to.

**Cash.** Sells are sequenced before buys so sales release cash before purchases
consume it. If actual fill prices move against a buy anyway, accounting refuses
with `INSUFFICIENT_CASH` and execution fails closed. There is no overdraft, no
fabricated cash and no automatic resize — resizing an authorised order would mean
trading a quantity the risk engine never approved.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import (
    ACCOUNTING_V2,
    PortfolioLedger,
    PortfolioStore,
    replay,
    state_fingerprint,
)

from .accounting_adapter import admit_fill
from .authorization import ExecutionPlan, PlanStatus, require_supported_accounting_version
from .errors import Code, ExecutionError
from .events import ExecutionEventType, FillAppliedEvent
from .journal import ExecutionJournal
from .paper_broker import DeterministicPaperBroker
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from .events import ExecutionFill
    from .models import Order
    from .paper_market import PaperExecutionSnapshot
    from .persistence import ExecutionStore

__all__ = ["ExecutionSession", "RecoveryOutcome"]


@dataclass(frozen=True)
class RecoveryOutcome:
    """What opening a session found, and what it did about it.

    Attributes:
        journal_status: How the anchor related to the journal.
        anchor_advanced: Whether recovery advanced the anchor over a verified
            tail.
        fills_applied: Fill ids recovery admitted to the book.
        fills_already_applied: Fill ids already in the book, recognised rather
            than re-admitted.
        detail: What happened, for the audit trail.
    """

    journal_status: str
    anchor_advanced: bool = False
    fills_applied: tuple[str, ...] = ()
    fills_already_applied: tuple[str, ...] = ()
    detail: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the outcome as a JSON-safe mapping."""
        return {
            "journal_status": self.journal_status,
            "anchor_advanced": self.anchor_advanced,
            "fills_applied": list(self.fills_applied),
            "fills_already_applied": list(self.fills_already_applied),
            "detail": self.detail,
        }


@dataclass
class ExecutionSession:
    """One plan, executed against one book, through one journal.

    Attributes:
        plan: The authorising plan.
        store: Durable execution storage.
        ledger: The in-memory accounting ledger.
        accounting_store: Durable accounting storage, which must be a V2 book.
        journal: The in-memory journal. Loaded from the store when absent.
        status: How far the plan has got.
        drift_detected: Whether an unrelated book change has been seen.
    """

    plan: ExecutionPlan
    store: ExecutionStore
    ledger: PortfolioLedger
    accounting_store: PortfolioStore
    journal: ExecutionJournal = field(default_factory=ExecutionJournal)
    status: PlanStatus = PlanStatus.PLANNED
    drift_detected: bool = False
    _expected_state: str = ""

    def __post_init__(self) -> None:
        """Bind the session to the book the plan authorises.

        Raises:
            ExecutionError: `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` if the
                accounting store is not V2; `EXECUTION_BOOK_MISMATCH` if it is a
                different ledger lineage than the plan was authorised against.
        """
        require_supported_accounting_version(
            self.accounting_store.accounting_version,
            context="the execution session's accounting store",
        )
        if self.accounting_store.generation != self.plan.book.ledger_generation:
            raise ExecutionError(
                Code.EXECUTION_BOOK_MISMATCH,
                f"the plan is authorised against ledger generation "
                f"{self.plan.book.ledger_generation!r} and the store holds "
                f"{self.accounting_store.generation!r}",
            )
        self._expected_state = self.plan.book.accounting_state_fingerprint

    @property
    def broker(self) -> DeterministicPaperBroker:
        """A broker writing into this session's journal."""
        return DeterministicPaperBroker(journal=self.journal)

    def _book_fingerprint(self) -> str:
        """The book's current accounting state fingerprint."""
        return state_fingerprint(replay(self.ledger, accounting_version=ACCOUNTING_V2))

    def require_no_drift(self) -> None:
        """Refuse to proceed if the book changed other than through this plan.

        The expected fingerprint advances as this plan's own fills are admitted,
        so the check distinguishes the plan's progress from outside
        interference. Anything else is drift.

        Raises:
            ExecutionError: `EXECUTION_STATE_DRIFT` if the book is not where this
                plan last left it. Already admitted fills stand — they happened,
                and nothing here can un-happen them.
        """
        current = self._book_fingerprint()
        if current != self._expected_state:
            self.drift_detected = True
            self.status = PlanStatus.FAILED
            raise ExecutionError(
                Code.EXECUTION_STATE_DRIFT,
                f"the book was {self._expected_state[:16]}… when this plan last "
                f"acted and is now {current[:16]}…. An event this plan did not "
                "cause has changed the portfolio, so the targets it was "
                "authorised against no longer describe it. No further order is "
                "submitted. Fills already admitted stand: they happened, and "
                "rolling them back would be un-trading a trade.",
            )

    def submit(self, order: Order, *, at: str) -> None:
        """Create, submit and acknowledge one order.

        Args:
            order: The order, which must be one the plan authorises.
            at: When, ISO 8601 with offset.

        Raises:
            ExecutionError: `EXECUTION_STATE_DRIFT` if the book moved outside
                this plan; `PLAN_ALREADY_TERMINAL` if the plan has stopped;
                whatever the plan or the state machine refuses.
        """
        if self.status.is_terminal:
            raise ExecutionError(
                Code.PLAN_ALREADY_TERMINAL,
                f"plan {self.plan.execution_plan_id} is {self.status} and submits nothing further",
            )
        self.require_no_drift()
        broker = self.broker
        self.store.commit(self.journal, broker.create(self.plan, order, at=at))
        self.store.commit(self.journal, broker.submit(self.plan, order, at=at))
        self.store.commit(self.journal, broker.acknowledge(self.plan, order, at=at))
        self.status = PlanStatus.IN_PROGRESS

    def fill(
        self, order: Order, *, snapshot: PaperExecutionSnapshot, acknowledged_at: str
    ) -> ExecutionFill | None:
        """Fill what the snapshot allows, then hand the fill to the book.

        The two phases are separate and each is durable before the next starts,
        so a crash between them is recoverable rather than ambiguous.

        Args:
            order: The order.
            snapshot: The explicit prices and availability.
            acknowledged_at: When the order became working.

        Returns:
            The fill, or None when the snapshot states no availability.

        Raises:
            ExecutionError: `ACCOUNTING_ADMISSION_FAILED` if the book refuses the
                fill — on `INSUFFICIENT_CASH`, for instance. The fill evidence
                stays durable and the plan fails closed, because the alternative
                is a journal claiming a trade the book never accepted.
        """
        self.require_no_drift()
        event = self.broker.fill(
            self.plan, order, snapshot=snapshot, acknowledged_at=acknowledged_at
        )
        if event is None:
            return None
        # Phase one: the fill is durable execution evidence. The book has not
        # seen it yet, and the journal says so.
        self.store.commit(self.journal, event)
        assert event.fill is not None  # noqa: S101 - the event validates it
        self._apply(event.fill)
        return event.fill

    def _apply(self, fill: ExecutionFill) -> None:
        """Admit a fill to the book and confirm the handoff.

        Args:
            fill: The durable fill evidence.

        Raises:
            ExecutionError: `ACCOUNTING_ADMISSION_FAILED` if the book refuses.
        """
        try:
            result = admit_fill(fill, ledger=self.ledger, store=self.accounting_store)
        except ExecutionError:
            self.status = PlanStatus.FAILED
            raise
        # Phase two: the book has it. Only now is the handoff recorded, so a
        # crash before this leaves the fill PENDING and recovery finishes it.
        self.store.commit(
            self.journal,
            FillAppliedEvent(
                event_id=f"{fill.fill_id}:APPLIED",
                event_type=ExecutionEventType.FILL_APPLIED,
                order_id=fill.order_id,
                execution_plan_id=self.plan.execution_plan_id,
                occurred_at=fill.effective_at,
                fill_id=fill.fill_id,
                accounting_event_id=str(result["accounting_event_id"]),
                accounting_sequence=int(result["accounting_sequence"]),
                accounting_state_fingerprint=str(result["accounting_state_fingerprint"]),
            ),
        )
        # The plan's own progress becomes the new expectation, so this fill is
        # not mistaken for outside drift on the next order.
        self._expected_state = str(result["accounting_state_fingerprint"])

    def cancel(self, order: Order, *, at: str, reason: str = "CANCELLED") -> None:
        """Cancel an order. Prior fills stand.

        Args:
            order: The order.
            at: When.
            reason: Why.
        """
        self.store.commit(self.journal, self.broker.cancel(self.plan, order, at=at, reason=reason))

    def reject(self, order: Order, *, at: str, reason: str) -> None:
        """Record a rejection. Nothing trades and the book is untouched.

        Args:
            order: The order.
            at: When.
            reason: Why.
        """
        self.store.commit(self.journal, self.broker.reject(self.plan, order, at=at, reason=reason))

    def recover(self) -> RecoveryOutcome:
        """Resolve the journal, its anchor and any unapplied fills.

        Reads the stored journal, advances the anchor over a verified tail if a
        crash left one, then admits every fill that is durable evidence but not
        yet in the book. Each admission is idempotent, so a fill the book already
        holds is recognised rather than re-applied.

        Returns:
            What was found and done.

        Raises:
            ExecutionError: `EXECUTION_JOURNAL_TRUNCATION_DETECTED`,
                `EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH`,
                `EXECUTION_ANCHOR_MISSING` or `EXECUTION_GENERATION_MISMATCH` —
                each fails closed, because each means a person needs to decide
                what happened.
        """
        self.journal = self.store.load_journal()
        result = self.store.classify()
        status = str(result["status"])
        advanced = False

        if status == "TRUNCATION_DETECTED":
            raise ExecutionError(Code.EXECUTION_JOURNAL_TRUNCATION_DETECTED, str(result["detail"]))
        if status == "HEAD_MISMATCH":
            raise ExecutionError(Code.EXECUTION_JOURNAL_HEAD_ANCHOR_MISMATCH, str(result["detail"]))
        if status == "ANCHOR_MISSING":
            raise ExecutionError(Code.EXECUTION_ANCHOR_MISSING, str(result["detail"]))
        if status == "GENERATION_MISMATCH":
            raise ExecutionError(Code.EXECUTION_GENERATION_MISMATCH, str(result["detail"]))
        if status == "RECOVERY_REQUIRED":
            self.store.write_anchor(self.store.anchor_for(self.journal, self.journal.derive()))
            advanced = True

        derived = self.journal.derive()
        applied: list[str] = []
        already: list[str] = []
        for fill in derived.pending_fills:
            outcome = admit_fill(fill, ledger=self.ledger, store=self.accounting_store)
            (applied if outcome["written"] else already).append(fill.fill_id)
            self.store.commit(
                self.journal,
                FillAppliedEvent(
                    event_id=f"{fill.fill_id}:APPLIED",
                    event_type=ExecutionEventType.FILL_APPLIED,
                    order_id=fill.order_id,
                    execution_plan_id=self.plan.execution_plan_id,
                    occurred_at=fill.effective_at,
                    fill_id=fill.fill_id,
                    accounting_event_id=str(outcome["accounting_event_id"]),
                    accounting_sequence=int(outcome["accounting_sequence"]),
                    accounting_state_fingerprint=str(outcome["accounting_state_fingerprint"]),
                ),
            )
            self._expected_state = str(outcome["accounting_state_fingerprint"])

        if not applied and not already:
            self._expected_state = self._book_fingerprint()

        return RecoveryOutcome(
            journal_status=status,
            anchor_advanced=advanced,
            fills_applied=tuple(applied),
            fills_already_applied=tuple(already),
            detail=result["detail"],
        )

    def settle(self) -> PlanStatus:
        """Decide the plan's final status from what its orders actually did.

        Returns:
            `COMPLETED` only when every planned order filled in full and nothing
            is outstanding. An order that was rejected, cancelled or partly
            filled yields `PARTIALLY_COMPLETED` — reported distinctly because a
            plan whose orders did not do what they set out to do is not a
            success, and calling it one would make every later report unreadable.
        """
        if self.drift_detected:
            self.status = PlanStatus.FAILED
            return self.status
        derived = self.journal.derive()
        if not self.plan.orders:
            self.status = PlanStatus.COMPLETED
            return self.status
        states = []
        for order in self.plan.orders:
            found = derived.statuses.get(order.order_id)
            states.append(None if found is None else found)
        if any(row is None or not row.is_terminal for row in states):
            self.status = PlanStatus.IN_PROGRESS
            return self.status
        if all(
            row is not None and row.state is OrderState.FILLED and row.is_fully_filled
            for row in states
        ):
            self.status = PlanStatus.COMPLETED
        else:
            self.status = PlanStatus.PARTIALLY_COMPLETED
        return self.status

    def describe(self) -> dict[str, Any]:
        """Describe the session, for the manifest."""
        return {
            "engine_version": EXECUTION_ENGINE_VERSION,
            "execution_plan_id": self.plan.execution_plan_id,
            "plan_status": str(self.status),
            "drift_detected": self.drift_detected,
            "accounting_engine_version": self.accounting_store.accounting_version,
            "ledger_generation": self.accounting_store.generation,
            "journal_sequence": len(self.journal),
            "journal_head": self.journal.head_hash,
            "pending_fills": [fill.fill_id for fill in self.journal.derive().pending_fills],
            "fill_applications": {
                key: str(value) for key, value in sorted(self.journal.derive().applications.items())
            },
            "handoff": (
                "fill evidence is durable before the book is touched, and the "
                "handoff is confirmed only after the book is durable. A crash "
                "between them leaves the fill PENDING and recovery finishes it "
                "exactly once."
            ),
        }
