"""Translating a provider's order status into this engine's lifecycle state.

A provider has more statuses than this engine has states, and the extra ones are
not noise — `pending_cancel` is genuinely different from `canceled`, and treating
them alike would report an order as finished while it is still working.

So the map is **exhaustive by declaration and closed by default**. A status this
module has not been taught is `BROKER_STATUS_UNMAPPED`, which stops the sweep for
that order rather than guessing. The permissive alternative is the one to avoid:
a default branch mapping anything unrecognised to `REJECTED` would, the day a
provider adds a status, silently report working orders as dead and release their
reserved quantity back into the next plan.

Two statuses are mapped to **no** local state at all, and that is the subtle
part. `pending_new` and `accepted_for_bidding` mean the venue has the order but
has not made it working; there is no local state for "being thought about", and
inventing one would widen the frozen lifecycle. They resolve to `None` — the
caller holds its local state and asks again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.state_machine import TERMINAL_STATES, OrderState

__all__ = [
    "PROVIDER_STATUS_MAP",
    "STATUS_MAP_POLICY",
    "StatusReading",
    "read_provider_status",
    "status_map_policy",
]

#: How a provider status becomes a local state: by declaration only, with an
#: unknown status refused rather than defaulted.
STATUS_MAP_POLICY = "EXPLICIT_PROVIDER_STATUS_MAP_V1"

#: Every provider status this adapter understands, and what it means locally.
#: `None` means the status carries no local state — the order is at the venue but
#: not yet working, and the frozen lifecycle has no state for that.
PROVIDER_STATUS_MAP: dict[str, OrderState | None] = {
    # working
    "new": OrderState.ACKNOWLEDGED,
    "accepted": OrderState.ACKNOWLEDGED,
    "held": OrderState.ACKNOWLEDGED,
    "partially_filled": OrderState.PARTIALLY_FILLED,
    # not yet working: no local state, deliberately
    "pending_new": None,
    "accepted_for_bidding": None,
    # a cancel or a replace has been asked for and has not happened. Still
    # working: reporting CANCELLED here would release quantity that can still
    # fill, and a fill after that release is an unplanned position.
    "pending_cancel": OrderState.ACKNOWLEDGED,
    "pending_replace": OrderState.ACKNOWLEDGED,
    "calculated": OrderState.ACKNOWLEDGED,
    # terminal
    "filled": OrderState.FILLED,
    "canceled": OrderState.CANCELLED,
    "cancelled": OrderState.CANCELLED,
    "expired": OrderState.EXPIRED,
    "done_for_day": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
    "suspended": OrderState.REJECTED,
    "stopped": OrderState.CANCELLED,
    "replaced": OrderState.CANCELLED,
}


@dataclass(frozen=True)
class StatusReading:
    """What one provider status was read as.

    Attributes:
        provider_status: The status as the provider spelled it.
        state: The local state, or `None` when the status carries none.
        terminal: Whether the local state is terminal.
    """

    provider_status: str
    state: OrderState | None
    terminal: bool

    @property
    def carries_state(self) -> bool:
        """Whether this status says anything about the local lifecycle."""
        return self.state is not None

    def as_json(self) -> dict[str, Any]:
        """Return the reading as a JSON-safe mapping."""
        return {
            "provider_status": self.provider_status,
            "state": str(self.state) if self.state else None,
            "terminal": self.terminal,
        }


def read_provider_status(provider_status: str | None, *, context: str = "") -> StatusReading:
    """Read a provider order status.

    Args:
        provider_status: The status string the provider reported.
        context: What was being read, for the message.

    Returns:
        The reading.

    Raises:
        ExecutionError: `BROKER_STATUS_UNMAPPED` if the status is missing or not
            declared above. Refused rather than defaulted: a default would, on
            the day the provider adds a status, report working orders as dead and
            release their quantity into the next plan.
    """
    if not provider_status or not provider_status.strip():
        raise ExecutionError(
            Code.BROKER_STATUS_UNMAPPED,
            f"{context or 'reading an order'}: the provider reported no status, and "
            "an order with no status has no knowable state",
        )
    key = provider_status.strip().lower()
    if key not in PROVIDER_STATUS_MAP:
        raise ExecutionError(
            Code.BROKER_STATUS_UNMAPPED,
            f"{context or 'reading an order'}: provider status {provider_status!r} is "
            "not in this adapter's map. It is refused rather than guessed — mapping "
            "an unknown status to a terminal state would report a working order as "
            f"dead. Known: {sorted(PROVIDER_STATUS_MAP)}",
        )
    state = PROVIDER_STATUS_MAP[key]
    return StatusReading(
        provider_status=provider_status.strip(),
        state=state,
        terminal=state in TERMINAL_STATES if state is not None else False,
    )


def status_map_policy() -> dict[str, Any]:
    """Describe the status map, for the manifest.

    Returns:
        The policy and the full declared map.
    """
    return {
        "policy": STATUS_MAP_POLICY,
        "unknown_status": "REFUSED as BROKER_STATUS_UNMAPPED; never defaulted",
        "map": {
            status: (str(state) if state else None)
            for status, state in sorted(PROVIDER_STATUS_MAP.items())
        },
        "states_without_local_meaning": sorted(
            status for status, state in PROVIDER_STATUS_MAP.items() if state is None
        ),
    }
