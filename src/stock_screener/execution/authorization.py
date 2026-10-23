"""The authorisation boundary between an approved target and a live order.

This is the narrowest and most important seam in the system. On one side the risk
engine says what a portfolio *should* contain; on the other, orders exist that
could actually trade. An `ExecutionPlan` is the only bridge, and it is a bridge
precisely because it binds everything it was computed from:

    the approved portfolio   — which targets were authorised
    the execution intents    — which deltas those targets implied
    the accounting book      — which ledger generation and engine version
    the accounting state     — what was held when the plan was made
    the valued snapshot      — what it was all marked against
    the execution policy     — which rules turned deltas into orders

Any of those differing at execution time means the plan authorises something
other than what is about to happen, and the answer is a refusal rather than a
best effort. The failure this prevents is specific and expensive: a plan computed
against a portfolio holding 500 shares, executed against one holding 50, sells
450 shares that are not there — or worse, succeeds against a book that has since
been changed by something nobody told the planner about.

**Two kinds of change are distinguished.** Fills from this plan change the book
constantly and are expected; that is the plan working. An *unrelated* event
arriving mid-plan is `EXECUTION_STATE_DRIFT`, and the conservative response is to
stop submitting new orders while leaving admitted fills exactly where they are.
Rolling them back would mean un-trading something that traded.

**New books must name `PORTFOLIO_ACCOUNTING_V2` explicitly.** P1.4 left V1 as the
accounting default so existing ledgers kept their meaning; inheriting that
default here would open new execution books under the basis convention P1.4
exists to replace, and nobody would notice until two strategies entered the same
security at different prices.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .versions import (
    EXECUTION_ENGINE_VERSION,
    FILL_ATTRIBUTION_RULE,
    ORDER_SEQUENCING_RULE,
    REQUIRED_ACCOUNTING_VERSION,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import Order

__all__ = [
    "BookBinding",
    "ExecutionPlan",
    "PlanStatus",
    "intents_fingerprint",
    "require_supported_accounting_version",
]


def require_supported_accounting_version(version: str, *, context: str) -> None:
    """Refuse an execution book under accounting rules this engine will not use.

    Args:
        version: The accounting engine version offered.
        context: What was being attempted, for the message.

    Raises:
        ExecutionError: `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` for anything
            other than V2. V1 remains replayable by the accounting layer, and is
            refused *here* because its global basis convention makes
            per-strategy realised P&L wrong whenever co-owners entered at
            different prices — which is exactly what an execution engine
            placing multi-strategy orders will cause.
    """
    if version != REQUIRED_ACCOUNTING_VERSION:
        raise ExecutionError(
            Code.UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION,
            f"{context} names accounting version {version!r}, and "
            f"{EXECUTION_ENGINE_VERSION} requires {REQUIRED_ACCOUNTING_VERSION}. "
            "This is demanded explicitly rather than inherited: the accounting "
            "layer still defaults to V1 so that ledgers written before P1.4 keep "
            "their meaning, and accepting that default here would open a new book "
            "under the pooled-basis convention whose per-strategy figures P1.4 "
            "exists to correct.",
        )


class PlanStatus(StrEnum):
    """How far a plan got, and whether it got there cleanly."""

    PLANNED = "PLANNED"
    """Authorised. Nothing submitted."""

    IN_PROGRESS = "IN_PROGRESS"
    """At least one order submitted; some not yet terminal."""

    COMPLETED = "COMPLETED"
    """Every planned order filled in full. Nothing outstanding."""

    PARTIALLY_COMPLETED = "PARTIALLY_COMPLETED"
    """Every order reached a terminal state and at least one did not fill in
    full. Reported distinctly from COMPLETED because a plan whose orders were
    cancelled or partly filled did not do what it set out to do, and calling
    that success would make every later report unreadable."""

    CANCELLED = "CANCELLED"
    """Stopped deliberately before completing."""

    FAILED = "FAILED"
    """Stopped by a refusal — drift, a rejection, or an accounting admission
    that would not go through. Admitted fills stand."""

    @property
    def is_terminal(self) -> bool:
        """Whether the plan can no longer progress."""
        return self in {
            PlanStatus.COMPLETED,
            PlanStatus.PARTIALLY_COMPLETED,
            PlanStatus.CANCELLED,
            PlanStatus.FAILED,
        }


@dataclass(frozen=True)
class BookBinding:
    """Which book a plan is authorised against.

    Attributes:
        ledger_generation: The accounting ledger lineage.
        accounting_engine_version: Which accounting rules. Must be V2.
        accounting_state_fingerprint: The state the plan was computed against.
        ledger_sequence: How far that ledger had got.
        ledger_head: That ledger's chain head.
    """

    ledger_generation: str
    accounting_engine_version: str
    accounting_state_fingerprint: str
    ledger_sequence: int
    ledger_head: str

    def __post_init__(self) -> None:
        """Verify the binding names a book this engine will trade against.

        Raises:
            ExecutionError: `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION` for a
                non-V2 book; `PLAN_INVALID` if the binding is incomplete, since
                a binding that cannot identify a book cannot detect a wrong one.
        """
        require_supported_accounting_version(
            self.accounting_engine_version, context="the execution book binding"
        )
        for name in (
            "ledger_generation",
            "accounting_state_fingerprint",
            "ledger_head",
        ):
            if not getattr(self, name):
                raise ExecutionError(
                    Code.PLAN_INVALID,
                    f"an execution book binding must carry {name}; a binding that "
                    "cannot identify its book cannot detect the wrong one",
                )

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "ledger_generation": self.ledger_generation,
            "accounting_engine_version": self.accounting_engine_version,
            "accounting_state_fingerprint": self.accounting_state_fingerprint,
            "ledger_sequence": self.ledger_sequence,
            "ledger_head": self.ledger_head,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return self.body()

    def require_same_book(self, other: BookBinding) -> None:
        """Refuse a book that is not the one this plan authorises.

        The ledger *sequence* is deliberately not compared: it advances as this
        plan's own fills are admitted, and treating that as a mismatch would make
        every plan fail on its second order. Lineage and engine version are the
        identity of a book; the state fingerprint is checked separately, where
        the distinction between this plan's own progress and outside interference
        can be drawn.

        Args:
            other: The book being offered.

        Raises:
            ExecutionError: `EXECUTION_BOOK_MISMATCH` on a different lineage or
                engine version.
        """
        if self.ledger_generation != other.ledger_generation:
            raise ExecutionError(
                Code.EXECUTION_BOOK_MISMATCH,
                f"this plan is authorised against ledger generation "
                f"{self.ledger_generation!r} and was offered "
                f"{other.ledger_generation!r}. Two ledgers can hold the same "
                "sequence numbers, so executing against the wrong one would look "
                "entirely normal.",
            )
        if self.accounting_engine_version != other.accounting_engine_version:
            raise ExecutionError(
                Code.EXECUTION_BOOK_MISMATCH,
                f"this plan is authorised under "
                f"{self.accounting_engine_version} and the book now reports "
                f"{other.accounting_engine_version}; the same fills would produce "
                "different cost bases",
            )


@dataclass(frozen=True)
class ExecutionPlan:
    """The authorisation that turns approved targets into orders.

    Attributes:
        execution_plan_id: Stable identifier.
        as_of: The moment the plan describes, ISO 8601 with offset.
        approved_portfolio_fingerprint: The targets authorised.
        execution_intents_fingerprint: The deltas those targets implied.
        sizing_snapshot_fingerprint: What the quantities were derived from.
        valued_snapshot_fingerprint: What the portfolio was marked against.
        book: Which accounting book this is authorised against.
        orders: The planned orders, in execution sequence.
        engine_version: The execution engine that produced it.
        sequencing_rule: How the orders were ordered.
        attribution_rule: How a partial fill divides across strategies.
        created_at: When it was created. Operational, outside the fingerprint.
    """

    execution_plan_id: str
    as_of: str
    approved_portfolio_fingerprint: str
    execution_intents_fingerprint: str
    sizing_snapshot_fingerprint: str
    valued_snapshot_fingerprint: str
    book: BookBinding
    orders: tuple[Order, ...] = ()
    engine_version: str = EXECUTION_ENGINE_VERSION
    sequencing_rule: str = ORDER_SEQUENCING_RULE
    attribution_rule: str = FILL_ATTRIBUTION_RULE
    created_at: str | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        """Verify the plan binds everything it was computed from.

        Raises:
            ExecutionError: `PLAN_INVALID` if any binding or the id is absent, or
                if two orders share an id. A plan missing a binding authorises
                less than it appears to.
        """
        for name in (
            "execution_plan_id",
            "as_of",
            "approved_portfolio_fingerprint",
            "execution_intents_fingerprint",
            "sizing_snapshot_fingerprint",
            "valued_snapshot_fingerprint",
        ):
            if not getattr(self, name):
                raise ExecutionError(
                    Code.PLAN_INVALID,
                    f"an execution plan must bind {name}; without it the plan "
                    "authorises less than it appears to, and a mismatch at "
                    "execution time would be undetectable",
                )
        seen: set[str] = set()
        for order in self.orders:
            if order.order_id in seen:
                raise ExecutionError(
                    Code.PLAN_INVALID,
                    f"{order.order_id} appears twice in one plan; whichever was "
                    "executed last would decide the trade",
                )
            seen.add(order.order_id)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body. No wall clock inside.

        Order *content* is digested in the sequence the plan will execute,
        because that sequence is a semantic decision this engine made and a
        different one would be a different plan.
        """
        return {
            "engine_version": self.engine_version,
            "execution_plan_id": self.execution_plan_id,
            "as_of": self.as_of,
            "sequencing_rule": self.sequencing_rule,
            "attribution_rule": self.attribution_rule,
            "bindings": {
                "approved_portfolio": self.approved_portfolio_fingerprint,
                "execution_intents": self.execution_intents_fingerprint,
                "sizing_snapshot": self.sizing_snapshot_fingerprint,
                "valued_snapshot": self.valued_snapshot_fingerprint,
                "book": self.book.body(),
            },
            # Order *content*, not each order's back-reference to this plan:
            # the authorisation an order carries is this very digest, so
            # including it would make the two mutually recursive.
            "orders": [order.content_body() for order in self.orders],
        }

    def fingerprint(self) -> str:
        """The plan's deterministic digest. This is the authorisation."""
        return fingerprint_of(self.body())

    @property
    def order_count(self) -> int:
        """How many orders the plan authorises."""
        return len(self.orders)

    def order(self, order_id: str) -> Order:
        """One planned order by id.

        Args:
            order_id: The id.

        Returns:
            The order.

        Raises:
            ExecutionError: `ORDER_NOT_FOUND` if the plan does not contain it.
        """
        for candidate in self.orders:
            if candidate.order_id == order_id:
                return candidate
        raise ExecutionError(
            Code.ORDER_NOT_FOUND,
            f"{order_id} is not in plan {self.execution_plan_id}",
        )

    def require_authorizes(self, order: Order) -> None:
        """Refuse an order this plan did not authorise.

        Args:
            order: The order offered.

        Raises:
            ExecutionError: `UNAUTHORIZED_ORDER` if the order names another plan;
                `EXECUTION_AUTHORIZATION_MISMATCH` if it names this plan with a
                stale fingerprint, or if its content differs from what the plan
                holds under that id.
        """
        if order.execution_plan_id != self.execution_plan_id:
            raise ExecutionError(
                Code.UNAUTHORIZED_ORDER,
                f"{order.order_id} names plan {order.execution_plan_id!r} and this "
                f"is {self.execution_plan_id!r}. Every order reaching a broker "
                "originates from an authorised plan; there is no path that "
                "accepts one without.",
            )
        if order.authorization_fingerprint != self.fingerprint():
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                f"{order.order_id} carries authorisation "
                f"{order.authorization_fingerprint[:16]}… and this plan hashes to "
                f"{self.fingerprint()[:16]}…. The order was authorised by a "
                "different version of this plan.",
            )
        held = self.order(order.order_id)
        if held.fingerprint() != order.fingerprint():
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                f"{order.order_id} hashes to {order.fingerprint()[:16]}… but the "
                f"plan authorises {held.fingerprint()[:16]}… under that id",
            )

    def parent_for(self, identity_key: tuple[str, int]) -> Order:
        """The planned order a child works the residual of.

        Args:
            identity_key: The security's durable key.

        Returns:
            The planned order.

        Raises:
            ExecutionError: `UNAUTHORIZED_ORDER` if the plan authorises nothing
                for that security. A child for an unplanned security would be a
                trade the risk engine never approved.
        """
        for order in self.orders:
            if order.identity.key == identity_key:
                return order
        raise ExecutionError(
            Code.UNAUTHORIZED_ORDER,
            f"plan {self.execution_plan_id} authorises nothing for "
            f"{identity_key}, so no child order may work a residual in it",
        )

    def require_authorizes_child(self, child: Order, *, session: str, generation: int) -> None:
        """Refuse a child order this plan did not derive.

        A child is not in the plan's own order list — the number of children
        depends on fills that have not happened when the plan is made. So
        authorisation is by *derivation* instead: the child's id is a digest over
        the plan fingerprint, the security, the session, the generation and the
        authorised residual, and recomputing it here proves the child came from
        this plan working that residual in that session rather than from
        somewhere else.

        That check is what makes a hand-crafted child impossible to pass. Raising
        a child's quantity, moving it to another session or pointing it at
        another security all change the digest, so none of them can keep the id.

        Args:
            child: The child order offered.
            session: The session it claims to live for.
            generation: Which child it claims to be.

        Raises:
            ExecutionError: `UNAUTHORIZED_ORDER` if the child names another plan
                or an unplanned security, or if its side differs from the
                parent's; `EXECUTION_AUTHORIZATION_MISMATCH` if its
                authorisation is stale, its quantity exceeds the parent's, or
                its id is not the one this plan would derive.
        """
        from .session_broker import child_order_id

        if child.execution_plan_id != self.execution_plan_id:
            raise ExecutionError(
                Code.UNAUTHORIZED_ORDER,
                f"{child.order_id} names plan {child.execution_plan_id!r} and this "
                f"is {self.execution_plan_id!r}",
            )
        if child.authorization_fingerprint != self.fingerprint():
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                f"{child.order_id} carries authorisation "
                f"{child.authorization_fingerprint[:16]}… and this plan hashes to "
                f"{self.fingerprint()[:16]}…",
            )
        parent = self.parent_for(child.identity.key)
        if child.side is not parent.side:
            raise ExecutionError(
                Code.UNAUTHORIZED_ORDER,
                f"{child.order_id} is a {child.side} and the plan authorises a "
                f"{parent.side} in {child.identity.label}",
            )
        if child.quantity > parent.quantity:
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                f"{child.order_id} is for {child.quantity} and the plan authorises "
                f"{parent.quantity} in total for {child.identity.label}. A child "
                "may work a residual; it may not exceed the whole authorisation.",
            )
        expected = child_order_id(
            plan_fingerprint=self.fingerprint(),
            identity_label=child.identity.label,
            session=session,
            generation=generation,
            authorized_residual=child.quantity,
        )
        if child.order_id != expected:
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                f"{child.order_id} is not the child id this plan derives for "
                f"{child.identity.label} in session {session} generation "
                f"{generation} at quantity {child.quantity} (expected "
                f"{expected}). The id is a digest over those inputs, so a child "
                "that changed any of them cannot keep it.",
            )

    def require_matches_authority(
        self,
        *,
        approved_portfolio_fingerprint: str,
        execution_intents_fingerprint: str,
    ) -> None:
        """Refuse targets or intents other than those this plan was built from.

        Args:
            approved_portfolio_fingerprint: The targets offered.
            execution_intents_fingerprint: The intents offered.

        Raises:
            ExecutionError: `EXECUTION_AUTHORIZATION_MISMATCH` on either.
        """
        if approved_portfolio_fingerprint != self.approved_portfolio_fingerprint:
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                "the approved portfolio offered is not the one this plan was "
                f"computed from ({approved_portfolio_fingerprint[:16]}… against "
                f"{self.approved_portfolio_fingerprint[:16]}…)",
            )
        if execution_intents_fingerprint != self.execution_intents_fingerprint:
            raise ExecutionError(
                Code.EXECUTION_AUTHORIZATION_MISMATCH,
                "the execution intents offered are not the ones this plan was "
                f"computed from ({execution_intents_fingerprint[:16]}… against "
                f"{self.execution_intents_fingerprint[:16]}…)",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the plan as a JSON-safe mapping."""
        return {
            **self.body(),
            "plan_fingerprint": self.fingerprint(),
            "created_at": self.created_at,
            "order_count": self.order_count,
            "orders_detail": [order.as_json() for order in self.orders],
            "note": (
                "a plan authorises orders; it does not create or submit them. "
                "Submitting is an explicit, separate call."
            ),
        }


def intents_fingerprint(intents: Sequence[Any]) -> str:
    """Digest a set of execution intents, independent of input order.

    The intents describe a set of required changes, and the order a caller
    happened to list them in is not part of that set. Sorting by durable identity
    before digesting means the same portfolio delta produces the same
    authorisation however it was assembled — which is what makes a plan
    comparable across runs.

    Args:
        intents: The `portfolio_risk.ExecutionIntent` values.

    Returns:
        The digest.
    """
    rows = sorted(
        (
            {
                "identity": intent.identity.label,
                "action": str(intent.action),
                "current_weight": intent.current_weight,
                "target_weight": intent.target_weight,
                "notional_delta": intent.notional_delta,
                "strategy_id": intent.strategy_id,
            }
            for intent in intents
        ),
        key=lambda row: (row["identity"], str(row["strategy_id"] or "")),
    )
    return fingerprint_of({"intents": rows})
