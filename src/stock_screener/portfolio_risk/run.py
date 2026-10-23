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

from .allocation import StrategyAllocationPolicy, StrategyBudget
from .drawdown import DrawdownBand
from .engine import evaluate
from .intents import Direction, StrategyIntent
from .models import PortfolioSnapshot, Position, SecurityIdentity
from .policy import ConstructionMode, MissingMetricPolicy, PolicyMode, RiskPolicy
from .risk_inputs import (
    CovarianceSnapshot,
    MetricState,
    PortfolioRiskSnapshot,
    SecurityRiskMetrics,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from .engine import PortfolioDecision

__all__ = ["evaluate_request", "load_allocation", "load_request", "load_risk"]


def _optional_float(body: Mapping[str, Any], key: str) -> float | None:
    """Read an optional numeric policy field without defaulting it to a value.

    A risk limit that quietly defaults to a number is the limit nobody chose, so
    an absent key stays absent rather than becoming zero.

    Args:
        body: The policy section.
        key: The field name.

    Returns:
        The value as a float, or None when the key is absent or null.
    """
    value = body.get(key)
    return None if value is None else float(value)


def _identity(raw: Mapping[str, Any]) -> SecurityIdentity:
    """Build an identity from its JSON form."""
    return SecurityIdentity(
        kind=str(raw["kind"]),
        identity_id=int(raw["identity_id"]),
        symbol=raw.get("symbol"),
    )


def load_allocation(body: Mapping[str, Any] | None) -> StrategyAllocationPolicy | None:
    """Build the strategy allocation policy from a request's `allocation` section.

    Args:
        body: The `allocation` section, or None for the single-strategy path.

    Returns:
        The policy, or None.
    """
    if not body:
        return None
    return StrategyAllocationPolicy(
        allocation_policy_id=str(body["allocation_policy_id"]),
        allocation_policy_version=str(body["allocation_policy_version"]),
        budgets=tuple(
            StrategyBudget(
                strategy_id=str(row["strategy_id"]),
                max_capital_weight=float(row["max_capital_weight"]),
                max_strategy_gross=_optional_float(row, "max_strategy_gross"),
                max_strategy_volatility=_optional_float(row, "max_strategy_volatility"),
                priority=float(row.get("priority", 0.0)),
            )
            for row in body["budgets"]
        ),
        production_authorized=bool(body.get("production_authorized", False)),
    )


def load_risk(body: Mapping[str, Any] | None) -> PortfolioRiskSnapshot | None:
    """Build the market-risk inputs from a JSON request's `risk` section.

    Args:
        body: The `risk` section, or None when the request supplies none. None
            keeps V1 semantics.

    Returns:
        The risk snapshot, or None.
    """
    if not body:
        return None
    metrics = tuple(
        SecurityRiskMetrics(
            identity=_identity(row["identity"]),
            metric_as_of=str(row["metric_as_of"]),
            source_fingerprint=str(row["source_fingerprint"]),
            annualized_volatility=(
                None
                if row.get("annualized_volatility") is None
                else float(row["annualized_volatility"])
            ),
            volatility_state=MetricState(str(row.get("volatility_state", "UNKNOWN"))),
            average_daily_dollar_volume=(
                None
                if row.get("average_daily_dollar_volume") is None
                else float(row["average_daily_dollar_volume"])
            ),
            liquidity_state=MetricState(str(row.get("liquidity_state", "UNKNOWN"))),
            price=None if row.get("price") is None else float(row["price"]),
        )
        for row in body.get("metrics", ())
    )
    covariance = None
    raw = body.get("covariance")
    if raw:
        covariance = CovarianceSnapshot(
            identities=tuple(_identity(entry) for entry in raw["identities"]),
            matrix=tuple(tuple(float(value) for value in row) for row in raw["matrix"]),
            as_of=str(raw["as_of"]),
            annualization=str(raw.get("annualization", "252 sessions per year")),
            source_fingerprint=str(raw.get("source_fingerprint", "")),
        )
    return PortfolioRiskSnapshot(
        as_of=str(body["as_of"]),
        portfolio_nav=float(body["portfolio_nav"]),
        portfolio_peak_nav=float(body["portfolio_peak_nav"]),
        metrics=metrics,
        covariance=covariance,
        source_fingerprint=str(body.get("source_fingerprint", "")),
    )


def load_request(
    body: Mapping[str, Any],
) -> tuple[list[StrategyIntent], PortfolioSnapshot, RiskPolicy]:
    """Build the engine's three inputs from a JSON request.

    Args:
        body: `{"intents": [...], "snapshot": {...}, "policy": {...}}`, optionally
            with a `risk` section loaded separately by `load_risk`.

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
        max_positions=(
            None if policy_raw.get("max_positions") is None else int(policy_raw["max_positions"])
        ),
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
        max_portfolio_volatility=_optional_float(policy_raw, "max_portfolio_volatility"),
        max_position_adv_fraction=_optional_float(policy_raw, "max_position_adv_fraction"),
        max_trade_adv_fraction=_optional_float(policy_raw, "max_trade_adv_fraction"),
        max_risk_metric_age_seconds=_optional_float(policy_raw, "max_risk_metric_age_seconds"),
        correlation_threshold=_optional_float(policy_raw, "correlation_threshold"),
        max_correlated_pair_exposure=_optional_float(policy_raw, "max_correlated_pair_exposure"),
        missing_correlation_policy=MissingMetricPolicy(
            str(policy_raw.get("missing_correlation_policy", "REJECT_INCREASE"))
        ),
        drawdown_bands=tuple(
            DrawdownBand(
                threshold=float(band["threshold"]),
                max_gross_multiplier=float(band.get("max_gross_multiplier", 1.0)),
                mode=PolicyMode(str(band.get("mode", "NORMAL"))),
                label=str(band.get("label", "")),
            )
            for band in policy_raw.get("drawdown_bands", ())
        ),
        missing_volatility_policy=MissingMetricPolicy(
            str(policy_raw.get("missing_volatility_policy", "REJECT_INCREASE"))
        ),
        missing_liquidity_policy=MissingMetricPolicy(
            str(policy_raw.get("missing_liquidity_policy", "REJECT_INCREASE"))
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
    body = json.loads(path.read_text(encoding="utf-8"))
    intents, snapshot, policy = load_request(body)
    return evaluate(
        intents,
        snapshot,
        policy,
        risk=load_risk(body.get("risk")),
        allocation=load_allocation(body.get("allocation")),
        code_commit=code_commit,
    )
