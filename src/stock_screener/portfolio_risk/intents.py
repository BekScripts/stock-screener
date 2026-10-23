"""What a strategy may request, and what the engine emits for execution.

A `StrategyIntent` is a request, not an instruction. That distinction is the whole
point of this layer: a strategy says what it would like to hold and why, and
something else decides what is actually authorised. The type is therefore
deliberately thin — it carries no order fields, no quantities, no broker
concepts, and nothing a strategy could use to claim priority over the risk
engine.

An `ExecutionIntent` is inert data describing a difference between two
portfolios. It is not an order: it has no side-effect, no broker identifier, no
time in force, no quantity in shares. Those belong to an execution layer that
does not exist yet, and keeping them out means nothing in this package can
accidentally become a trade.

`Action` classifies the difference without implying sequencing. Whether exits
settle before entries is an execution concern that depends on the broker and the
settlement regime, so this layer labels the change and says nothing about order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .arithmetic import HOLD_TOLERANCE, is_close, quantise
from .fingerprint import digest

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .models import SecurityIdentity

__all__ = [
    "Action",
    "Direction",
    "ExecutionIntent",
    "IntentError",
    "StrategyIntent",
    "classify",
    "intents_fingerprint",
]


class IntentError(ValueError):
    """Raised when an intent could not have been meant as stated."""


class Direction(StrEnum):
    """Which side of the market an intent is on."""

    LONG = "LONG"
    SHORT = "SHORT"


class Action(StrEnum):
    """How a target differs from what is currently held.

    These classify a difference. They imply no ordering between one another: a
    broker and a settlement regime decide whether an exit must precede an entry,
    and this layer has no basis to assume either.
    """

    ENTER = "ENTER"
    """Not currently held; the target is non-zero."""

    INCREASE = "INCREASE"
    """Held, and the target is larger."""

    REDUCE = "REDUCE"
    """Held, and the target is smaller but still non-zero."""

    EXIT = "EXIT"
    """Held, and the target is zero."""

    HOLD = "HOLD"
    """Held at the target already, within the hold tolerance."""


@dataclass(frozen=True)
class StrategyIntent:
    """One security a strategy would like to hold, and why.

    A strategy's internal scoring model is its own. What reaches this layer is
    normalised: a requested weight, a priority for deterministic tie-breaking, and
    a fingerprint binding the request to the evidence that produced it.

    Attributes:
        strategy_id: Which strategy is asking.
        strategy_version: Its frozen version, so a changed strategy is visible.
        decision_id: The decision this intent belongs to.
        as_of: The moment the strategy decided, ISO 8601 with offset.
        identity: The security, by durable key.
        direction: Long or short. The baseline policy enables only long.
        requested_weight: The fraction of NAV requested. Non-negative for a long.
        priority: Rank or conviction, higher first. The deterministic basis for
            truncation when more names are requested than the policy allows; ties
            fall back to durable identity, never to iteration order.
        reason: Why the strategy wants it. Prose, for a human reading the record.
        source_fingerprint: The digest of the evidence behind the request. Absent
            is refused: an unattributable request cannot be audited later.
        requested_notional: An absolute amount instead of a weight, when the
            strategy thinks that way. Converted against NAV before construction.
    """

    strategy_id: str
    strategy_version: str
    decision_id: str
    as_of: str
    identity: SecurityIdentity
    direction: Direction = Direction.LONG
    requested_weight: float | None = None
    priority: float = 0.0
    reason: str | None = None
    source_fingerprint: str | None = None
    requested_notional: float | None = None

    def __post_init__(self) -> None:
        """Refuse a malformed request rather than repairing it.

        Raises:
            IntentError: On a missing or unusable weight, a non-finite value, a
                missing source fingerprint, or both a weight and a notional.
        """
        if not self.strategy_id:
            raise IntentError("an intent must name the strategy that issued it")
        if not self.source_fingerprint:
            raise IntentError(
                f"{self.identity.label}: an intent must carry a source_fingerprint. "
                "A request nothing can be traced back to cannot be audited, and "
                "silently accepting one is how an unattributable position appears."
            )
        if (self.requested_weight is None) == (self.requested_notional is None):
            raise IntentError(
                f"{self.identity.label}: supply exactly one of requested_weight or "
                "requested_notional; supplying both leaves which one governs undefined"
            )
        for name in ("requested_weight", "requested_notional", "priority"):
            value = getattr(self, name)
            if value is None:
                continue
            try:
                quantise(float(value))
            except ValueError as exc:
                raise IntentError(f"{self.identity.label}: {name} {exc}") from exc

    def weight_against(self, nav: float) -> float:
        """The requested weight, converting a notional against NAV if needed.

        Args:
            nav: The portfolio's net asset value.

        Returns:
            The requested fraction of NAV.

        Raises:
            IntentError: If NAV is not positive, which leaves a notional request
                unconvertible.
        """
        if self.requested_weight is not None:
            return quantise(self.requested_weight)
        if nav <= 0:
            raise IntentError(
                f"{self.identity.label}: a notional request cannot be converted against "
                f"a non-positive NAV ({nav!r})"
            )
        return quantise(float(self.requested_notional or 0.0) / nav)

    @property
    def key(self) -> tuple[str, int]:
        """The durable identity key."""
        return self.identity.key

    @property
    def sort_key(self) -> tuple[float, str, int]:
        """Deterministic ordering: priority descending, then durable identity."""
        return (-self.priority, self.identity.kind, self.identity.identity_id)

    def as_json(self) -> dict[str, Any]:
        """Return the intent as a JSON-safe mapping."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "decision_id": self.decision_id,
            "as_of": self.as_of,
            "identity": self.identity.as_json(),
            "direction": str(self.direction),
            "requested_weight": (
                None if self.requested_weight is None else quantise(self.requested_weight)
            ),
            "requested_notional": self.requested_notional,
            "priority": self.priority,
            "reason": self.reason,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class ExecutionIntent:
    """An inert description of one position's required change.

    Not an order. There is no quantity in shares, no broker symbol, no time in
    force and no side-effect — an execution layer will later turn these into
    orders, and until it exists nothing here can become a trade.

    Attributes:
        identity: The security.
        action: How the target differs from the holding.
        current_weight: What is held now.
        target_weight: What is authorised.
        notional_delta: The signed change in market value, against the snapshot
            NAV. Informational: share counts and lot sizing belong to execution.
        reason_codes: Why the target is what it is, machine-readable.
        strategy_id: Which strategy requested it, where one did.
    """

    identity: SecurityIdentity
    action: Action
    current_weight: float
    target_weight: float
    notional_delta: float
    reason_codes: tuple[str, ...] = ()
    strategy_id: str | None = None

    @property
    def weight_delta(self) -> float:
        """The signed change in weight."""
        return quantise(self.target_weight - self.current_weight)

    def as_json(self) -> dict[str, Any]:
        """Return the intent as a JSON-safe mapping."""
        return {
            "identity": self.identity.as_json(),
            "action": str(self.action),
            "current_weight": quantise(self.current_weight),
            "target_weight": quantise(self.target_weight),
            "weight_delta": self.weight_delta,
            "notional_delta": quantise(self.notional_delta),
            "reason_codes": list(self.reason_codes),
            "strategy_id": self.strategy_id,
            "is_an_order": False,
            "broker_fields": None,
        }


def classify(current_weight: float, target_weight: float) -> Action:
    """Classify how a target differs from a holding.

    Args:
        current_weight: What is held.
        target_weight: What is authorised.

    Returns:
        The action. A difference within `HOLD_TOLERANCE` is `HOLD`, because
        trading a basis point of drift costs more than the drift.
    """
    if is_close(current_weight, target_weight, tolerance=HOLD_TOLERANCE):
        return Action.HOLD
    held = not is_close(current_weight, 0.0, tolerance=HOLD_TOLERANCE)
    wanted = not is_close(target_weight, 0.0, tolerance=HOLD_TOLERANCE)
    if not held and wanted:
        return Action.ENTER
    if held and not wanted:
        return Action.EXIT
    return Action.INCREASE if target_weight > current_weight else Action.REDUCE


def intents_fingerprint(intents: Sequence[StrategyIntent]) -> str:
    """Digest an intent set, independent of the order it was supplied in.

    Args:
        intents: The intents.

    Returns:
        SHA-256 over the canonical form, sorted by durable identity.
    """
    return digest(
        [
            intent.as_json()
            for intent in sorted(intents, key=lambda row: (row.strategy_id, row.identity))
        ]
    )
