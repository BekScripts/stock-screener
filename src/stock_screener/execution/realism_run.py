"""Running a V1.1 multi-session paper execution from a JSON request.

The seam that lets a person hand the engine a fixture and read back every child
order, every fill, every cost and the resulting book. It fetches nothing: every
quote, volume, fee, session and policy value is in the request, which is what
makes a golden output able to detect drift byte for byte.

Kept separate from `run.py` so the V1 path is untouched. A V1 request has no
policy, no calendar and no session snapshots, and it continues to execute under
V1's reference-price model exactly as P2.0 froze it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import stock_screener.portfolio_accounting as accounting
from stock_screener.portfolio_risk import Action, ExecutionIntent, SecurityIdentity

from .authorization import BookBinding, PlanStatus
from .calendar import ExecutionCalendar
from .errors import Code, ExecutionError
from .journal import ExecutionJournal
from .manifest import realism_manifest
from .persistence import ExecutionStore
from .planner import ExecutionSizingSnapshot, SizingPrice, plan_execution
from .realism_policy import PaperExecutionPolicy
from .session_engine import SessionExecutionSession
from .session_market import PaperMarketSessionSnapshot, quotes_from
from .versions import REQUIRED_ACCOUNTING_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal
    from pathlib import Path

__all__ = ["load_realism_request", "run_session_execution"]


def load_realism_request(path: Path) -> dict[str, Any]:
    """Read a V1.1 paper execution request from JSON.

    Args:
        path: The request file.

    Returns:
        The parsed request.

    Raises:
        ExecutionError: `PLAN_INVALID` if the file carries no opening book,
            policy, calendar or market sessions. Each is required because
            nothing is defaulted: a missing policy would mean undeclared
            friction, and a missing calendar would mean rollover by date
            arithmetic.
    """
    body: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    for key in ("opening", "execution_policy", "calendar", "market_sessions"):
        if not isinstance(body, dict) or key not in body:
            raise ExecutionError(
                Code.PLAN_INVALID,
                f"{path.name} carries no {key!r}. Nothing here is defaulted: a "
                "missing policy would mean undeclared friction and a missing "
                "calendar would mean rollover by date arithmetic.",
            )
    return body


def _decimal(value: Any) -> Decimal:
    """Parse a Decimal from its JSON form."""
    return accounting.as_decimal(value, field="request value")


def run_session_execution(body: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    """Execute a V1.1 multi-session paper plan and return everything it produced.

    Args:
        body: The request: an `opening` book, `execution_policy`, `calendar`,
            `intents`, `sizing` and `market_sessions`.
        root: Where the `.portfolio` and `.execution` stores live.

    Returns:
        The manifest, the plan, the child orders, the costs, the realism report
        and the final book.

    Raises:
        ExecutionError: Whatever the policy, the planner, the market, the
            lifecycle or the book refuses.
    """
    identities = {
        str(row["label"]): SecurityIdentity(
            kind=str(row["kind"]),
            identity_id=int(row["identity_id"]),
            symbol=row.get("symbol"),
        )
        for row in body.get("securities", ())
    }
    policy_body = body["execution_policy"]
    policy = PaperExecutionPolicy(
        policy_id=str(policy_body["policy_id"]),
        adverse_slippage_bps=_decimal(policy_body.get("adverse_slippage_bps", "0")),
        max_volume_participation=_decimal(policy_body.get("max_volume_participation", "0.1")),
        maximum_execution_sessions=int(policy_body.get("maximum_execution_sessions", 3)),
        spread_required=bool(policy_body.get("spread_required", True)),
        volume_required=bool(policy_body.get("volume_required", True)),
        production_authorized=False,
    )
    calendar = ExecutionCalendar.of(
        body["calendar"]["sessions"], name=str(body["calendar"].get("name", "UNSPECIFIED"))
    )

    opening = body["opening"]
    accounting_store = accounting.PortfolioStore(
        root / ".portfolio", accounting_version=REQUIRED_ACCOUNTING_VERSION
    )
    ledger = accounting.PortfolioLedger()
    accounting_store.commit(
        ledger,
        accounting.OpeningBalanceEvent(
            event_id=str(opening["event_id"]),
            event_type=accounting.EventType.OPENING_BALANCE,
            effective_at=str(opening["effective_at"]),
            source_id=str(opening.get("source_id", "PAPER_BOOK_IMPORT")),
            source_fingerprint=str(opening.get("source_fingerprint", "fp-opening")),
            cash=_decimal(opening["cash"]),
            positions=tuple(
                accounting.OpeningPosition(
                    identity=identities[str(row["identity"])],
                    quantity=_decimal(row["quantity"]),
                    cost_basis_per_share=_decimal(row["cost_basis_per_share"]),
                    attribution=tuple(
                        accounting.StrategySlice(
                            strategy_id=str(slice_["strategy_id"]),
                            quantity=_decimal(slice_["quantity"]),
                        )
                        for slice_ in row.get("attribution", ())
                    ),
                )
                for row in opening.get("positions", ())
            ),
        ),
    )
    initial_state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
    initial_fingerprint = accounting.state_fingerprint(initial_state)

    intents = [
        ExecutionIntent(
            identity=identities[str(row["identity"])],
            action=Action(str(row["action"])),
            current_weight=float(row["current_weight"]),
            target_weight=float(row["target_weight"]),
            notional_delta=float(row["notional_delta"]),
            strategy_id=row.get("strategy_id"),
        )
        for row in body.get("intents", ())
    ]
    sizing_body = body["sizing"]
    sizing = ExecutionSizingSnapshot(
        as_of=str(sizing_body["as_of"]),
        nav=_decimal(sizing_body["nav"]),
        cash=_decimal(sizing_body["cash"]),
        prices=tuple(
            SizingPrice(
                identity=identities[str(row["identity"])],
                price=_decimal(row["price"]),
            )
            for row in sizing_body.get("prices", ())
        ),
        source=str(sizing_body.get("source", "UNSPECIFIED")),
    )
    plan = plan_execution(
        execution_plan_id=str(body["execution_plan_id"]),
        as_of=str(body["as_of"]),
        intents=intents,
        sizing=sizing,
        book=BookBinding(
            ledger_generation=accounting_store.generation,
            accounting_engine_version=REQUIRED_ACCOUNTING_VERSION,
            accounting_state_fingerprint=initial_fingerprint,
            ledger_sequence=len(ledger),
            ledger_head=ledger.head_hash,
        ),
        approved_portfolio_fingerprint=str(body["approved_portfolio_fingerprint"]),
        valued_snapshot_fingerprint=str(body["valued_snapshot_fingerprint"]),
        attribution={
            str(key): tuple((str(name), _decimal(value)) for name, value in rows)
            for key, rows in body.get("attribution", {}).items()
        }
        or None,
        created_at=str(body.get("created_at", "")) or None,
    )

    snapshots = {
        str(row["session"]): PaperMarketSessionSnapshot(
            session=str(row["session"]),
            as_of=str(row["as_of"]),
            quotes=quotes_from(row.get("quotes", ()), identities=identities),
            source=str(row.get("source", "UNSPECIFIED")),
        )
        for row in body["market_sessions"]
    }
    available = {
        str(key): {str(session): _decimal(value) for session, value in rows.items()}
        for key, rows in body.get("available_quantity", {}).items()
    }

    store = ExecutionStore(root / ".execution", execution_plan_id=plan.execution_plan_id)
    session = SessionExecutionSession(
        plan=plan,
        policy=policy,
        calendar=calendar,
        store=store,
        ledger=ledger,
        accounting_store=accounting_store,
        journal=ExecutionJournal(),
    )

    first_session = str(body.get("first_session", calendar.sessions[0]))
    refusal: dict[str, Any] | None = None
    per_identity: dict[str, list[dict[str, Any]]] = {}
    for order in plan.orders:
        try:
            outcomes = session.execute_identity(
                order,
                snapshots=snapshots,
                first_session=first_session,
                available=available.get(order.identity.label),
            )
            per_identity[order.identity.label] = [row.as_json() for row in outcomes]
        except ExecutionError as exc:
            # Recorded rather than raised: a plan that stops part-way is a real
            # outcome and the report has to say which order stopped it. Raising
            # would lose the fills that did happen.
            refusal = {
                "order_id": order.order_id,
                "code": exc.code,
                "detail": exc.detail,
            }
            session.status = PlanStatus.FAILED
            break

    status = session.settle() if refusal is None else PlanStatus.FAILED
    final_state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
    final_fingerprint = accounting.state_fingerprint(final_state)
    costs = session.cost_report()
    realism = session.realism_report()
    anchor = store.read_anchor()
    store_report = {key: value for key, value in store.verify().items() if key != "root"}
    return {
        "manifest": realism_manifest(
            policy=policy,
            calendar=calendar,
            execution_plan_fingerprint=plan.fingerprint(),
            journal_generation=store.generation,
            journal_sequence=len(session.journal),
            journal_head=session.journal.head_hash,
            journal_anchor_fingerprint=anchor.fingerprint() if anchor else None,
            accounting_generation=accounting_store.generation,
            accounting_version=accounting_store.accounting_version,
            initial_accounting_state=initial_fingerprint,
            final_accounting_state=final_fingerprint,
            session_snapshot_fingerprints=tuple(
                snapshots[key].fingerprint() for key in sorted(snapshots)
            ),
            cost_report_fingerprint=costs.fingerprint(),
            realism_report_fingerprint=str(realism["realism_report_fingerprint"]),
            code_commit=body.get("code_commit"),
        ),
        "plan": plan.as_json(),
        "session_outcomes": per_identity,
        "order_state": session.journal.derive().as_json(),
        "cost_report": costs.as_json(),
        "realism_report": realism,
        "final_book": final_state.as_json(),
        "store": store_report,
        "plan_status": str(status),
        "refusal": refusal,
    }
