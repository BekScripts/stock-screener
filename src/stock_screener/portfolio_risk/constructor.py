"""Portfolio construction: what the intents would express, before risk is applied.

This module answers one question — *what portfolio would best express these
intents?* — and deliberately not the other one, *how much of it is permitted?*
Collapsing the two into a single function is the usual shape, and it makes the
result impossible to explain: when a position comes out at 4% you cannot tell
whether the strategy asked for 4%, or asked for 25% and a cap reduced it, or
asked for 8% and a budget scaled it.

So construction here is almost boring. It converts each request to a weight
against NAV, orders them deterministically, makes cash explicit, and applies one
declared choice: whether an underinvested request means what it says.

That choice cannot be inferred from the numbers. Weights summing to 0.60 might be
a strategy deliberately holding 40% cash, or a ranking expressing relative
conviction that happens to sum to less than one. Those want opposite treatment,
so the policy states which, and this module never guesses.

Baseline only: no optimiser, no covariance, no volatility targeting, no Kelly.
Those belong to later phases, on top of an authority model that already works.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .fingerprint import digest
from .policy import ConstructionMode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .intents import StrategyIntent
    from .models import PortfolioSnapshot, SecurityIdentity
    from .policy import RiskPolicy

__all__ = [
    "CONSTRUCTOR_ID",
    "ConstructedPortfolio",
    "construct",
]

#: The constructor's identity, recorded on the decision.
CONSTRUCTOR_ID = "BASELINE_TARGET_WEIGHT_CONSTRUCTOR_V1"


@dataclass(frozen=True)
class ConstructedPortfolio:
    """The portfolio the intents express, before any limit is applied.

    Attributes:
        requested: `{identity key: weight}` as the strategy asked, in identity
            order.
        identities: The identity objects, by key, so later stages keep the symbol
            for display without ever keying on it.
        priorities: `{identity key: priority}`, the deterministic basis for
            truncation.
        construction_mode: Which mode was applied.
        requested_gross: Sum of absolute requested weights.
        notes: What construction did, for the record.
    """

    requested: dict[tuple[str, int], float]
    identities: dict[tuple[str, int], SecurityIdentity]
    priorities: dict[tuple[str, int], float]
    construction_mode: str
    requested_gross: float
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def requested_cash_weight(self) -> float:
        """What the request leaves uninvested. May be negative if oversubscribed."""
        return quantise(1.0 - self.requested_gross)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body, in identity order."""
        return {
            "constructor_id": CONSTRUCTOR_ID,
            "construction_mode": self.construction_mode,
            "requested": [
                {
                    "identity": self.identities[key].as_json(),
                    "requested_weight": self.requested[key],
                    "priority": self.priorities[key],
                }
                for key in sorted(self.requested)
            ],
            "requested_gross_exposure": self.requested_gross,
            "requested_cash_weight": self.requested_cash_weight,
            "notes": list(self.notes),
        }

    def fingerprint(self) -> str:
        """The constructed portfolio's deterministic digest."""
        return digest(self.body())


def construct(
    intents: Sequence[StrategyIntent],
    snapshot: PortfolioSnapshot,
    policy: RiskPolicy,
) -> ConstructedPortfolio:
    """Express the intents as target weights, with cash explicit.

    Args:
        intents: The validated strategy requests.
        snapshot: The portfolio, for converting a notional request against NAV.
        policy: Supplies the construction mode and the investable budget.

    Returns:
        The constructed portfolio. No risk limit has been applied: per-name caps,
        counts, exposure and the cash reserve are the risk engine's to impose, and
        keeping them out of here is what makes a final weight explainable.
    """
    nav = snapshot.nav
    requested: dict[tuple[str, int], float] = {}
    identities: dict[tuple[str, int], SecurityIdentity] = {}
    priorities: dict[tuple[str, int], float] = {}
    for intent in intents:
        requested[intent.key] = intent.weight_against(nav)
        identities[intent.key] = intent.identity
        priorities[intent.key] = intent.priority

    notes: list[str] = []
    gross = quantise(sum(abs(weight) for weight in requested.values()))
    mode = policy.construction_mode
    if mode is ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET and gross > EXPOSURE_TOLERANCE:
        budget = policy.investable_budget
        factor = budget / gross
        requested = {key: quantise(weight * factor) for key, weight in requested.items()}
        gross = quantise(sum(abs(weight) for weight in requested.values()))
        notes.append(
            f"requested weights scaled by {factor:.6f} to fill the investable budget "
            f"{budget:.6f}; the policy declares these weights relative rather than absolute"
        )
    else:
        notes.append(
            "requested weights taken as stated; the remainder is held as cash because "
            "the policy declares the weights absolute"
        )
    notes.append(
        "no per-name cap, count cap, exposure cap or cash reserve applied here — "
        "those are the risk engine's, so a final weight can always be attributed"
    )
    return ConstructedPortfolio(
        requested=dict(sorted(requested.items())),
        identities=identities,
        priorities=priorities,
        construction_mode=str(mode),
        requested_gross=gross,
        notes=tuple(notes),
    )
