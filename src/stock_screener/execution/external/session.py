"""Which venue a run uses — one, named, and never two.

A session fills its orders either in the in-process deterministic simulator or
at an external paper venue. Not both, and not "mostly one". The temptation to
mix is real and specific: a developer whose venue is unreachable wants the run to
carry on locally, and a run that falls back has produced a journal in which some
fills were simulated and some were real, with nothing in the records saying
which. Every number derived from it — the cost report, the realised P&L, the
participation ceiling — is then a blend of two incompatible things.

So the venue is declared once, written into every artefact, and checked. An
artefact from one venue entering a session bound to the other is
`EXTERNAL_VENUE_MISMATCH`.

There is no third member of this enum, no `AUTO`, and no fallback. A venue that
cannot be reached ends the run; it does not quietly become a simulation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError

if TYPE_CHECKING:
    from .binding import ExternalBrokerAccountBinding

__all__ = [
    "VENUE_BINDING_POLICY",
    "ExecutionVenue",
    "VenueBinding",
]

#: How a run's venue is decided: once, by declaration, with no fallback.
VENUE_BINDING_POLICY = "SINGLE_VENUE_PER_SESSION_NO_FALLBACK_V1"


class ExecutionVenue(StrEnum):
    """Where a session's fills come from."""

    LOCAL_DETERMINISTIC_PAPER = "LOCAL_DETERMINISTIC_PAPER"
    """The in-process simulator of P2.0/P2.1. Contacts nothing."""

    EXTERNAL_PAPER = "EXTERNAL_PAPER"
    """A provider's paper venue, through the adapter. Contacts a network."""

    @property
    def is_external(self) -> bool:
        """Whether this venue reaches a network."""
        return self is ExecutionVenue.EXTERNAL_PAPER

    @property
    def is_live(self) -> bool:
        """Always false. There is no live venue in this enum, by construction."""
        return False


@dataclass(frozen=True)
class VenueBinding:
    """The venue one session uses, and the account behind it when it is external.

    Attributes:
        venue: Which venue.
        account: The paper account binding, required for an external venue and
            forbidden for a local one. A local session holding a credentialed
            binding is a configuration that would eventually be used.
    """

    venue: ExecutionVenue
    account: ExternalBrokerAccountBinding | None = None

    def __post_init__(self) -> None:
        """Refuse a binding that describes two venues or neither.

        Raises:
            ExecutionError: `EXTERNAL_VENUE_MISMATCH` if an external venue has no
                account, or a local venue carries one.
        """
        if self.venue.is_external and self.account is None:
            raise ExecutionError(
                Code.EXTERNAL_VENUE_MISMATCH,
                "an external paper session needs a proven account binding; without "
                "one there is nothing to submit to and nothing to reconcile against",
            )
        if not self.venue.is_external and self.account is not None:
            raise ExecutionError(
                Code.EXTERNAL_VENUE_MISMATCH,
                "a local deterministic session must not carry a broker account "
                "binding. The simulator cannot use it, and a session that holds "
                "one is a configuration that will eventually be used by accident",
            )

    @classmethod
    def local(cls) -> VenueBinding:
        """Bind a session to the in-process simulator.

        Returns:
            The binding.
        """
        return cls(venue=ExecutionVenue.LOCAL_DETERMINISTIC_PAPER)

    @classmethod
    def external(cls, account: ExternalBrokerAccountBinding) -> VenueBinding:
        """Bind a session to a proven external paper account.

        Args:
            account: The proven paper binding.

        Returns:
            The binding.
        """
        return cls(venue=ExecutionVenue.EXTERNAL_PAPER, account=account)

    def require(self, venue: ExecutionVenue, *, context: str) -> None:
        """Refuse an operation belonging to another venue.

        Args:
            venue: The venue the operation belongs to.
            context: What was being attempted.

        Raises:
            ExecutionError: `EXTERNAL_VENUE_MISMATCH` if it is not this session's
                venue. This is the check that stops a run blending simulated and
                real fills into one journal.
        """
        if venue is not self.venue:
            raise ExecutionError(
                Code.EXTERNAL_VENUE_MISMATCH,
                f"{context}: this session is bound to {self.venue} and the operation "
                f"belongs to {venue}. A journal holding both simulated and venue "
                "fills cannot say which is which, and every figure derived from it "
                "would be a blend of the two",
            )

    def require_artefact(self, recorded_venue: str | None, *, context: str) -> None:
        """Refuse an artefact recorded under another venue.

        Args:
            recorded_venue: What the artefact says its venue was. `None` means
                an artefact from before venues were recorded.
            context: What was being read.

        Raises:
            ExecutionError: `EXTERNAL_VENUE_MISMATCH` if it does not match.
        """
        if recorded_venue is None:
            raise ExecutionError(
                Code.EXTERNAL_VENUE_MISMATCH,
                f"{context}: the artefact records no venue, so it cannot be shown to "
                "belong to this session's. It is refused rather than assumed",
            )
        if recorded_venue != str(self.venue):
            raise ExecutionError(
                Code.EXTERNAL_VENUE_MISMATCH,
                f"{context}: the artefact was recorded at {recorded_venue} and this "
                f"session is bound to {self.venue}",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "policy": VENUE_BINDING_POLICY,
            "venue": str(self.venue),
            "is_external": self.venue.is_external,
            "live_authorized": False,
            "fallback": "NONE — an unreachable venue ends the run, it does not simulate",
            "account": self.account.as_json() if self.account else None,
        }
