"""Loading a decision request from JSON, for developer inspection.

The engine takes objects, not files. This module is the thin seam that lets a
human hand it a fixture and read back what would be authorised — useful for
reviewing a policy change, and the basis of the golden output that makes semantic
drift obvious.

It loads and validates; it decides nothing. And it reaches no broker: the result
is a description of a portfolio, not an instruction to trade one.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from .engine import evaluate
from .intents import Direction, StrategyIntent
from .models import PortfolioSnapshot, Position, SecurityIdentity
from .policy import ConstructionMode, PolicyMode, RiskPolicy

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .engine import PortfolioDecision

__all__ = ["evaluate_request", "load_request"]


def _identity(raw: Mapping[str, Any]) -> SecurityIdentity:
    """Build an identity from its JSON form."""
    return SecurityIdentity(
        kind=str(raw["kind"]),
        identity_id=int(raw["identity_id"]),
        symbol=raw.get("symbol"),
    )


def load_request(
    body: Mapping[str, Any],
) -> tuple[list[StrategyIntent], PortfolioSnapshot, RiskPolicy]:
    """Build the engine's three inputs from a JSON request.

    Args:
        body: `{"intents": [...], "snapshot": {...}, "policy": {...}}`.

    Returns:
        The intents, the snapshot and the policy.

    Raises:
        KeyError: If a required section or field is absent. A partially specified
            request is refused rather than defaulted, because a defaulted risk
            limit is the one nobody meant.
    """
    snapshot_raw = body["snapshot"]
    snapshot = PortfolioSnapshot(
        as_of=str(snapshot_raw["as_of"]),
        cash=float(snapshot_raw["cash"]),
        positions=tuple(
            Position(
                identity=_identity(row["identity"]),
                quantity=float(row["quantity"]),
                mark_price=float(row["mark_price"]),
                cost_basis=row.get("cost_basis"),
                strategy_id=row.get("strategy_id"),
            )
            for row in snapshot_raw.get("positions", ())
        ),
        price_fingerprint=snapshot_raw.get("price_fingerprint"),
    )
    policy_raw = body["policy"]
    policy = RiskPolicy(
        policy_id=str(policy_raw["policy_id"]),
        policy_version=str(policy_raw["policy_version"]),
        mode=PolicyMode(str(policy_raw.get("mode", "NORMAL"))),
        construction_mode=ConstructionMode(
            str(policy_raw.get("construction_mode", "PRESERVE_ABSOLUTE_WEIGHTS"))
        ),
        long_only=bool(policy_raw.get("long_only", True)),
        max_gross_exposure=float(policy_raw["max_gross_exposure"]),
        max_net_exposure=(
            None
            if policy_raw.get("max_net_exposure") is None
            else float(policy_raw["max_net_exposure"])
        ),
        max_position_weight=float(policy_raw["max_position_weight"]),
        max_positions=int(policy_raw["max_positions"]),
        min_cash_weight=float(policy_raw["min_cash_weight"]),
        max_trade_notional_weight=(
            None
            if policy_raw.get("max_trade_notional_weight") is None
            else float(policy_raw["max_trade_notional_weight"])
        ),
        min_position_weight=(
            None
            if policy_raw.get("min_position_weight") is None
            else float(policy_raw["min_position_weight"])
        ),
        production_authorized=bool(policy_raw.get("production_authorized", False)),
    )
    intents = [
        StrategyIntent(
            strategy_id=str(row["strategy_id"]),
            strategy_version=str(row["strategy_version"]),
            decision_id=str(row["decision_id"]),
            as_of=str(row["as_of"]),
            identity=_identity(row["identity"]),
            direction=Direction(str(row.get("direction", "LONG"))),
            requested_weight=(
                None if row.get("requested_weight") is None else float(row["requested_weight"])
            ),
            requested_notional=(
                None if row.get("requested_notional") is None else float(row["requested_notional"])
            ),
            priority=float(row.get("priority", 0.0)),
            reason=row.get("reason"),
            source_fingerprint=row.get("source_fingerprint"),
        )
        for row in body.get("intents", ())
    ]
    return intents, snapshot, policy


def evaluate_request(path: Path, *, code_commit: str | None = None) -> PortfolioDecision:
    """Load a request from a JSON file and evaluate it.

    Args:
        path: The request file.
        code_commit: The commit evaluating, recorded on the manifest.

    Returns:
        The decision.
    """
    intents, snapshot, policy = load_request(json.loads(path.read_text(encoding="utf-8")))
    return evaluate(intents, snapshot, policy, code_commit=code_commit)
