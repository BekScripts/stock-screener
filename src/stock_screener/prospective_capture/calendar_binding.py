"""Binding the prospective calendar to the session source that validated it.

R1.11 found a hazard worth a module of its own. The runner validates its frozen
calendar by replaying it over the benchmark sessions a database supplies, and
two databases in this repository supply very different answers: the research
dataset holds 1,531 sessions from 2020, the operational database 282 from 2025.
Both replays return `agrees: true`, because the calendar genuinely reproduces
whatever subset it is shown. The weaker one simply proves far less.

The existing digest gate does not catch it. It proves *which file* was passed,
not that the file is the authoritative one, so passing the 282-session database
with its own correct digest clears every check and prints a reassuring number.

So the calendar is bound to the session source itself: its identity, its count,
its span and a digest over the sessions. A materially weaker source then fails
with `CALENDAR_BINDING_MISMATCH` rather than quietly certifying less.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.prospective_runner import CALENDAR_ID, verify_against_history

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

__all__ = [
    "AUTHORITATIVE_CALENDAR",
    "CalendarBinding",
    "CalendarBindingError",
    "bind_calendar",
    "session_digest",
]


class CalendarBindingError(RuntimeError):
    """Raised when the session source is not the authoritative one."""


#: The session source the prospective protocol was validated against, frozen
#: here so a substitution is detectable. The digest is over the ISO dates joined
#: by newlines, which is stable under any read order because the query sorts.
AUTHORITATIVE_CALENDAR: dict[str, Any] = {
    "calendar_id": CALENDAR_ID,
    "source": "PIT_RESEARCH_DATASET_V2 benchmark_prices",
    "source_database": "stage9k.db",
    "session_count": 1531,
    "first_session": "2020-07-27",
    "last_session": "2026-08-28",
    "sessions_sha256": "c39d5bed4d9d7b8db8ec94b1422823f23cb3c2829b7c1cc4ae37c1ff0f413802",
    "derivation_rule": (
        "sessions are derived from NYSE_SESSION_CALENDAR_V1's rules and validated "
        "by replay against this source; no date is supplied by hand"
    ),
    "why_bound": (
        "the operational database also answers this query, with 282 sessions from "
        "2025. The calendar reproduces that subset too and reports agreement, so "
        "agreement alone certifies nothing about coverage."
    ),
}


def session_digest(sessions: Sequence[date]) -> str:
    """Digest a session list so one source cannot pass for another.

    Args:
        sessions: The sessions, in ascending order.

    Returns:
        SHA-256 over the ISO dates joined by newlines.
    """
    return hashlib.sha256(
        "\n".join(session.isoformat() for session in sessions).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class CalendarBinding:
    """The calendar's provenance, as verified.

    Attributes:
        calendar_id: The frozen calendar's identity.
        source: What supplied the sessions.
        session_count: How many sessions were replayed.
        first_session: The earliest.
        last_session: The latest.
        sessions_sha256: The digest over them.
        replay: The calendar's own agreement report.
        authoritative: Whether this is the bound source.
    """

    calendar_id: str
    source: str
    session_count: int
    first_session: str
    last_session: str
    sessions_sha256: str
    replay: dict[str, Any]
    authoritative: bool

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "calendar_id": self.calendar_id,
            "source": self.source,
            "session_count": self.session_count,
            "coverage": {"first": self.first_session, "last": self.last_session},
            "sessions_sha256": self.sessions_sha256,
            "replay": self.replay,
            "authoritative": self.authoritative,
            "expected": dict(AUTHORITATIVE_CALENDAR),
            "derivation_rule": AUTHORITATIVE_CALENDAR["derivation_rule"],
        }


def bind_calendar(sessions: Sequence[date], *, source: str) -> CalendarBinding:
    """Verify the calendar against the authoritative session source.

    Two things must hold, and the second is the one R1.11 found missing: the
    calendar must reproduce the sessions, *and* the sessions must be the ones it
    was certified against. A replay that agrees over a thinner source is not a
    weaker pass, it is a different claim.

    Args:
        sessions: The benchmark sessions, ascending.
        source: What supplied them, for the record and the failure message.

    Returns:
        The verified binding.

    Raises:
        CalendarBindingError: If the source is not authoritative, or the calendar
            does not reproduce it.
    """
    if not sessions:
        raise CalendarBindingError(
            f"CALENDAR_BINDING_MISMATCH: {source} supplied no sessions; the "
            "prospective calendar cannot be validated against nothing"
        )
    digest = session_digest(sessions)
    expected_count = int(AUTHORITATIVE_CALENDAR["session_count"])
    observed_first, observed_last = sessions[0].isoformat(), sessions[-1].isoformat()
    if digest != AUTHORITATIVE_CALENDAR["sessions_sha256"]:
        raise CalendarBindingError(
            f"CALENDAR_BINDING_MISMATCH: {source} supplied {len(sessions)} sessions "
            f"({observed_first} to {observed_last}) digesting to {digest[:16]}…, but the "
            f"prospective protocol is bound to {expected_count} sessions "
            f"({AUTHORITATIVE_CALENDAR['first_session']} to "
            f"{AUTHORITATIVE_CALENDAR['last_session']}) digesting to "
            f"{str(AUTHORITATIVE_CALENDAR['sessions_sha256'])[:16]}…. A calendar that "
            "agrees over a thinner source certifies less, not the same."
        )
    replay = verify_against_history(list(sessions))
    if not replay["agrees"]:
        raise CalendarBindingError(
            f"CALENDAR_BINDING_MISMATCH: the calendar does not reproduce {source}: {replay}"
        )
    return CalendarBinding(
        calendar_id=CALENDAR_ID,
        source=source,
        session_count=len(sessions),
        first_session=observed_first,
        last_session=observed_last,
        sessions_sha256=digest,
        replay=replay,
        authoritative=True,
    )
