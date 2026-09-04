"""Attaching a security class to a monthly listing observation, safely.

The listing record says a symbol traded on a venue in a month. The SEC cover page
says what class a symbol names. Joining them is what turns venue evidence into
membership evidence — and the join is where a reused ticker does its damage.

**The failure this module exists to prevent, measured.** `BBBY` was Bed Bath &
Beyond on Nasdaq through 2023-05 and is Beyond, Inc. on NYSE from 2025-08. The
only free route from a ticker to a CIK is `company_tickers.json`, which lists
*current* registrants — so a naive join hands `BBBY` Beyond's CIK, reads Beyond's
2025 cover page, and stamps `COMMON_STOCK` onto Bed Bath's 2022 months. The class
happens to be right and the reasoning is entirely wrong; the same mechanism would
stamp a warrant issuer's class onto a predecessor that was common stock.

So a filing may classify an observation only when **both** hold:

- it was public by then — acceptance datetime at or before the observation month,
  never the period-end date, which is earlier than anything was knowable; and
- it falls inside the *same unbroken run* of observations for that symbol and
  venue, so it belongs to the occupant the observation belongs to.

A run boundary is a change of hands, and evidence never crosses one. Where no
filing satisfies both, the class is `UNKNOWN` — which is a correct answer, and
far better than the confident wrong one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from domain import ClassEvidence, ClassVerdict, SecurityClass, common_stock_for_symbol

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from domain import CoverSecurity

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class FilingCover:
    """One filing's cover page, with the timestamp that makes it usable.

    Attributes:
        accepted: The SEC acceptance date — the earliest defensible moment the
            facts were public. **Never the report period end**, which precedes
            publication by weeks and would backdate everything.
        securities: The registered classes the cover page named.
        accession: The filing's accession number, for the audit trail.
    """

    accepted: date
    securities: Sequence[CoverSecurity]
    accession: str | None = None


#: The verdict returned when no filing may classify an observation.
UNKNOWN_CLASS = ClassVerdict(
    SecurityClass.UNKNOWN, ClassEvidence.ABSENT, SecurityClass.UNKNOWN, SecurityClass.UNKNOWN
)


def occupancy_of(runs: Sequence[tuple[date, date]], moment: date) -> int | None:
    """Return which occupancy a date belongs to, or None when it belongs to none.

    A symbol's observed runs are its successive occupants. A date inside a run
    belongs to that occupant. A date *before the first* run belongs to the first
    occupant — a company files for years before the listing record's window
    opens, and refusing those filings would leave every security's earliest
    months unclassifiable. A date in a **gap between** runs, or after the last,
    belongs to no occupant that can be named, and is refused.

    Args:
        runs: The unbroken runs, earliest first.
        moment: The date to place.

    Returns:
        The run's index, or None where the date cannot be attributed.
    """
    if not runs:
        return None
    month = _month_start(moment)
    if month < runs[0][0]:
        return 0
    for index, (first, last) in enumerate(runs):
        if first <= month <= last:
            return index
    return None


def class_for_observation(
    covers: Sequence[FilingCover],
    symbol: str,
    month: date,
    runs: Sequence[tuple[date, date]],
) -> tuple[ClassVerdict, FilingCover | None]:
    """Return the class for one monthly observation, and the filing that gave it.

    Args:
        covers: Every cover page held for the issuer, any order.
        symbol: The symbol the observation names.
        month: The observation month, as its first day.
        runs: Every unbroken run of observations for this symbol and venue,
            earliest first. The month must fall inside one of them.

    Returns:
        The verdict and the filing behind it. `UNKNOWN_CLASS` and None where no
        filing is both PIT-valid and attributable to the same occupant.
    """
    here = occupancy_of(runs, month)
    if here is None:
        return UNKNOWN_CLASS, None
    usable = [
        cover
        for cover in covers
        # PIT: public by the start of the month it describes. A filing accepted
        # mid-month did not exist for a decision made at the month's open.
        if cover.accepted <= month
        # Occupancy: same occupant as the month, so a later tenant of the symbol
        # can never classify an earlier one's months.
        and occupancy_of(runs, cover.accepted) == here
    ]
    if not usable:
        return UNKNOWN_CLASS, None
    newest = max(usable, key=lambda c: c.accepted)
    verdict = common_stock_for_symbol(newest.securities, symbol)
    if verdict is None:
        return UNKNOWN_CLASS, newest
    return verdict, newest


def _month_start(value: date) -> date:
    """The first day of a date's month."""
    return value.replace(day=1)
