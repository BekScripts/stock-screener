"""Reconstructing the investable universe as it stood on a past date.

The pure rule — which recorded events place a security in the set on a date, and
which leave its state unknown — is `domain.universe_members_as_of`. This module
is the retrieval around it: it reads the append-only `universe_events` record and
the delisting registry, projects them into the neutral shape the domain function
consumes, and hands back the reconstructed set.

It reads three tables and writes none. `universe_events` supplies membership as
the system witnessed it; `delistings` supplies the exits the universe pass may
never have seen, because a security can leave the market without a capture ever
running on the day it did; and `price_history` adjudicates between them.

That third table is there because the first two can only be joined by ticker — a
delisted security has no company row and `companies` carries no CIK — and a
ticker is not an identity. A reused symbol would otherwise attribute one
issuer's exit to another issuer that merely inherited its letters. The price
record refuses that: a company still printing bars after a delisting's effective
date did not delist then, whatever the symbol says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from data_access import (
    CompanyRepository,
    DelistingRepository,
    PriceHistory,
    UniverseEventRepository,
)
from domain import MembershipEvent, UniverseAsOf, universe_members_as_of

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import Session

    from data_access.models import UniverseEvent

log = structlog.get_logger(__name__)

#: The observation kind that marks an event as a first-sighting rather than a
#: witnessed transition. Mirrors the string the capture pass writes.
_BASELINE = "BASELINE"


def _to_membership_event(row: UniverseEvent) -> MembershipEvent:
    """Project a stored universe event into the domain's neutral shape."""
    return MembershipEvent(
        company_id=row.company_id,
        ticker=row.ticker,
        effective_date=row.effective_date,
        is_active=row.is_active,
        is_baseline=row.observation_kind == _BASELINE,
    )


def _delisted_by(session: Session, as_of: date) -> set[int]:
    """Return the ids of companies whose ticker delisted on or before a date.

    Linked by ticker, because a delisting carries nothing else and `companies`
    has no CIK to match on. That link is not an identity, and treating it as one
    is how a reused symbol retires the wrong issuer: the `CZR` that delisted in
    July 2020 is the Caesars Entertainment Corporation that merged into
    Eldorado, and the `CZR` trading today is the survivor that took the symbol.
    Attributing the first one's exit to the second removes a live company from
    every subsequent universe.

    **The price record settles it.** A company with a stored bar after the
    delisting's effective date demonstrably did not stop trading then, so the
    delisting belongs to a different issuer and is not applied to it. A company
    with no bars at all cannot refute the delisting and stays excluded, which is
    the conservative direction: a name wrongly dropped costs coverage, and a
    dead name wrongly kept is survivorship bias.
    """
    companies = CompanyRepository(session).list_all()
    by_ticker = {company.ticker: company.id for company in companies}
    registry = DelistingRepository(session).by_ticker(list(by_ticker))
    last_bar = _last_bar_by_company(session)

    delisted: set[int] = set()
    for ticker, delisting in registry.items():
        if delisting.effective_date > as_of or ticker not in by_ticker:
            continue
        company_id = by_ticker[ticker]
        traded_since = last_bar.get(company_id)
        if traded_since is not None and traded_since > delisting.effective_date:
            log.debug(
                "delisting not applied: the company kept trading after it",
                ticker=ticker,
                effective_date=str(delisting.effective_date),
                last_bar=str(traded_since),
            )
            continue
        delisted.add(company_id)
    return delisted


def _last_bar_by_company(session: Session) -> dict[int, date]:
    """Return the newest stored session for every company, in one query."""
    rows = session.execute(
        select(PriceHistory.company_id, func.max(PriceHistory.date)).group_by(
            PriceHistory.company_id
        )
    )
    return dict(rows.tuples().all())


def reconstruct_universe(session: Session, as_of: date) -> UniverseAsOf:
    """Reconstruct which securities were in the investable set on a date.

    Args:
        session: Open database session.
        as_of: The date to reconstruct membership for.

    Returns:
        The membership set, each member carrying whether its presence was
        observed or assumed from a baseline, together with the companies whose
        state on the date cannot be determined from the record.
    """
    events = [_to_membership_event(row) for row in UniverseEventRepository(session).all_events()]
    universe = universe_members_as_of(events, as_of, delisted_ids=_delisted_by(session, as_of))
    log.info(
        "universe reconstructed",
        as_of=str(as_of),
        members=len(universe.members),
        observed=len(universe.observed),
        undetermined=len(universe.undetermined),
    )
    return universe
