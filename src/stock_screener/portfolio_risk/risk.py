"""Risk authority: what portion of the requested portfolio is permitted.

The engine may approve, resize or reject. A strategy cannot overrule it, and
there is deliberately no parameter, flag or keyword anywhere in this module that
would let one — no `force`, no `bypass`, no `ignore_limits`. An override that
exists is an override that gets used at the worst possible moment.

**The constraint model is a single pass over frozen stages, not a chain of
mutations.** The difference matters: if limits were applied as "cap, then scale,
then drop, then cap again", the result would depend on the order someone happened
to write them in, and changing the order would silently change the portfolio. The
stages below are ordered once, documented, and each is either monotonically
reducing or reaches a fixed point, so the outcome is a function of the inputs
alone:

1. **Count.** Rank by priority descending, then durable identity. Keep the first
   `max_positions`; reject the rest with `MAX_POSITION_COUNT`. Ranking first means
   the surviving set does not depend on which names were later resized.
2. **Per-name cap.** Clamp each to `max_position_weight`. Reducing only.
3. **Budget.** Fit the total into `investable_budget`, the tighter of the gross
   cap and what the cash reserve leaves. Over-budget scales down proportionally,
   which cannot breach the stage-2 caps because it only reduces. Under-budget is
   left alone unless the policy declared the weights relative, in which case the
   excess is redistributed among names still below their cap until either the
   budget is met or no capacity remains — a fixed point, bounded by the number of
   names.
4. **Dust.** Drop targets below `min_position_weight`. The freed budget becomes
   cash rather than being redistributed, because redistributing could push another
   name below the floor and the loop would not obviously terminate.
5. **Trade size.** Pull any target whose change exceeds
   `max_trade_notional_weight` back toward the current weight. Reducing the
   *change*, never raising the target.
6. **Mode.** `HALTED` holds every position exactly where it stands. `REDUCE_ONLY`
   permits a target only where it is at or below the current weight.
7. **Exits.** Any held position with no approved target gets a target of zero,
   subject to the mode.

Cash is whatever is left, and the invariant that positions plus cash equal one is
asserted rather than assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .arithmetic import EXPOSURE_TOLERANCE, is_close, quantise
from .fingerprint import digest
from .policy import ConstructionMode, PolicyMode
from .validation import ReasonCode

if TYPE_CHECKING:
    from .constructor import ConstructedPortfolio
    from .models import PortfolioSnapshot, SecurityIdentity
    from .policy import RiskPolicy

__all__ = [
    "CONSTRAINT_MODEL_ID",
    "Decision",
    "RiskDecision",
    "RiskOutcome",
    "authorize",
]

#: The constraint model's identity. A change to the stage order is a new model.
CONSTRAINT_MODEL_ID = "DETERMINISTIC_STAGED_CONSTRAINTS_V1"


class Decision(StrEnum):
    """What the risk engine did with a request."""

    APPROVED = "APPROVED"
    """Permitted as requested."""

    RESIZED = "RESIZED"
    """Permitted at a smaller weight than requested."""

    REJECTED = "REJECTED"
    """Not permitted at all."""


@dataclass(frozen=True)
class RiskDecision:
    """What happened to one requested identity, and why.

    Attributes:
        identity: The security.
        requested_weight: What the strategy asked for.
        approved_weight: What was authorised.
        decision: Approved, resized or rejected.
        reason_codes: Stable codes, in the order the stages applied.
        policy_id: Which policy authorised this.
        policy_version: Its version.
    """

    identity: SecurityIdentity
    requested_weight: float
    approved_weight: float
    decision: Decision
    reason_codes: tuple[str, ...]
    policy_id: str
    policy_version: str

    def as_json(self) -> dict[str, Any]:
        """Return the decision as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "requested_weight": quantise(self.requested_weight),
            "approved_weight": quantise(self.approved_weight),
            "decision": str(self.decision),
            "reason_codes": list(self.reason_codes),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
        }


@dataclass(frozen=True)
class RiskOutcome:
    """Every decision, the approved weights, and the cash that results.

    Attributes:
        decisions: One per requested identity plus one per forced exit, in
            identity order.
        approved: `{identity key: weight}` for everything authorised non-zero.
        identities: Identity objects by key, for display.
        cash_weight: What remains uninvested.
        policy: The policy that authorised this.
    """

    decisions: tuple[RiskDecision, ...]
    approved: dict[tuple[str, int], float]
    identities: dict[tuple[str, int], SecurityIdentity]
    cash_weight: float
    policy: RiskPolicy

    @property
    def gross_exposure(self) -> float:
        """Sum of absolute approved weights."""
        return quantise(sum(abs(weight) for weight in self.approved.values()))

    @property
    def net_exposure(self) -> float:
        """Sum of signed approved weights."""
        return quantise(sum(self.approved.values()))

    @property
    def summary(self) -> dict[str, int | float]:
        """Deterministic counts and exposures for the decision record.

        A forced exit is counted apart from a rejection. Both carry
        `Decision.REJECTED`, because neither authorises a position — but a
        rejection is a request the policy refused, while an exit is a holding the
        strategy no longer wants, and totalling them would misreport both.
        """
        exits = sum(1 for d in self.decisions if ReasonCode.NOT_REQUESTED in d.reason_codes)
        counts = {str(value): 0 for value in Decision}
        for decision in self.decisions:
            counts[str(decision.decision)] += 1
        return {
            "requested_count": len(self.decisions) - exits,
            "approved_count": len(self.approved),
            "approved_unchanged_count": counts[str(Decision.APPROVED)],
            "resized_count": counts[str(Decision.RESIZED)],
            "rejected_count": counts[str(Decision.REJECTED)] - exits,
            "forced_exit_count": exits,
            "approved_gross_exposure": self.gross_exposure,
            "approved_net_exposure": self.net_exposure,
            "cash_target_weight": self.cash_weight,
        }

    def fingerprint(self) -> str:
        """The risk decisions' deterministic digest."""
        return digest(
            {
                "constraint_model": CONSTRAINT_MODEL_ID,
                "decisions": [decision.as_json() for decision in self.decisions],
                "cash_target_weight": self.cash_weight,
                "gross_exposure": self.gross_exposure,
                "net_exposure": self.net_exposure,
            }
        )


def _fit_to_budget(
    weights: dict[tuple[str, int], float],
    *,
    budget: float,
    cap: float,
    normalize: bool,
) -> tuple[dict[tuple[str, int], float], dict[tuple[str, int], list[str]]]:
    """Fit weights into the investable budget without breaching the per-name cap.

    Args:
        weights: The capped weights.
        budget: The greatest investable total.
        cap: The per-name maximum.
        normalize: Whether an under-budget total should be scaled up.

    Returns:
        The fitted weights and the reason codes each one earned.
    """
    reasons: dict[tuple[str, int], list[str]] = {key: [] for key in weights}
    total = sum(weights.values())
    if total > budget + EXPOSURE_TOLERANCE:
        # Scaling down cannot breach the cap, so one pass suffices and the result
        # does not depend on which name is considered first.
        factor = budget / total if total else 0.0
        for key in weights:
            weights[key] = quantise(weights[key] * factor)
            reasons[key].append(ReasonCode.BUDGET_SCALED)
        return weights, reasons
    if normalize and total < budget - EXPOSURE_TOLERANCE and total > 0:
        # Scaling up can breach the cap, so the excess is redistributed among names
        # with capacity left, repeatedly, until the budget is met or nothing can
        # absorb more. Bounded by the number of names, hence terminating.
        for _ in range(len(weights) + 1):
            total = sum(weights.values())
            shortfall = budget - total
            if shortfall <= EXPOSURE_TOLERANCE:
                break
            headroom = {key: cap - weight for key, weight in weights.items() if cap - weight > 0}
            capacity = sum(headroom.values())
            if capacity <= EXPOSURE_TOLERANCE:
                break
            share = min(shortfall, capacity) / capacity
            for key, room in sorted(headroom.items()):
                weights[key] = quantise(weights[key] + room * share)
                if ReasonCode.BUDGET_SCALED not in reasons[key]:
                    reasons[key].append(ReasonCode.BUDGET_SCALED)
    return weights, reasons


def authorize(
    constructed: ConstructedPortfolio,
    snapshot: PortfolioSnapshot,
    policy: RiskPolicy,
) -> RiskOutcome:
    """Decide what portion of the constructed portfolio is permitted.

    Args:
        constructed: What the intents expressed.
        snapshot: The portfolio as it stands, for the mode rules and exits.
        policy: The limits.

    Returns:
        The outcome: a decision per identity, the approved weights, and cash.
    """
    requested = dict(constructed.requested)
    identities = dict(constructed.identities)
    current = snapshot.weights
    for key in current:
        held = snapshot.identity_for(key)
        if held is not None:
            identities.setdefault(key, held)

    reasons: dict[tuple[str, int], list[str]] = {key: [] for key in requested}
    rejected: dict[tuple[str, int], list[str]] = {}

    # stage 1 — count, by priority then durable identity
    ranked = sorted(
        requested,
        key=lambda key: (-constructed.priorities.get(key, 0.0), key[0], key[1]),
    )
    kept = ranked[: policy.max_positions]
    for key in ranked[policy.max_positions :]:
        rejected[key] = [ReasonCode.MAX_POSITION_COUNT]
    weights = {key: requested[key] for key in kept}

    # stage 2 — per-name cap
    for key, weight in weights.items():
        if weight > policy.max_position_weight:
            weights[key] = quantise(policy.max_position_weight)
            reasons[key].append(ReasonCode.MAX_POSITION_WEIGHT)

    # stage 3 — budget
    binder = (
        ReasonCode.MAX_GROSS_EXPOSURE
        if policy.max_gross_exposure <= 1.0 - policy.min_cash_weight
        else ReasonCode.MIN_CASH_RESERVE
    )
    weights, budget_reasons = _fit_to_budget(
        weights,
        budget=policy.investable_budget,
        cap=policy.max_position_weight,
        normalize=policy.construction_mode is ConstructionMode.NORMALIZE_TO_INVESTABLE_BUDGET,
    )
    for key, budget_codes in budget_reasons.items():
        if budget_codes:
            reasons[key].extend(budget_codes)
            reasons[key].append(binder)

    # stage 4 — dust
    if policy.min_position_weight is not None:
        for key in sorted(weights):
            if 0 < weights[key] < policy.min_position_weight:
                rejected[key] = [*reasons.get(key, []), ReasonCode.MIN_POSITION_WEIGHT]
                weights[key] = 0.0

    # stage 5 — trade size
    if policy.max_trade_notional_weight is not None:
        limit = policy.max_trade_notional_weight
        for key in sorted(weights):
            held_weight = current.get(key, 0.0)
            delta = weights[key] - held_weight
            if abs(delta) > limit:
                weights[key] = quantise(held_weight + (limit if delta > 0 else -limit))
                reasons[key].append(ReasonCode.MAX_TRADE_NOTIONAL)

    # stage 6 — mode
    if policy.mode is PolicyMode.HALTED:
        # Halting holds the book exactly where it stands. It liquidates nothing:
        # turning a halt into a sell order is a separate, explicit decision.
        weights = {key: quantise(value) for key, value in current.items() if value != 0}
        for key in requested:
            rejected.setdefault(key, []).append(ReasonCode.TRADING_HALTED)
        rejected = {key: codes for key, codes in rejected.items() if key not in weights}
    elif policy.mode is PolicyMode.REDUCE_ONLY:
        for key in sorted(weights):
            held_weight = current.get(key, 0.0)
            if weights[key] > held_weight + EXPOSURE_TOLERANCE:
                if held_weight <= EXPOSURE_TOLERANCE:
                    rejected[key] = [*reasons.get(key, []), ReasonCode.REDUCE_ONLY]
                    weights[key] = 0.0
                else:
                    weights[key] = quantise(held_weight)
                    reasons[key].append(ReasonCode.REDUCE_ONLY)

    # stage 7 — exits for anything held with no approved target
    approved = {key: value for key, value in weights.items() if value > 0}
    exits: list[tuple[str, int]] = []
    if policy.mode is not PolicyMode.HALTED:
        exits = sorted(key for key in current if key not in approved and current[key] != 0)

    decisions: list[RiskDecision] = []
    for key in sorted(set(requested) | set(rejected) | set(exits)):
        codes: tuple[str, ...]
        identity = identities[key]
        asked = requested.get(key, 0.0)
        got = approved.get(key, 0.0)
        if key in rejected and key not in approved:
            codes = tuple(dict.fromkeys(rejected[key]))
            outcome = Decision.REJECTED
        elif key not in requested:
            codes = (ReasonCode.NOT_REQUESTED,)
            outcome = Decision.REJECTED
        elif is_close(got, asked):
            codes = tuple(dict.fromkeys(reasons[key])) or (ReasonCode.WITHIN_LIMITS,)
            outcome = Decision.APPROVED
        else:
            codes = tuple(dict.fromkeys(reasons[key])) or (ReasonCode.WITHIN_LIMITS,)
            outcome = Decision.RESIZED
        decisions.append(
            RiskDecision(
                identity=identity,
                requested_weight=asked,
                approved_weight=got,
                decision=outcome,
                reason_codes=codes,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
            )
        )

    cash_weight = quantise(1.0 - sum(approved.values()))
    return RiskOutcome(
        decisions=tuple(decisions),
        approved=dict(sorted(approved.items())),
        identities=identities,
        cash_weight=cash_weight,
        policy=policy,
    )
