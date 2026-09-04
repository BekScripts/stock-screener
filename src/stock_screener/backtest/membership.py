"""Reconstructing which securities were in the research universe, and when.

The pure rules are in `domain.membership`. This is the retrieval around them:
it gathers, for every security of both identity kinds, the **dated** facts the
record holds about its presence in the market, hands them to the domain, and
stores the intervals that come back.

## The evidence matrix

Every source below is used for exactly what it proves and nothing more. The
third column is the one that matters, because the temptation in this phase is to
read a source as evidence of something adjacent to what it establishes.

| Source | Proves | Does **not** prove | Dating |
| --- | --- | --- | --- |
| `fundamental_observations` | a report was filed that day | it was listed or eligible | real |
| `security_identifiers` | it held a CUSIP and symbol | anything about venue | real |
| `price_history` | it traded on a session | **membership** — see below | real |
| `delistings` (by identity) | it left on a stated date | why, or what a holder got | real |
| `companies.exchange`/`.name` | today's venue and line | anything about the past | **none** |
| `historical_securities.exchange` | last-known venue, 391/3,150 | when it applied | **none** |
| `companies.sector` | today's sector, 623/5,769 | anything about the past | **none** |

**Price and settlement evidence bound intervals; they never grant membership.**
They appear here only to establish *when* a security was in the market alongside
filing evidence, and to support the conservative direction — a security with no
trace at all before some date was not in the universe before it. There is no path
in this module by which "it has a price" produces `MEMBER`; the membership claim
always rests on the listing attributes, which are the undated ones, which is why
every `MEMBER` verdict this reconstruction can currently produce is `INFERRED`.

## Why every `MEMBER` is inferred, and why that is the honest answer

The universe is defined by `evaluate_eligibility`, which reads exchange, security
type and sector. **None of the three is dated anywhere in this system.** Every
stored exchange carries the day capture first ran; `security_identifiers.venue`
is NULL on every row; the fails-to-deliver file carries no venue at all.

So a verdict of `MEMBER` necessarily carries an attribute backwards, and
`MembershipConfidence.INFERRED` plus `inferred_inputs` is how it says so. The
rule applied is deterministic and stated: *a security's last-known venue and line
are taken to have held over the window in which dated evidence places it in the
market, and no further.* A study that will not accept that filters on confidence
and receives an empty universe — which is the true answer to "what can be proven
from dated evidence alone", and is worth being able to ask.

## What this module refuses to do

- It never reads `companies` to decide whether a **historical** security was a
  member. The two identity spaces stay separate; a delisted security's evidence
  comes from its own tables.
- It never extrapolates past its evidence. A date outside every interval is
  `UNKNOWN` by omission, and `members_as_of` reports those securities explicitly
  rather than letting them read as non-members.
- It never asserts `NOT_YET_IN_RECORD` at the edge of the record's own reach. A
  security first seen at the observation record's floor cannot be distinguished
  from one the record simply does not go back far enough for.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import func, select

from data_access import (
    CompanyRepository,
    HistoricalSecurityRepository,
    UniverseMembershipRepository,
    to_membership_interval,
)
from data_access.models import (
    Company,
    Delisting,
    FundamentalObservation,
    HistoricalObservation,
    HistoricalPrice,
    HistoricalSecurity,
    PriceHistory,
    SecurityIdentifier,
)
from domain import (
    ListingAttributes,
    MembershipConfidence,
    MembershipEvidence,
    MembershipInterval,
    MembershipState,
    MembershipVerdict,
    build_membership_intervals,
    is_supported_listing,
    is_unsupported_sector,
    membership_as_of,
)
from stock_screener.backtest.outcomes import TERMINAL_MATCH_GRACE_DAYS

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

log = structlog.get_logger(__name__)

#: What the `source` column records for a reconstruction of this kind.
MEMBERSHIP_SOURCE = "PIT_RECONSTRUCTION"

#: The identity kinds a membership row can carry, mirroring `outcomes`.
COMPANY = "COMPANY"
HISTORICAL = "HISTORICAL"


@dataclass(frozen=True)
class MembershipReconstruction:
    """What one reconstruction pass produced.

    Attributes:
        companies: Live company rows considered.
        securities: Historical securities considered.
        intervals: Membership intervals written.
        by_state: Counts by `(state, confidence)`.
        without_evidence: Securities the record holds nothing dated about, which
            therefore have no intervals and read `UNKNOWN` on every date.
    """

    companies: int = 0
    securities: int = 0
    intervals: int = 0
    by_state: tuple[tuple[str, int], ...] = ()
    without_evidence: int = 0

    def summary(self) -> str:
        """One line describing the pass."""
        breakdown = ", ".join(f"{label} {count}" for label, count in self.by_state)
        return (
            f"reconstruct-membership: {self.companies} companies + {self.securities} "
            f"historical securities, {self.intervals} intervals written "
            f"({breakdown or 'none'}); {self.without_evidence} with no dated evidence"
        )


def _record_floor(session: Session) -> date | None:
    """The earliest date the observation record can speak to at all.

    A security first traced at this floor cannot be distinguished from one that
    existed earlier and simply predates the record, so the reconstruction
    declines to call the period before it a non-membership. Taken from the
    observation record rather than the price record, because filings reach back
    furthest.
    """
    return session.scalar(select(func.min(FundamentalObservation.filed_date)))


def _first_company_evidence(session: Session) -> dict[int, date]:
    """Earliest dated trace of each live company, across filings and prices."""
    earliest: dict[int, date] = {}
    for source in (
        select(
            FundamentalObservation.company_id, func.min(FundamentalObservation.filed_date)
        ).group_by(FundamentalObservation.company_id),
        select(PriceHistory.company_id, func.min(PriceHistory.date)).group_by(
            PriceHistory.company_id
        ),
    ):
        for company_id, first in session.execute(source):
            if first is None:
                continue
            current = earliest.get(company_id)
            if current is None or first < current:
                earliest[company_id] = first
    return earliest


def _first_security_evidence(session: Session) -> dict[int, date]:
    """Earliest dated trace of each historical security, across its own tables.

    Reads only the historical tables. A delisted security's presence is
    established from its own record, never from a `companies` row that may not
    exist and, where it does, belongs to whoever holds the symbol today.
    """
    earliest: dict[int, date] = {}
    for source in (
        select(
            SecurityIdentifier.historical_security_id, func.min(SecurityIdentifier.valid_from)
        ).group_by(SecurityIdentifier.historical_security_id),
        select(HistoricalPrice.historical_security_id, func.min(HistoricalPrice.date)).group_by(
            HistoricalPrice.historical_security_id
        ),
        select(
            HistoricalObservation.historical_security_id,
            func.min(HistoricalObservation.filed_date),
        ).group_by(HistoricalObservation.historical_security_id),
    ):
        for security_id, first in session.execute(source):
            if first is None:
                continue
            current = earliest.get(security_id)
            if current is None or first < current:
                earliest[security_id] = first
    return earliest


def _company_exits(session: Session) -> dict[int, date]:
    """Dated exits for live company rows, adjudicated by the price record.

    `delistings` is keyed by ticker and a ticker is not an identity, so a naive
    join retires whoever holds the symbol today for an exit that belonged to an
    earlier occupant — the `CZR` and `BBBY` shape. The price record settles it,
    exactly as `reconstruct_universe` already does: a company still printing bars
    after an exit's effective date demonstrably did not take that exit.
    """
    companies = CompanyRepository(session).list_all()
    by_ticker = {company.ticker: company.id for company in companies}
    last_bar = dict(
        session.execute(
            select(PriceHistory.company_id, func.max(PriceHistory.date)).group_by(
                PriceHistory.company_id
            )
        )
        .tuples()
        .all()
    )

    exits: dict[int, date] = {}
    rows = session.scalars(select(Delisting).order_by(Delisting.effective_date))
    for delisting in rows:
        company_id = by_ticker.get(delisting.ticker)
        if company_id is None or company_id in exits:
            continue
        traded_since = last_bar.get(company_id)
        if traded_since is not None and traded_since > delisting.effective_date:
            continue
        exits[company_id] = delisting.effective_date
    return exits


def _security_exits(session: Session) -> dict[int, date]:
    """Dated exits for historical securities, matched by identity.

    A security's exit is the delisting falling inside one of **its** identifier
    intervals, the same rule `resolve_terminal_outcome` uses. Where no delisting
    matches, the security's own `last_trading_date` is the exit — it is the
    security's own record of when it stopped, not a symbol collision.
    """
    intervals: dict[int, list[SecurityIdentifier]] = {}
    for row in session.scalars(select(SecurityIdentifier)):
        intervals.setdefault(row.historical_security_id, []).append(row)

    by_ticker: dict[str, list[Delisting]] = {}
    for delisting in session.scalars(select(Delisting).order_by(Delisting.effective_date)):
        by_ticker.setdefault(delisting.ticker.upper(), []).append(delisting)

    exits: dict[int, date] = {}
    for security in session.scalars(select(HistoricalSecurity)):
        matched: list[date] = []
        for interval in intervals.get(security.id, []):
            if interval.ticker is None:
                continue
            end = interval.valid_to or security.last_trading_date or interval.valid_from
            window_end = end + timedelta(days=TERMINAL_MATCH_GRACE_DAYS)
            matched.extend(
                delisting.effective_date
                for delisting in by_ticker.get(interval.ticker.upper(), [])
                if interval.valid_from <= delisting.effective_date <= window_end
            )
        if matched:
            exits[security.id] = min(matched)
        elif security.last_trading_date is not None:
            exits[security.id] = security.last_trading_date
    return exits


def _company_listing(company: Company) -> ListingAttributes:
    """Project a live company's listing attributes, flagged as undated.

    Every flag is False on purpose. `companies.exchange`, `.name` and `.sector`
    carry today's values and nothing dates them, so a verdict resting on them can
    only ever be `INFERRED`. Hard-coding the flags rather than deriving them is
    deliberate: the day a dated venue source is ingested, this is the one place
    that has to change, and it will fail loudly if it does not.
    """
    sector = company.sector
    supported = is_supported_listing(company.ticker, company.name or "", company.exchange)
    return ListingAttributes(
        supported_listing=supported,
        venue_is_dated=False,
        type_is_dated=False,
        sector_excluded=(
            None if sector is None else is_unsupported_sector(sector, company.industry)
        ),
        sector_is_dated=False,
        source="companies (current state, carried back)",
    )


def _security_listing(security: HistoricalSecurity) -> ListingAttributes:
    """Project a historical security's listing attributes, flagged as undated.

    A security with no recorded exchange gets `supported_listing=None`, which the
    domain reads as "no attributes to judge it by" and answers `UNKNOWN`. That is
    the common case — 391 of 3,150 historical securities carry a venue — and
    inventing one from the fact that the security traded is precisely the
    substitution this phase forbids.
    """
    if security.exchange is None:
        return ListingAttributes(
            supported_listing=None,
            source="historical_securities (no venue recorded)",
        )
    supported = is_supported_listing(
        security.primary_ticker, security.name or "", security.exchange
    )
    return ListingAttributes(
        supported_listing=supported,
        venue_is_dated=False,
        type_is_dated=False,
        sector_excluded=(
            None
            if security.sector is None
            else is_unsupported_sector(security.sector, security.industry)
        ),
        sector_is_dated=False,
        source="historical_securities (last-known venue, carried back)",
    )


def _rows_for(
    intervals: Sequence[MembershipInterval],
    *,
    company_id: int | None,
    security_id: int | None,
) -> list[dict[str, Any]]:
    """Project domain intervals onto storable rows."""
    return [
        {
            "company_id": company_id,
            "historical_security_id": security_id,
            "state": str(interval.state),
            "confidence": str(interval.confidence),
            "reason": str(interval.reason),
            "valid_from": interval.valid_from,
            "valid_to": interval.valid_to,
            "inferred_inputs": ",".join(interval.inferred_inputs) or None,
            "source": MEMBERSHIP_SOURCE,
            "detail": interval.detail,
        }
        for interval in intervals
    ]


def reconstruct_membership_record(session: Session) -> MembershipReconstruction:
    """Rebuild every security's membership intervals from dated evidence.

    Args:
        session: An open session.

    Returns:
        What was written, with the state breakdown and the count of securities
        the record holds nothing dated about.
    """
    floor = _record_floor(session)
    companies = CompanyRepository(session).list_all()
    securities = HistoricalSecurityRepository(session).list_all()
    first_company = _first_company_evidence(session)
    first_security = _first_security_evidence(session)
    company_exits = _company_exits(session)
    security_exits = _security_exits(session)

    rows: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    without_evidence = 0

    for company in companies:
        evidence = MembershipEvidence(
            first_seen=first_company.get(company.id),
            delisted_on=company_exits.get(company.id),
            record_floor=floor,
            listing=_company_listing(company),
        )
        built = build_membership_intervals(evidence)
        if not built:
            without_evidence += 1
        for interval in built:
            key = f"{interval.state}/{interval.confidence}"
            counts[key] = counts.get(key, 0) + 1
        rows.extend(_rows_for(built, company_id=company.id, security_id=None))

    for security in securities:
        evidence = MembershipEvidence(
            first_seen=first_security.get(security.id),
            delisted_on=security_exits.get(security.id),
            record_floor=floor,
            listing=_security_listing(security),
        )
        built = build_membership_intervals(evidence)
        if not built:
            without_evidence += 1
        for interval in built:
            key = f"{interval.state}/{interval.confidence}"
            counts[key] = counts.get(key, 0) + 1
        rows.extend(_rows_for(built, company_id=None, security_id=security.id))

    written = UniverseMembershipRepository(session).replace_for(
        rows,
        company_ids=[company.id for company in companies],
        historical_security_ids=[security.id for security in securities],
    )
    log.info(
        "membership reconstructed",
        companies=len(companies),
        securities=len(securities),
        intervals=written,
        without_evidence=without_evidence,
    )
    return MembershipReconstruction(
        companies=len(companies),
        securities=len(securities),
        intervals=written,
        by_state=tuple(sorted(counts.items())),
        without_evidence=without_evidence,
    )


@dataclass(frozen=True)
class UniverseAsOfMembership:
    """The reconstructed universe for a date, with its uncertainty in view.

    Attributes:
        as_of: The decision date.
        members: `(identity kind, id, verdict)` for securities in the universe.
        non_members: The same, for securities the record places outside it.
        unknown: `(identity kind, id)` for securities the record cannot place.
            **Reported, never dropped.** A caller that cannot see how large this
            is has no way to judge the universe it was handed.
    """

    as_of: date
    members: tuple[tuple[str, int, MembershipVerdict], ...] = field(default_factory=tuple)
    non_members: tuple[tuple[str, int, MembershipVerdict], ...] = field(default_factory=tuple)
    unknown: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    def _members_of(self, kind: str) -> int:
        return sum(1 for identity, _, _ in self.members if identity == kind)

    @property
    def observed_members(self) -> int:
        """Members whose every input is dated."""
        return sum(
            1
            for _, _, verdict in self.members
            if verdict.confidence is MembershipConfidence.OBSERVED
        )

    @property
    def inferred_members(self) -> int:
        """Members resting on an attribute the record does not date."""
        return sum(
            1
            for _, _, verdict in self.members
            if verdict.confidence is MembershipConfidence.INFERRED
        )

    @property
    def company_members(self) -> int:
        """Members that are live company rows."""
        return self._members_of(COMPANY)

    @property
    def historical_members(self) -> int:
        """Members that are securities no longer in `companies`."""
        return self._members_of(HISTORICAL)

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.as_of}: {len(self.members)} members "
            f"({self.company_members} live, {self.historical_members} delisted; "
            f"{self.observed_members} observed, {self.inferred_members} inferred), "
            f"{len(self.non_members)} non-members, {len(self.unknown)} unknown"
        )


def members_as_of(session: Session, as_of: date) -> UniverseAsOfMembership:
    """Return the securities available to a backtest on a date.

    **The Phase 9C query.** One read of `universe_membership`, both identity
    kinds, no `include_delisted` flag: a security that left the market is in the
    result because its interval covers the date, and excluding it takes a
    deliberate filter rather than a forgotten one.

    Securities with no covering interval are returned in `unknown` rather than
    omitted. Omitting them would make the universe look complete, which is the
    single most useful lie a survivorship-biased dataset tells.

    Args:
        session: An open session.
        as_of: The decision date.

    Returns:
        Members, non-members and the securities the record cannot place.
    """
    covering = UniverseMembershipRepository(session).covering(as_of)
    placed: set[tuple[str, int]] = set()
    members: list[tuple[str, int, MembershipVerdict]] = []
    non_members: list[tuple[str, int, MembershipVerdict]] = []

    unplaceable: list[tuple[str, int]] = []
    for row in covering:
        kind = COMPANY if row.company_id is not None else HISTORICAL
        identity = row.company_id if row.company_id is not None else row.historical_security_id
        if identity is None:  # pragma: no cover — the check constraint forbids it
            continue
        placed.add((kind, identity))
        verdict = membership_as_of([to_membership_interval(row)], as_of)
        if verdict.state is MembershipState.MEMBER:
            members.append((kind, identity, verdict))
        elif verdict.state is MembershipState.NON_MEMBER:
            non_members.append((kind, identity, verdict))
        else:
            # A covering interval whose verdict is UNKNOWN — a security the
            # record places in the market but cannot qualify, because its
            # listing attributes are absent. It belongs in `unknown` beside the
            # securities with no interval at all: both are securities this
            # universe cannot vouch for, and letting either fall between the
            # three groups would make the universe look smaller and cleaner than
            # it is. Every security must land in exactly one group.
            unplaceable.append((kind, identity))

    unknown = (
        unplaceable
        + [
            (COMPANY, company.id)
            for company in CompanyRepository(session).list_all()
            if (COMPANY, company.id) not in placed
        ]
        + [
            (HISTORICAL, security.id)
            for security in HistoricalSecurityRepository(session).list_all()
            if (HISTORICAL, security.id) not in placed
        ]
    )

    return UniverseAsOfMembership(
        as_of=as_of,
        members=tuple(members),
        non_members=tuple(non_members),
        unknown=tuple(sorted(unknown)),
    )


def membership_for_security(
    session: Session, as_of: date, *, company_id: int | None = None, security_id: int | None = None
) -> MembershipVerdict:
    """Return one security's membership verdict for a date.

    Args:
        session: An open session.
        as_of: The decision date.
        company_id: A live company's id, or None.
        security_id: A historical security's id, or None.

    Returns:
        The verdict, `UNKNOWN` where no interval covers the date.

    Raises:
        ValueError: If neither identity or both are given. The membership record
            has the same exactly-one rule the outcome record does, and a caller
            that names both is asking about two securities.
    """
    if (company_id is None) == (security_id is None):
        message = (
            "membership is asked about exactly one identity; got "
            f"company_id={company_id!r}, security_id={security_id!r}"
        )
        raise ValueError(message)
    repository = UniverseMembershipRepository(session)
    rows = (
        repository.for_company(company_id)
        if company_id is not None
        else repository.for_historical_security(security_id or 0)
    )
    return membership_as_of([to_membership_interval(row) for row in rows], as_of)
