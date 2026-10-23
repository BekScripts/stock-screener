"""Which securities a filing may safely be credited with, before it is read.

Phase 9L.3 measured the cost of getting this wrong. The planner credited every
filing with covering **every tracked identity sharing its CIK**, and those
secondary identities are usually baby bonds and preferred stock: CIK 1042046
(`AFG`, `AFGB`, `AFGC`, `AFGD`, `AFGE`) yields 16 common-stock cover rows and 64
`NOTE_OR_DEBT`. Predicted 6,402 month-ends; delivered 2,544.

The confusion was between two different questions:

    Can we obtain this filing?          -> (CIK, accession).  Issuer level.
    Can it support this security-month? -> (identity, month). Security level.

A CIK answers the first and cannot answer the second. This module answers the
second, structurally, from evidence that already exists — never by multiplying an
inflated number by a correction factor afterwards.

## Multi-class does not mean non-common

The tempting fix is "distrust every CIK with more than one identity", and it is
wrong. `LSXMA`/`LSXMB`/`LSXMK` are three Liberty tracking-stock classes and all
three are common stock; `PARA`/`PARAA` likewise. Phase 9L.3 confirmed all five
joining `EXACT` with `COMMON_STOCK` rows. So the rule is not about how *many*
identities share an issuer — it is about which of them the record already shows
carrying common stock.

## Conservative where the record is silent

Where an issuer has several tracked identities and none is yet known to be the
common-stock one, the credit is **zero**, not a share. That is
`MULTI_CLASS_UNASSIGNED`: the filing stays a legitimate acquisition target,
because reading it is how the ambiguity gets resolved, but it earns no predicted
coverage it might not deliver.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

log = structlog.get_logger(__name__)

#: An identity key: `("COMPANY" | "HISTORICAL", id)`.
Identity = tuple[str, int]


class IdentityClass(StrEnum):
    """What the existing record says one tracked identity is."""

    COMMON_ELIGIBLE = "COMMON_ELIGIBLE"
    """A stored cover row classified common stock joins to it."""

    NON_COMMON = "NON_COMMON"
    """Rows exist for its symbol and none of them is common stock."""

    UNKNOWN_CLASS = "UNKNOWN_CLASS"
    """No cover row has yet named it either way."""

    AMBIGUOUS = "AMBIGUOUS"
    """Rows exist but cannot be attributed to it safely."""


class Reach(StrEnum):
    """How far a filing from one issuer may be credited."""

    SAFE_SINGLE = "SAFE_SINGLE"
    """One tracked identity for the issuer. No attribution choice to get wrong."""

    SAFE_COMMON_CLASS = "SAFE_COMMON_CLASS"
    """Several identities, and the record already says which carry common stock."""

    MULTI_CLASS_UNASSIGNED = "MULTI_CLASS_UNASSIGNED"
    """Several identities and none yet known to be the common one. Credit nothing."""

    IDENTITY_AMBIGUOUS = "IDENTITY_AMBIGUOUS"
    """Attribution itself is unsafe. Credit nothing."""

    NO_CIK = "NO_CIK"
    """No tracked identity maps to this filer at all."""


@dataclass(frozen=True)
class IssuerReach:
    """What one issuer's filings may be credited with.

    Attributes:
        cik: The filer.
        kind: Why this credit, in one word a report can print.
        credited: Identities a filing from this issuer may support.
        tracked: Every tracked identity mapping to the issuer.
        excluded_non_common: Identities withheld because they are not common stock.
        unknown: Identities the record has not yet classified.
    """

    cik: int
    kind: Reach
    credited: frozenset[Identity]
    tracked: frozenset[Identity]
    excluded_non_common: frozenset[Identity] = frozenset()
    unknown: frozenset[Identity] = frozenset()

    @property
    def over_credit_avoided(self) -> int:
        """Identities the old CIK-sharing rule would have credited and this does not."""
        return len(self.tracked) - len(self.credited)


@dataclass(frozen=True)
class ReachabilityModel:
    """Security-level reachability for every issuer, built from existing evidence.

    Attributes:
        by_cik: The credit decision per filer.
        identity_class: What each tracked identity was judged to be.
    """

    by_cik: Mapping[int, IssuerReach] = field(default_factory=dict)
    identity_class: Mapping[Identity, IdentityClass] = field(default_factory=dict)

    def credited(self, cik: int) -> frozenset[Identity]:
        """Identities a filing from this issuer may be credited with."""
        found = self.by_cik.get(cik)
        return found.credited if found else frozenset()

    def counts(self) -> dict[str, int]:
        """How many issuers fell into each reach category."""
        tally: dict[str, int] = defaultdict(int)
        for reach in self.by_cik.values():
            tally[str(reach.kind)] += 1
        return dict(tally)

    def class_counts(self) -> dict[str, int]:
        """How many identities fell into each class."""
        tally: dict[str, int] = defaultdict(int)
        for value in self.identity_class.values():
            tally[str(value)] += 1
        return dict(tally)


def build_reachability(
    tracked: Mapping[int, Iterable[Identity]],
    common_observations: Iterable[tuple[int, Identity]],
    other_observations: Iterable[tuple[int, Identity]] = (),
    ambiguous: Iterable[Identity] = (),
) -> ReachabilityModel:
    """Decide, per issuer, which identities its filings may be credited with.

    Args:
        tracked: Tracked universe identities per CIK.
        common_observations: `(cik, identity)` where a stored cover row
            classified **common stock** attributed safely to that identity.
        other_observations: `(cik, identity)` where rows attributed to the
            identity but none was common stock.
        ambiguous: Identities whose attribution is not safe.

    Returns:
        The model. Deliberately built only from what is already known: a caller
        replaying history passes a snapshot, and gets the answer the planner
        would have given at that time.
    """
    common: dict[int, set[Identity]] = defaultdict(set)
    for cik, identity in common_observations:
        common[cik].add(identity)
    other: dict[int, set[Identity]] = defaultdict(set)
    for cik, identity in other_observations:
        other[cik].add(identity)
    unsafe = set(ambiguous)

    identity_class: dict[Identity, IdentityClass] = {}
    by_cik: dict[int, IssuerReach] = {}

    for cik, identities in tracked.items():
        every = frozenset(identities)
        if not every:
            by_cik[cik] = IssuerReach(cik, Reach.NO_CIK, frozenset(), every)
            continue

        known_common = frozenset(i for i in every if i in common.get(cik, set()))
        known_other = frozenset(
            i for i in every if i not in known_common and i in other.get(cik, set())
        )
        unknown = frozenset(i for i in every if i not in known_common and i not in known_other)

        for identity in known_common:
            identity_class[identity] = IdentityClass.COMMON_ELIGIBLE
        for identity in known_other:
            identity_class[identity] = IdentityClass.NON_COMMON
        for identity in unknown:
            identity_class[identity] = (
                IdentityClass.AMBIGUOUS if identity in unsafe else IdentityClass.UNKNOWN_CLASS
            )

        if known_common:
            # The record already names the common-stock security or securities.
            # Several is normal and correct — LSXMA/LSXMB/LSXMK are three real
            # common classes of one issuer, not an error to be collapsed.
            kind = Reach.SAFE_SINGLE if len(every) == 1 else Reach.SAFE_COMMON_CLASS
            credited = known_common
        elif len(every) == 1:
            only = next(iter(every))
            if only in unsafe:
                kind, credited = Reach.IDENTITY_AMBIGUOUS, frozenset()
            else:
                # One tracked identity and it is in the common-stock research
                # universe, so there is no attribution choice to get wrong.
                kind, credited = Reach.SAFE_SINGLE, every
        else:
            # Several identities, none yet shown to carry common stock. The old
            # rule credited all of them; the honest answer is none. The filing
            # stays worth fetching — reading it is how this gets resolved.
            kind, credited = Reach.MULTI_CLASS_UNASSIGNED, frozenset()

        by_cik[cik] = IssuerReach(
            cik=cik,
            kind=kind,
            credited=credited,
            tracked=every,
            excluded_non_common=known_other,
            unknown=unknown,
        )

    model = ReachabilityModel(by_cik=by_cik, identity_class=identity_class)
    log.info("reachability built", issuers=len(by_cik), reach=model.counts())
    return model
