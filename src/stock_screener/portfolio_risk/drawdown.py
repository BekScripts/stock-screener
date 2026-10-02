"""Drawdown bands, and the effective policy they derive.

A drawdown overlay can only tighten. That is the single property this module
exists to guarantee: every path through it produces limits at least as strict as
the base policy, never looser. A multiplier above one is refused, a band that
weakens the mode at a deeper drawdown is refused, and the effective mode is always
the stricter of the base mode and the band's.

The thresholds here are *shapes*, not recommendations. P1.1 chooses no production
drawdown levels and does not search history for them — doing so would be exactly
the parameter fitting the research track spent five phases learning to distrust.
What P1.1 provides is the machinery to express a band and the certainty that
expressing one cannot accidentally permit more risk.

Halting still does not liquidate. A band that escalates to `HALTED` stops new risk
and holds the book; it emits no sale. Liquidation remains a separate, explicit act.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from .arithmetic import quantise
from .fingerprint import digest
from .policy import PolicyError, PolicyMode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .policy import RiskPolicy
    from .risk_inputs import PortfolioRiskSnapshot

__all__ = [
    "MODE_SEVERITY",
    "DrawdownBand",
    "EffectiveRiskPolicy",
    "derive_effective_policy",
    "stricter_mode",
    "validate_bands",
]

#: Deterministic severity ordering. The effective mode is the maximum of the base
#: mode and any mode a drawdown band imposes, so safety can only increase.
MODE_SEVERITY: dict[PolicyMode, int] = {
    PolicyMode.NORMAL: 0,
    PolicyMode.REDUCE_ONLY: 1,
    PolicyMode.HALTED: 2,
}


def stricter_mode(left: PolicyMode, right: PolicyMode) -> PolicyMode:
    """The stricter of two modes.

    Args:
        left: One mode.
        right: The other.

    Returns:
        Whichever has the higher severity. A base policy already at `HALTED` is
        never relaxed by a band that would have said `NORMAL`.
    """
    return left if MODE_SEVERITY[left] >= MODE_SEVERITY[right] else right


@dataclass(frozen=True)
class DrawdownBand:
    """One drawdown threshold and the limits it imposes.

    Attributes:
        threshold: The drawdown at or below which this band activates, as a
            non-positive decimal — `-0.10` is a 10% drawdown.
        max_gross_multiplier: What the base gross limit is multiplied by. Must be
            in `[0, 1]`: a band may only tighten.
        mode: The mode this band imposes, combined with the base mode by severity.
        label: A human name for the band, for the record.
    """

    threshold: float
    max_gross_multiplier: float = 1.0
    mode: PolicyMode = PolicyMode.NORMAL
    label: str = ""

    def __post_init__(self) -> None:
        """Refuse a band that could loosen a limit.

        Raises:
            PolicyError: On a positive threshold or a multiplier outside `[0, 1]`.
        """
        if self.threshold > 0:
            raise PolicyError(
                f"a drawdown threshold is a non-positive decimal; got {self.threshold!r}. "
                "A positive threshold would activate on a gain."
            )
        if not 0.0 <= self.max_gross_multiplier <= 1.0:
            raise PolicyError(
                f"max_gross_multiplier must be between 0 and 1, got "
                f"{self.max_gross_multiplier!r}. A multiplier above 1 would let a "
                "drawdown authorise more risk than the base policy, which inverts the "
                "purpose of the overlay."
            )

    def as_json(self) -> dict[str, Any]:
        """Return the band as a JSON-safe mapping."""
        return {
            "label": self.label,
            "threshold": quantise(self.threshold),
            "max_gross_multiplier": quantise(self.max_gross_multiplier),
            "mode": str(self.mode),
        }


def validate_bands(bands: Sequence[DrawdownBand]) -> tuple[DrawdownBand, ...]:
    """Check a band ladder is ordered and monotonically stricter.

    Args:
        bands: The bands, shallowest first.

    Returns:
        The bands as a tuple.

    Raises:
        PolicyError: On duplicate or unsorted thresholds, a multiplier that rises
            with depth, or a mode that weakens with depth. Each would mean a deeper
            loss permitted more risk than a shallower one.
    """
    if not bands:
        return ()
    for shallower, deeper in pairwise(bands):
        if deeper.threshold >= shallower.threshold:
            raise PolicyError(
                f"drawdown bands must be ordered from shallowest to deepest with "
                f"distinct thresholds; {shallower.threshold!r} is followed by "
                f"{deeper.threshold!r}"
            )
        if deeper.max_gross_multiplier > shallower.max_gross_multiplier:
            raise PolicyError(
                f"the band at {deeper.threshold!r} allows gross multiplier "
                f"{deeper.max_gross_multiplier!r}, above the "
                f"{shallower.max_gross_multiplier!r} allowed at the shallower "
                f"{shallower.threshold!r}. A deeper drawdown may not permit more risk."
            )
        if MODE_SEVERITY[deeper.mode] < MODE_SEVERITY[shallower.mode]:
            raise PolicyError(
                f"the band at {deeper.threshold!r} imposes {deeper.mode}, weaker than "
                f"the {shallower.mode} imposed at the shallower {shallower.threshold!r}"
            )
    return tuple(bands)


@dataclass(frozen=True)
class EffectiveRiskPolicy:
    """The limits actually in force, after the drawdown overlay.

    The base policy is never mutated. This is derived from it and the risk
    snapshot, carries its own fingerprint, and records which band produced it, so
    a tightened limit can always be traced to the loss that caused it.

    Attributes:
        base_policy_fingerprint: The policy this derives from.
        risk_snapshot_fingerprint: The snapshot that drove the derivation.
        mode: The stricter of the base mode and the active band's.
        max_gross_exposure: The base gross limit times the band's multiplier.
        drawdown: The drawdown that selected the band.
        active_band: The band in force, or None when none triggered.
        base_mode: The base policy's mode, for the record.
        base_max_gross_exposure: The base gross limit, for the record.
        min_cash_weight: Carried through unchanged.
    """

    base_policy_fingerprint: str
    risk_snapshot_fingerprint: str
    mode: PolicyMode
    max_gross_exposure: float
    drawdown: float
    active_band: dict[str, Any] | None
    base_mode: PolicyMode
    base_max_gross_exposure: float
    min_cash_weight: float

    @property
    def investable_budget(self) -> float:
        """The greatest investable fraction under the effective limits."""
        return quantise(min(self.max_gross_exposure, 1.0 - self.min_cash_weight))

    @property
    def tightened(self) -> bool:
        """Whether the overlay changed anything."""
        return (
            self.mode is not self.base_mode
            or self.max_gross_exposure < self.base_max_gross_exposure
        )

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "base_policy_fingerprint": self.base_policy_fingerprint,
            "risk_snapshot_fingerprint": self.risk_snapshot_fingerprint,
            "mode": str(self.mode),
            "base_mode": str(self.base_mode),
            "max_gross_exposure": quantise(self.max_gross_exposure),
            "base_max_gross_exposure": quantise(self.base_max_gross_exposure),
            "investable_budget": self.investable_budget,
            "min_cash_weight": quantise(self.min_cash_weight),
            "drawdown": quantise(self.drawdown),
            "active_band": self.active_band,
            "tightened": self.tightened,
            "overlay_may_only_tighten": True,
        }

    def fingerprint(self) -> str:
        """The effective policy's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the effective policy with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


def derive_effective_policy(
    policy: RiskPolicy, snapshot: PortfolioRiskSnapshot | None
) -> EffectiveRiskPolicy:
    """Derive the limits in force from the base policy and the drawdown.

    Args:
        policy: The base policy, which is not mutated.
        snapshot: The risk snapshot. None leaves the base limits untouched, with a
            drawdown of zero and no active band.

    Returns:
        The effective policy.
    """
    drawdown = snapshot.drawdown if snapshot is not None else 0.0
    bands = validate_bands(policy.drawdown_bands)
    active: DrawdownBand | None = None
    for band in bands:
        if drawdown <= band.threshold:
            active = band
    mode = policy.mode if active is None else stricter_mode(policy.mode, active.mode)
    multiplier = 1.0 if active is None else active.max_gross_multiplier
    return EffectiveRiskPolicy(
        base_policy_fingerprint=policy.fingerprint(),
        risk_snapshot_fingerprint=(
            snapshot.fingerprint() if snapshot is not None else "NO_RISK_SNAPSHOT"
        ),
        mode=mode,
        max_gross_exposure=quantise(policy.max_gross_exposure * multiplier),
        drawdown=drawdown,
        active_band=active.as_json() if active is not None else None,
        base_mode=policy.mode,
        base_max_gross_exposure=policy.max_gross_exposure,
        min_cash_weight=policy.min_cash_weight,
    )
