"""What a cancellation request established, which is never its status code.

A cancellation is a mutation, so it inherits every hazard submission has: the
request may be accepted and the reply lost, and repeating a `DELETE` on that
evidence is how a second one gets sent. But it also has a hazard submission does
not. A cancellation can be *accepted* and still not happen, because the order
can lose a race to a fill. A venue answering `204` means "I have taken your
request", not "the order is dead".

So the outcome is never read from the mutation. It is read from the order.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .models import ExternalOrderSnapshot

__all__ = [
    "CANCELLATION_PROTOCOL",
    "CancellationOutcome",
    "CancellationResult",
]

#: How a cancellation is made durable and settled. In the manifest alongside the
#: submission protocol, because it has the same one-mutation discipline.
CANCELLATION_PROTOCOL = "DURABLE_INTENT_THEN_READ_BACK_NEVER_RESEND_V1"


class CancellationOutcome(StrEnum):
    """What is known about one cancellation."""

    NOT_REQUESTED = "NOT_REQUESTED"
    """Nothing was sent — the order was refused locally before any request."""

    REQUEST_DURABLE = "REQUEST_DURABLE"
    """The intent is on disk and the request has not been made. The state a
    crash between the two leaves behind."""

    REMOTE_CANCELLED = "REMOTE_CANCELLED"
    """The venue reports the order terminal and unfilled. The cancellation
    worked, and a read proved it rather than a status code implying it."""

    REMOTE_FILLED = "REMOTE_FILLED"
    """The order filled before the cancellation took effect. Its fills stand.
    Kept distinct from `REMOTE_CANCELLED` because reporting a filled order as
    cancelled would release quantity the book actually owns."""

    REMOTE_STILL_OPEN = "REMOTE_STILL_OPEN"
    """The venue still has the order working. The cancellation is not complete,
    and treating it as complete would release quantity that can still fill."""

    REMOTE_STATE_UNKNOWN = "REMOTE_STATE_UNKNOWN"
    """The order could not be read back. The one outcome that must never be
    treated as any of the others, and the one that stops progression."""

    @property
    def is_settled(self) -> bool:
        """Whether the order's fate is known."""
        return self in {
            CancellationOutcome.NOT_REQUESTED,
            CancellationOutcome.REMOTE_CANCELLED,
            CancellationOutcome.REMOTE_FILLED,
        }

    @property
    def is_terminal(self) -> bool:
        """Whether the order is finished at the venue."""
        return self in {
            CancellationOutcome.REMOTE_CANCELLED,
            CancellationOutcome.REMOTE_FILLED,
        }

    @property
    def may_request_again(self) -> bool:
        """Whether a further, deliberate cancellation request is admissible.

        True only for `REMOTE_STILL_OPEN`, where a read has *proven* the order
        is still working. Never true for `REMOTE_STATE_UNKNOWN`: that is the
        state in which a second `DELETE` would be a blind retry, which is the
        thing this protocol exists to prevent.
        """
        return self is CancellationOutcome.REMOTE_STILL_OPEN


@dataclass(frozen=True)
class CancellationResult:
    """What one cancellation established.

    Attributes:
        remote_order_id: The venue's order id.
        outcome: What is known.
        snapshot: The venue's view of the order, when it could be read.
        detail: A sanitised explanation. Never carries a credential.
        mutating_requests: How many mutating requests this made. One per
            `cancel_order` call, zero for a resolution-only read.
        lookups: How many read requests settling it required.
        request_id: The provider's request id for the mutation, where sent.
    """

    remote_order_id: str
    outcome: CancellationOutcome
    snapshot: ExternalOrderSnapshot | None = None
    detail: str = ""
    mutating_requests: int = 0
    lookups: int = 0
    request_id: str | None = None

    @property
    def filled_quantity(self) -> str:
        """How much the venue says traded, as an exact string."""
        return str(self.snapshot.filled_quantity) if self.snapshot else "0"

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "protocol": CANCELLATION_PROTOCOL,
            "remote_order_id": self.remote_order_id,
            "outcome": str(self.outcome),
            "settled": self.outcome.is_settled,
            "terminal": self.outcome.is_terminal,
            "may_request_again": self.outcome.may_request_again,
            "snapshot": self.snapshot.as_json() if self.snapshot else None,
            "detail": self.detail,
            "mutating_requests": self.mutating_requests,
            "lookups": self.lookups,
            "request_id": self.request_id,
        }
