"""One injectable clock, so operational time is testable and never scattered.

Two kinds of time run through this system and conflating them has been a
recurring hazard. **Operational time** is when this process did something —
when a run started, when a kill was activated, when health was last green. It
comes from this clock. **Semantic time** is when something happened in the
market — when an order filled, when a session closed, when a price was good as
of. That comes from the provider or the calendar and never from here.

The distinction matters because operational time is allowed to be approximate
and must never enter a financial figure or a fingerprint that is supposed to
identify a trade. A run id derived from the wall clock would differ on every
retry, which is the opposite of what a derived id is for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FakeClock", "SystemClock", "parse_instant"]


def parse_instant(value: str) -> datetime:
    """Read an ISO-8601 instant, requiring an offset.

    Args:
        value: The timestamp.

    Returns:
        The instant, timezone-aware.

    Raises:
        ValueError: If it cannot be parsed, or carries no offset. A naive
            timestamp is refused rather than assumed UTC: an operational record
            whose timezone was guessed is worse than one that failed to parse,
            because nobody will ever notice the guess.
    """
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        msg = f"{value!r} carries no UTC offset; an operational instant must be explicit"
        raise ValueError(msg)
    return parsed


@runtime_checkable
class Clock(Protocol):
    """Whatever can say what time it is."""

    def now(self) -> datetime:
        """Return the current instant, timezone-aware.

        Returns:
            The instant.
        """
        ...

    def iso(self) -> str:
        """Return the current instant as ISO-8601 with an offset.

        Returns:
            The timestamp.
        """
        ...


@dataclass(frozen=True)
class SystemClock:
    """The real clock. UTC, always timezone-aware."""

    def now(self) -> datetime:
        """Return the current instant in UTC."""
        return datetime.now(UTC)

    def iso(self) -> str:
        """Return the current instant as ISO-8601."""
        return self.now().isoformat()


@dataclass
class FakeClock:
    """A clock a test drives, so no operational test depends on the real one.

    Attributes:
        instant: What the clock currently reads.
        reads: How many times it was asked. A test can assert that a decision
            consulted the clock once rather than re-reading it mid-decision,
            which is how two halves of one check end up disagreeing.
    """

    instant: datetime
    reads: int = field(default=0)

    @classmethod
    def at(cls, value: str) -> FakeClock:
        """Build a clock reading one instant.

        Args:
            value: An ISO-8601 instant with an offset.

        Returns:
            The clock.
        """
        return cls(instant=parse_instant(value))

    def now(self) -> datetime:
        """Return the fixed instant."""
        self.reads += 1
        return self.instant

    def iso(self) -> str:
        """Return the fixed instant as ISO-8601."""
        return self.now().isoformat()

    def advance(self, *, seconds: float = 0, minutes: float = 0, hours: float = 0) -> FakeClock:
        """Move the clock forward.

        Args:
            seconds: Seconds to add.
            minutes: Minutes to add.
            hours: Hours to add.

        Returns:
            Self, for chaining.
        """
        self.instant += timedelta(seconds=seconds, minutes=minutes, hours=hours)
        return self
