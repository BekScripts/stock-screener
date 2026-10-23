"""Running a whole paper execution from a JSON request, for review and goldens.

The engine takes objects, not files. This is the seam that lets a person hand it
a fixture and read back exactly what would have been ordered, what filled and
what the book looks like afterwards — which is how a change to the lifecycle gets
reviewed, and what makes the golden outputs able to detect drift byte for byte.

It reaches no broker and opens no socket. Every price, every availability and
every fee is in the request.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import stock_screener.portfolio_accounting as accounting
from stock_screener.portfolio_risk import Action, ExecutionIntent, SecurityIdentity

from .authorization import BookBinding, PlanStatus
from .engine import ExecutionSession
from .errors import Code, ExecutionError
from .journal import ExecutionJournal
from .manifest import execution_manifest
from .paper_market import ExecutionPrice, PaperExecutionSnapshot
from .persistence import ExecutionStore
from .planner import ExecutionSizingSnapshot, SizingPrice, plan_execution
from .reports import build_plan_report
from .versions import REQUIRED_ACCOUNTING_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping
    from decimal import Decimal
    from pathlib import Path

__all__ = ["evaluate_request", "load_request", "run_paper_execution"]


def load_request(path: Path) -> dict[str, Any]:
    """Read a paper execution request from JSON.

    Args:
        path: The request file.

    Returns:
        The parsed request.

    Raises:
        ExecutionError: `PLAN_INVALID` if the file carries no opening book or no
            intents. A request with nothing to execute would report an empty plan
            as a success.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or "opening" not in body:
        raise ExecutionError(
            Code.PLAN_INVALID,
            f"{path.name} carries no opening book; a paper execution begins from an "
            "explicit opening balance or an existing V2 ledger, never from "
            "fabricated capital",
        )
    return body


def _identity(raw: Mapping[str, Any]) -> SecurityIdentity:
    """Build a durable identity from its JSON form."""
    return SecurityIdentity(
        kind=str(raw["kind"]),
        identity_id=int(raw["identity_id"]),
        symbol=raw.get("symbol"),
    )


def _decimal(value: Any) -> Decimal:
    """Parse a Decimal from its JSON form."""
    return accounting.as_decimal(value, field="request value")


def run_paper_execution(body: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    """Execute a paper plan end to end and return everything it produced.

    Args:
        body: The request: an `opening` book, `intents`, a `sizing` snapshot and
            an ordered list of `steps` saying which snapshot each order fills
            against and whether it is cancelled or rejected.
        root: Where the `.portfolio` and `.execution` stores live.

    Returns:
        The manifest, the plan, the journal state, the plan report and the final
        book.

    Raises:
        ExecutionError: Whatever the planner, the plan, the lifecycle or the book
            refuses.
    """
    opening = body["opening"]
    identities = {str(row["label"]): _identity(row) for row in body.get("securities", ())}

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
            reason_codes=tuple(row.get("reason_codes", ())),
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
                source_fingerprint=str(row.get("source_fingerprint", "")),
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

    store = ExecutionStore(root / ".execution", execution_plan_id=plan.execution_plan_id)
    session = ExecutionSession(
        plan=plan,
        store=store,
        ledger=ledger,
        accounting_store=accounting_store,
        journal=ExecutionJournal(),
    )

    snapshots: list[str] = []
    refusal: dict[str, Any] | None = None
    for step in body.get("steps", ()):
        order = plan.order(str(step["order_id"]))
        try:
            session.submit(order, at=str(step["submitted_at"]))
            acknowledged = str(step["submitted_at"])
            for leg in step.get("fills", ()):
                snapshot = PaperExecutionSnapshot(
                    as_of=str(leg["as_of"]),
                    prices=tuple(
                        ExecutionPrice(
                            identity=identities[str(row["identity"])],
                            price=_decimal(row["price"]),
                            available_quantity=(
                                None
                                if row.get("available_quantity") is None
                                else _decimal(row["available_quantity"])
                            ),
                            fee=_decimal(row.get("fee", "0")),
                            source_fingerprint=str(row.get("source_fingerprint", "")),
                        )
                        for row in leg.get("prices", ())
                    ),
                    source=str(leg.get("source", "UNSPECIFIED")),
                )
                snapshots.append(snapshot.fingerprint())
                session.fill(order, snapshot=snapshot, acknowledged_at=acknowledged)
            if step.get("reject_at"):
                session.reject(
                    order, at=str(step["reject_at"]), reason=str(step.get("reason", "REJECTED"))
                )
            if step.get("cancel_at"):
                session.cancel(
                    order, at=str(step["cancel_at"]), reason=str(step.get("reason", "CANCELLED"))
                )
        except ExecutionError as exc:
            # Recorded rather than raised: a plan that stops part-way is a real
            # outcome, and the report has to be able to say which order stopped
            # it and why. Raising would lose the fills that did happen.
            refusal = {"order_id": order.order_id, "code": exc.code, "detail": exc.detail}
            session.status = PlanStatus.FAILED
            break

    status = session.settle() if refusal is None else PlanStatus.FAILED
    final_state = accounting.replay(ledger, accounting_version=REQUIRED_ACCOUNTING_VERSION)
    final_fingerprint = accounting.state_fingerprint(final_state)
    derived = session.journal.derive()
    report = build_plan_report(
        plan,
        derived,
        status=status,
        initial_accounting_fingerprint=initial_fingerprint,
        final_accounting_fingerprint=final_fingerprint,
        execution_snapshot_fingerprints=tuple(dict.fromkeys(snapshots)),
        drift_detected=session.drift_detected,
    )
    anchor = store.read_anchor()
    # The store's own report carries its absolute root, which is useful to a
    # person and meaningless in a golden: it differs between runs and machines
    # and describes where the files are rather than what they say.
    store_report = {key: value for key, value in store.verify().items() if key != "root"}
    return {
        "manifest": execution_manifest(
            execution_plan_fingerprint=plan.fingerprint(),
            journal_generation=store.generation,
            journal_sequence=len(session.journal),
            journal_head=session.journal.head_hash,
            journal_anchor_fingerprint=anchor.fingerprint() if anchor else None,
            accounting_generation=accounting_store.generation,
            accounting_version=accounting_store.accounting_version,
            initial_accounting_state=initial_fingerprint,
            final_accounting_state=final_fingerprint,
            paper_snapshot_fingerprints=tuple(dict.fromkeys(snapshots)),
            plan_report_fingerprint=report.fingerprint(),
            code_commit=body.get("code_commit"),
        ),
        "plan": plan.as_json(),
        "journal": session.journal.as_json(),
        "order_state": derived.as_json(),
        "plan_report": report.as_json(),
        "final_book": final_state.as_json(),
        "store": store_report,
        "refusal": refusal,
    }


def evaluate_request(body: Mapping[str, Any], *, root: Path) -> dict[str, Any]:
    """Alias for `run_paper_execution`, matching the other packages' naming.

    Args:
        body: The request.
        root: Where the stores live.

    Returns:
        Everything the execution produced.
    """
    return run_paper_execution(body, root=root)
