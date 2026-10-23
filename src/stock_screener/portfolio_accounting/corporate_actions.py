"""Which corporate actions this engine models, and why the rest are refused.

The supported set is two: a cash dividend and a share split. That is a small set,
and the smallness is the design. Every other action's accounting treatment
depends on terms an event cannot carry — a merger's exchange ratio and cash
component, a spinoff's allocation of basis between parent and child, a rights
distribution's subscription price, the tax character of a return of capital. An
engine that guessed at those would produce a cost basis that looks exactly like a
correct one and is wrong by an amount nobody can later recover.

So the policy is stated rather than implied: supported actions are applied,
everything else is refused by name with `UNSUPPORTED_CORPORATE_ACTION`, and the
refusal says what to do instead — record the resulting fills, cash movements and
quantity changes as explicit events, where a human has decided the economics and
the ledger records the decision rather than inventing it.

Silence is the failure mode this prevents. An unmodelled action that is simply
ignored leaves the ledger confidently describing a security that no longer exists
as recorded, and nothing in the system ever asks about it again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .errors import AccountingError, Code
from .events import UNSUPPORTED_ACTIONS, EventType

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "CORPORATE_ACTION_POLICY",
    "SUPPORTED_ACTIONS",
    "action_policy",
    "classify_action",
    "refuse_unsupported",
]

#: The corporate-action contract. Adding a supported action is a new version,
#: because a ledger replayed under a wider policy is a different portfolio.
CORPORATE_ACTION_POLICY = "PORTFOLIO_CORPORATE_ACTIONS_V1"

#: What this engine applies, and the treatment it applies.
SUPPORTED_ACTIONS: Mapping[str, str] = {
    "CASH_DIVIDEND": (
        "credited to cash as income, with cost basis untouched. Entitlement is "
        "the producer's responsibility: inferring it from current holdings would "
        "miss a position sold between the ex-date and the pay date and would "
        "credit one bought after it."
    ),
    "SPLIT": (
        "quantity scales by the ratio and total cost does not, so average cost "
        "moves by exactly the ratio and no gain or loss is created. A reverse "
        "split's fractional residue is recorded rather than rounded away."
    ),
}

#: Why each named unsupported action needs terms an event cannot carry.
UNSUPPORTED_REASONS: Mapping[str, str] = {
    "MERGER": (
        "the treatment depends on the exchange ratio, any cash component and "
        "whether the exchange is taxable; each decides a different cost basis "
        "for the surviving holding"
    ),
    "SPINOFF": (
        "basis must be allocated between parent and child by relative fair "
        "value at distribution, which the event does not carry"
    ),
    "RIGHTS_DISTRIBUTION": (
        "the subscription price and whether the rights were exercised, sold or "
        "lapsed each produce a different outcome"
    ),
    "TENDER_OFFER": (
        "the proration factor decides how much was actually tendered, and it is "
        "not known until after the offer closes"
    ),
    "STOCK_DIVIDEND": (
        "it resembles a split but is not always one; whether basis is "
        "reallocated depends on the character of the distribution"
    ),
    "SYMBOL_SUBSTITUTION": (
        "a symbol change is display-only and already harmless here, since "
        "identity is `(kind, identity_id)` and never the ticker. A substitution "
        "that actually changes the security is a different security"
    ),
    "CASH_IN_LIEU": (
        "it realises a fractional position at a price the event does not state, "
        "so the realised amount would be invented"
    ),
    "RETURN_OF_CAPITAL": (
        "it reduces cost basis rather than being income, and treating it as a "
        "dividend would overstate income and understate every later realised gain"
    ),
}


def classify_action(action: str) -> dict[str, Any]:
    """Say whether an action is modelled, and what follows.

    Args:
        action: The action's name, however the producer spelled it.

    Returns:
        The classification, with the treatment for a supported action and the
        reason for an unsupported one.
    """
    normalised = action.strip().upper().replace(" ", "_").replace("-", "_")
    if normalised in SUPPORTED_ACTIONS:
        return {
            "action": normalised,
            "supported": True,
            "event_type": (
                str(EventType.CASH_DIVIDEND)
                if normalised == "CASH_DIVIDEND"
                else str(EventType.SPLIT)
            ),
            "treatment": SUPPORTED_ACTIONS[normalised],
            "reason": None,
        }
    return {
        "action": normalised,
        "supported": False,
        "event_type": None,
        "treatment": None,
        "named": normalised in UNSUPPORTED_ACTIONS,
        "reason": UNSUPPORTED_REASONS.get(
            normalised,
            "this engine has no treatment for it, and an action it cannot name is "
            "one it certainly cannot account for",
        ),
    }


def refuse_unsupported(action: str, *, identity_label: str) -> None:
    """Refuse an action this engine does not model.

    Args:
        action: The action's name.
        identity_label: The affected security, for the message.

    Raises:
        AccountingError: `UNSUPPORTED_CORPORATE_ACTION` unless the action is
            supported, in which case nothing is raised.
    """
    classification = classify_action(action)
    if classification["supported"]:
        return
    raise AccountingError(
        Code.UNSUPPORTED_CORPORATE_ACTION,
        f"{identity_label}: {classification['action']} is not modelled — "
        f"{classification['reason']}. Record the resulting fills, cash and "
        "quantity changes as explicit events, so the economics are decided by "
        "someone who knows the terms and the ledger records that decision rather "
        "than a guess.",
    )


def action_policy() -> dict[str, Any]:
    """Describe the corporate-action policy.

    Returns:
        The policy, for the manifest and for anyone asking what is supported.
    """
    return {
        "policy": CORPORATE_ACTION_POLICY,
        "supported": dict(sorted(SUPPORTED_ACTIONS.items())),
        "refused_by_name": dict(sorted(UNSUPPORTED_REASONS.items())),
        "default": "REFUSE",
        "rationale": (
            "an unmodelled action is refused rather than approximated, because a "
            "guessed cost basis is indistinguishable from a correct one "
            "downstream, and an ignored action leaves the ledger describing a "
            "security that no longer exists as recorded"
        ),
    }
