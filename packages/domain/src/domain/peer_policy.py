"""Which securities constitute the cross-section a percentile is measured against.

A percentile is a statement about a population, so it is only as meaningful as
the population is well-chosen. `MARKET_SIGNALS_V1` never chose one: it ranked
whatever feature rows its caller happened to hand it. The Phase 9E audit measured
what that produced, and both of the populations in use were wrong in opposite
directions.

**Live-only was survivorship-biased.** Company signal rows were ranked against
the current roster, so every security that had since delisted was missing from
the cross-section it belonged to — 343 of them on 2022-06-30 alone.

**Everything-with-a-feature-row was stale-peer contaminated.** A security keeps
producing feature rows for as long as it has *any* bar at or before the date, so
one that stopped trading in 2022 was still being ranked on a 2026 date, on 2022
features. On 2026-03-31, **all 2,562** historical peers were securities that had
already left the market, 1,753 of them more than a year stale.

This module states the policy `MARKET_SIGNALS_V2` uses instead:

    V2 peers(D) = members_as_of(D)  ∩  securities with market data on D

Both halves are load-bearing and neither is sufficient alone. Membership without
the market-data test readmits the stale securities, because a delisted name is a
member on dates *before* it left and the record still holds its old bars.
Market data without membership readmits everything the universe excludes — an
unsupported venue, a security whose membership the record cannot place at all.

## What this module does not decide

It does not decide what "market data on D" means — the application does, from the
evidence it holds, and passes the answer in. Keeping that out of here is
deliberate: the *policy* is a domain rule and belongs under test, while the
*evidence* for it is a storage question that has changed twice already.

Nothing here performs I/O, reads a clock, or ranks anything. It is given
candidates and returns which of them form the cross-section, with a stated reason
for every one it leaves out.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from domain.features import MarketFeatures


class PeerExclusion(StrEnum):
    """Why a security is not part of the cross-section on a date.

    Every excluded candidate carries one. A peer universe that shrank without
    saying why would be indistinguishable from the silent filters this whole
    phase sequence exists to remove.
    """

    INCLUDED = "INCLUDED"
    """Not an exclusion. The security is part of the cross-section."""

    NOT_A_MEMBER = "NOT_A_MEMBER"
    """The record places it outside the universe on this date — it had left, or
    had not yet arrived, or its listing is of a kind the universe excludes."""

    MEMBERSHIP_UNKNOWN = "MEMBERSHIP_UNKNOWN"
    """The record cannot place it either way. Deliberately distinct from
    `NOT_A_MEMBER`: an unknown is not a no, and collapsing the two would hide how
    much of the cross-section is unaccounted for."""

    NO_MARKET_DATA_ON_DATE = "NO_MARKET_DATA_ON_DATE"
    """It was a member, and it did not trade on this date. **The stale-peer
    rule.** A security whose newest bar predates the date is being described by
    old prices, and ranking the market against it measures the market against a
    security that was not in it."""

    NO_FEATURES = "NO_FEATURES"
    """No feature row at all, so there is nothing to rank."""


@dataclass(frozen=True)
class PeerCandidate:
    """One security offered to the cross-section, with the evidence about it.

    Attributes:
        key: The security's identity, opaque here. The application supplies
            whatever it uses — `(kind, id)` in this project — and this module
            never interprets it.
        features: Its stored features for the date. None when it has none.
        is_member: Whether membership places it in the universe on the date.
        membership_known: Whether membership could place it at all. False means
            `UNKNOWN`, which is not the same as `is_member=False`.
        has_market_data: Whether it actually had market data on the date, as the
            application's stated policy defines that.
    """

    key: tuple[str, int]
    features: MarketFeatures | None
    is_member: bool
    membership_known: bool
    has_market_data: bool


@dataclass(frozen=True)
class PeerSelection:
    """The cross-section, and a reason for everything left out of it.

    Attributes:
        included: The candidates forming the cross-section, in the order given.
        excluded: `(key, reason)` for every candidate that is not in it.
    """

    included: tuple[PeerCandidate, ...] = ()
    excluded: tuple[tuple[tuple[str, int], PeerExclusion], ...] = ()

    @property
    def population(self) -> int:
        """How many securities constitute the cross-section."""
        return len(self.included)

    @property
    def features(self) -> tuple[MarketFeatures, ...]:
        """The feature sets to build a peer context from."""
        return tuple(
            candidate.features for candidate in self.included if candidate.features is not None
        )

    def reasons(self) -> tuple[tuple[str, int], ...]:
        """Exclusion counts by reason, most common first."""
        counts: dict[str, int] = {}
        for _, reason in self.excluded:
            counts[str(reason)] = counts.get(str(reason), 0) + 1
        return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def classify_peer(candidate: PeerCandidate) -> PeerExclusion:
    """Return whether one candidate joins the cross-section, and why not if not.

    The order of the tests is the order of the evidence's strength, so the reason
    names the first thing that disqualified the security rather than the last.

    Args:
        candidate: The security and what is known about it on the date.

    Returns:
        `INCLUDED`, or the reason it is not.
    """
    if candidate.features is None:
        return PeerExclusion.NO_FEATURES
    if not candidate.membership_known:
        return PeerExclusion.MEMBERSHIP_UNKNOWN
    if not candidate.is_member:
        return PeerExclusion.NOT_A_MEMBER
    if not candidate.has_market_data:
        return PeerExclusion.NO_MARKET_DATA_ON_DATE
    return PeerExclusion.INCLUDED


def select_peers(candidates: Iterable[PeerCandidate]) -> PeerSelection:
    """Choose the cross-section for one date, accounting for every candidate.

    Deterministic and order-independent in effect: inclusion depends only on each
    candidate's own evidence, never on its neighbours or its position, so the
    same candidates in any order select the same peers. The returned order
    mirrors the input, and `build_peer_context` sorts what it is given, so the
    percentiles do not depend on it either.

    Args:
        candidates: Every security with a claim to the cross-section.

    Returns:
        The selection, with a reason recorded for each exclusion.
    """
    included: list[PeerCandidate] = []
    excluded: list[tuple[tuple[str, int], PeerExclusion]] = []
    for candidate in candidates:
        reason = classify_peer(candidate)
        if reason is PeerExclusion.INCLUDED:
            included.append(candidate)
        else:
            excluded.append((candidate.key, reason))
    return PeerSelection(included=tuple(included), excluded=tuple(excluded))


def explain_peer(candidates: Sequence[PeerCandidate], key: tuple[str, int]) -> PeerExclusion | None:
    """Return why one named security was, or was not, in the cross-section.

    The question a researcher has to be able to ask of any historical peer
    universe: *why was this security in it, and why was that one not?*

    Args:
        candidates: The candidates the selection was made from.
        key: The security to explain.

    Returns:
        Its verdict, or None when it was never a candidate at all.
    """
    for candidate in candidates:
        if candidate.key == key:
            return classify_peer(candidate)
    return None
