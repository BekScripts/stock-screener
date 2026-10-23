"""Whether a security was in the research universe on a past date.

Phase 9A settled *what* a security is. Phase 9B settled what it *did*. This
settles the question between them, and it is the one a backtest asks first:
**which securities were there to be chosen from?**

Three concepts are kept apart here, because collapsing them is how a universe
quietly becomes today's roster wearing a date:

- **Identity** — which security this is. Answered by `domain.identity`.
- **Membership** — was it a listed, investable security over this period? A
  slow-moving listing fact, recorded here as dated intervals.
- **Eligibility** — did it clear the project's thresholds on a given day? A
  fast-moving quantitative screen, answered by `evaluate_eligibility` against
  point-in-time metrics, and deliberately *not* re-implemented here.

A study needs all three. A study that has only the third, run over today's
roster, is measuring survivors.

## The asymmetry that shapes everything below

The project's universe is defined by `evaluate_eligibility`, which reads five
profile fields. Their historical availability is not uniform, and pretending
otherwise is the failure this module exists to prevent:

| Input | Dated in this system? |
| --- | --- |
| price, market cap, liquidity, staleness | **yes** — PIT features, observations |
| exchange | **no** — every stored value carries today's date |
| security type (ticker, name) | ticker is dated; name is not |
| sector | **no**, and mostly absent besides |
| active flag | **no** |

So a *complete* membership reconstruction is impossible from the free record, and
no amount of code changes that. What is possible is a reconstruction that says so
— which is why `MembershipConfidence` exists and why nothing here can produce
`OBSERVED` from an attribute the record does not date.

## The three states, and why `UNKNOWN` is a first-class answer

`MEMBER`, `NON_MEMBER` and `UNKNOWN` are not two states and an error. A security
the record cannot place is genuinely unplaceable, and a reconstruction that
resolved it either way would be manufacturing history. `UNKNOWN` is what lets a
caller see the size of what is not known instead of inheriting it silently.

Confidence carries the provenance the state cannot:

- `OBSERVED` — every input behind the verdict is dated. A delisting on a stated
  effective date makes `NON_MEMBER` observed; nothing in the current record can
  make `MEMBER` observed, because no dated venue evidence exists.
- `INFERRED` — a documented rule was applied to an attribute the record does not
  date, bounded by evidence that does. Reproducible, explainable, and never
  presented as fact.

**Price presence is never membership.** A bar proves a security traded; it does
not prove it was on a supported venue, was common stock, or cleared any
threshold. Trading evidence appears below only to *bound* an interval whose
membership claim rests on something else, and to support `NON_MEMBER` — the
conservative direction. There is no path through this module by which "it has a
price" becomes "it was a member".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

#: One day, for closing an interval the day before its successor opens.
_ONE_DAY = timedelta(days=1)


class MembershipState(StrEnum):
    """Whether a security was in the research universe over a period."""

    MEMBER = "MEMBER"
    """It was a listed, investable security of the kind this project screens."""

    NON_MEMBER = "NON_MEMBER"
    """It was not — it had left the market, or had not yet reached it, or its
    listing is of a kind the universe excludes."""

    UNKNOWN = "UNKNOWN"
    """The record cannot place it. Not a failure and not a soft no: a study must
    be able to count these and decide for itself, which it cannot do if they have
    been quietly resolved to either of the other two."""


class MembershipConfidence(StrEnum):
    """How a membership verdict was reached, which the state alone cannot say."""

    OBSERVED = "OBSERVED"
    """Every input behind the verdict carries a date the record establishes. In
    the current evidence base only `NON_MEMBER` can reach this — a dated
    delisting, or a first appearance demonstrably after the date asked about."""

    INFERRED = "INFERRED"
    """A documented rule was applied to an attribute the record does not date —
    in practice the exchange and security type, carried back across a window
    bounded by dated evidence. Reproducible and explainable, and never to be read
    as observation. `MembershipInterval.inferred_inputs` names exactly which
    attributes were carried."""

    ASSERTED = "ASSERTED"
    """Established from evidence outside the system's feeds and supplied by a
    caller. The only route by which a listing fact no free source carries can be
    recorded at all."""


class MembershipReason(StrEnum):
    """Why an interval holds the state it does. The audit trail of the verdict."""

    LISTED = "LISTED"
    """Dated evidence places the security in the market over the interval, and
    its listing attributes qualify it."""

    DELISTED = "DELISTED"
    """A dated exit removed it on the interval's first day."""

    NOT_YET_IN_RECORD = "NOT_YET_IN_RECORD"
    """Every dated trace of the security begins after this interval, and begins
    late enough that the record's own start cannot explain the silence."""

    UNSUPPORTED_LISTING = "UNSUPPORTED_LISTING"
    """The listing attributes disqualify it — a venue the universe excludes, or a
    line that is not common stock. Inferred wherever those attributes are
    undated, and marked as such."""

    NO_DATED_EVIDENCE = "NO_DATED_EVIDENCE"
    """Nothing dated places the security either way over this interval."""


@dataclass(frozen=True)
class ListingAttributes:
    """The qualitative listing facts, and whether the record dates any of them.

    Kept as one object with its own honesty flags so that a caller cannot pass an
    exchange without also saying whether that exchange is a dated fact or today's
    value carried back. The flags are what stop a current attribute from ever
    producing an `OBSERVED` verdict.

    Attributes:
        supported_listing: Whether the venue and line qualify for the universe.
            None where the attributes needed to decide are absent entirely.
        venue_is_dated: Whether the exchange behind that verdict carries a date
            the record establishes. False for every value this system currently
            stores.
        type_is_dated: Whether the security-type verdict rests on dated inputs.
        sector_excluded: Whether the sector is one the universe excludes. None
            where no sector is recorded, which is the common case.
        sector_is_dated: Whether that sector carries a date.
        source: What supplied the attributes, for the audit trail.
    """

    supported_listing: bool | None
    venue_is_dated: bool = False
    type_is_dated: bool = False
    sector_excluded: bool | None = None
    sector_is_dated: bool = False
    source: str | None = None

    @property
    def fully_dated(self) -> bool:
        """Whether every attribute behind a listing verdict is dated.

        The gate on `OBSERVED` membership. It is currently False for every
        security in this system, and the reconstruction reports that rather than
        rounding it away.
        """
        if not (self.venue_is_dated and self.type_is_dated):
            return False
        return self.sector_excluded is None or self.sector_is_dated

    @property
    def inferred_inputs(self) -> tuple[str, ...]:
        """Names the attributes that had to be carried back undated."""
        carried: list[str] = []
        if not self.venue_is_dated:
            carried.append("exchange")
        if not self.type_is_dated:
            carried.append("security_type")
        if self.sector_excluded is not None and not self.sector_is_dated:
            carried.append("sector")
        return tuple(carried)


@dataclass(frozen=True)
class MembershipEvidence:
    """Every dated fact the record holds about one security's presence.

    Deliberately carries no prices and no thresholds. Membership is a listing
    question; whether a member also cleared the market-cap and liquidity bar on a
    particular day is eligibility, and `evaluate_eligibility` already answers it
    against point-in-time metrics.

    Attributes:
        first_seen: Earliest date any dated source places the security in the
            market — a filing, an identifier interval, a settlement record.
            None when the record holds nothing dated at all.
        last_seen: Latest such date, or None.
        delisted_on: Effective date of a dated exit, or None. A stated exit is
            the strongest evidence in the model.
        record_floor: The earliest date the evidence base itself can speak to. A
            first appearance at or near this floor cannot distinguish "it did not
            exist" from "our record does not reach", so the reconstruction
            declines to call the earlier period `NON_MEMBER`.
        listing: The qualitative attributes and their dating.
    """

    first_seen: date | None
    last_seen: date | None = None
    delisted_on: date | None = None
    record_floor: date | None = None
    listing: ListingAttributes = field(
        default_factory=lambda: ListingAttributes(supported_listing=None)
    )


@dataclass(frozen=True)
class MembershipInterval:
    """One security's membership state over one closed date range.

    Ranges are inclusive at both ends and must not overlap for one security, so
    exactly one interval answers for any date it covers. A date covered by none
    is `UNKNOWN` — the reconstruction does not extrapolate past its evidence in
    either direction.

    Attributes:
        state: `MEMBER`, `NON_MEMBER` or `UNKNOWN`.
        confidence: How the verdict was reached.
        reason: Which rule produced it.
        valid_from: First date the verdict holds, inclusive.
        valid_to: Last date it holds, inclusive. None means still open.
        inferred_inputs: The undated attributes carried back to reach it. Empty
            for an observed verdict, and the reason a caller can audit an
            inference rather than take it on trust.
        detail: The sentence an audit prints.
    """

    state: MembershipState
    confidence: MembershipConfidence
    reason: MembershipReason
    valid_from: date
    valid_to: date | None = None
    inferred_inputs: tuple[str, ...] = ()
    detail: str = ""

    def covers(self, on: date) -> bool:
        """Whether this interval's verdict holds on a date."""
        if on < self.valid_from:
            return False
        return self.valid_to is None or on <= self.valid_to

    @property
    def is_member(self) -> bool:
        """Whether the interval places the security in the universe."""
        return self.state is MembershipState.MEMBER


@dataclass(frozen=True)
class MembershipVerdict:
    """What the record says about one security on one date.

    Attributes:
        state: The verdict.
        confidence: How it was reached.
        reason: Which rule produced it.
        inferred_inputs: Undated attributes carried back to reach it.
        detail: The sentence an audit prints.
    """

    state: MembershipState
    confidence: MembershipConfidence
    reason: MembershipReason
    inferred_inputs: tuple[str, ...] = ()
    detail: str = ""

    @property
    def is_member(self) -> bool:
        """Whether the security was in the universe. False for `UNKNOWN`."""
        return self.state is MembershipState.MEMBER

    @property
    def is_known(self) -> bool:
        """Whether the record placed the security either way."""
        return self.state is not MembershipState.UNKNOWN


#: The verdict for a security the record holds nothing dated about.
_NOTHING_KNOWN = MembershipVerdict(
    state=MembershipState.UNKNOWN,
    confidence=MembershipConfidence.OBSERVED,
    reason=MembershipReason.NO_DATED_EVIDENCE,
    detail="no dated source places this security in or out of the market",
)


def build_membership_intervals(evidence: MembershipEvidence) -> tuple[MembershipInterval, ...]:
    """Turn one security's dated evidence into its membership intervals.

    The rules, in the order they are applied, each of them a decision about what
    the record can and cannot support:

    1. **No dated evidence at all → no intervals.** Every date is `UNKNOWN`. The
       reconstruction does not invent a window to put a security in.
    2. **Before the first dated trace → `NON_MEMBER`, observed** — but only when
       that first trace is *strictly after* `record_floor`. At the floor the
       silence is our record's, not the market's, and the period is left
       uncovered so it reads `UNKNOWN`.
    3. **From the first trace to the exit (or open) → the listing verdict.**
       Where the attributes qualify it, `MEMBER`; where they disqualify it,
       `NON_MEMBER`; where they are absent, `UNKNOWN`. Confidence is `OBSERVED`
       only if every attribute behind the verdict is dated, which in the current
       evidence base never happens — so this interval is `INFERRED`, and names
       what was carried.
    4. **From a dated exit onward → `NON_MEMBER`, observed.** The strongest rule
       in the model, and the one that makes a delisted security disappear from
       later universes without disappearing from earlier ones.

    Args:
        evidence: The dated facts about one security.

    Returns:
        Non-overlapping intervals, earliest first. Dates outside them are
        `UNKNOWN` by omission rather than by assertion.
    """
    if evidence.first_seen is None:
        return ()

    intervals: list[MembershipInterval] = []
    first = evidence.first_seen

    if evidence.record_floor is not None and first > evidence.record_floor:
        intervals.append(
            MembershipInterval(
                state=MembershipState.NON_MEMBER,
                confidence=MembershipConfidence.OBSERVED,
                reason=MembershipReason.NOT_YET_IN_RECORD,
                valid_from=evidence.record_floor,
                valid_to=_day_before(first),
                detail=(
                    f"no dated trace before {first}, and the record reaches back to "
                    f"{evidence.record_floor}"
                ),
            )
        )

    listed_to = _day_before(evidence.delisted_on) if evidence.delisted_on else None
    if listed_to is None or listed_to >= first:
        intervals.append(_listing_interval(evidence, first, listed_to))

    if evidence.delisted_on is not None:
        intervals.append(
            MembershipInterval(
                state=MembershipState.NON_MEMBER,
                confidence=MembershipConfidence.OBSERVED,
                reason=MembershipReason.DELISTED,
                valid_from=evidence.delisted_on,
                valid_to=None,
                detail=f"a dated exit removed it on {evidence.delisted_on}",
            )
        )
    return tuple(intervals)


def _listing_interval(
    evidence: MembershipEvidence, first: date, last: date | None
) -> MembershipInterval:
    """Build the interval covering a security's time in the market."""
    listing = evidence.listing
    carried = listing.inferred_inputs
    confidence = (
        MembershipConfidence.OBSERVED if listing.fully_dated else MembershipConfidence.INFERRED
    )

    if listing.supported_listing is None:
        return MembershipInterval(
            state=MembershipState.UNKNOWN,
            confidence=MembershipConfidence.OBSERVED,
            reason=MembershipReason.NO_DATED_EVIDENCE,
            valid_from=first,
            valid_to=last,
            detail="the record establishes no listing attributes to judge it by",
        )

    if listing.sector_excluded or not listing.supported_listing:
        return MembershipInterval(
            state=MembershipState.NON_MEMBER,
            confidence=confidence,
            reason=MembershipReason.UNSUPPORTED_LISTING,
            valid_from=first,
            valid_to=last,
            inferred_inputs=carried,
            detail=(
                "the listing attributes exclude it from the universe"
                + (f"; carried undated: {', '.join(carried)}" if carried else "")
            ),
        )

    return MembershipInterval(
        state=MembershipState.MEMBER,
        confidence=confidence,
        reason=MembershipReason.LISTED,
        valid_from=first,
        valid_to=last,
        inferred_inputs=carried,
        detail=(
            f"dated evidence places it in the market from {first}"
            + (f"; carried undated: {', '.join(carried)}" if carried else "")
        ),
    )


def membership_as_of(intervals: Iterable[MembershipInterval], as_of: date) -> MembershipVerdict:
    """Return one security's membership verdict for a date.

    Args:
        intervals: That security's intervals, any order.
        as_of: The decision date.

    Returns:
        The verdict of the covering interval, or `UNKNOWN` where none covers the
        date — which is an answer, not an absence of one.
    """
    for interval in intervals:
        if interval.covers(as_of):
            return MembershipVerdict(
                state=interval.state,
                confidence=interval.confidence,
                reason=interval.reason,
                inferred_inputs=interval.inferred_inputs,
                detail=interval.detail,
            )
    return _NOTHING_KNOWN


@dataclass(frozen=True)
class UniverseMembership:
    """The reconstructed universe for a date, with its uncertainty in view.

    Attributes:
        as_of: The decision date.
        members: Securities the record places in the universe, each with the
            confidence behind it.
        non_members: Securities the record places outside it.
        unknown: Securities the record cannot place. **Reported, never dropped**
            — a caller that cannot see the size of what is unknown has no way to
            judge the universe it was handed.
    """

    as_of: date
    members: tuple[tuple[int, MembershipVerdict], ...] = ()
    non_members: tuple[tuple[int, MembershipVerdict], ...] = ()
    unknown: tuple[int, ...] = ()

    @property
    def observed_members(self) -> int:
        """Members whose every input is dated."""
        return sum(
            1 for _, verdict in self.members if verdict.confidence is MembershipConfidence.OBSERVED
        )

    @property
    def inferred_members(self) -> int:
        """Members resting on an attribute the record does not date."""
        return sum(
            1 for _, verdict in self.members if verdict.confidence is MembershipConfidence.INFERRED
        )

    @property
    def total(self) -> int:
        """Securities considered, of every state."""
        return len(self.members) + len(self.non_members) + len(self.unknown)

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.as_of}: {len(self.members)} members "
            f"({self.observed_members} observed, {self.inferred_members} inferred), "
            f"{len(self.non_members)} non-members, {len(self.unknown)} unknown "
            f"of {self.total} securities"
        )


def reconstruct_membership(
    evidence: Sequence[tuple[int, MembershipEvidence]], as_of: date
) -> UniverseMembership:
    """Reconstruct the universe for a date from each security's dated evidence.

    Args:
        evidence: `(security key, evidence)` pairs. The key is opaque here — the
            application supplies whatever identity it uses, and this module never
            interprets it.
        as_of: The decision date.

    Returns:
        The three groups, with the unknown ones counted rather than discarded.
    """
    members: list[tuple[int, MembershipVerdict]] = []
    non_members: list[tuple[int, MembershipVerdict]] = []
    unknown: list[int] = []

    for key, facts in evidence:
        verdict = membership_as_of(build_membership_intervals(facts), as_of)
        if verdict.state is MembershipState.MEMBER:
            members.append((key, verdict))
        elif verdict.state is MembershipState.NON_MEMBER:
            non_members.append((key, verdict))
        else:
            unknown.append(key)

    members.sort(key=lambda item: item[0])
    non_members.sort(key=lambda item: item[0])
    unknown.sort()
    return UniverseMembership(
        as_of=as_of,
        members=tuple(members),
        non_members=tuple(non_members),
        unknown=tuple(unknown),
    )


def _day_before(value: date) -> date:
    """The day before a date, for closing an interval against its successor."""
    return value - _ONE_DAY
