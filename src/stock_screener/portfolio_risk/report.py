"""The risk report: what the engine changed, and why.

An approved portfolio alone is not auditable. A 6% position tells you nothing
about whether the strategy asked for 6%, asked for 25% and met a cap, or asked for
10% and met a liquidity ceiling — and those have different consequences for whoever
has to explain the book afterwards.

So the report carries both sides of every number: requested, constructed and
approved exposure; portfolio volatility before and after the ceiling; cash before
and after; and the names each control touched. It makes no performance claim and
estimates no outcome. It says what the risk authority did.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .arithmetic import quantise
from .fingerprint import digest

if TYPE_CHECKING:
    from .risk import RiskOutcome
    from .risk_inputs import PortfolioRiskSnapshot

__all__ = ["PortfolioRiskReport", "build_risk_report"]


@dataclass(frozen=True)
class PortfolioRiskReport:
    """Pre-risk and post-risk state, side by side.

    Attributes:
        body_fields: The report's content, already JSON-safe.
    """

    body_fields: dict[str, Any]

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return dict(sorted(self.body_fields.items()))

    def fingerprint(self) -> str:
        """The report's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the report with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


def build_risk_report(
    outcome: RiskOutcome,
    *,
    risk: PortfolioRiskSnapshot | None,
    requested_gross: float,
    requested_count: int,
    current_cash_weight: float,
) -> PortfolioRiskReport:
    """Summarise what the risk authority changed.

    Args:
        outcome: The risk outcome, carrying the pre-risk weights and diagnostics.
        risk: The market-risk inputs, when supplied.
        requested_gross: Gross exposure as the strategy asked for it.
        requested_count: How many names the strategy asked for.
        current_cash_weight: Cash before the decision.

    Returns:
        The report.
    """
    advanced = dict(outcome.advanced)
    liquidity = advanced.get("liquidity") or {}
    volatility = advanced.get("portfolio_volatility") or {}
    metrics = advanced.get("volatility_metrics") or {}
    effective = outcome.effective
    constructed_gross = quantise(sum(abs(value) for value in outcome.constructed.values()))

    per_position: list[dict[str, Any]] = []
    by_identity = risk.by_identity if risk is not None else {}
    for key, weight in sorted(outcome.approved.items()):
        row = by_identity.get(key)
        vol = row.annualized_volatility if row is not None else None
        per_position.append(
            {
                "identity": outcome.identities[key].label,
                "constructed_weight": outcome.constructed.get(key),
                "approved_weight": weight,
                "annualized_volatility": vol,
                "weight_times_volatility": (quantise(weight * vol) if vol is not None else None),
            }
        )

    covariance = risk.covariance if risk is not None else None
    diagnostics: dict[str, Any] = {}
    if covariance is not None and outcome.approved:
        priced = {key: w for key, w in outcome.approved.items() if key in covariance.index}
        if priced:
            diagnostics = {
                **covariance.correlation_diagnostics(list(priced)),
                "component_variance": covariance.component_variance(priced),
                "portfolio_variance": quantise(covariance.variance_of(priced)),
                "component_variance_sums_to_portfolio_variance": True,
                "diagnostics_only": "no correlation or risk-budget rule is enforced in P1.1",
            }

    return PortfolioRiskReport(
        body_fields={
            "report_id": "PORTFOLIO_RISK_REPORT_V1_1",
            "base_policy": outcome.policy.as_json(),
            "effective_policy": effective.as_json() if effective is not None else None,
            "risk_snapshot_fingerprint": (risk.fingerprint() if risk is not None else None),
            "covariance_fingerprint": (
                covariance.fingerprint() if covariance is not None else None
            ),
            "drawdown": effective.drawdown if effective is not None else None,
            "exposure": {
                "requested_gross": requested_gross,
                "constructed_gross": constructed_gross,
                "approved_gross": outcome.gross_exposure,
                "approved_net": outcome.net_exposure,
                "cash_before": quantise(current_cash_weight),
                "cash_after": outcome.cash_weight,
            },
            "counts": {
                "requested_positions": requested_count,
                "constructed_positions": len(outcome.constructed),
                "approved_positions": len(outcome.approved),
            },
            "portfolio_volatility_before_risk": volatility.get("portfolio_volatility_before"),
            "portfolio_volatility_after_risk": volatility.get("portfolio_volatility_after"),
            "volatility_scaling_applied": bool(volatility.get("applied")),
            "volatility_scale": volatility.get("scale"),
            "liquidity_constrained_positions": sorted(
                {
                    *liquidity.get("position_capacity_limited", ()),
                    *liquidity.get("trade_capacity_limited", ()),
                }
            ),
            "position_capacity_limited": liquidity.get("position_capacity_limited", []),
            "trade_capacity_limited": liquidity.get("trade_capacity_limited", []),
            "liquidity_unknown": liquidity.get("liquidity_unknown", []),
            "missing_risk_metric": metrics.get("missing_risk_metric", []),
            "per_position": per_position,
            "covariance_diagnostics": diagnostics,
            "reason_summary": _reason_summary(outcome),
            "makes_no_performance_claim": True,
            "estimates_no_market_impact": True,
        }
    )


def _reason_summary(outcome: RiskOutcome) -> dict[str, int]:
    """How many identities each reason code touched, in code order.

    Args:
        outcome: The risk outcome.

    Returns:
        `{reason code: count}`, sorted so the summary is comparable between runs.
    """
    counts: dict[str, int] = {}
    for decision in outcome.decisions:
        for code in decision.reason_codes:
            counts[code] = counts.get(code, 0) + 1
    return dict(sorted(counts.items()))
