"""Fail closed on malformed strategy output.

Every check here could instead be a quiet repair, and every quiet repair would be
worse. Two intents for one security could be summed; a negative weight under a
long-only policy could be clamped; a missing fingerprint could be filled in with
a placeholder. Each of those turns a strategy bug into a position, and the
position is the thing nobody notices.

So the rule is uniform: a request that could not have been meant as stated is
refused, with a machine-readable code, and no portfolio is authorised. "Best
effort" is not a mode this layer has.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .arithmetic import quantise

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .intents import StrategyIntent
    from .models import PortfolioSnapshot
    from .policy import RiskPolicy

__all__ = [
    "REASON_CODES",
    "ReasonCode",
    "ValidationError",
    "validate_inputs",
]


class ValidationError(ValueError):
    """Raised when inputs cannot authorise any portfolio."""

    def __init__(self, code: str, detail: str) -> None:
        """Carry the machine-readable code alongside the prose.

        Args:
            code: The stable reason code.
            detail: What went wrong, for a human.
        """
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class ReasonCode:
    """Stable codes for every refusal and adjustment this layer makes.

    Prose changes; these do not. A monitor counting `MAX_POSITION_WEIGHT` events
    must keep working after someone improves a message.
    """

    # input validity
    INVALID_INTENT = "INVALID_INTENT"
    DUPLICATE_STRATEGY_INTENT = "DUPLICATE_STRATEGY_INTENT"
    MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED = "MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED"
    MISSING_SOURCE_FINGERPRINT = "MISSING_SOURCE_FINGERPRINT"
    AS_OF_MISMATCH = "AS_OF_MISMATCH"
    DECISION_ID_MISMATCH = "DECISION_ID_MISMATCH"
    NORMALIZATION_IMPOSSIBLE = "NORMALIZATION_IMPOSSIBLE"
    INVALID_PORTFOLIO_SNAPSHOT = "INVALID_PORTFOLIO_SNAPSHOT"
    RISK_POLICY_UNAVAILABLE = "RISK_POLICY_UNAVAILABLE"

    # risk limits
    LONG_ONLY_VIOLATION = "LONG_ONLY_VIOLATION"
    MAX_POSITION_WEIGHT = "MAX_POSITION_WEIGHT"
    MAX_POSITION_COUNT = "MAX_POSITION_COUNT"
    MAX_GROSS_EXPOSURE = "MAX_GROSS_EXPOSURE"
    MAX_NET_EXPOSURE = "MAX_NET_EXPOSURE"
    MIN_CASH_RESERVE = "MIN_CASH_RESERVE"
    MIN_POSITION_WEIGHT = "MIN_POSITION_WEIGHT"
    MAX_TRADE_NOTIONAL = "MAX_TRADE_NOTIONAL"
    BUDGET_SCALED = "BUDGET_SCALED"

    # modes
    TRADING_HALTED = "TRADING_HALTED"
    REDUCE_ONLY = "REDUCE_ONLY"

    # P1.2 — multi-strategy allocation, correlation concentration, risk budgets
    UNKNOWN_STRATEGY = "UNKNOWN_STRATEGY"
    STRATEGY_BUDGET_MISSING = "STRATEGY_BUDGET_MISSING"
    STRATEGY_BUDGET_INVALID = "STRATEGY_BUDGET_INVALID"
    STRATEGY_GROSS_LIMIT = "STRATEGY_GROSS_LIMIT"
    STRATEGY_VOLATILITY_LIMIT = "STRATEGY_VOLATILITY_LIMIT"
    STRATEGY_CAPITAL_CEILING = "STRATEGY_CAPITAL_CEILING"
    STRATEGY_IDLE = "STRATEGY_IDLE"
    CORRELATION_CONCENTRATION_LIMIT = "CORRELATION_CONCENTRATION_LIMIT"
    CORRELATION_UNDEFINED = "CORRELATION_UNDEFINED"
    COVARIANCE_INDEFINITE = "COVARIANCE_INDEFINITE"
    UNATTRIBUTED_EXISTING = "UNATTRIBUTED_EXISTING"

    # P1.1 — volatility, liquidity, covariance and drawdown
    PORTFOLIO_VOLATILITY_LIMIT = "PORTFOLIO_VOLATILITY_LIMIT"
    POSITION_LIQUIDITY_LIMIT = "POSITION_LIQUIDITY_LIMIT"
    TRADE_LIQUIDITY_LIMIT = "TRADE_LIQUIDITY_LIMIT"
    LIQUIDITY_UNKNOWN = "LIQUIDITY_UNKNOWN"
    VOLATILITY_UNKNOWN = "VOLATILITY_UNKNOWN"
    RISK_METRIC_MISSING = "RISK_METRIC_MISSING"
    COVARIANCE_UNAVAILABLE = "COVARIANCE_UNAVAILABLE"
    DRAWDOWN_GROSS_LIMIT = "DRAWDOWN_GROSS_LIMIT"
    DRAWDOWN_REDUCE_ONLY = "DRAWDOWN_REDUCE_ONLY"
    DRAWDOWN_HALTED = "DRAWDOWN_HALTED"
    RISK_SNAPSHOT_AFTER_DECISION = "RISK_SNAPSHOT_AFTER_DECISION"
    RISK_METRIC_AFTER_DECISION = "RISK_METRIC_AFTER_DECISION"
    RISK_METRIC_STALE = "RISK_METRIC_STALE"
    RISK_SNAPSHOT_INVALID = "RISK_SNAPSHOT_INVALID"

    # ordinary outcomes
    WITHIN_LIMITS = "WITHIN_LIMITS"
    NOT_REQUESTED = "NOT_REQUESTED"


#: Every code this layer can emit, for documentation and for asserting that a
#: reason reaching a record is one a consumer could have been written against.
REASON_CODES: frozenset[str] = frozenset(
    value
    for name, value in vars(ReasonCode).items()
    if not name.startswith("_") and isinstance(value, str)
)


def validate_inputs(
    intents: Sequence[StrategyIntent],
    snapshot: PortfolioSnapshot | None,
    policy: RiskPolicy | None,
    *,
    allocation_policy_id: str | None = None,
) -> dict[str, Any]:
    """Check that these inputs could authorise a portfolio at all.

    Args:
        intents: The strategy's requests.
        snapshot: The portfolio as it stands.
        policy: The limits to authorise under.
        allocation_policy_id: The allocation policy in force, when one is. Several
            strategies are permitted only with one: splitting capital between them
            because there happen to be two is a capital decision nobody made.

    Returns:
        A summary of what was validated, for the manifest.

    Raises:
        ValidationError: On any condition that makes authorisation unsound. The
            code on the exception is the stable reason.
    """
    if policy is None:
        raise ValidationError(
            ReasonCode.RISK_POLICY_UNAVAILABLE,
            "no risk policy was supplied; an unauthorised portfolio is not a "
            "portfolio this layer will emit",
        )
    if snapshot is None:
        raise ValidationError(
            ReasonCode.INVALID_PORTFOLIO_SNAPSHOT,
            "no portfolio snapshot was supplied; a target cannot be expressed "
            "against an unknown current state",
        )

    strategies = sorted({intent.strategy_id for intent in intents})
    if len(strategies) > 1 and allocation_policy_id is None:
        raise ValidationError(
            ReasonCode.MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED,
            f"intents arrived from {len(strategies)} strategies ({strategies}) with no "
            "allocation policy between them. Equal weighting is not a default this "
            "layer may invent: how capital is divided between strategies is a "
            "decision with its own evidence.",
        )

    # One decision covers one decision, whichever strategies contributed to it.
    seen: dict[tuple[str, int, str], str] = {}
    decisions = sorted({intent.decision_id for intent in intents})
    as_ofs = sorted({intent.as_of for intent in intents})
    if len(decisions) > 1:
        raise ValidationError(
            ReasonCode.DECISION_ID_MISMATCH,
            f"intents span {len(decisions)} decisions ({decisions}); one authorisation "
            "covers one decision",
        )
    if len(as_ofs) > 1:
        raise ValidationError(
            ReasonCode.AS_OF_MISMATCH,
            f"intents carry {len(as_ofs)} different as_of values ({as_ofs}); a single "
            "portfolio cannot be authorised against two moments",
        )
    if as_ofs and as_ofs[0] > snapshot.as_of:
        raise ValidationError(
            ReasonCode.AS_OF_MISMATCH,
            f"the intents are dated {as_ofs[0]} but the portfolio snapshot is dated "
            f"{snapshot.as_of}. Authorising a later decision against an earlier "
            "portfolio would size positions against holdings that have since changed.",
        )

    for intent in intents:
        # Keyed by strategy as well as identity: two strategies wanting the same
        # security is ordinary and aggregates economically, while one strategy
        # asking twice means it disagrees with itself.
        marker = (*intent.key, intent.strategy_id)
        if marker in seen:
            raise ValidationError(
                ReasonCode.DUPLICATE_STRATEGY_INTENT,
                f"{intent.identity.label} was requested twice by {intent.strategy_id}. "
                "The requests are not summed: two intents for one security means the "
                "strategy disagrees with itself, and guessing which it meant would "
                "invent a position size nobody chose.",
            )
        seen[marker] = intent.strategy_id
        weight = intent.weight_against(snapshot.nav)
        if policy.long_only and weight < 0:
            raise ValidationError(
                ReasonCode.LONG_ONLY_VIOLATION,
                f"{intent.identity.label} requests {weight:+.6f} under a long-only "
                "policy. A negative weight is not clamped to zero: the strategy asked "
                "for a short, and silently making it flat would misreport what ran.",
            )

    return {
        "intents": len(intents),
        "strategies": strategies,
        "allocation_policy_id": allocation_policy_id,
        "decision_id": decisions[0] if decisions else None,
        "as_of": as_ofs[0] if as_ofs else None,
        "snapshot_as_of": snapshot.as_of,
        "snapshot_nav": snapshot.nav,
        "policy_id": policy.policy_id,
        "policy_version": policy.policy_version,
        "requested_gross_exposure": quantise(
            sum(abs(intent.weight_against(snapshot.nav)) for intent in intents)
        ),
        "validated": True,
        "repairs_applied": 0,
    }
