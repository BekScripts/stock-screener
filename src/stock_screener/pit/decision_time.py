"""What `as_of` means, stated once and precisely.

Every point-in-time question is really a question about an *instant*: was this
document public at the moment the decision was taken? The repository has always
answered with a date, which cannot settle it — a filing date says nothing about
the hour, and filings arrive throughout the day and after it.

## The contract

**`as_of` is a date, and it denotes a decision taken at the close of that
trading day — 16:00 in America/New_York.**

That is chosen rather than assumed, for three reasons. The capture pass already
runs after the close and stamps a UTC calendar date, so a close-of-day reading
keeps this package's dates in step with `snapshot_date` and `score_date`. A daily
trader decides on the day's closing information and acts at the next open, so the
close is the moment a decision actually exists. And the alternative — an
open-of-day reading — would discard the entire trading session's information for
no gain, since nothing in this repository acts intraday.

## Why a date comparison cannot express it

Measured across 2,546 XBRL-bearing filings from 45 companies, of those accepted
on their stated filing date:

    before the open    26.4%
    during the session 27.9%
    after the close    45.7%

So `filed_date <= as_of` shows a close-of-day decision roughly 46% of that day's
filings before they were public, and `filed_date < as_of` hides the 54% that
genuinely were. **Neither operator is a correct availability test.** Changing one
to the other trades a look-ahead error for an omission error; it does not fix the
semantics.

The correct test compares an instant to an instant: a filing is available iff the
SEC accepted it at or before the close of `as_of`.

## When the instant is unknown

An observation whose accession has no stored acceptance time falls back to
`filed_date < as_of` — treated as though it arrived after the close of its filing
date. That is the conservative direction: it may withhold a filing for one day,
and it can never reveal one early. The alternative fallback would be a
look-ahead, and a point-in-time record that is sometimes optimistic is worth less
than one that is always honest.
"""

from __future__ import annotations

from datetime import datetime, time
from enum import StrEnum
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from datetime import date

#: The exchange the decision clock follows. U.S. equities, so New York — and a
#: named zone rather than a fixed offset, because the close moves with daylight
#: saving and a fixed -5 would be an hour wrong for two-thirds of the year.
MARKET_TIMEZONE = ZoneInfo("America/New_York")

#: Regular-session close.
MARKET_CLOSE = time(16, 0)

#: Regular-session open.
MARKET_OPEN = time(9, 30)


class DecisionPoint(StrEnum):
    """The moment within a day that a decision is taken.

    `CLOSE` is the contract this repository uses. `OPEN` exists because a future
    strategy acting on the opening auction would need it, and because naming
    both makes the choice visible rather than buried in a comparison operator.
    """

    CLOSE = "CLOSE"
    OPEN = "OPEN"


def decision_instant(as_of: date, point: DecisionPoint = DecisionPoint.CLOSE) -> datetime:
    """Return the exact instant a decision on `as_of` was taken.

    Args:
        as_of: The trading day the decision belongs to.
        point: Where in that day the decision sits. Defaults to the close.

    Returns:
        An aware datetime in the market's timezone. Convert or compare directly;
        it carries a real offset, so a comparison against a stored UTC instant is
        correct without further arithmetic.

    Note:
        No market calendar is consulted, because this repository has none. A date
        that was not a trading day still yields an instant, and a caller asking
        about a Sunday gets Sunday's close — which is harmless for an
        availability test and would be wrong for anything claiming a session.
    """
    clock = MARKET_CLOSE if point is DecisionPoint.CLOSE else MARKET_OPEN
    return datetime.combine(as_of, clock, tzinfo=MARKET_TIMEZONE)
