"""Whether a security may be researched in a given month, and why not when not.

The end of the Phase 9K sequence. Everything before it established *evidence*;
this decides what that evidence permits. The rule it encodes is deliberately
narrow, because every audit in the sequence found the same failure waiting in the
same place — a security admitted on evidence that did not actually say what the
caller wanted it to say.

## The chain, in order

    identity → listing observation → supported venue → security class → month

The order is load-bearing and cannot be reordered for convenience. A security
does not become eligible because it has a price, a market capitalisation, a
current ticker or a current CIK; those are read *after* membership, never as a
route into it. `classify_month` therefore takes no prices at all — the
application applies the eligibility screen to what this returns, and cannot
apply it the other way round.

## Monthly, and never daily

The listing source publishes one file per venue per month. A month in which a
symbol appears is `MARKET_SHARE_OBSERVED`; a *day* inside it is an inference,
because a security that left on the 5th appears in that file exactly as one that
traded to the 31st. `UniverseState.ELIGIBLE` is a claim about a **month**. There
is no daily state in this module and no function that produces one.

## Absence is never exclusion

A month with no observation is `UNKNOWN`. It is not `NOT_LISTED` — no such state
exists anywhere in this layer — and it is not `OUTSIDE_OBSERVABLE_UNIVERSE`,
which is a positive finding about a security's venue rather than a shrug. Phase
9K.6 adjudicated 604 zero-observation securities and left **202 of them UNKNOWN**
on purpose: the evidence did not establish that they were absent, only that it
could not place them. Collapsing those two would convert a measured blind spot
into a silent exclusion, which is the bias the whole sequence exists to remove.

## Class evidence expires

An SEC cover page states a security's class on the day it was accepted. Carrying
that forward is an inference whose strength decays, so it is bounded at
`CLASS_CARRY_FORWARD_DAYS` — Phase 9K.2 measured the median gap between a
security's monthly observation and its newest prior filing at 54 days, with 83.3%
inside 90. Past the bound the class is `UNKNOWN` and the month is not eligible.
Carried evidence is never reported as directly observed; `ClassEvidenceMode`
keeps the two apart so a study can require the stronger one.

Nothing here performs I/O or reads a clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from domain.listing import ListingEvidenceState, Venue
from domain.security_class import SecurityClass

if TYPE_CHECKING:
    from datetime import date

#: How stale a security-class observation may be and still describe a month.
#: Phase 9K.2 measured the age of the newest PIT-valid filing at each monthly
#: observation: median 54 days, 83.3% within 90. Ninety days buys that coverage
#: without carrying a class across a quarter it was never observed in.
CLASS_CARRY_FORWARD_DAYS = 90

#: The venues this universe admits. NYSE American is deliberately absent: the
#: free monthly source publishes Tape A and Tape C only, so an American listing
#: is invisible to it and is censored rather than guessed at.
SUPPORTED_UNIVERSE_VENUES = frozenset({Venue.NASDAQ, Venue.NYSE})

#: The only class the research universe admits.
ELIGIBLE_CLASS = SecurityClass.COMMON_STOCK


class UniverseState(StrEnum):
    """What the evidence permits for one security in one month."""

    ELIGIBLE = "ELIGIBLE"
    """Observed on a supported venue that month, and classified common stock on
    PIT-valid evidence. A **monthly** claim, never a daily one."""

    OUTSIDE_OBSERVABLE_UNIVERSE = "OUTSIDE_OBSERVABLE_UNIVERSE"
    """Positive evidence places the security on a venue this source cannot
    report — NYSE American, Arca, Cboe. A finding, not missing data."""

    NON_COMMON_OR_EXCLUDED = "NON_COMMON_OR_EXCLUDED"
    """Class evidence establishes something the universe excludes: a unit,
    warrant, right, note, fund, preferred line or depositary share."""

    OUTSIDE_WINDOW = "OUTSIDE_WINDOW"
    """The month precedes the supported window. Kept rather than dropped so the
    accounting still balances."""

    UNKNOWN = "UNKNOWN"
    """The evidence does not establish a classification. **Not** absent, not
    delisted, not excluded, not zero-volume — merely unestablished."""

    @property
    def censored(self) -> bool:
        """Whether the security is knowably outside the universe this month.

        `UNKNOWN` is excluded from this: an unresolved security is not a
        censored one, and an audit that pooled them would lose the distinction
        Phase 9K.6 spent a whole phase establishing.
        """
        return self in {
            UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE,
            UniverseState.NON_COMMON_OR_EXCLUDED,
        }


class ClassEvidenceMode(StrEnum):
    """How a month's security class was reached."""

    DIRECT = "DIRECT"
    """A filing accepted inside that month stated it."""

    CARRIED_FORWARD = "CARRIED_FORWARD"
    """An earlier filing stated it, within the carry-forward bound. An
    inference, and labelled one."""

    ABSENT = "ABSENT"
    """No usable class evidence — none held, or older than the bound."""


@dataclass(frozen=True)
class MonthVerdict:
    """One security, one month, and the reasoning behind the answer.

    Attributes:
        state: What the evidence permits.
        security_class: The class reached, or `UNKNOWN`.
        class_mode: How the class was reached.
        class_age_days: Age of the class evidence at the month, when there is
            any. None where no evidence was offered.
        reason: A short sentence naming what decided it.
    """

    state: UniverseState
    security_class: SecurityClass
    class_mode: ClassEvidenceMode
    class_age_days: int | None
    reason: str

    @property
    def eligible(self) -> bool:
        """Whether the month is a member — before price, size and liquidity."""
        return self.state is UniverseState.ELIGIBLE


def class_mode_for(
    age_days: int | None, bound: int = CLASS_CARRY_FORWARD_DAYS
) -> ClassEvidenceMode:
    """Return how class evidence of a given age may be used.

    Args:
        age_days: Days between the evidence's acceptance and the month. None
            when no evidence exists. Negative is impossible for PIT-valid
            evidence and is treated as absent rather than silently accepted.
        bound: The carry-forward limit.

    Returns:
        `DIRECT` inside the month, `CARRIED_FORWARD` within the bound, `ABSENT`
        beyond it.
    """
    if age_days is None or age_days < 0:
        return ClassEvidenceMode.ABSENT
    if age_days == 0:
        return ClassEvidenceMode.DIRECT
    return ClassEvidenceMode.CARRIED_FORWARD if age_days <= bound else ClassEvidenceMode.ABSENT


def classify_month(
    *,
    listing_state: ListingEvidenceState,
    venue: Venue | None,
    security_class: SecurityClass,
    class_age_days: int | None,
    month: date,
    supported_from: date,
    venue_is_censored: bool = False,
    bound: int = CLASS_CARRY_FORWARD_DAYS,
) -> MonthVerdict:
    """Decide one security's state for one month.

    The tests run in the order of the chain, so the reason names the first thing
    that decided the month rather than the last.

    Args:
        listing_state: What the monthly listing record says.
        venue: The venue the observation established, if any.
        security_class: The class the SEC cover page established.
        class_age_days: Age of that class evidence at the month.
        month: The month being decided, as its first day.
        supported_from: The first month the universe covers.
        venue_is_censored: Positive evidence that the security's venue is one
            this source cannot report. Distinct from simply having no
            observation.
        bound: Class carry-forward limit.

    Returns:
        The verdict. `ELIGIBLE` only when every step passes.
    """
    mode = class_mode_for(class_age_days, bound)

    if month < supported_from:
        return MonthVerdict(
            UniverseState.OUTSIDE_WINDOW,
            security_class,
            mode,
            class_age_days,
            f"month {month} precedes the supported window from {supported_from}",
        )

    if venue_is_censored:
        return MonthVerdict(
            UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE,
            security_class,
            mode,
            class_age_days,
            "venue evidence places the security outside the reportable source",
        )

    if listing_state is not ListingEvidenceState.MARKET_SHARE_OBSERVED:
        return MonthVerdict(
            UniverseState.UNKNOWN,
            security_class,
            mode,
            class_age_days,
            "no listing observation for this month — unestablished, not absent",
        )

    if venue is None or venue not in SUPPORTED_UNIVERSE_VENUES:
        return MonthVerdict(
            UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE,
            security_class,
            mode,
            class_age_days,
            f"venue {venue} is not one this universe admits",
        )

    if mode is ClassEvidenceMode.ABSENT:
        return MonthVerdict(
            UniverseState.UNKNOWN,
            SecurityClass.UNKNOWN,
            mode,
            class_age_days,
            "no class evidence within the carry-forward bound",
        )

    if security_class is SecurityClass.UNKNOWN:
        return MonthVerdict(
            UniverseState.UNKNOWN,
            security_class,
            mode,
            class_age_days,
            "class evidence exists but establishes nothing",
        )

    if security_class is not ELIGIBLE_CLASS:
        return MonthVerdict(
            UniverseState.NON_COMMON_OR_EXCLUDED,
            security_class,
            mode,
            class_age_days,
            f"class {security_class} is not admitted",
        )

    return MonthVerdict(
        UniverseState.ELIGIBLE,
        security_class,
        mode,
        class_age_days,
        f"observed on {venue} and classified {security_class} ({mode})",
    )
