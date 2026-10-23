"""One decision date's outcomes, survivors and delisted names in one result.

The Phase 9B deliverable, and its whole point is what it does *not* require. A
study asking "what did everything I could have bought on 2023-06-30 go on to do"
runs one query against `forward_returns` and gets both kinds of security back.
There is no `include_delisted` flag, no second table, and no `UNION` for a caller
to remember — which matters because the failure mode of every alternative is
silent. A forgotten union does not raise; it returns a tidy, confident,
survivor-only answer.

That is why Option C was chosen in Phase 9A: `forward_returns` carries
`company_id` **or** `historical_security_id`, exactly one, so a delisted
security's outcome is an ordinary row. Excluding it takes a deliberate `WHERE`;
including it takes nothing at all.

## What this module adds on top of that row

A raw row names an identity and a return. Three further things a study needs are
resolved here, and each has a rule about where it may *not* look:

**A label, from the security's own record.** A company row's ticker comes from
`companies`. A historical security's comes from its `security_identifiers`
interval **covering the decision date** — not from today's `companies` table,
which may have reissued that symbol to somebody else entirely, and not from a
later interval belonging to the same security under a name it had not taken yet.
Where no interval covers the date the security's own stored `primary_ticker` is
used and the row says so, so a reader can tell a dated symbol from a fallback.

**A terminal outcome, resolved by identity rather than by symbol.** `delistings`
is keyed by ticker, and a ticker is not an identity — so a security's exit is the
delisting whose effective date falls inside one of *its* identifier intervals,
never merely one that shares its symbol. VMware is the worked case: three rows
under `VMW`, two stock-merger legs and the cash election that states $142.50, and
only the identity intervals say which of them belongs to this security.

**Nothing inferred from a price.** A terminal value comes from
`domain.terminal_outcome`, which is handed reported considerations and never a
price series. A security that traded down to a cent and left has an `UNKNOWN`
terminal value, not a one-cent one.

## What is deliberately absent

No score, no eligibility, no sector, no market capitalisation and no universe
membership. Those are decision-date state, they belong to the reconstruction
path, and computing them here from a `companies` row would be exactly the
today's-state leakage this phase exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import distinct, func, select

from data_access import (
    CompanyRepository,
    DelistingRepository,
    ForwardReturnRepository,
    HistoricalSecurityRepository,
    SecurityIdentifierRepository,
)
from data_access.models import (
    ForwardReturnRecord,
    HistoricalCorporateAction,
    HistoricalPrice,
    HistoricalSecurity,
    SecurityIdentifier,
)
from domain import (
    CorporateActionKind,
    DelistingReason,
    OutcomeStatus,
    TerminalOutcome,
    TerminalStatus,
    classify_delisting,
    terminal_outcome,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import date

    from sqlalchemy import Select
    from sqlalchemy.orm import Session

    from data_access.models import Company

log = structlog.get_logger(__name__)

#: How far past an identifier interval a delisting may fall and still belong to
#: the security that interval names. The same settlement lag the identity layer
#: uses: a security's last observed evidence can precede its formal exit by a few
#: days, and a merger effective on a Monday may last print on the Friday.
TERMINAL_MATCH_GRACE_DAYS = 10


class IdentityKind:
    """Which table a measurement's identity lives in. String values, for reports."""

    COMPANY = "COMPANY"
    """A live `companies` row — a security still in the investable set."""

    HISTORICAL = "HISTORICAL"
    """A `historical_securities` row — a security that left the market."""


@dataclass(frozen=True)
class OutcomeRow:
    """One security's measured outcome from one decision date.

    Attributes:
        identity_kind: `COMPANY` or `HISTORICAL`.
        identity_id: The primary key within that kind. **Not comparable across
            kinds** — company 42 and historical security 42 are different
            securities.
        ticker: The symbol, resolved as of the decision date for a historical
            security and from the live row for a company.
        ticker_is_dated: True when the symbol came from an identifier interval
            covering the decision date; False when it fell back to the security's
            stored last-known symbol. A study that needs a point-in-time symbol
            must be able to tell the two apart.
        horizon_sessions: Sessions measured.
        status: `REALISED`, `TRUNCATED`, `INTERRUPTED`, `UNPRICED` or `NO_ENTRY`.
        entry_date: Session the entry was taken on, or None.
        entry_price: Open on that session, or None.
        exit_date: Session the exit was taken on, or None.
        exit_price: Open on that session, or None.
        total_return: `exit / entry - 1`. None unless `status` is `REALISED` —
            never a zero standing in for an unmeasured outcome.
        last_seen_date: Where an interrupted security was last priced.
        last_seen_price: What it was last priced at.
        terminal: What a holder was finally left with, for a security that left
            the market. None for a company row, which has not left.
    """

    identity_kind: str
    identity_id: int
    ticker: str
    ticker_is_dated: bool
    horizon_sessions: int
    status: str
    entry_date: date | None
    entry_price: float | None
    exit_date: date | None
    exit_price: float | None
    total_return: float | None
    last_seen_date: date | None
    last_seen_price: float | None
    terminal: TerminalOutcome | None = None

    @property
    def is_historical(self) -> bool:
        """Whether this outcome belongs to a security that left the market."""
        return self.identity_kind == IdentityKind.HISTORICAL

    @property
    def is_realised(self) -> bool:
        """Whether the horizon produced a measured return."""
        return self.status == OutcomeStatus.REALISED.value


def _action_kind(kind: str) -> CorporateActionKind | None:
    """Read a stored corporate-action kind, or None where the domain has no member."""
    try:
        return CorporateActionKind(kind)
    except ValueError:
        return None


def resolve_terminal_outcome(
    session: Session,
    security: HistoricalSecurity,
    *,
    entry_price: float | None = None,
) -> TerminalOutcome:
    """Determine what a holder of a delisted security was finally left with.

    The exit is found **by identity, not by symbol**. A security's identifier
    intervals say which dates it held which ticker; a delisting belongs to it
    when its effective date falls inside one of those windows, within the
    settlement grace. A row that merely shares the symbol — a later occupant's
    exit, or an earlier one's — is not this security's exit and is never read as
    one.

    Where a security has no identifier intervals at all, the fallback is the
    security's own stored `status` and `terminal_value_per_share`. Those were
    established by asserted evidence when the row was ingested, which is weaker
    than a dated match but is the security's *own* record and not a symbol
    collision. Receiverships reach the system only this way, since no
    corporate-action feed files one.

    Args:
        session: An open session.
        security: The historical security.
        entry_price: A price to express the outcome as a return against. None
            leaves `total_return` None.

    Returns:
        The outcome, `UNKNOWN` wherever nothing establishes a value. A falling
        price is never read as a settlement.
    """
    asserted_reason = None
    try:
        asserted_reason = DelistingReason(security.status)
    except ValueError:
        asserted_reason = None

    intervals = SecurityIdentifierRepository(session).for_security(security.id)
    matched = []
    for interval in intervals:
        if interval.ticker is None:
            continue
        end = interval.valid_to or security.last_trading_date or interval.valid_from
        window_end = end + timedelta(days=TERMINAL_MATCH_GRACE_DAYS)
        for delisting in DelistingRepository(session).for_ticker(interval.ticker):
            if interval.valid_from <= delisting.effective_date <= window_end:
                matched.append(delisting)

    if matched:
        # Prefer an exit that states a consideration. VMware's three rows are
        # two stock-merger legs and the cash election at $142.50; only the last
        # of them says what a holder actually received.
        stated = [row for row in matched if row.cash_per_share is not None]
        chosen = stated[0] if stated else matched[0]
        return terminal_outcome(
            classify_delisting(_action_kind(chosen.kind), asserted_reason=asserted_reason),
            effective_date=chosen.effective_date,
            reported_value_per_share=chosen.cash_per_share,
            value_source=f"{chosen.source}:{chosen.kind}",
            entry_price=entry_price,
        )

    return terminal_outcome(
        asserted_reason or DelistingReason.UNKNOWN,
        effective_date=security.last_trading_date,
        reported_value_per_share=security.terminal_value_per_share,
        value_source=security.terminal_value_source,
        entry_price=entry_price,
    )


def _historical_ticker(
    session: Session, security: HistoricalSecurity, as_of: date
) -> tuple[str, bool]:
    """Return a delisted security's symbol as of a date, and whether it was dated.

    From the identifier interval covering the date — never from `companies`,
    which may have reissued the symbol, and never from a later interval this
    security had not yet taken up.
    """
    for interval in SecurityIdentifierRepository(session).for_security(security.id):
        if interval.ticker is None or interval.valid_from > as_of:
            continue
        if interval.valid_to is None or interval.valid_to >= as_of:
            return (interval.ticker, True)
    return (security.primary_ticker, False)


def outcome_rows(
    session: Session,
    observation_date: date,
    *,
    horizon_sessions: int | None = None,
    with_terminal: bool = True,
) -> tuple[OutcomeRow, ...]:
    """Return every measured outcome for a decision date, both identity kinds.

    One read of `forward_returns`, no flag and no union. A delisted security is
    in the result because its row is in the table, and the only way to leave it
    out is to filter it out on purpose.

    Args:
        session: An open session.
        observation_date: The decision date.
        horizon_sessions: Restrict to one horizon. None returns all of them.
        with_terminal: Resolve each delisted security's terminal outcome.
            Turning it off skips the per-security identity work when a caller
            only needs the returns.

    Returns:
        The rows, companies first then historical securities, each ordered by id.
    """
    records = ForwardReturnRepository(session).for_observation_date(
        observation_date, horizon_sessions=horizon_sessions
    )
    companies = {company.id: company for company in CompanyRepository(session).list_all()}
    securities = {
        security.id: security for security in HistoricalSecurityRepository(session).list_all()
    }

    rows: list[OutcomeRow] = []
    for record in records:
        row = (
            _company_row(record, companies)
            if record.company_id is not None
            else _historical_row(session, record, securities, observation_date, with_terminal)
        )
        if row is not None:
            rows.append(row)
    return tuple(rows)


def _company_row(record: ForwardReturnRecord, companies: Mapping[int, Company]) -> OutcomeRow:
    """Build the row for a surviving company's measurement."""
    company = companies.get(record.company_id or 0)
    ticker = company.ticker if company is not None else ""
    return OutcomeRow(
        identity_kind=IdentityKind.COMPANY,
        identity_id=record.company_id or 0,
        ticker=ticker,
        ticker_is_dated=False,
        horizon_sessions=record.horizon_sessions,
        status=record.status,
        entry_date=record.entry_date,
        entry_price=record.entry_price,
        exit_date=record.exit_date,
        exit_price=record.exit_price,
        total_return=record.total_return,
        last_seen_date=record.last_seen_date,
        last_seen_price=record.last_seen_price,
        terminal=None,
    )


def _historical_row(
    session: Session,
    record: ForwardReturnRecord,
    securities: Mapping[int, HistoricalSecurity],
    observation_date: date,
    with_terminal: bool,
) -> OutcomeRow | None:
    """Build the row for a delisted security's measurement."""
    security = securities.get(record.historical_security_id or 0)
    if security is None:  # pragma: no cover — the foreign key forbids it
        return None
    ticker, dated = _historical_ticker(session, security, observation_date)
    terminal = (
        resolve_terminal_outcome(session, security, entry_price=record.entry_price)
        if with_terminal
        else None
    )
    return OutcomeRow(
        identity_kind=IdentityKind.HISTORICAL,
        identity_id=security.id,
        ticker=ticker,
        ticker_is_dated=dated,
        horizon_sessions=record.horizon_sessions,
        status=record.status,
        entry_date=record.entry_date,
        entry_price=record.entry_price,
        exit_date=record.exit_date,
        exit_price=record.exit_price,
        total_return=record.total_return,
        last_seen_date=record.last_seen_date,
        last_seen_price=record.last_seen_price,
        terminal=terminal,
    )


@dataclass(frozen=True)
class OutcomeCoverage:
    """How much of a decision date's outcome record is survivorship-aware.

    Attributes:
        observation_date: The date described.
        companies: Company-backed securities measured.
        historical: Historical securities measured.
        realised: Outcomes that produced a number.
        interrupted: Outcomes cut short by a security leaving the market.
        terminal_settled: Delisted securities whose final consideration is known.
        terminal_unknown: Delisted securities whose final consideration is not.
    """

    observation_date: date
    companies: int = 0
    historical: int = 0
    realised: int = 0
    interrupted: int = 0
    terminal_settled: int = 0
    terminal_unknown: int = 0

    @property
    def total(self) -> int:
        """Securities measured, of both kinds."""
        return self.companies + self.historical

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.observation_date}: {self.total} securities "
            f"({self.companies} live, {self.historical} delisted), "
            f"{self.realised} realised, {self.interrupted} interrupted, "
            f"terminal {self.terminal_settled} settled / {self.terminal_unknown} unknown"
        )


def outcome_coverage(
    session: Session, observation_date: date, *, horizon_sessions: int
) -> OutcomeCoverage:
    """Summarise one decision date's outcome record across both identity kinds.

    Args:
        session: An open session.
        observation_date: The decision date.
        horizon_sessions: The horizon to describe. One horizon, so a security is
            counted once rather than five times.

    Returns:
        The counts.
    """
    rows = outcome_rows(session, observation_date, horizon_sessions=horizon_sessions)
    historical = [row for row in rows if row.is_historical]
    return OutcomeCoverage(
        observation_date=observation_date,
        companies=sum(1 for row in rows if not row.is_historical),
        historical=len(historical),
        realised=sum(1 for row in rows if row.is_realised),
        interrupted=sum(1 for row in rows if row.status == OutcomeStatus.INTERRUPTED.value),
        terminal_settled=sum(
            1 for row in historical if row.terminal is not None and row.terminal.is_measurable
        ),
        terminal_unknown=sum(
            1
            for row in historical
            if row.terminal is not None and row.terminal.status is TerminalStatus.UNKNOWN
        ),
    )


@dataclass(frozen=True)
class SurvivorshipAudit:
    """Whether the outcome record is survivorship-aware, and by how much.

    The distinction this exists to protect: **an architecture that can carry
    delisted securities is not the same as a dataset that does.** The first is a
    property of the schema and is either true or false; the second is a
    percentage, and reporting the first while implying the second is how a
    censored dataset gets described as a complete one.

    Attributes:
        observation_date: The decision date measured.
        horizon_sessions: The horizon measured.
        securities: Historical securities in the registry.
        with_identity: How many carry at least one dated identifier interval.
        with_prices: How many carry at least one stored bar.
        with_actions: How many carry at least one corporate action.
        with_terminal_value: How many have a stated terminal consideration.
        cik_resolved: How many carry a filer number, and so *could* be scored.
        trading_on_date: How many the price record shows trading on or before the
            decision date and still trading after it. Evidence of presence, and
            deliberately **not** called membership.
        membership_observed: How many appear in the universe-event record for the
            date. Historical securities have no `companies` row, so the universe
            pass has never recorded one — this is expected to be zero, and saying
            so plainly is the point of the field.
        with_outcomes: How many have at least one measured forward return.
        company_rows: Company-backed measurements stored, across all dates.
        historical_rows: Historical-security-backed measurements stored.
    """

    observation_date: date
    horizon_sessions: int
    securities: int = 0
    with_identity: int = 0
    with_prices: int = 0
    with_actions: int = 0
    with_terminal_value: int = 0
    cik_resolved: int = 0
    trading_on_date: int = 0
    membership_observed: int = 0
    with_outcomes: int = 0
    company_rows: int = 0
    historical_rows: int = 0

    def _share(self, count: int) -> float:
        """A count as a percentage of the registry, or 0.0 for an empty one."""
        return 0.0 if self.securities == 0 else 100.0 * count / self.securities

    @property
    def architecture_capable(self) -> bool:
        """Whether both identity kinds actually coexist in `forward_returns`.

        Measured, not asserted. The schema has permitted this since Phase 9A;
        this says rows of both kinds are really there, which is the only version
        of the claim a study can rely on.
        """
        return self.company_rows > 0 and self.historical_rows > 0

    @property
    def identity_reconstructed_pct(self) -> float:
        """Share of historical securities with a dated identity."""
        return self._share(self.with_identity)

    @property
    def membership_observed_pct(self) -> float:
        """Share whose universe membership on the date was actually witnessed."""
        return self._share(self.membership_observed)

    @property
    def outcomes_available_pct(self) -> float:
        """Share with at least one measured forward return."""
        return self._share(self.with_outcomes)

    def lines(self) -> tuple[str, ...]:
        """Render the audit, one figure a line."""
        return (
            "Historical identity",
            f"  historical securities          {self.securities:,}",
            f"  CIK-resolved                   {self.cik_resolved:,}",
            f"  CIK-unresolved                 {self.securities - self.cik_resolved:,}",
            f"  with a dated identity          {self.with_identity:,}",
            f"  with prices                    {self.with_prices:,}",
            f"  with corporate actions         {self.with_actions:,}",
            f"  with a terminal value          {self.with_terminal_value:,}",
            "Outcomes",
            f"  company-backed rows            {self.company_rows:,}",
            f"  historical-security rows       {self.historical_rows:,}",
            f"  securities with any outcome    {self.with_outcomes:,}",
            f"  trading on {self.observation_date}          {self.trading_on_date:,}",
            f"  universe membership observed   {self.membership_observed:,}",
            "Survivorship",
            f"  architecture capable           {'YES' if self.architecture_capable else 'NO'}",
            f"  historical identity reconstructed  {self.identity_reconstructed_pct:.1f}%",
            f"  historical membership observed     {self.membership_observed_pct:.1f}%",
            f"  historical outcomes available      {self.outcomes_available_pct:.1f}%",
        )


def survivorship_audit(
    session: Session, *, observation_date: date, horizon_sessions: int
) -> SurvivorshipAudit:
    """Measure how survivorship-aware the outcome record actually is.

    Every figure is a query. Nothing is inferred from the schema's *ability* to
    hold a delisted security's outcome — the point of the audit is the gap
    between that ability and the record.

    Args:
        session: An open session.
        observation_date: The decision date to describe.
        horizon_sessions: The horizon to describe.

    Returns:
        The counts and the three percentages.
    """

    def count(statement: Select[tuple[int]]) -> int:
        return int(session.scalar(statement) or 0)

    securities = count(select(func.count()).select_from(HistoricalSecurity))
    company_rows, historical_rows = ForwardReturnRepository(session).counts_by_identity()

    trading = count(
        select(func.count(distinct(HistoricalPrice.historical_security_id)))
        .select_from(HistoricalPrice)
        .where(HistoricalPrice.date <= observation_date)
    )

    return SurvivorshipAudit(
        observation_date=observation_date,
        horizon_sessions=horizon_sessions,
        securities=securities,
        with_identity=count(
            select(func.count(distinct(SecurityIdentifier.historical_security_id))).select_from(
                SecurityIdentifier
            )
        ),
        with_prices=count(
            select(func.count(distinct(HistoricalPrice.historical_security_id))).select_from(
                HistoricalPrice
            )
        ),
        with_actions=count(
            select(
                func.count(distinct(HistoricalCorporateAction.historical_security_id))
            ).select_from(HistoricalCorporateAction)
        ),
        with_terminal_value=count(
            select(func.count())
            .select_from(HistoricalSecurity)
            .where(HistoricalSecurity.terminal_value_per_share.is_not(None))
        ),
        cik_resolved=count(
            select(func.count())
            .select_from(HistoricalSecurity)
            .where(HistoricalSecurity.cik.is_not(None))
        ),
        trading_on_date=trading,
        # Structurally zero, and reported rather than omitted. `universe_events`
        # is keyed by `company_id`, and a historical security has no company row
        # — so no universe pass has ever witnessed one, and none can until
        # membership is recorded against an identity rather than against
        # `companies`. Writing it as a constant is more honest than a query
        # contrived to return zero.
        membership_observed=0,
        with_outcomes=count(
            select(func.count(distinct(ForwardReturnRecord.historical_security_id))).select_from(
                ForwardReturnRecord
            )
        ),
        company_rows=company_rows,
        historical_rows=historical_rows,
    )
