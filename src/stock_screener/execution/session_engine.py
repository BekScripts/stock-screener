"""Working a residual across sessions, as a chain of child DAY orders.

P2.0 submitted one order per security and filled it. V1.1 cannot: a DAY order
lives for one session, and a participation ceiling means a large order usually
will not finish inside one. So the shape changes:

    authorised quantity
      -> child DAY order, session 1   -> partial fill -> EXPIRED
      -> child DAY order, session 2   -> partial fill -> EXPIRED
      -> child DAY order, session 3   -> fill         -> FILLED

Each child is a distinct immutable order with a deterministic id, and the expired
ones stay expired. Nothing is revived, because a DAY order that survived
overnight would not be a DAY order — and the place that mistake hides is exactly
here, where keeping one order alive is simpler to write.

**Residual comes from admitted fills, never from submitted quantity.** A child
submitted for 400 that filled 250 leaves 750 outstanding on a 1,000-share parent,
not 600. Deriving the residual from what was *sent* would silently lose the
difference on every partial fill.

**The horizon is finite.** When the policy's session budget runs out with
quantity left, execution stops and the plan is `PARTIALLY_COMPLETED`. An
unbounded rollover would keep trading against a portfolio decision made
arbitrarily long ago, which is a different trade from the one that was approved.

**Drift still stops it.** Between sessions, an unrelated accounting event means
the portfolio the plan was authorised against no longer exists, so no further
child is issued. The engine does not re-run strategy or risk to adapt — that
would be inventing an authorisation nobody gave. A new plan is required.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import (
    ACCOUNTING_V2,
    PortfolioLedger,
    PortfolioStore,
    is_zero,
    quantise_quantity,
    replay,
    state_fingerprint,
)

from .accounting_adapter import admit_fill
from .authorization import PlanStatus, require_supported_accounting_version
from .cost import SessionFillCost, build_cost_report, cost_of_fill
from .errors import Code, ExecutionError
from .events import ExecutionEventType, FillAppliedEvent, OrderFilledEvent
from .fingerprint import fingerprint_of
from .journal import ExecutionJournal
from .session_broker import SessionPaperBroker, build_child_order
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION_V1_1

if TYPE_CHECKING:
    from .authorization import ExecutionPlan
    from .calendar import ExecutionCalendar
    from .cost import ExecutionCostReport
    from .models import Order
    from .persistence import ExecutionStore
    from .realism_policy import PaperExecutionPolicy
    from .session_market import PaperMarketSessionSnapshot

__all__ = ["SessionExecutionOutcome", "SessionExecutionSession"]


@dataclass(frozen=True)
class SessionExecutionOutcome:
    """What one session's work on one security did.

    Attributes:
        session: The session.
        order_id: The child order worked.
        generation: Which child this was.
        authorized: What the child was authorised to trade.
        filled: What filled.
        residual_after: What remains across the whole parent afterwards.
        expired: Whether the child expired with quantity outstanding.
        capacity_ceiling: The session's participation ceiling.
        reason: Why nothing filled, where nothing did.
    """

    session: str
    order_id: str
    generation: int
    authorized: Decimal
    filled: Decimal
    residual_after: Decimal
    expired: bool
    capacity_ceiling: Decimal | None = None
    reason: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the outcome as a JSON-safe mapping."""
        return {
            "session": self.session,
            "order_id": self.order_id,
            "generation": self.generation,
            "authorized_quantity": str(self.authorized),
            "filled_quantity": str(self.filled),
            "residual_after": str(self.residual_after),
            "expired": self.expired,
            "participation_ceiling": (
                None if self.capacity_ceiling is None else str(self.capacity_ceiling)
            ),
            "reason": self.reason,
        }


@dataclass
class SessionExecutionSession:
    """One plan executed across sessions, under a declared friction policy.

    Attributes:
        plan: The authorising plan.
        policy: The declared friction.
        calendar: Where later sessions come from.
        store: Durable execution storage.
        ledger: The in-memory accounting ledger.
        accounting_store: Durable accounting storage. Must be a V2 book.
        journal: The in-memory journal.
        status: How far the plan has got.
        drift_detected: Whether an unrelated book change has been seen.
        outcomes: What each session did, in order.
        costs: One cost entry per fill.
    """

    plan: ExecutionPlan
    policy: PaperExecutionPolicy
    calendar: ExecutionCalendar
    store: ExecutionStore
    ledger: PortfolioLedger
    accounting_store: PortfolioStore
    journal: ExecutionJournal = field(default_factory=ExecutionJournal)
    status: PlanStatus = PlanStatus.PLANNED
    drift_detected: bool = False
    outcomes: list[SessionExecutionOutcome] = field(default_factory=list)
    costs: list[SessionFillCost] = field(default_factory=list)
    _expected_state: str = ""
    _order_sessions: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Bind the session to the book the plan authorises.

        Raises:
            ExecutionError: `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` for a
                non-V2 book; `EXECUTION_BOOK_MISMATCH` for another lineage.
        """
        require_supported_accounting_version(
            self.accounting_store.accounting_version,
            context="the session execution store",
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
    def broker(self) -> SessionPaperBroker:
        """A broker over this session's journal, under this policy."""
        return SessionPaperBroker(journal=self.journal, policy=self.policy)

    def _book_fingerprint(self) -> str:
        """The book's current state fingerprint."""
        return state_fingerprint(replay(self.ledger, accounting_version=ACCOUNTING_V2))

    def require_no_drift(self) -> None:
        """Refuse to issue further orders if the book changed outside this plan.

        Raises:
            ExecutionError: `EXECUTION_STATE_DRIFT`. Already admitted fills
                stand; nothing is rolled back and no strategy or risk pass is
                re-run to adapt, because adapting would mean trading on an
                authorisation nobody gave.
        """
        current = self._book_fingerprint()
        if current != self._expected_state:
            self.drift_detected = True
            self.status = PlanStatus.FAILED
            raise ExecutionError(
                Code.EXECUTION_STATE_DRIFT,
                f"the book was {self._expected_state[:16]}… when this plan last "
                f"acted and is now {current[:16]}…. An event this plan did not "
                "cause has changed the portfolio between sessions, so the targets "
                "it was authorised against no longer describe it. No further child "
                "order is issued, and no strategy or risk pass is re-run to adapt: "
                "a new plan is required. Fills already admitted stand.",
            )

    def filled_for(self, order_ids: set[str]) -> Decimal:
        """How much a set of child orders has actually filled.

        Args:
            order_ids: The children to total.

        Returns:
            The filled quantity, from admitted fills rather than from submitted
            quantity — a child sent for 400 that filled 250 contributes 250.
        """
        derived = self.journal.derive()
        return quantise_quantity(
            sum(
                (fill.quantity for fill in derived.fills.values() if fill.order_id in order_ids),
                Decimal(0),
            )
        )

    def execute_identity(
        self,
        parent: Order,
        *,
        snapshots: dict[str, PaperMarketSessionSnapshot],
        first_session: str,
        available: dict[str, Decimal] | None = None,
    ) -> list[SessionExecutionOutcome]:
        """Work one security's authorised quantity across sessions.

        Args:
            parent: The planned order whose quantity is being worked. It is never
                submitted itself; children are created from it.
            snapshots: `{session: snapshot}` for the sessions available.
            first_session: Where to start.
            available: Optional `{session: quantity}` further explicit caps.

        Returns:
            What each session did.

        Raises:
            ExecutionError: `EXECUTION_STATE_DRIFT` between sessions; whatever a
                quote, a volume, the lifecycle or the book refuses.
        """
        outcomes: list[SessionExecutionOutcome] = []
        children: set[str] = set()
        session: str | None = first_session
        generation = 0

        while session is not None and generation < self.policy.maximum_execution_sessions:
            residual = quantise_quantity(parent.quantity - self.filled_for(children))
            if is_zero(residual):
                break
            self.require_no_drift()
            generation += 1
            snapshot = snapshots.get(session)
            if snapshot is None:
                # No market data for this session. Nothing is invented and no
                # child is created, because a child with no snapshot to fill
                # against would expire having never had a chance.
                outcomes.append(
                    SessionExecutionOutcome(
                        session=session,
                        order_id="",
                        generation=generation,
                        authorized=residual,
                        filled=Decimal(0),
                        residual_after=residual,
                        expired=False,
                        reason="NO_MARKET_SNAPSHOT_FOR_SESSION",
                    )
                )
                break

            child = build_child_order(
                plan=self.plan,
                parent=parent,
                session=session,
                generation=generation,
                residual=residual,
                sizing_price=parent.sizing_price,
                created_at=snapshot.as_of,
            )
            children.add(child.order_id)
            self._order_sessions[child.order_id] = session
            outcome = self._work_child(
                child,
                snapshot=snapshot,
                session=session,
                generation=generation,
                residual_before=residual,
                parent=parent,
                children=children,
                available=(available or {}).get(session),
            )
            outcomes.append(outcome)
            self.outcomes.append(outcome)
            if is_zero(outcome.residual_after):
                break
            session = self.calendar.next_session(session)

        self.status = PlanStatus.IN_PROGRESS
        return outcomes

    def _work_child(
        self,
        child: Order,
        *,
        snapshot: PaperMarketSessionSnapshot,
        session: str,
        generation: int,
        residual_before: Decimal,
        parent: Order,
        children: set[str],
        available: Decimal | None,
    ) -> SessionExecutionOutcome:
        """Create, submit, fill and expire one child order.

        Args:
            child: The child order.
            snapshot: The session's market.
            session: The session.
            generation: Which child this is.
            residual_before: What it was created to work.
            parent: The planned order.
            children: Every child created so far.
            available: An optional further explicit cap.

        Returns:
            What this session did.
        """
        broker = self.broker
        existing = self.journal.derive().statuses.get(child.order_id)
        if existing is None:
            # Creation is idempotent because the child id is deterministic: a
            # crash between creating and submitting leaves the same id, and this
            # branch recognises it rather than creating a second child.
            self.store.commit(
                self.journal, broker.create(self.plan, child, session=session, at=snapshot.as_of)
            )
        existing = self.journal.derive().statuses.get(child.order_id)
        assert existing is not None  # noqa: S101 - created above
        if existing.state is OrderState.CREATED:
            self.store.commit(self.journal, broker.submit(self.plan, child, at=snapshot.as_of))
            self.store.commit(self.journal, broker.acknowledge(self.plan, child, at=snapshot.as_of))

        reason: str | None = None
        ceiling: Decimal | None = None
        remaining_capacity: Decimal | None = None
        try:
            capacity = broker.capacity(
                child,
                snapshot=snapshot,
                session=session,
                order_sessions=self._order_sessions,
            )
            ceiling = capacity.ceiling
            remaining_capacity = capacity.remaining
        except ExecutionError as exc:
            # A missing quote or an unknown volume is not a fill failure to
            # propagate: the child simply has nothing to trade against, and it
            # will expire at the close like any unfilled DAY order. Anything else
            # is a real fault and is re-raised.
            if exc.code not in {Code.PAPER_VOLUME_REQUIRED, Code.PAPER_QUOTE_REQUIRED}:
                raise
            reason = exc.code

        if reason is None:
            event = broker.fill(
                self.plan,
                child,
                snapshot=snapshot,
                session=session,
                order_sessions=self._order_sessions,
                available_quantity=available,
            )
            if event is None:
                reason = (
                    "PARTICIPATION_CAPACITY_EXHAUSTED"
                    if remaining_capacity is not None and remaining_capacity <= Decimal(0)
                    else "NO_FILLABLE_QUANTITY"
                )
            else:
                self.store.commit(self.journal, event)
                assert event.fill is not None  # noqa: S101 - the event validates it
                self._apply(event.fill)
                self._record_cost(event)

        status = self.journal.derive().status(child.order_id)
        expired = False
        if status.state is not OrderState.FILLED:
            expiry = broker.expire(self.plan, child, session=session, at=_session_close(snapshot))
            if expiry is not None:
                self.store.commit(self.journal, expiry)
                expired = True

        filled = self.filled_for({child.order_id})
        return SessionExecutionOutcome(
            session=session,
            order_id=child.order_id,
            generation=generation,
            authorized=residual_before,
            filled=filled,
            residual_after=quantise_quantity(parent.quantity - self.filled_for(children)),
            expired=expired,
            capacity_ceiling=ceiling,
            reason=reason,
        )

    def _apply(self, fill: Any) -> None:
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
        self._expected_state = str(result["accounting_state_fingerprint"])

    def _record_cost(self, event: OrderFilledEvent) -> None:
        """Decompose a fill's execution cost.

        Args:
            event: The fill event, which carries the market it traded against.
        """
        fill = event.fill
        assert fill is not None  # noqa: S101 - the event validates it
        if event.bid is None or event.ask is None:
            return
        self.costs.append(
            cost_of_fill(
                identity=fill.identity,
                session=event.session or "",
                side=fill.side,
                bid=event.bid,
                ask=event.ask,
                filled_quantity=fill.quantity,
                fill_price=fill.price,
                fee=fill.fee,
                fill_id=fill.fill_id,
                observed_share_volume=event.observed_share_volume,
                participation_ceiling=event.participation_ceiling,
            )
        )

    def cost_report(self) -> ExecutionCostReport:
        """What this plan cost to execute.

        Returns:
            The report. Posted to nothing — the book already holds the prices.
        """
        return build_cost_report(
            execution_plan_id=self.plan.execution_plan_id,
            policy_fingerprint=self.policy.fingerprint(),
            session_costs=tuple(self.costs),
            planned={
                order.identity.key: (order.identity, order.quantity) for order in self.plan.orders
            },
        )

    def settle(self) -> PlanStatus:
        """Decide the plan's final status from what actually filled.

        Returns:
            `COMPLETED` only when every planned quantity filled in full.
            Otherwise `PARTIALLY_COMPLETED` — including when the session horizon
            ran out, which is a real outcome rather than a failure.
        """
        if self.drift_detected:
            self.status = PlanStatus.FAILED
            return self.status
        if not self.plan.orders:
            self.status = PlanStatus.COMPLETED
            return self.status
        derived = self.journal.derive()
        complete = True
        for order in self.plan.orders:
            filled = quantise_quantity(
                sum(
                    (
                        fill.quantity
                        for fill in derived.fills.values()
                        if fill.identity.key == order.identity.key
                    ),
                    Decimal(0),
                )
            )
            if filled != order.quantity:
                complete = False
        self.status = PlanStatus.COMPLETED if complete else PlanStatus.PARTIALLY_COMPLETED
        return self.status

    def realism_report(self) -> dict[str, Any]:
        """What the friction did to this plan.

        Returns:
            The report, fingerprinted. It makes no performance claim.
        """
        costs = self.cost_report()
        derived = self.journal.derive()
        expiries = sum(
            1 for status in derived.statuses.values() if status.state is OrderState.EXPIRED
        )
        residual = quantise_quantity(
            sum(
                (
                    order.quantity
                    - quantise_quantity(
                        sum(
                            (
                                fill.quantity
                                for fill in derived.fills.values()
                                if fill.identity.key == order.identity.key
                            ),
                            Decimal(0),
                        )
                    )
                    for order in self.plan.orders
                ),
                Decimal(0),
            )
        )
        body = {
            "engine_version": EXECUTION_ENGINE_VERSION_V1_1,
            "execution_plan_id": self.plan.execution_plan_id,
            "fill_model": self.broker.describe()["fill_model"],
            "execution_policy_fingerprint": self.policy.fingerprint(),
            "calendar_fingerprint": self.calendar.fingerprint(),
            "plan_status": str(self.status),
            "sessions_used": list(costs.sessions),
            "session_count": len(costs.sessions),
            "child_order_count": len(derived.statuses),
            "expiry_count": expiries,
            "drift_detected": self.drift_detected,
            "unfilled_residual": str(residual),
            "participation": [row.as_json() for row in self.costs],
            "session_outcomes": [row.as_json() for row in self.outcomes],
            "totals": {
                "spread_cost": str(costs.spread_cost),
                "slippage_cost": str(costs.slippage_cost),
                "fees": str(costs.fees),
                "implementation_shortfall": str(costs.implementation_shortfall),
                "total_execution_cost": str(costs.total_execution_cost),
            },
            "performance_claim": "NONE",
            "accounting_effect": "NONE",
        }
        return {**body, "realism_report_fingerprint": fingerprint_of(body)}


def _session_close(snapshot: PaperMarketSessionSnapshot) -> str:
    """When a session's DAY orders expire.

    The snapshot's own instant. It represents the session's completed execution
    opportunity, so expiring at it keeps every event in the session on one
    timeline — and avoids inventing a close time the snapshot never stated.

    Args:
        snapshot: The session's market.

    Returns:
        The expiry instant.
    """
    return snapshot.as_of
