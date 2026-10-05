"""The declared execution friction: spread, slippage, participation, horizon.

P2.0 modelled no execution cost at all and said so. This module is where the
cost comes from, and every number in it is *declared by a caller* rather than
derived by the engine. That is deliberate and it is the main safeguard here.

The tempting alternative is to calibrate these values — fit a slippage figure
that makes a backtest match some observed result, or pick a participation rate
that lets a strategy's historical trades clear. Doing that turns an execution
model into a free parameter of the strategy, and the strategy's apparent edge
then partly comes from the cost assumption chosen to flatter it. So the policy is
an input with a fingerprint, every example value is marked
`NOT_PRODUCTION_AUTHORIZED`, and nothing in the package reads a return, a price
history or a P&L figure to choose one.

The friction is also strictly adverse. Slippage worsens both sides of a trade and
`adverse_slippage_bps` may not be negative, because a model that could improve a
fill is a model that can manufacture edge. None of this claims to be market
impact: impact is a function of the order's own size against available liquidity,
and this is a flat declared haircut. The distinction is kept in the vocabulary so
a later phase can add impact without anyone believing it was already there.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from stock_screener.portfolio_accounting import as_decimal

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .versions import (
    DAY_EXPIRY_POLICY,
    EXECUTION_ENGINE_VERSION_V1_1,
    PAPER_SESSION_FILL_MODEL,
    SESSION_ROLLOVER_POLICY,
)

__all__ = [
    "EXAMPLE_REALISM_POLICY",
    "REALISM_POLICY_CONTRACT",
    "PaperExecutionPolicy",
]

#: The policy contract. A change to what it binds is a new version, because a
#: cost report that cannot name the friction behind it is not reproducible.
REALISM_POLICY_CONTRACT = "PAPER_EXECUTION_POLICY_V1"

#: Basis points per unit. Named so the division is never guessed at.
BPS_DIVISOR = Decimal(10_000)


@dataclass(frozen=True)
class PaperExecutionPolicy:
    """The friction a paper execution is run under. Immutable and fingerprinted.

    Attributes:
        policy_id: What this policy is called.
        adverse_slippage_bps: How far past the touch price a fill lands, in basis
            points, always against the trader. Zero is valid and means the fill
            is at the touch. Negative is refused.
        max_volume_participation: The largest share of a session's observed
            volume this system may take, as a fraction. A ceiling, not a target.
        maximum_execution_sessions: How many sessions a residual may roll
            through before execution stops. Finite by construction: an unbounded
            rollover would let a plan trade against a portfolio decision made
            arbitrarily long ago.
        spread_required: Whether a fill needs both sides of a quote. True is the
            honest default — without a quote there is no spread to cross, and
            substituting a single price silently removes the cost.
        volume_required: Whether a fill needs an observed volume. True is the
            honest default, because an unknown volume means an unknown capacity
            rather than an unlimited one.
        policy_version: The contract this policy satisfies.
        production_authorized: Whether these particular numbers have been
            established as correct for live use. Always False here.
    """

    policy_id: str
    adverse_slippage_bps: Decimal = Decimal(0)
    max_volume_participation: Decimal = Decimal("0.1")
    maximum_execution_sessions: int = 3
    spread_required: bool = True
    volume_required: bool = True
    policy_version: str = REALISM_POLICY_CONTRACT
    production_authorized: bool = False

    def __post_init__(self) -> None:
        """Validate the declared friction.

        Raises:
            ExecutionError: `INVALID_EXECUTION_POLICY` on a missing id, a
                negative slippage, a participation outside `(0, 1]`, or a
                non-positive session horizon. Negative slippage is refused
                specifically because a model that improves a fill can
                manufacture edge, and a participation above 1 would let the
                system take more than the whole session's volume.
        """
        if not self.policy_id:
            raise ExecutionError(
                Code.INVALID_EXECUTION_POLICY, "an execution policy must name itself"
            )
        slippage = as_decimal(self.adverse_slippage_bps, field="adverse_slippage_bps")
        if not slippage.is_finite() or slippage < Decimal(0):
            raise ExecutionError(
                Code.INVALID_EXECUTION_POLICY,
                f"{self.policy_id}: adverse_slippage_bps is {slippage}. It may not be "
                "negative: slippage in this engine is strictly adverse, and a model "
                "that could improve a fill is a model that can manufacture edge.",
            )
        participation = as_decimal(self.max_volume_participation, field="max_volume_participation")
        if not participation.is_finite() or not (Decimal(0) < participation <= Decimal(1)):
            raise ExecutionError(
                Code.INVALID_EXECUTION_POLICY,
                f"{self.policy_id}: max_volume_participation is {participation}, "
                "outside (0, 1]. Zero would make every order unfillable and above "
                "one would let this system take more than the whole session traded.",
            )
        if self.maximum_execution_sessions < 1:
            raise ExecutionError(
                Code.INVALID_EXECUTION_POLICY,
                f"{self.policy_id}: maximum_execution_sessions is "
                f"{self.maximum_execution_sessions}. It must be at least one and "
                "finite: an unbounded rollover would let a plan keep trading "
                "against a portfolio decision made arbitrarily long ago.",
            )
        object.__setattr__(self, "adverse_slippage_bps", slippage)
        object.__setattr__(self, "max_volume_participation", participation)

    @property
    def slippage_fraction(self) -> Decimal:
        """The slippage as a fraction of price."""
        return self.adverse_slippage_bps / BPS_DIVISOR

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "policy_version": self.policy_version,
            "policy_id": self.policy_id,
            "engine_version": EXECUTION_ENGINE_VERSION_V1_1,
            "fill_model": PAPER_SESSION_FILL_MODEL,
            "expiry_policy": DAY_EXPIRY_POLICY,
            "rollover_policy": SESSION_ROLLOVER_POLICY,
            "adverse_slippage_bps": str(self.adverse_slippage_bps),
            "max_volume_participation": str(self.max_volume_participation),
            "maximum_execution_sessions": self.maximum_execution_sessions,
            "spread_required": self.spread_required,
            "volume_required": self.volume_required,
        }

    def fingerprint(self) -> str:
        """The policy's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the policy as a JSON-safe mapping."""
        return {
            **self.body(),
            "policy_fingerprint": self.fingerprint(),
            "production_authorized": self.production_authorized,
            "authorization": (
                "PRODUCTION_AUTHORIZED"
                if self.production_authorized
                else "NOT_PRODUCTION_AUTHORIZED"
            ),
            "calibration": (
                "NONE — every value is declared by the caller. Nothing in this "
                "package reads a return, a price history or a P&L figure to choose "
                "one, because a cost model fitted to make a strategy look good is a "
                "free parameter of that strategy."
            ),
            "slippage_direction": "ADVERSE_ONLY",
            "market_impact": (
                "NOT_MODELLED — impact is a function of an order's size against "
                "available liquidity. This is a flat declared haircut and is not "
                "the same thing."
            ),
        }


#: An example policy, for fixtures and tests. Its numbers exercise code paths and
#: are **not** validated execution assumptions: 10bps of slippage and a 10%
#: participation ceiling are round numbers chosen to make arithmetic checkable by
#: hand, not measurements of anything. Promoting them would need a dedicated
#: execution-cost study that this phase does not perform.
EXAMPLE_REALISM_POLICY = PaperExecutionPolicy(
    policy_id="EXAMPLE_REALISM_V1_1",
    adverse_slippage_bps=Decimal(10),
    max_volume_participation=Decimal("0.10"),
    maximum_execution_sessions=3,
    spread_required=True,
    volume_required=True,
    production_authorized=False,
)
