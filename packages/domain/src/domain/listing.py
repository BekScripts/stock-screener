"""What a monthly market-share file says about where a security was listed.

The Phase 9K audit found the one free record of historical venue this project
can use: Nasdaq's public monthly *Market Share by Symbol* files. This module
holds the rules for reading them, and every rule here exists because the audit
found a way the source misleads a careless reader.

## The source is not a listing roster

A row says: **this symbol appeared in this month's file for this venue.** It does
not say the security was listed on any particular day, and it does not say it was
listed at all — a security appears when trading in it was reported, and the file
carries a handful of zero-volume rows besides. So the raw state is named
`MARKET_SHARE_OBSERVED` rather than anything with "listed" in it, and the
membership verdict is derived from it separately.

The inverse matters more. **Absence from a month is not absence from the
market.** A security that did not trade can be missing, and 34.6% of observed
symbols have at least one hole inside their own run. There is therefore no
`NOT_LISTED` state in this module at all; the absence of an observation is
`UNKNOWN`, and no caller can turn it into anything else.

## The file family changed meaning, silently

`Listed*` is the hazard. It is **Tape B** — NYSE Arca, NYSE American, Cboe —
through 2021-12, and **Tape A**, which is NYSE, from 2022-01. The audit pinned
the change by probing for securities that can only be one or the other: the
2017-01 file carries `SPY` and not `JPM`, the 2022-01 file carries `JPM` and not
`SPY`. A reader that took the family name at face value across that boundary
would silently swap NYSE for Arca in 2022.

So the expected tape is a function of the month, `tape_for` states it, and
`classify_tape` re-derives it from the file's own contents so a mismatch can be
caught rather than trusted. One month — 2021-07 — is Tape A inside the Tape B
era, which is exactly why the check reads the file instead of the calendar.

## NYSE American is censored, not missing

Tape B never separates NYSE American from Arca, and from 2022 the family stops
carrying Tape B at all. There is no month in which this source establishes that a
security was listed on NYSE American. `CENSORED_VENUE` says so explicitly, so a
study can count what it could not see instead of quietly scoring it as absent.

Nothing here performs I/O or reads a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable

#: The first month in which the `Listed*` family carries Tape A — NYSE. Before
#: it the same filenames carry Tape B, and reading them as NYSE is the single
#: worst mistake available with this source.
NYSE_TAPE_A_FROM = date(2022, 1, 1)

#: The first month this project accepts evidence from. Phase 9K established that
#: Nasdaq and NYSE are both covered only from here.
SUPPORTED_FROM = date(2022, 1, 1)

#: Securities that trade only on NYSE, used to recognise a Tape A file from its
#: own contents. Large, long-lived, and never Nasdaq or Arca.
TAPE_A_SENTINELS = frozenset({"JPM", "GE", "XOM", "BA", "T", "PFE", "KO"})

#: Securities that trade only on NYSE Arca — a Tape B file names these and none
#: of the Tape A sentinels.
TAPE_B_SENTINELS = frozenset({"SPY", "GDX", "IWM"})

#: How many sentinels must appear before a tape is claimed. Four of seven for
#: Tape A tolerates a name leaving the market without weakening the test.
_TAPE_A_MINIMUM = 4
_TAPE_B_MINIMUM = 2


class SourceFamily(StrEnum):
    """Which of the two published file families a month's file belongs to."""

    NASDAQ = "NASDAQ"
    """The `NASDAQ*` files. Tape C throughout the published history."""

    LISTED = "LISTED"
    """The `Listed*` files. Tape B until 2022-01, Tape A after it."""


class Tape(StrEnum):
    """The consolidated tape a file reports on.

    Not cosmetic. The tape is what fixes the venue, and the whole reason this
    enum exists separately from `Venue` is that one family carries two tapes at
    different times.
    """

    A = "A"
    """NYSE-listed securities."""

    B = "B"
    """NYSE Arca, NYSE American and regional listings, undifferentiated. This
    project can read no venue out of it — see `CENSORED_VENUE`."""

    C = "C"
    """Nasdaq-listed securities."""

    UNKNOWN = "UNKNOWN"
    """The file's contents match no tape. Never imported."""


class Venue(StrEnum):
    """The exchange an observation establishes, where one can be established."""

    NASDAQ = "NASDAQ"
    NYSE = "NYSE"


class ListingEvidenceState(StrEnum):
    """What the record can say about one security, one venue, one date.

    There is deliberately **no** `NOT_LISTED` member. The source cannot produce
    that claim, so the type cannot express it, and no caller can accidentally
    write it.
    """

    MARKET_SHARE_OBSERVED = "MARKET_SHARE_OBSERVED"
    """The symbol appears in that month's file for that venue. Month resolution,
    and a statement about the *source*, not yet about membership."""

    INFERRED_INTERVAL = "INFERRED_INTERVAL"
    """The date falls inside a run of consecutive observed months. Continuity
    across the month boundaries is inferred from the observations either side and
    is never promoted to `MARKET_SHARE_OBSERVED`."""

    UNKNOWN = "UNKNOWN"
    """No observation, and none may be inferred — a hole inside a run, anything
    before the first or after the last observation, or a security this source
    cannot see at all. **This is what absence means.**"""

    CENSORED_VENUE = "CENSORED_VENUE"
    """The venue in question is one this source structurally cannot report —
    NYSE American. Distinct from `UNKNOWN` because the reason is known and
    permanent, so a study can quantify the blind spot rather than discover it."""


@dataclass(frozen=True)
class MonthlyObservation:
    """One symbol seen in one month's file for one venue.

    Attributes:
        symbol: The ticker as the file printed it, upper-cased.
        venue: The venue the file's tape establishes.
        month: The first day of the observation month, as the month's label.
        consolidated_volume: The month's consolidated volume, kept because a
            zero distinguishes a security that was carried but did not trade.
    """

    symbol: str
    venue: Venue
    month: date
    consolidated_volume: float | None = None


def month_start(value: date) -> date:
    """Return the first day of a date's month, the canonical observation label."""
    return value.replace(day=1)


def next_month(value: date) -> date:
    """Return the first day of the month after the one a date falls in."""
    return date(value.year + (value.month == 12), (value.month % 12) + 1, 1)


def previous_month(value: date) -> date:
    """Return the first day of the month before the one a date falls in."""
    return date(value.year - (value.month == 1), (value.month - 2) % 12 + 1, 1)


def tape_for(family: SourceFamily, month: date) -> Tape:
    """Return the tape a family's file is *expected* to carry in a month.

    An expectation, never a conclusion. `classify_tape` reads the file itself,
    and the importer imports only where the two agree — because the one month
    that breaks this rule (2021-07) is precisely the kind of thing a calendar
    rule cannot know about.

    Args:
        family: Which published family the file belongs to.
        month: Any date in the observation month.

    Returns:
        `C` for the Nasdaq family; `A` or `B` for the Listed family, split at
        `NYSE_TAPE_A_FROM`.
    """
    if family is SourceFamily.NASDAQ:
        return Tape.C
    return Tape.A if month_start(month) >= NYSE_TAPE_A_FROM else Tape.B


def venue_for_tape(tape: Tape) -> Venue | None:
    """Return the venue a tape establishes, or None where it establishes none.

    Tape B returns None on purpose: it pools NYSE Arca, NYSE American and
    regional listings with no field separating them, so no venue claim survives
    it. That None is what becomes `CENSORED_VENUE` upstream.
    """
    if tape is Tape.C:
        return Venue.NASDAQ
    if tape is Tape.A:
        return Venue.NYSE
    return None


def classify_tape(symbols: Collection[str]) -> Tape:
    """Recognise a file's tape from the securities it names.

    The check the audit turned into a rule. Tape A and Tape B files disagree
    about securities that cannot move: `JPM` is NYSE and `SPY` is Arca, and no
    month contains both under one tape. Reading the tape out of the contents is
    what lets the importer refuse a file whose meaning has changed.

    Args:
        symbols: Every symbol the file names, any case.

    Returns:
        The tape the contents support, or `UNKNOWN` when neither sentinel set
        is present in strength.
    """
    upper = {value.strip().upper() for value in symbols}
    if len(TAPE_A_SENTINELS & upper) >= _TAPE_A_MINIMUM:
        return Tape.A
    if len(TAPE_B_SENTINELS & upper) >= _TAPE_B_MINIMUM:
        return Tape.B
    return Tape.C if upper else Tape.UNKNOWN


def observed_months(observations: Iterable[MonthlyObservation], venue: Venue) -> tuple[date, ...]:
    """Return the months one venue was observed in, earliest first, deduplicated."""
    return tuple(sorted({o.month for o in observations if o.venue is venue}))


def month_state(
    observations: Iterable[MonthlyObservation],
    venue: Venue,
    month: date,
) -> ListingEvidenceState:
    """Return what the record establishes for one security, venue and **month**.

    The only granularity the source directly supports, so the only one that may
    return `MARKET_SHARE_OBSERVED`.

    Args:
        observations: Every monthly observation held for the security.
        venue: The venue being asked about.
        month: Any date in the month of interest.

    Returns:
        `MARKET_SHARE_OBSERVED` when that month's file names the symbol, and
        `UNKNOWN` otherwise — including for a hole between two observed months,
        which this source cannot fill.
    """
    if month_start(month) in set(observed_months(observations, venue)):
        return ListingEvidenceState.MARKET_SHARE_OBSERVED
    return ListingEvidenceState.UNKNOWN


def daily_state(
    observations: Iterable[MonthlyObservation],
    venue: Venue,
    as_of: date,
) -> ListingEvidenceState:
    """Return what the record establishes for one security, venue and **day**.

    **No day is ever `MARKET_SHARE_OBSERVED`.** The file attests to a month, not
    to a date within it: a security that left the market on the 5th appears in
    that month exactly as one that traded through the 31st does. Reading a day
    out of a month is therefore an inference every time, and this function names
    it one every time.

    That is the whole distinction between this function and `month_state`, and
    it is the reason the two are separate rather than one call with a flag —
    a caller has to choose the granularity it is willing to defend.

    Args:
        observations: Every monthly observation held for the security.
        venue: The venue being asked about.
        as_of: The decision date.

    Returns:
        `INFERRED_INTERVAL` when the date's month carries an observation, and
        `UNKNOWN` otherwise. Never `MARKET_SHARE_OBSERVED`.
    """
    if month_state(observations, venue, as_of) is ListingEvidenceState.MARKET_SHARE_OBSERVED:
        return ListingEvidenceState.INFERRED_INTERVAL
    return ListingEvidenceState.UNKNOWN


def continuous_runs(
    observations: Iterable[MonthlyObservation], venue: Venue
) -> tuple[tuple[date, date], ...]:
    """Return the unbroken runs of observed months, as inclusive month pairs.

    A run is a *claim of continuous listing* and is therefore inferred, never
    observed: the months at its ends were seen, and the continuity joining them
    is the inference. Two runs separated by a hole stay separate — collapsing
    them is the forward-fill this layer exists to prevent, and it is what would
    silently join Bed Bath & Beyond to the unrelated issuer that later took its
    symbol.

    Args:
        observations: Every monthly observation held for the security.
        venue: The venue being asked about.

    Returns:
        `(first_month, last_month)` per run, earliest first.
    """
    months = observed_months(observations, venue)
    if not months:
        return ()
    runs: list[tuple[date, date]] = []
    start = previous = months[0]
    for month in months[1:]:
        if month == next_month(previous):
            previous = month
            continue
        runs.append((start, previous))
        start = previous = month
    runs.append((start, previous))
    return tuple(runs)
