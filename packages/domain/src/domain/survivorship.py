"""Who existed to be decided about, and what became of the ones that left.

Everything in the point-in-time layer bounds what a past decision could *know*.
This module answers the prior question a survivorship-aware study has to settle
before any of that matters: **which securities were in the investable set on a
past date, and — for the ones that are no longer trading — why they left.**

Two pure concerns live here, both functions of already-fetched records:

`classify_delisting`
    Turns the mechanics a provider reports (a cash merger, a worthless removal)
    into the *fate* a study reasons about (acquired, bankrupt, plainly delisted).
    The mechanics answer "what settled the shares"; the fate answers "what
    happened to the company", and a backtest weighing a holder's outcome needs
    the second. Where the source cannot establish a reason it is `UNKNOWN` — a
    stated absence, never a guess.

`universe_members_as_of`
    Reconstructs membership on a past date from the append-only universe-event
    record. It is deliberately unable to invent history: a `BASELINE` row asserts
    nothing about the time before it was written, so a question asked before the
    baseline is answered `UNKNOWN` rather than with today's roster. That refusal
    is the whole value — a reconstruction that quietly returned the current
    universe for every past date would be survivorship bias wearing a timestamp.

Neither function performs I/O, reads a clock, or touches configuration. The
application supplies the records and the date; the judgement is here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

from domain.models import CorporateActionKind

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date


class DelistingReason(StrEnum):
    """Why a security stopped trading, as distinct from how its shares settled.

    `CorporateActionKind` records the settlement mechanism — the thing a price
    series has to be corrected for. This records the *fate*, which is what a
    study measuring what happened to a holder actually reasons about. The two
    are related but not the same: a cash merger and a scheme of arrangement are
    both `ACQUIRED` even though one pays cash and the other shares.

    The members are exhaustive over the outcomes this project is willing to
    assert. Anything the source cannot pin down is `UNKNOWN`, which is a fact
    about our evidence and must never be collapsed into one of the others to
    make a total look tidy.
    """

    ACQUIRED = "ACQUIRED"
    """Absorbed by another entity — a merger or takeover, settled in cash,
    shares, or both. The holder received consideration; the outcome is
    computable when its amount is known."""

    DELISTED = "DELISTED"
    """Removed from its exchange while the company itself continued — a listing
    that failed a standard, went private, or moved to an unlisted market. The
    shares may still have had value the tape stopped recording, so no return is
    implied by the event alone."""

    BANKRUPT = "BANKRUPT"
    """Wound up through insolvency, the equity extinguished. A worthless removal
    is the reported fact, not an assumption: the holder was left with nothing,
    and -100% is the measurement rather than a guess."""

    RECEIVERSHIP = "RECEIVERSHIP"
    """Seized by a regulator or placed under an administrator rather than merged
    or removed — the fate of a failed bank. Named separately because a data
    source almost never files it under any corporate-action type (SVB and First
    Republic appear under none), so it is here to be *asserted by evidence a
    study supplies*, never inferred from the action feed."""

    UNKNOWN = "UNKNOWN"
    """The security left the market and the source does not establish why. The
    honest default. A study may exclude these, treat them as a worst case, or
    seek better evidence — but it must be able to see that the reason is absent
    rather than have one manufactured for it."""


#: The corporate-action kinds that settle a security into another entity. Each
#: maps to `ACQUIRED`: the distinction between them is the settlement currency,
#: which `DelistingRecord` already carries, not the fate.
_ACQUISITION_KINDS: frozenset[CorporateActionKind] = frozenset(
    {
        CorporateActionKind.CASH_MERGER,
        CorporateActionKind.STOCK_MERGER,
        CorporateActionKind.STOCK_AND_CASH_MERGER,
    }
)


def classify_delisting(
    kind: CorporateActionKind | None,
    *,
    asserted_reason: DelistingReason | None = None,
) -> DelistingReason:
    """Determine why a security left the market from what the source reported.

    The mapping is deliberately narrow. Only the kinds this project can defend a
    fate for produce anything other than `UNKNOWN`:

    - a merger of any settlement is `ACQUIRED`;
    - a worthless removal is `BANKRUPT`, because the extinguished equity *is* the
      reported fact;
    - everything else, including a `None` kind, is `UNKNOWN`.

    Receivership is never returned from a `kind`, because no action feed files it
    as one. It can only enter through `asserted_reason`, which lets a caller
    record a fate established by evidence outside the action record — a
    regulator's announcement, a filed administration order. An assertion always
    wins, because it is more specific than the mechanism and is the only route
    by which `RECEIVERSHIP` or a corrected `DELISTED` can be stated at all.

    Args:
        kind: The corporate-action kind the provider reported, or None when the
            security is known to have left but no action explains it.
        asserted_reason: A fate established outside the action feed. Overrides
            the mechanism-derived reason when given.

    Returns:
        The fate, or `UNKNOWN` when the source supports none.
    """
    if asserted_reason is not None:
        return asserted_reason
    if kind in _ACQUISITION_KINDS:
        return DelistingReason.ACQUIRED
    if kind is CorporateActionKind.WORTHLESS_REMOVAL:
        return DelistingReason.BANKRUPT
    return DelistingReason.UNKNOWN


class MembershipKind(StrEnum):
    """How confident a membership answer is, which is as important as the answer.

    A reconstruction that could not distinguish these would be worthless: a
    security *known* to have been trading and one merely *assumed* present
    because no exit was ever recorded look identical in a plain roster, and only
    one of them is safe to trade in a study.
    """

    OBSERVED = "OBSERVED"
    """The system watched this security's state on or before the date. The
    membership is a fact it recorded, not one it inferred."""

    BASELINE = "BASELINE"
    """The only information is the state as first seen, and the date asked for is
    on or after that first sighting. The security was present, but nothing is
    known about when it became so."""


@dataclass(frozen=True)
class MembershipEvent:
    """One recorded observation of a security's place in the investable set.

    A neutral projection of a stored universe event, so this module stays free
    of the database. The application maps its rows onto these.

    Attributes:
        company_id: The owning company's identity.
        ticker: The symbol as it stood when the event was recorded.
        effective_date: For an observed transition, the date it was seen; for a
            baseline, the date the state was first recorded. Never a guess at
            when an unwitnessed change truly happened.
        is_active: Whether the security was part of the investable set at
            `effective_date`.
        is_baseline: True when the row is a first-sighting that asserts nothing
            about the time before it, False when it is a witnessed transition.
    """

    company_id: int
    ticker: str
    effective_date: date
    is_active: bool
    is_baseline: bool


@dataclass(frozen=True)
class Membership:
    """One security's membership verdict for a date.

    Attributes:
        company_id: The owning company's identity.
        ticker: The symbol as of the deciding event.
        kind: Whether the verdict was observed or rests on a baseline.
        as_of_event_date: The date of the event the verdict was decided from.
    """

    company_id: int
    ticker: str
    kind: MembershipKind
    as_of_event_date: date


@dataclass(frozen=True)
class UniverseAsOf:
    """The reconstructed investable set for a date, with its own honesty markers.

    Attributes:
        as_of: The date the membership describes.
        members: Every security that was in the set on the date, each carrying
            whether its presence was observed or assumed from a baseline.
        undetermined: Companies whose earliest record is a baseline dated *after*
            `as_of`, so their state on the date is genuinely unknown. Reported,
            not silently dropped and not silently included — either would be a
            different bias.
    """

    as_of: date
    members: tuple[Membership, ...] = ()
    undetermined: tuple[int, ...] = ()

    @property
    def member_ids(self) -> frozenset[int]:
        """The company ids in the set, for a fast membership test."""
        return frozenset(member.company_id for member in self.members)

    @property
    def observed(self) -> tuple[Membership, ...]:
        """Only the members whose presence the system actually watched."""
        return tuple(m for m in self.members if m.kind is MembershipKind.OBSERVED)

    @property
    def rests_on_baseline(self) -> bool:
        """Whether any member is present only on a baseline's authority.

        A study over a date this returns True for is reading a universe the
        record cannot fully vouch for, and a readiness check should say so.
        """
        return any(m.kind is MembershipKind.BASELINE for m in self.members)


def universe_members_as_of(
    events: Iterable[MembershipEvent],
    as_of: date,
    *,
    delisted_ids: Iterable[int] = (),
) -> UniverseAsOf:
    """Reconstruct which securities were in the investable set on a date.

    The rule is one sentence: for each company, the latest event dated on or
    before `as_of` decides its state, and a company whose only record is a
    baseline dated after `as_of` is undetermined rather than assumed present.

    A company is a member when that deciding event has `is_active` true. The
    verdict is `OBSERVED` when a witnessed transition on or before the date
    settled it, and `BASELINE` when the newest thing known by the date is a
    first-sighting — present, but with no claim about how long. A company
    delisted by the date (via `delisted_ids`, or an inactive event) is excluded,
    which is the entire point: the current roster contains none of them.

    Args:
        events: Every recorded membership observation, any order. Multiple rows
            per company are expected; the latest on or before `as_of` wins.
        as_of: The date to reconstruct membership for.
        delisted_ids: Companies known from the delisting registry to have left
            the market on or before `as_of`. Excluded outright, because a
            security can leave without the universe pass ever witnessing it.

    Returns:
        The membership set and the companies whose state cannot be determined.
    """
    excluded = set(delisted_ids)
    latest: dict[int, MembershipEvent] = {}
    seen_ids: set[int] = set()
    for event in events:
        seen_ids.add(event.company_id)
        if event.effective_date > as_of:
            continue
        current = latest.get(event.company_id)
        if current is None or event.effective_date >= current.effective_date:
            # `>=` so that, for two events on one day, a later-listed transition
            # supersedes a baseline: the ordering within a date is by arrival,
            # and the transition is the more specific statement.
            if current is not None and event.effective_date == current.effective_date:
                if current.is_baseline and not event.is_baseline:
                    latest[event.company_id] = event
                continue
            latest[event.company_id] = event

    members: list[Membership] = []
    undetermined: list[int] = []
    for company_id in seen_ids:
        if company_id in excluded:
            continue
        decided = latest.get(company_id)
        if decided is None:
            # Nothing is known by the date: this company's earliest record —
            # baseline or witnessed transition alike — begins after `as_of`, and
            # a record that starts later says nothing about the date asked for.
            undetermined.append(company_id)
            continue
        if not decided.is_active:
            continue
        members.append(
            Membership(
                company_id=company_id,
                ticker=decided.ticker,
                kind=(MembershipKind.BASELINE if decided.is_baseline else MembershipKind.OBSERVED),
                as_of_event_date=decided.effective_date,
            )
        )

    members.sort(key=lambda m: m.ticker)
    undetermined.sort()
    return UniverseAsOf(as_of=as_of, members=tuple(members), undetermined=tuple(undetermined))


@dataclass(frozen=True)
class ReadinessGap:
    """One reason a date is not fully answerable for a survivorship-aware study.

    Attributes:
        code: A stable machine-readable identifier for the gap.
        detail: A human-readable explanation, for a report.
    """

    code: str
    detail: str


@dataclass(frozen=True)
class UniverseReadiness:
    """Whether a date's reconstructed universe can be trusted by a study.

    Not a verdict on the whole backtest — only on the membership question this
    module answers. It exists so the readiness harness can fold a per-date
    universe check into its overall judgement without re-deriving the rules.

    Attributes:
        as_of: The date judged.
        member_count: How many securities were in the set.
        observed_share: The fraction of members whose presence was observed
            rather than assumed from a baseline. 1.0 is a fully witnessed
            universe; a low value is one still resting on its first sighting.
        gaps: Every reason the date falls short of a fully trustworthy answer.
    """

    as_of: date
    member_count: int
    observed_share: float
    gaps: tuple[ReadinessGap, ...] = field(default_factory=tuple)

    @property
    def trustworthy(self) -> bool:
        """True when nothing undermines the date's membership answer."""
        return not self.gaps


def assess_universe_readiness(universe: UniverseAsOf) -> UniverseReadiness:
    """Judge whether a reconstructed universe is safe for a study to run on.

    Two things disqualify a date, and both are about honesty rather than size.
    A universe with undetermined companies is one the record cannot place on the
    date at all. A universe resting entirely on baselines is one with no
    membership history yet — every name is present only because no exit has been
    seen, which is exactly the shape survivorship bias takes before any time has
    accumulated.

    Args:
        universe: A reconstructed membership set.

    Returns:
        The readiness verdict, carrying the specific gaps found.
    """
    total = len(universe.members)
    observed = len(universe.observed)
    observed_share = 1.0 if total == 0 else observed / total

    gaps: list[ReadinessGap] = []
    if universe.undetermined:
        gaps.append(
            ReadinessGap(
                code="UNDETERMINED_MEMBERS",
                detail=(
                    f"{len(universe.undetermined)} companies have no record on or before "
                    f"{universe.as_of}; their membership cannot be determined."
                ),
            )
        )
    if total > 0 and observed == 0:
        gaps.append(
            ReadinessGap(
                code="BASELINE_ONLY_UNIVERSE",
                detail=(
                    f"all {total} members rest on a baseline; no membership transition has "
                    "been observed by this date, so the roster cannot be distinguished from "
                    "today's survivors."
                ),
            )
        )
    if total == 0:
        gaps.append(
            ReadinessGap(
                code="EMPTY_UNIVERSE",
                detail=f"no securities are reconstructable as members on {universe.as_of}.",
            )
        )

    return UniverseReadiness(
        as_of=universe.as_of,
        member_count=total,
        observed_share=observed_share,
        gaps=tuple(gaps),
    )
