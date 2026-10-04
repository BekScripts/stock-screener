"""What execution did, reported without claiming what it earned.

These reports say which orders were placed, what filled, at what prices and for
what fees. They deliberately contain no realised P&L, no unrealised P&L and no
NAV, because those are accounting's figures and a second copy of them would
eventually disagree with the first. An execution report that computed its own
P&L would be a second set of books wearing a different name.

Fill prices are averaged by volume. A plain mean of fill prices would misreport a
one-share fill at 50 alongside a 999-share fill at 10 as 30 — out by a factor of
three on a number people read to judge execution quality.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import is_zero, quantise_price, quantise_quantity

from .fingerprint import fingerprint_of
from .state_machine import OrderState
from .versions import EXECUTION_ENGINE_VERSION

if TYPE_CHECKING:
    from .authorization import ExecutionPlan, PlanStatus
    from .events import ExecutionFill
    from .journal import OrderLedger
    from .state_machine import OrderStatus

__all__ = ["ExecutionPlanReport", "OrderReport", "build_plan_report"]


@dataclass(frozen=True)
class OrderReport:
    """What one order did.

    Attributes:
        body: The report's content, already JSON-safe.
    """

    body: dict[str, Any]

    def fingerprint(self) -> str:
        """The report's deterministic digest."""
        return fingerprint_of(self.body)

    def as_json(self) -> dict[str, Any]:
        """Return the report with its fingerprint."""
        return {**self.body, "report_fingerprint": self.fingerprint()}


@dataclass(frozen=True)
class ExecutionPlanReport:
    """What a whole plan did.

    Attributes:
        body: The report's content, already JSON-safe.
    """

    body: dict[str, Any]

    def fingerprint(self) -> str:
        """The report's deterministic digest."""
        return fingerprint_of(self.body)

    def as_json(self) -> dict[str, Any]:
        """Return the report with its fingerprint."""
        return {**self.body, "report_fingerprint": self.fingerprint()}


def _order_report(
    status: OrderStatus,
    *,
    fills: tuple[ExecutionFill, ...],
    applications: dict[str, str],
) -> OrderReport:
    """Build one order's report.

    Args:
        status: The order's derived state.
        fills: Its fills, in id order.
        applications: Whether each fill reached the book.

    Returns:
        The report.
    """
    order = status.order
    return OrderReport(
        body={
            "order_id": order.order_id,
            "identity": order.identity.label,
            "side": str(order.side),
            "order_type": str(order.order_type),
            "time_in_force": str(order.time_in_force),
            "sequence_group": order.sequence_group,
            "ordered_quantity": str(order.quantity),
            "sizing_price": str(order.sizing_price),
            "planned_notional": str(order.notional),
            "filled_quantity": str(status.cumulative_filled_quantity),
            "remaining_quantity": str(status.remaining_quantity),
            "vwap_fill_price": str(status.vwap) if status.vwap is not None else None,
            "filled_notional": str(quantise_price(status.cumulative_notional)),
            "total_fees": str(quantise_price(status.cumulative_fee)),
            "fill_count": status.fill_count,
            "terminal_state": str(status.state),
            "is_terminal": status.is_terminal,
            "reason": status.reason,
            "lifecycle": [
                {
                    "fill_id": fill.fill_id,
                    "quantity": str(fill.quantity),
                    "price": str(fill.price),
                    "fee": str(fill.fee),
                    "effective_at": fill.effective_at,
                    "accounting_event_id": fill.accounting_event_id,
                    "application": applications.get(fill.fill_id, "PENDING"),
                    "attribution": [row.as_json() for row in fill.attribution],
                }
                for fill in fills
            ],
            "note": (
                "fill prices are volume-weighted. No P&L appears here: realised "
                "and unrealised P&L and NAV are accounting's figures, and a "
                "second copy would eventually disagree with the first."
            ),
        }
    )


def build_plan_report(
    plan: ExecutionPlan,
    derived: OrderLedger,
    *,
    status: PlanStatus,
    initial_accounting_fingerprint: str,
    final_accounting_fingerprint: str,
    execution_snapshot_fingerprints: tuple[str, ...] = (),
    drift_detected: bool = False,
) -> ExecutionPlanReport:
    """Build the report for a whole plan.

    Args:
        plan: The plan.
        derived: The order state replayed from the journal.
        status: The plan's settled status.
        initial_accounting_fingerprint: The book before the plan ran.
        final_accounting_fingerprint: The book afterwards.
        execution_snapshot_fingerprints: Which paper snapshots were filled
            against, so a reader can see what prices were supplied.
        drift_detected: Whether an unrelated book change stopped the plan.

    Returns:
        The report.
    """
    reports: list[OrderReport] = []
    applications = {key: str(value) for key, value in derived.applications.items()}
    planned_notional = Decimal(0)
    filled_notional = Decimal(0)
    total_fees = Decimal(0)
    counts = {"filled": 0, "partial": 0, "cancelled": 0, "rejected": 0, "open": 0}

    for order in plan.orders:
        status_row = derived.statuses.get(order.order_id)
        planned_notional = quantise_price(planned_notional + order.notional)
        if status_row is None:
            counts["open"] += 1
            continue
        fills = tuple(
            derived.fills[key]
            for key in sorted(derived.fills)
            if derived.fills[key].order_id == order.order_id
        )
        reports.append(_order_report(status_row, fills=fills, applications=applications))
        filled_notional = quantise_price(filled_notional + status_row.cumulative_notional)
        total_fees = quantise_price(total_fees + status_row.cumulative_fee)
        if status_row.state is OrderState.FILLED:
            counts["filled"] += 1
        elif status_row.state is OrderState.REJECTED:
            counts["rejected"] += 1
        elif status_row.state is OrderState.CANCELLED:
            counts["cancelled"] += 1
            if not is_zero(status_row.cumulative_filled_quantity):
                counts["partial"] += 1
        elif not status_row.is_terminal:
            counts["open"] += 1
            if not is_zero(status_row.cumulative_filled_quantity):
                counts["partial"] += 1

    return ExecutionPlanReport(
        body={
            "engine_version": EXECUTION_ENGINE_VERSION,
            "execution_plan_id": plan.execution_plan_id,
            "plan_fingerprint": plan.fingerprint(),
            "plan_status": str(status),
            "drift_detected": drift_detected,
            "sequencing_rule": plan.sequencing_rule,
            "attribution_rule": plan.attribution_rule,
            "order_count": plan.order_count,
            "orders_filled": counts["filled"],
            "orders_partially_filled": counts["partial"],
            "orders_cancelled": counts["cancelled"],
            "orders_rejected": counts["rejected"],
            "orders_open": counts["open"],
            "planned_notional": str(planned_notional),
            "filled_notional": str(filled_notional),
            "total_fees": str(total_fees),
            "filled_quantity_total": str(
                quantise_quantity(
                    sum(
                        (row.cumulative_filled_quantity for row in derived.statuses.values()),
                        Decimal(0),
                    )
                )
            ),
            "accounting": {
                "engine_version": plan.book.accounting_engine_version,
                "ledger_generation": plan.book.ledger_generation,
                "initial_state_fingerprint": initial_accounting_fingerprint,
                "final_state_fingerprint": final_accounting_fingerprint,
                "authority": (
                    "holdings, cash, NAV and P&L are accounting's alone. This "
                    "report names the book's state before and after so the two "
                    "can be reconciled, and duplicates none of its figures."
                ),
            },
            "journal": {
                "sequence": derived.journal_sequence,
                "head": derived.journal_head,
                "pending_fill_ids": [fill.fill_id for fill in derived.pending_fills],
            },
            "execution_snapshot_fingerprints": list(execution_snapshot_fingerprints),
            "orders": [row.as_json() for row in reports],
            "performance_claim": "NONE",
        }
    )
