"""Risk limits as versioned data, never as code.

A limit buried in a conditional is a limit nobody can audit, compare across
versions, or point to afterwards and say *that* is what authorised this trade. So
the policy is a value: declarative, validated, fingerprinted, and recorded on
every decision it authorises.

**These numbers are not validated thresholds.** `EXAMPLE_POLICY_V1` exists to
exercise the engine and is labelled `NOT_PRODUCTION_AUTHORIZED` for the same
reason the research track labels an exploratory result exploratory: nothing here
has established what a correct maximum position weight is for this strategy, and
a number that appears in a test has a way of becoming a number someone trades.
Choosing live limits is a dedicated risk-policy phase with its own evidence.

The three modes are deliberately distinct, and the distinction that matters most
is between halting and liquidating. `HALTED` stops trading; it does not sell
anything. Conflating them would mean an operator reaching for a kill switch
during an incident and discovering it had liquidated the book at whatever the
market was doing at that moment. Liquidation is a separate, explicit act.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .drawdown import DrawdownBand

from .arithmetic import arithmetic_policy, quantise
from .fingerprint import digest
from .risk_inputs import MAX_PLAUSIBLE_VOLATILITY

__all__ = [
    "EXAMPLE_ADVANCED_RISK_POLICY_V1",
    "EXAMPLE_POLICY_V1",
    "ConstructionMode",
    "MissingMetricPolicy",
    "PolicyError",
    "PolicyMode",
    "RiskPolicy",
]


class PolicyError(ValueError):
    """Raised when a policy is internally inconsistent."""


class PolicyMode(StrEnum):
    """What the policy currently permits."""

    NORMAL = "NORMAL"
    """Every action is available, subject to the limits."""

    REDUCE_ONLY = "REDUCE_ONLY"
    """No new positions and no increases. Reductions and exits are allowed.

    The mode for an incident where the book should get smaller but not vanish.
    """

    HALTED = "HALTED"
    """No target may change at all. Every position is held where it stands.

    Halting is not liquidating. A halted policy authorises no trade in either
    direction, and emits no exit the strategy did not already hold — turning a
    halt into a liquidation is a separate, explicit decision that this layer
    deliberately cannot make.
    """


class MissingMetricPolicy(StrEnum):
    """What to do when a risk metric a limit needs is not available.

    The default is deliberately the cautious one. An unknown volatility is not
    zero and an unknown ADV is not infinite, so the question is not *what value
    should we assume* but *what may we permit while ignorant* — and the answer that
    costs least when wrong is: no new risk, but let the position be reduced.
    """

    REJECT_INCREASE = "REJECT_INCREASE"
    """No new or increased exposure in that name. Reductions and exits still pass.

    This is also what "allow reduce only" means: a reduction is never blocked by a
    missing metric, so there is one behaviour here rather than two names for it.
    """

    IGNORE_METRIC = "IGNORE_METRIC"
    """The limit simply does not apply where its metric is absent.

    Honest when the metric is genuinely optional. Dangerous as a default, which is
    why it must be asked for.
    """

    REQUIRE_METRIC = "REQUIRE_METRIC"
    """The whole decision fails closed. Nothing is authorised at all."""


class ConstructionMode(StrEnum):
    """What to do when the requested weights do not fill the investable budget."""

    PRESERVE_ABSOLUTE_WEIGHTS = "PRESERVE_ABSOLUTE_WEIGHTS"
    """Take the requested weights as stated, holding the remainder as cash.

    A strategy that asks for 60% invested means 60% invested. Scaling it to fully
    invested would overrule a deliberate cash position, which is a strategy
    decision this layer has no basis to reverse.
    """

    NORMALIZE_TO_INVESTABLE_BUDGET = "NORMALIZE_TO_INVESTABLE_BUDGET"
    """Scale the requested weights to fill the budget exactly.

    For a strategy whose weights are relative rather than absolute — a ranking
    that says *these names, in this proportion* without claiming an exposure level.
    """


@dataclass(frozen=True)
class RiskPolicy:
    """The limits under which a portfolio may be authorised.

    Attributes:
        policy_id: Which policy this is.
        policy_version: Its version. A changed limit is a new version.
        mode: What the policy currently permits.
        construction_mode: How an underinvested request is treated.
        long_only: Whether negative target weights are refused.
        max_gross_exposure: Cap on the sum of absolute target weights.
        max_net_exposure: Cap on the sum of signed target weights. None is no cap.
        max_position_weight: Cap on any single target weight.
        max_positions: Cap on the number of held positions.
        min_cash_weight: Cash that must remain unallocated.
        max_trade_notional_weight: Cap on a single trade as a fraction of NAV.
            None is no cap.
        min_position_weight: Targets below this are dropped rather than held as
            dust. None keeps every approved target.
        max_portfolio_volatility: Ceiling on estimated annualised portfolio
            volatility. None means no volatility constraint is evaluated. A
            ceiling, never a target: exposure is not raised to reach it.
        max_position_adv_fraction: A position may not exceed this fraction of the
            security's average daily dollar volume. None means no position capacity
            limit.
        max_trade_adv_fraction: A single change in exposure may not exceed this
            fraction of ADV. Separate from position capacity, because holding a
            large position and acquiring one in a day are different risks.
        drawdown_bands: The overlay ladder, shallowest first. Empty means no
            overlay.
        missing_volatility_policy: What an unknown volatility or absent covariance
            row permits.
        missing_liquidity_policy: What an unknown ADV permits.
        max_risk_metric_age_seconds: How stale a risk metric may be. None means no
            freshness bound — P1.1 defines no production threshold for this and
            will not invent one.
        correlation_threshold: The signed correlation at or above which a pair is
            treated as concentrated. None disables the control. Positive
            correlation only: under a long-only book two names moving oppositely
            diversify rather than concentrate.
        max_correlated_pair_exposure: The greatest combined weight a concentrated
            pair may carry.
        missing_correlation_policy: What an undefined correlation permits. A
            zero-variance security has no correlation with anything, which is not
            the same as having a zero one.
        production_authorized: Whether these limits have been established as
            suitable for live capital. False for every policy in this phase.
    """

    policy_id: str
    policy_version: str
    mode: PolicyMode = PolicyMode.NORMAL
    construction_mode: ConstructionMode = ConstructionMode.PRESERVE_ABSOLUTE_WEIGHTS
    long_only: bool = True
    max_gross_exposure: float = 1.0
    max_net_exposure: float | None = None
    max_position_weight: float = 0.10
    max_positions: int = 20
    min_cash_weight: float = 0.05
    max_trade_notional_weight: float | None = None
    min_position_weight: float | None = None
    max_portfolio_volatility: float | None = None
    max_position_adv_fraction: float | None = None
    max_trade_adv_fraction: float | None = None
    drawdown_bands: tuple[DrawdownBand, ...] = ()
    missing_volatility_policy: MissingMetricPolicy = MissingMetricPolicy.REJECT_INCREASE
    missing_liquidity_policy: MissingMetricPolicy = MissingMetricPolicy.REJECT_INCREASE
    max_risk_metric_age_seconds: float | None = None
    correlation_threshold: float | None = None
    max_correlated_pair_exposure: float | None = None
    missing_correlation_policy: MissingMetricPolicy = MissingMetricPolicy.REJECT_INCREASE
    production_authorized: bool = False

    def __post_init__(self) -> None:
        """Refuse a policy that cannot be satisfied by any portfolio.

        Raises:
            PolicyError: On a limit outside its admissible range, or on a
                combination no portfolio could meet.
        """
        if not 0.0 <= self.min_cash_weight <= 1.0:
            raise PolicyError(
                f"min_cash_weight must be between 0 and 1, got {self.min_cash_weight!r}"
            )
        if self.max_gross_exposure < 0:
            raise PolicyError(
                f"max_gross_exposure may not be negative, got {self.max_gross_exposure!r}"
            )
        if self.max_position_weight <= 0:
            raise PolicyError(
                f"max_position_weight must be positive, got {self.max_position_weight!r}; "
                "a policy permitting no position size authorises nothing"
            )
        if self.max_positions < 1:
            raise PolicyError(f"max_positions must be at least 1, got {self.max_positions!r}")
        if self.max_net_exposure is not None and self.max_net_exposure < 0 and self.long_only:
            raise PolicyError(
                f"a long-only policy cannot require net exposure below zero "
                f"({self.max_net_exposure!r})"
            )
        if self.min_position_weight is not None and (
            self.min_position_weight <= 0 or self.min_position_weight > self.max_position_weight
        ):
            raise PolicyError(
                f"min_position_weight {self.min_position_weight!r} must be positive and no "
                f"greater than max_position_weight {self.max_position_weight!r}"
            )
        if self.max_trade_notional_weight is not None and self.max_trade_notional_weight <= 0:
            raise PolicyError(
                f"max_trade_notional_weight must be positive, got "
                f"{self.max_trade_notional_weight!r}"
            )
        for name in (
            "max_portfolio_volatility",
            "max_position_adv_fraction",
            "max_trade_adv_fraction",
            "max_risk_metric_age_seconds",
        ):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise PolicyError(f"{name} must be positive when set, got {value!r}")
        if self.correlation_threshold is not None and not (
            -1.0 <= self.correlation_threshold <= 1.0
        ):
            raise PolicyError(
                f"correlation_threshold must be between -1 and 1, got "
                f"{self.correlation_threshold!r}"
            )
        if self.max_correlated_pair_exposure is not None and (
            self.max_correlated_pair_exposure <= 0
        ):
            raise PolicyError(
                f"max_correlated_pair_exposure must be positive when set, got "
                f"{self.max_correlated_pair_exposure!r}"
            )
        if (self.correlation_threshold is None) != (self.max_correlated_pair_exposure is None):
            raise PolicyError(
                "correlation_threshold and max_correlated_pair_exposure are set "
                "together or not at all: a threshold with no limit constrains nothing, "
                "and a limit with no threshold would apply to every pair"
            )
        if (
            self.max_portfolio_volatility is not None
            and self.max_portfolio_volatility > MAX_PLAUSIBLE_VOLATILITY
        ):
            raise PolicyError(
                f"max_portfolio_volatility is {self.max_portfolio_volatility!r}, above "
                f"the plausible ceiling {MAX_PLAUSIBLE_VOLATILITY}. Volatility is an "
                "annualised decimal: 20% is 0.20, not 20."
            )
        if self.drawdown_bands:
            from .drawdown import validate_bands

            validate_bands(self.drawdown_bands)
        # A policy whose per-name capacity cannot fill its budget is only
        # inconsistent when it promises to fill it. Under PRESERVE_ABSOLUTE_WEIGHTS
        # a concentrated policy that leaves most of the book in cash is a
        # deliberate, legitimate choice, so the check applies to the mode that
        # would otherwise scale weights toward a budget it can never reach.
        if (
            self.construction_mode is ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET
            and self.max_position_weight * self.max_positions < self.investable_budget - 1e-12
        ):
            raise PolicyError(
                f"the policy promises to fill its budget and cannot: {self.max_positions} "
                f"positions at {self.max_position_weight} each is "
                f"{self.max_position_weight * self.max_positions:.4f}, below the investable "
                f"budget {self.investable_budget:.4f}. Either raise the caps or declare the "
                "weights absolute with PRESERVE_ABSOLUTE_WEIGHTS."
            )

    @property
    def investable_budget(self) -> float:
        """The greatest fraction of NAV the policy allows to be invested.

        The tighter of the gross cap and what is left after the cash reserve. Both
        are real limits, so the budget is the minimum rather than either alone.
        """
        return quantise(min(self.max_gross_exposure, 1.0 - self.min_cash_weight))

    @property
    def permits_increase(self) -> bool:
        """Whether the mode allows a position to grow or a new one to open."""
        return self.mode is PolicyMode.NORMAL

    @property
    def permits_reduction(self) -> bool:
        """Whether the mode allows a position to shrink or close."""
        return self.mode in (PolicyMode.NORMAL, PolicyMode.REDUCE_ONLY)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "mode": str(self.mode),
            "construction_mode": str(self.construction_mode),
            "long_only": self.long_only,
            "max_gross_exposure": self.max_gross_exposure,
            "max_net_exposure": self.max_net_exposure,
            "max_position_weight": self.max_position_weight,
            "max_positions": self.max_positions,
            "min_cash_weight": self.min_cash_weight,
            "max_trade_notional_weight": self.max_trade_notional_weight,
            "min_position_weight": self.min_position_weight,
            "investable_budget": self.investable_budget,
            "production_authorized": self.production_authorized,
            "arithmetic": arithmetic_policy(),
        }

    @property
    def uses_advanced_controls(self) -> bool:
        """Whether any P1.1 control is actually configured.

        The advanced block enters the fingerprint only when it does. A policy that
        sets no volatility ceiling, no capacity fraction and no drawdown band is
        the same policy it was before those concepts existed, so it must digest the
        same — which is what keeps a V1 decision reproducible rather than something
        that had to be re-recorded when the engine grew.
        """
        return any(
            value is not None
            for value in (
                self.max_portfolio_volatility,
                self.max_position_adv_fraction,
                self.max_trade_adv_fraction,
                self.max_risk_metric_age_seconds,
                self.correlation_threshold,
                self.max_correlated_pair_exposure,
            )
        ) or bool(self.drawdown_bands)

    @property
    def uses_correlation_control(self) -> bool:
        """Whether the pairwise correlation concentration control is configured.

        The correlation block enters the fingerprint only when it does, for the
        same reason the advanced block does: a policy that sets no threshold is the
        same policy it was before the control existed, so a V1.1 decision and its
        golden output still reproduce.
        """
        return self.correlation_threshold is not None

    def advanced_body(self) -> dict[str, Any]:
        """The P1.1 limits, plus the P1.2 correlation block where configured."""
        body: dict[str, Any] = {
            "max_portfolio_volatility": self.max_portfolio_volatility,
            "max_position_adv_fraction": self.max_position_adv_fraction,
            "max_trade_adv_fraction": self.max_trade_adv_fraction,
            "drawdown_bands": [band.as_json() for band in self.drawdown_bands],
            "missing_volatility_policy": str(self.missing_volatility_policy),
            "missing_liquidity_policy": str(self.missing_liquidity_policy),
            "max_risk_metric_age_seconds": self.max_risk_metric_age_seconds,
        }
        if self.uses_correlation_control:
            body["correlation_concentration"] = {
                "correlation_threshold": self.correlation_threshold,
                "max_correlated_pair_exposure": self.max_correlated_pair_exposure,
                "missing_correlation_policy": str(self.missing_correlation_policy),
                "positive_correlation_only": True,
            }
        return body

    def fingerprint(self) -> str:
        """The policy's deterministic digest. A changed limit changes it."""
        body = self.body()
        if self.uses_advanced_controls:
            body = {**body, "advanced_risk_controls": self.advanced_body()}
        return digest(body)

    def as_json(self) -> dict[str, Any]:
        """Return the policy with its fingerprint."""
        body = self.body()
        if self.uses_advanced_controls:
            body = {**body, "advanced_risk_controls": self.advanced_body()}
        return {**body, "fingerprint": self.fingerprint()}


#: A policy for exercising the engine. **NOT_PRODUCTION_AUTHORIZED**: these
#: numbers were chosen to make the limits observable in tests, not established as
#: correct for live capital. Promoting them requires a dedicated risk-policy
#: phase with its own evidence; nothing in this phase provides that evidence.
EXAMPLE_POLICY_V1 = RiskPolicy(
    policy_id="EXAMPLE_POLICY_V1",
    policy_version="1",
    mode=PolicyMode.NORMAL,
    construction_mode=ConstructionMode.PRESERVE_ABSOLUTE_WEIGHTS,
    long_only=True,
    max_gross_exposure=1.00,
    max_position_weight=0.10,
    max_positions=20,
    min_cash_weight=0.05,
    production_authorized=False,
)


#: An advanced policy for exercising the P1.1 controls. **NOT_PRODUCTION_AUTHORIZED**:
#: every number here was chosen to make a limit observable in a test, and none was
#: derived from historical return, Sharpe or drawdown — deriving them that way is
#: exactly the parameter fitting this programme exists to avoid. Promoting any of
#: them requires a dedicated risk-policy phase with its own evidence.
def example_advanced_risk_policy() -> RiskPolicy:
    """Build the P1.1 example policy.

    A function rather than a module constant because the drawdown bands import from
    a module that imports this one; constructing on demand keeps the import graph
    acyclic without weakening the type.

    Returns:
        The example policy. Not authorised for live capital.
    """
    from .drawdown import DrawdownBand as _Band

    return RiskPolicy(
        policy_id="EXAMPLE_ADVANCED_RISK_POLICY_V1",
        policy_version="1",
        mode=PolicyMode.NORMAL,
        construction_mode=ConstructionMode.PRESERVE_ABSOLUTE_WEIGHTS,
        long_only=True,
        max_gross_exposure=1.00,
        max_position_weight=0.10,
        max_positions=20,
        min_cash_weight=0.05,
        max_portfolio_volatility=0.20,
        max_position_adv_fraction=0.05,
        max_trade_adv_fraction=0.02,
        drawdown_bands=(
            _Band(
                threshold=-0.10, max_gross_multiplier=0.75, mode=PolicyMode.NORMAL, label="caution"
            ),
            _Band(
                threshold=-0.20,
                max_gross_multiplier=0.50,
                mode=PolicyMode.REDUCE_ONLY,
                label="reduce-only",
            ),
            _Band(
                threshold=-0.30,
                max_gross_multiplier=0.50,
                mode=PolicyMode.HALTED,
                label="halted",
            ),
        ),
        missing_volatility_policy=MissingMetricPolicy.REJECT_INCREASE,
        missing_liquidity_policy=MissingMetricPolicy.REJECT_INCREASE,
        production_authorized=False,
    )


#: The example advanced policy. NOT_PRODUCTION_AUTHORIZED.
EXAMPLE_ADVANCED_RISK_POLICY_V1 = example_advanced_risk_policy()
