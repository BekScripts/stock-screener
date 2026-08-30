"""What happened next: forward returns measured from a point-in-time snapshot.

A snapshot says what was knowable on a date. This says what the security then
did, and it is the other half a research question needs. Neither half is a
strategy — nothing here ranks, scores, thresholds or recommends. It measures.

Four decisions carry the whole module, and each of them is a place where a
plausible shortcut would quietly manufacture a result.

**The entry is the next session's open, not this session's close.** A snapshot
for date D is computable only once D has closed, so a decision taken on it
cannot be filled at D's close — that price is already in the past at the moment
the decision exists. The first price a holder of that snapshot could actually
transact at is the open of D+1. Close-to-close is the more common convention and
it is not available here: it would pay the entry at a price the decision itself
depended on.

**The horizon is counted on the market's calendar, not the security's.** Twenty
sessions means twenty sessions the market was open. Counting a security's own
bars would make a security that stopped trading for a month appear to reach its
horizon on time, which is the exact opposite of what happened to a holder.

**A ratio of adjusted prices is a total return.** The `adjustment=all` basis that
makes a stored close useless for a market capitalisation is what makes it right
here: distributions are already reflected on both ends, and a split inside the
horizon is a non-event because both ends are on the same basis. The property
that has to be worked around in `pricing` is the property relied on here.

**A security that stops trading has no return, and saying so is the point.** The
measurement is refused rather than filled in. Assuming -100% would libel every
company acquired for cash; assuming 0% would erase every failure; dropping the
row entirely would rebuild the survivorship bias the point-in-time record exists
to remove. It is recorded as interrupted, with the last price seen and the date
it was seen, so a study can decide — visibly — what to do about it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

#: The horizons measured, in market sessions. One session is the next open to
#: the one after it; 120 is roughly six months.
FORWARD_HORIZONS: tuple[int, ...] = (1, 5, 20, 60, 120)

#: How many sessions a security may be missing from the end of the record before
#: it is judged to have stopped trading rather than merely to be un-ingested.
#: Five absorbs a stale ingestion — a run that finished on Tuesday leaves every
#: security short by two days — without absorbing a delisting, which leaves a
#: security short by every session since.
DEFAULT_STALE_SESSIONS = 5


class OutcomeStatus(StrEnum):
    """Why a forward return does or does not have a number.

    Three states, not two, for the same reason `Tradability` has three: "no
    number" conflates a horizon that has not elapsed yet with a security that
    stopped trading inside it, and those mean opposite things to a study.
    """

    REALISED = "REALISED"
    """Both ends priced. The return is a measurement."""

    TRUNCATED = "TRUNCATED"
    """The horizon extends past the price record — usually because the future
    has not happened yet. Nothing is wrong; there is simply nothing to measure
    yet, and the same observation will realise once time passes."""

    INTERRUPTED = "INTERRUPTED"
    """The market kept trading and the security did not. A holder's outcome was
    decided by something other than a price — an acquisition, a bankruptcy, a
    move to an unlisted market — and no price series can say which."""

    UNPRICED = "UNPRICED"
    """The security was still trading and we simply have no bar for that
    session. A gap in the record, not an event in the market, and kept apart
    from `INTERRUPTED` because a study counting how often securities leave the
    market must not count our missing rows among them."""

    NO_ENTRY = "NO_ENTRY"
    """No entry price existed at all: the security had not begun trading, or had
    already stopped, on the first session after the observation."""


@dataclass(frozen=True)
class ForwardReturn:
    """One horizon's outcome from one observation date.

    Attributes:
        horizon_sessions: How many market sessions the horizon spans.
        status: Whether there is a number, and why not when there is not.
        entry_date: The session the entry price was taken from.
        entry_price: The open of the first session after the observation.
        exit_date: The session the exit price was taken from, when there is one.
        exit_price: The open of the session `horizon_sessions` later.
        total_return: `exit / entry - 1`, on an adjusted basis, so distributions
            and splits are already in it. None unless `status` is `REALISED`.
        last_seen_date: For an interrupted outcome, the security's final session.
        last_seen_price: For an interrupted outcome, its final close. Recorded
            because it is evidence, not because it is a substitute for the exit.
    """

    horizon_sessions: int
    status: OutcomeStatus
    entry_date: date | None = None
    entry_price: float | None = None
    exit_date: date | None = None
    exit_price: float | None = None
    total_return: float | None = None
    last_seen_date: date | None = None
    last_seen_price: float | None = None


def _entry_session(calendar: Sequence[date], observation_date: date) -> int | None:
    """Return the index of the first market session strictly after a date.

    Strict, and that is the look-ahead boundary. A snapshot for D is a statement
    about D's close; the session it can be acted in is the one after.
    """
    for index, session in enumerate(calendar):
        if session > observation_date:
            return index
    return None


def forward_returns(
    calendar: Sequence[date],
    opens: Mapping[date, float],
    closes: Mapping[date, float],
    observation_date: date,
    horizons: Sequence[int] = FORWARD_HORIZONS,
    stale_after: int = DEFAULT_STALE_SESSIONS,
) -> tuple[ForwardReturn, ...]:
    """Measure what a security did after an observation date.

    Args:
        calendar: Every session the market was open, ascending. Taken from the
            benchmark series, which trades every session, so a gap in it is a
            market holiday and a gap in the security is the security's own.
        opens: The security's adjusted opens by session. Entries are taken here.
        closes: The security's adjusted closes by session, used only to record
            where an interrupted security was last seen.
        observation_date: The date the snapshot describes.
        horizons: Session counts to measure. Defaults to `FORWARD_HORIZONS`.
        stale_after: How many sessions a security may be missing from the end of
            the record before a horizon running past that record is judged
            interrupted rather than merely not yet elapsed.

    Returns:
        One `ForwardReturn` per horizon, in the order given. Every horizon is
        returned, including the ones with no number — an absent row and a
        refused measurement are different facts, and only one of them is
        recoverable later.
    """
    start = _entry_session(calendar, observation_date)
    if start is None or (entry_price := opens.get(calendar[start])) is None or entry_price <= 0:
        return tuple(
            ForwardReturn(horizon_sessions=horizon, status=OutcomeStatus.NO_ENTRY)
            for horizon in horizons
        )

    entry_date = calendar[start]
    traded = sorted(closes)
    last_seen = traded[-1] if traded else None
    ceased = last_seen is not None and last_seen < calendar[max(0, len(calendar) - 1 - stale_after)]

    results: list[ForwardReturn] = []
    for horizon in horizons:
        target = start + horizon
        if target >= len(calendar):
            # Past the end of the calendar. For a security still trading that is
            # simply the future, and the same observation realises once time
            # passes. For one that stopped trading before the calendar ended it
            # is not: waiting will never produce a price, and calling it
            # truncated would let a study drop the row as "not ripe yet" — which
            # is survivorship bias reintroduced under a gentler name.
            results.append(
                ForwardReturn(
                    horizon_sessions=horizon,
                    status=(OutcomeStatus.INTERRUPTED if ceased else OutcomeStatus.TRUNCATED),
                    entry_date=entry_date,
                    entry_price=entry_price,
                    last_seen_date=last_seen if ceased else None,
                    last_seen_price=(closes.get(last_seen) if ceased and last_seen else None),
                )
            )
            continue

        exit_date = calendar[target]
        exit_price = opens.get(exit_date)
        if exit_price is not None and exit_price > 0:
            results.append(
                ForwardReturn(
                    horizon_sessions=horizon,
                    status=OutcomeStatus.REALISED,
                    entry_date=entry_date,
                    entry_price=entry_price,
                    exit_date=exit_date,
                    exit_price=exit_price,
                    total_return=exit_price / entry_price - 1.0,
                )
            )
            continue

        # The market was open and this security was not priced. Two very
        # different things look identical at this line. If the security never
        # traded again, it left the market inside the horizon and a holder's
        # outcome was decided by an acquisition or a failure. If it is trading
        # today, this is a hole in our record and nothing happened to the
        # company at all. Only the first is a fact about the market, and only
        # the first may be counted as one.
        left_the_market = ceased and last_seen is not None and exit_date > last_seen
        results.append(
            ForwardReturn(
                horizon_sessions=horizon,
                status=(OutcomeStatus.INTERRUPTED if left_the_market else OutcomeStatus.UNPRICED),
                entry_date=entry_date,
                entry_price=entry_price,
                exit_date=exit_date,
                last_seen_date=last_seen if left_the_market else None,
                last_seen_price=(closes.get(last_seen) if left_the_market and last_seen else None),
            )
        )

    return tuple(results)
