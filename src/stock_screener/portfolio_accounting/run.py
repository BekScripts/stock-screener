"""Loading a ledger and a valuation from JSON, for inspection and goldens.

The engine takes objects, not files. This module is the seam that lets a person
hand it a fixture and read back the books that result — which is how a policy
change gets reviewed, and what makes the golden outputs able to detect semantic
drift byte for byte.

It loads, replays and reports. It appends nothing to a real ledger, it reaches no
broker, and it never writes outside a path the caller named.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .accounting import replay
from .anchor import state_fingerprint
from .attribution import attribute_portfolio
from .errors import AccountingError, Code
from .fingerprint import accounting_manifest
from .ledger import PortfolioLedger
from .persistence import event_from_json
from .pnl import value_portfolio
from .reconciliation import reconcile, statement_from
from .valuation import marks_from
from .versions import resolve_version

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = ["evaluate_request", "load_ledger", "load_request"]


def load_ledger(events: Sequence[Mapping[str, Any]]) -> PortfolioLedger:
    """Build a ledger by appending events in their given order.

    Args:
        events: Event bodies in the form `PortfolioEvent.as_json` writes, each
            with its `event_type` and `payload`.

    Returns:
        The ledger.

    Raises:
        AccountingError: Whatever the ledger refuses — a reused id, a regressed
            effective time, a missing opening balance.
    """
    ledger = PortfolioLedger()
    for body in events:
        ledger.append(event_from_json(body))
    return ledger


def load_request(path: Path) -> dict[str, Any]:
    """Read an accounting request from JSON.

    Args:
        path: The request file.

    Returns:
        The parsed request.

    Raises:
        AccountingError: If the file does not carry an `events` list. An empty
            ledger has no books to report, and silently reporting zeroes would
            look like a portfolio that holds nothing rather than a request that
            said nothing.
    """
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or not body.get("events"):
        raise AccountingError(
            Code.LEDGER_EMPTY,
            f"{path.name} carries no events. A request with no ledger produces "
            "zeroes that look like a portfolio holding nothing, which is why it is "
            "refused rather than reported.",
        )
    return body


def _pool_identities(state: Any, valued: Any) -> dict[str, Any]:
    """Prove the pool totals equal the portfolio totals, in the output itself.

    The identities are enforced in code and raise when they fail. Restating them
    in the artefact means a reader of a golden file can check them by hand rather
    than trusting that a test ran.

    Args:
        state: The replayed state.
        valued: The valuation.

    Returns:
        Each identity, both sides, and whether it holds.
    """
    from .money import ZERO, quantise_cash

    pools = state.cost_pools()
    marks = {row.identity.key: row.mark_price for row in valued.positions}
    pool_basis = quantise_cash(sum((p.total_cost_basis for p in pools), ZERO))
    pool_realised = quantise_cash(sum((p.realised_trading_pnl_gross for p in pools), ZERO))
    pool_unrealised = quantise_cash(
        sum(
            (
                p.unrealised_pnl(marks[p.identity.key])
                for p in pools
                if p.is_open and p.identity.key in marks
            ),
            ZERO,
        )
    )
    pool_value = quantise_cash(
        sum(
            (
                p.market_value(marks[p.identity.key])
                for p in pools
                if p.is_open and p.identity.key in marks
            ),
            ZERO,
        )
    )
    return {
        "convention": state.version.basis_policy,
        "pool_count": len(pools),
        "cost_basis": {
            "pools": str(pool_basis),
            "global": str(state.total_cost_basis),
            "holds": pool_basis == state.total_cost_basis,
        },
        "realised_trading_pnl_gross": {
            "pools": str(pool_realised),
            "global": str(quantise_cash(state.realised_pnl)),
            "holds": pool_realised == quantise_cash(state.realised_pnl),
        },
        "unrealised_pnl": {
            "pools": str(pool_unrealised),
            "global": str(quantise_cash(valued.pnl.unrealised)),
            "holds": pool_unrealised == quantise_cash(valued.pnl.unrealised),
        },
        "market_value": {
            "pools": str(pool_value),
            "global": str(quantise_cash(valued.market_value)),
            "holds": pool_value == quantise_cash(valued.market_value),
        },
    }


def evaluate_request(body: Mapping[str, Any]) -> dict[str, Any]:
    """Replay a ledger, value it, attribute it and reconcile it.

    Args:
        body: The request: `events`, an optional `valuation` block with
            `valuation_time` and `marks`, and an optional `external_statement`.

    Returns:
        The books, as a JSON-safe mapping. Every identity is checked in the
        course of producing it, so a returned result is one where they held.

    Raises:
        AccountingError: Whatever the ledger, valuation or identities refuse.
    """
    version = resolve_version(body.get("accounting_version"))
    ledger = load_ledger(body["events"])
    state = replay(ledger, accounting_version=version)
    result: dict[str, Any] = {
        "manifest": accounting_manifest(
            accounting_version=str(version),
            ledger_generation=body.get("ledger_generation"),
            ledger_sequence=len(ledger),
            ledger_head=ledger.head_hash,
            state_fingerprint=state_fingerprint(state),
            code_commit=body.get("code_commit"),
        ),
        "ledger": {
            "record_count": len(ledger),
            "head_hash": ledger.head_hash,
            "verification": ledger.verify(),
        },
        "state": state.as_json(),
        "valuation": None,
        "attribution": None,
        "reconciliation": None,
    }

    valuation = body.get("valuation")
    if valuation:
        snapshot = marks_from(
            valuation.get("marks", ()),
            valuation_time=str(valuation["valuation_time"]),
            staleness_limit_hours=valuation.get("staleness_limit_hours"),
        )
        valued = value_portfolio(
            state, snapshot, require_complete=bool(valuation.get("require_complete", True))
        )
        result["valuation"] = valued.as_json()
        if version.uses_cost_pools:
            result["pool_identities"] = _pool_identities(state, valued)
        result["marks"] = snapshot.as_json()
        result["attribution"] = attribute_portfolio(state, valued).as_json()

    external = body.get("external_statement")
    if external:
        statement = statement_from(
            external.get("positions", ()),
            as_of=str(external["as_of"]),
            source=str(external.get("source", "EXTERNAL")),
            cash=external["cash"],
            complete=bool(external.get("complete", True)),
        )
        tolerance = external.get("cash_tolerance")
        from .money import as_decimal

        result["reconciliation"] = reconcile(
            state,
            statement,
            cash_tolerance=(
                as_decimal(tolerance, field="cash tolerance") if tolerance is not None else None
            ),
        ).as_json()
        result["external_statement"] = statement.as_json()

    return result
