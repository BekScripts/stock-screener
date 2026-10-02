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
from typing import Any

from .arithmetic import arithmetic_policy, quantise
from .fingerprint import digest

__all__ = [
    "EXAMPLE_POLICY_V1",
    "ConstructionMode",
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

    def fingerprint(self) -> str:
        """The policy's deterministic digest. A changed limit changes it."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the policy with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


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
