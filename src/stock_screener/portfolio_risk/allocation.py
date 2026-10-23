"""Capital sleeves: how several strategies request capital through one authority.

A strategy asks for positions. An allocator decides how much capital that
strategy may command at all. Those are different questions, and P1.0 deliberately
refused to answer the second one by guessing — two strategies arriving without an
allocation policy still raise `MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED`, because
splitting capital fifty-fifty because there happen to be two of them is a capital
decision nobody made.

Four rules shape this module.

**Budgets are ceilings, not reservations.** A strategy granted 30% that asks for
12% gets 12%, and the remaining 18% becomes **cash** — it does not flow to another
strategy. Redistributing it would mean one strategy's restraint silently funding
another's risk, and would break the monotonicity the risk engine depends on.

**Sleeve weights are relative; global weights are absolute.** Under a multi-strategy
policy a `requested_weight` is read as a weight *within that strategy's sleeve*, so
its global contribution is `sleeve budget * normalised within-sleeve weight`. That
is a semantic choice, declared rather than inferred from magnitude — and the
single-strategy path keeps the old absolute reading untouched, so no existing
caller is silently reinterpreted.

**Exposure aggregates economically; attribution does not.** If two strategies each
want 4% of one security, the portfolio holds one 8% position, not two. But the
record keeps who asked for what, because afterwards the question is never only
*how much do we hold* but *why, and on whose behalf*.

**Nothing here is performance-weighted.** The allocator cannot see a return, a
Sharpe ratio or a win rate, and the package imports nothing that could supply
one. Capital budgets are explicit policy inputs. Learning them is a research
problem with its own evidence, not a default.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from .arithmetic import EXPOSURE_TOLERANCE, quantise
from .fingerprint import digest
from .intents import intents_fingerprint
from .validation import ReasonCode, ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .intents import StrategyIntent
    from .models import SecurityIdentity
    from .risk_inputs import CovarianceSnapshot

log = structlog.get_logger(__name__)

__all__ = [
    "ALLOCATOR_ID",
    "EXAMPLE_MULTI_STRATEGY_POLICY_V1",
    "AggregatedRequest",
    "BudgetSemantics",
    "StrategyAllocationPolicy",
    "StrategyBudget",
    "StrategyContribution",
    "StrategySleeve",
    "allocate",
]

#: The allocator's identity, recorded on the decision.
ALLOCATOR_ID = "FIXED_SLEEVE_ALLOCATOR_V1"


class BudgetSemantics(StrEnum):
    """What a strategy budget means. One interpretation, frozen."""

    CEILING_UNUSED_BECOMES_CASH = "CEILING_UNUSED_BECOMES_CASH"
    """A maximum sleeve. Unused capacity is cash, never another strategy's.

    The only semantics P1.2 implements. A reallocation policy — who may claim
    unused capacity, and on what basis — is a separate decision with its own
    consequences, and inventing one here would let a quiet default move capital.
    """


@dataclass(frozen=True)
class StrategyBudget:
    """How much capital one strategy may command, and at what risk.

    Attributes:
        strategy_id: Which strategy this governs.
        max_capital_weight: The sleeve ceiling, as a fraction of NAV.
        max_strategy_gross: Cap on the sleeve's own gross exposure. None means the
            capital ceiling is the only limit.
        max_strategy_volatility: Cap on the sleeve's standalone annualised
            volatility, measured in global portfolio weights. None means no
            sleeve volatility limit.
        priority: Only consulted where a policy genuinely needs a tie-break.
            Budgets normally make it unnecessary, and a hidden "first strategy
            wins" is what it exists to prevent.
    """

    strategy_id: str
    max_capital_weight: float
    max_strategy_gross: float | None = None
    max_strategy_volatility: float | None = None
    priority: float = 0.0

    def __post_init__(self) -> None:
        """Refuse a budget that could not be honoured.

        Raises:
            ValidationError: On a missing id or a limit outside its range.
        """
        if not self.strategy_id:
            raise ValidationError(
                ReasonCode.STRATEGY_BUDGET_INVALID, "a budget must name its strategy"
            )
        if not 0.0 <= self.max_capital_weight <= 1.0:
            raise ValidationError(
                ReasonCode.STRATEGY_BUDGET_INVALID,
                f"{self.strategy_id}: max_capital_weight must be between 0 and 1, got "
                f"{self.max_capital_weight!r}",
            )
        for name in ("max_strategy_gross", "max_strategy_volatility"):
            value = getattr(self, name)
            if value is not None and value <= 0:
                raise ValidationError(
                    ReasonCode.STRATEGY_BUDGET_INVALID,
                    f"{self.strategy_id}: {name} must be positive when set, got {value!r}",
                )

    def as_json(self) -> dict[str, Any]:
        """Return the budget as a JSON-safe mapping."""
        return {
            "strategy_id": self.strategy_id,
            "max_capital_weight": quantise(self.max_capital_weight),
            "max_strategy_gross": self.max_strategy_gross,
            "max_strategy_volatility": self.max_strategy_volatility,
            "priority": self.priority,
        }


@dataclass(frozen=True)
class StrategyAllocationPolicy:
    """Which strategies may request capital, and how much.

    Attributes:
        allocation_policy_id: Which policy this is.
        allocation_policy_version: Its version. A changed budget is a new version.
        budgets: One per permitted strategy.
        semantics: What a budget means. One frozen interpretation.
        production_authorized: Whether these budgets have been established as
            suitable for live capital. False throughout P1.2.
    """

    allocation_policy_id: str
    allocation_policy_version: str
    budgets: tuple[StrategyBudget, ...]
    semantics: BudgetSemantics = BudgetSemantics.CEILING_UNUSED_BECOMES_CASH
    production_authorized: bool = False

    def __post_init__(self) -> None:
        """Refuse a policy that could not allocate coherently.

        Raises:
            ValidationError: On no budgets, a repeated strategy, or budgets whose
                total exceeds the whole portfolio.
        """
        if not self.budgets:
            raise ValidationError(
                ReasonCode.STRATEGY_BUDGET_INVALID,
                "an allocation policy with no budgets authorises no strategy, which is "
                "not the same as refusing multi-strategy allocation",
            )
        ids = [budget.strategy_id for budget in self.budgets]
        if len(set(ids)) != len(ids):
            raise ValidationError(
                ReasonCode.STRATEGY_BUDGET_INVALID,
                f"a strategy has more than one budget: {sorted(ids)}",
            )
        total = sum(budget.max_capital_weight for budget in self.budgets)
        if total > 1.0 + EXPOSURE_TOLERANCE:
            raise ValidationError(
                ReasonCode.STRATEGY_BUDGET_INVALID,
                f"the sleeve ceilings total {total:.6f}, above the whole portfolio. "
                "Ceilings may sum below one — the remainder is cash — but a total "
                "above one could only be met by leverage this layer does not have.",
            )

    @property
    def by_strategy(self) -> dict[str, StrategyBudget]:
        """Budgets keyed by strategy."""
        return {budget.strategy_id: budget for budget in self.budgets}

    @property
    def total_capital_ceiling(self) -> float:
        """The sum of every sleeve ceiling."""
        return quantise(sum(budget.max_capital_weight for budget in self.budgets))

    def body(self) -> dict[str, Any]:
        """The fingerprinted body, in strategy order."""
        return {
            "allocation_policy_id": self.allocation_policy_id,
            "allocation_policy_version": self.allocation_policy_version,
            "semantics": str(self.semantics),
            "budgets": [
                budget.as_json() for budget in sorted(self.budgets, key=lambda row: row.strategy_id)
            ],
            "total_capital_ceiling": self.total_capital_ceiling,
            "unused_sleeve_capacity": "becomes cash; never redistributed",
            "performance_weighted": False,
            "production_authorized": self.production_authorized,
        }

    def fingerprint(self) -> str:
        """The policy's digest. A changed budget or strategy changes it."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the policy with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


@dataclass(frozen=True)
class StrategyContribution:
    """One strategy's request for one security, at every stage.

    Attributes:
        strategy_id: Who asked.
        identity: What for.
        sleeve_weight: The normalised weight within that strategy's sleeve.
        global_requested_weight: Sleeve budget times the sleeve weight.
        allocated_weight: After sleeve-level limits, before global risk.
        approved_weight: After global risk, reconciled against the aggregate.
    """

    strategy_id: str
    identity: SecurityIdentity
    sleeve_weight: float
    global_requested_weight: float
    allocated_weight: float
    approved_weight: float = 0.0

    def as_json(self) -> dict[str, Any]:
        """Return the contribution as a JSON-safe mapping."""
        return {
            "strategy_id": self.strategy_id,
            "identity": self.identity.as_json(),
            "sleeve_weight": quantise(self.sleeve_weight),
            "global_requested_weight": quantise(self.global_requested_weight),
            "allocated_weight": quantise(self.allocated_weight),
            "approved_weight": quantise(self.approved_weight),
        }


@dataclass(frozen=True)
class StrategySleeve:
    """One strategy's allocated capital, and what it did with it.

    Attributes:
        strategy_id: Which strategy.
        budget: Its budget.
        intents_fingerprint: The digest of its intent set.
        contributions: Its per-identity contributions, in identity order.
        used_capital: The sleeve capital actually requested.
        standalone_volatility: The sleeve's own annualised volatility in global
            weights, where the covariance allows it.
        reason_codes: Why the sleeve was reduced, where it was.
    """

    strategy_id: str
    budget: StrategyBudget
    intents_fingerprint: str
    contributions: tuple[StrategyContribution, ...]
    used_capital: float
    standalone_volatility: float | None = None
    reason_codes: tuple[str, ...] = ()

    @property
    def unused_capital(self) -> float:
        """Sleeve capacity the strategy did not request. Becomes cash."""
        return quantise(max(self.budget.max_capital_weight - self.used_capital, 0.0))

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "strategy_id": self.strategy_id,
            "budget": self.budget.as_json(),
            "intents_fingerprint": self.intents_fingerprint,
            "contributions": [row.as_json() for row in self.contributions],
            "used_capital": quantise(self.used_capital),
            "unused_capital": self.unused_capital,
            "standalone_volatility": self.standalone_volatility,
            "reason_codes": list(self.reason_codes),
            "unused_capital_becomes_cash": True,
        }

    def fingerprint(self) -> str:
        """The sleeve's deterministic digest."""
        return digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the sleeve with its fingerprint."""
        return {**self.body(), "fingerprint": self.fingerprint()}


@dataclass
class AggregatedRequest:
    """Every sleeve, and the one economic request they add up to.

    Attributes:
        sleeves: One per strategy that submitted intents, in strategy order.
        aggregate: `{identity key: summed global weight}`.
        identities: Identity objects by key.
        policy: The allocation policy in force.
    """

    sleeves: tuple[StrategySleeve, ...]
    aggregate: dict[tuple[str, int], float]
    identities: dict[tuple[str, int], SecurityIdentity]
    policy: StrategyAllocationPolicy
    contributions: dict[tuple[str, int], list[StrategyContribution]] = field(default_factory=dict)

    @property
    def requested_gross(self) -> float:
        """The aggregate economic request, before global risk."""
        return quantise(sum(self.aggregate.values()))

    def body(self) -> dict[str, Any]:
        """The fingerprinted body, in deterministic order."""
        return {
            "allocator_id": ALLOCATOR_ID,
            "allocation_policy_fingerprint": self.policy.fingerprint(),
            "sleeves": [sleeve.as_json() for sleeve in self.sleeves],
            "aggregate": [
                {"identity": self.identities[key].label, "weight": weight}
                for key, weight in sorted(self.aggregate.items())
            ],
            "requested_gross": self.requested_gross,
            "aggregation_key": "(kind, identity_id)",
        }

    def fingerprint(self) -> str:
        """The aggregate request's deterministic digest."""
        return digest(self.body())


def _sleeve_volatility(
    contributions: Sequence[StrategyContribution], covariance: CovarianceSnapshot | None
) -> float | None:
    """A sleeve's standalone volatility in global portfolio weights.

    Args:
        contributions: The sleeve's contributions.
        covariance: The validated covariance, or None.

    Returns:
        The annualised volatility, or None when the covariance cannot describe
        every identity in the sleeve — never zero, which would read as riskless.
    """
    if covariance is None:
        return None
    weights = {row.identity.key: row.allocated_weight for row in contributions}
    if not weights or not covariance.covers(list(weights)):
        return None
    return covariance.volatility_of(weights)


def allocate(
    intents: Sequence[StrategyIntent],
    policy: StrategyAllocationPolicy,
    *,
    nav: float,
    covariance: CovarianceSnapshot | None = None,
) -> AggregatedRequest:
    """Turn several strategies' intents into one economic request.

    The stages are fixed: normalise each sleeve, scale it to its capital ceiling,
    apply the sleeve's own gross and volatility ceilings, then aggregate by durable
    identity. Every stage only reduces, and capacity a sleeve does not use becomes
    cash rather than another sleeve's budget.

    Args:
        intents: Intents from one or more strategies.
        policy: The allocation policy. Required for more than one strategy.
        nav: Portfolio net asset value, for converting a notional request.
        covariance: The validated covariance, for sleeve volatility ceilings.

    Returns:
        The sleeves and the aggregate request.

    Raises:
        ValidationError: On an unknown strategy, a missing budget, or a strategy
            whose intents cannot be normalised.
    """
    budgets = policy.by_strategy
    grouped: dict[str, list[StrategyIntent]] = {}
    for intent in intents:
        grouped.setdefault(intent.strategy_id, []).append(intent)

    unknown = sorted(set(grouped) - set(budgets))
    if unknown:
        raise ValidationError(
            ReasonCode.UNKNOWN_STRATEGY,
            f"{unknown} submitted intents but {policy.allocation_policy_id} names no "
            "budget for them. An unnamed strategy is not given a zero allocation "
            "silently; it is refused, because a strategy nobody authorised should not "
            "be able to reach the portfolio at all.",
        )

    sleeves: list[StrategySleeve] = []
    identities: dict[tuple[str, int], SecurityIdentity] = {}
    aggregate: dict[tuple[str, int], float] = {}
    contributions: dict[tuple[str, int], list[StrategyContribution]] = {}

    for strategy_id in sorted(grouped):
        budget = budgets[strategy_id]
        sleeve_intents = grouped[strategy_id]
        raw = {intent.key: intent.weight_against(nav) for intent in sleeve_intents}
        for intent in sleeve_intents:
            identities[intent.key] = intent.identity
        total = sum(raw.values())
        codes: list[str] = []
        if total <= EXPOSURE_TOLERANCE:
            normalised = dict.fromkeys(raw, 0.0)
        else:
            # Sleeve-relative: the weights describe proportions inside the sleeve,
            # so they are normalised and then scaled by the sleeve's ceiling. The
            # magnitudes alone could not say whether they were meant as relative or
            # absolute, which is why the policy declares it.
            normalised = {key: weight / total for key, weight in raw.items()}
        allocated = {
            key: quantise(share * budget.max_capital_weight) for key, share in normalised.items()
        }

        if budget.max_strategy_gross is not None:
            gross = sum(allocated.values())
            if gross > budget.max_strategy_gross + EXPOSURE_TOLERANCE:
                factor = budget.max_strategy_gross / gross
                allocated = {key: quantise(w * factor) for key, w in allocated.items()}
                codes.append(ReasonCode.STRATEGY_GROSS_LIMIT)

        sleeve_rows = [
            StrategyContribution(
                strategy_id=strategy_id,
                identity=identities[key],
                sleeve_weight=quantise(normalised[key]),
                global_requested_weight=quantise(normalised[key] * budget.max_capital_weight),
                allocated_weight=allocated[key],
            )
            for key in sorted(allocated)
        ]

        if budget.max_strategy_volatility is not None and covariance is not None:
            standalone = _sleeve_volatility(sleeve_rows, covariance)
            if standalone is not None and standalone > budget.max_strategy_volatility:
                # Volatility is homogeneous of degree one in the weights, so one
                # scale lands on the ceiling and preserves every ratio. Only this
                # sleeve moves; no other sleeve is enlarged to use the freed room.
                factor = budget.max_strategy_volatility / standalone
                sleeve_rows = [
                    StrategyContribution(
                        strategy_id=row.strategy_id,
                        identity=row.identity,
                        sleeve_weight=row.sleeve_weight,
                        global_requested_weight=row.global_requested_weight,
                        allocated_weight=quantise(row.allocated_weight * factor),
                    )
                    for row in sleeve_rows
                ]
                codes.append(ReasonCode.STRATEGY_VOLATILITY_LIMIT)

        sleeve = StrategySleeve(
            strategy_id=strategy_id,
            budget=budget,
            intents_fingerprint=intents_fingerprint(sleeve_intents),
            contributions=tuple(sleeve_rows),
            used_capital=quantise(sum(row.allocated_weight for row in sleeve_rows)),
            standalone_volatility=_sleeve_volatility(sleeve_rows, covariance),
            reason_codes=tuple(codes),
        )
        sleeves.append(sleeve)
        for row in sleeve_rows:
            aggregate[row.identity.key] = quantise(
                aggregate.get(row.identity.key, 0.0) + row.allocated_weight
            )
            contributions.setdefault(row.identity.key, []).append(row)

    # Strategies the policy names but which submitted nothing: their capital stays
    # cash. Recorded so an idle sleeve is visible rather than merely absent.
    for strategy_id in sorted(set(budgets) - set(grouped)):
        sleeves.append(
            StrategySleeve(
                strategy_id=strategy_id,
                budget=budgets[strategy_id],
                intents_fingerprint=intents_fingerprint([]),
                contributions=(),
                used_capital=0.0,
                reason_codes=(ReasonCode.STRATEGY_IDLE,),
            )
        )

    log.info(
        "portfolio.allocated",
        strategies=len(sleeves),
        identities=len(aggregate),
        requested_gross=quantise(sum(aggregate.values())),
    )
    return AggregatedRequest(
        sleeves=tuple(sorted(sleeves, key=lambda row: row.strategy_id)),
        aggregate=dict(sorted(aggregate.items())),
        identities=identities,
        policy=policy,
        contributions={key: list(rows) for key, rows in sorted(contributions.items())},
    )


#: An allocation policy for exercising the allocator. **NOT_PRODUCTION_AUTHORIZED**:
#: these sleeve budgets were chosen to force code paths, not derived from any
#: strategy's results. The strategy names are fixture identities and are not wired
#: to any implementation — P1.2 builds no strategy and reads no performance.
EXAMPLE_MULTI_STRATEGY_POLICY_V1 = StrategyAllocationPolicy(
    allocation_policy_id="EXAMPLE_MULTI_STRATEGY_POLICY_V1",
    allocation_policy_version="1",
    budgets=(
        StrategyBudget(strategy_id="COMPOUNDER_EXAMPLE", max_capital_weight=0.40),
        StrategyBudget(
            strategy_id="MOMENTUM_EXAMPLE",
            max_capital_weight=0.30,
            max_strategy_gross=0.25,
        ),
        StrategyBudget(
            strategy_id="EARNINGS_EXAMPLE",
            max_capital_weight=0.20,
            max_strategy_volatility=0.18,
        ),
    ),
    production_authorized=False,
)
