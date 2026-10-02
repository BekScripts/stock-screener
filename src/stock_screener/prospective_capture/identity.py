"""Resolving a prospective listing to the identity the contract ranks by.

The frozen contract fixes the identity key as `(kind, identity_id)` with kind in
`{COMPANY, HISTORICAL}`, "unchanged from the historical layer", and says a ticker
is "a dated display attribute, never an identity". The prospective universe,
however, arrives from a broker asset list keyed by ticker and nothing else. So
something must map one to the other, and that something is the research
dataset's identity registry.

What this module reads is deliberately narrow: the `companies` table, for `id`,
`ticker` and the classification the frozen scorer already consumes. It reads no
fundamental value, no price and no score. Those come from the prospective
snapshots, because reading them here would be the historical fallback the
prospective contract does not permit.

A listing with no research identity is excluded and counted, never invented.
`HISTORICAL` identities are delisted securities and do not appear in a current
broker universe, so a prospective cross-section is COMPANY-only — a property of
the stream rather than a defect, recorded here so it is never mistaken for one.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.backtest_harness.universe import COMPANY, HISTORICAL

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

log = structlog.get_logger(__name__)

__all__ = [
    "IDENTITY_EXCLUSIONS",
    "ProspectiveIdentity",
    "resolve_identities",
]

#: Why a listing in the broker universe may carry no research identity.
IDENTITY_EXCLUSIONS: dict[str, str] = {
    "NO_RESEARCH_IDENTITY": (
        "the ticker is not in the research dataset's companies table, so it has no "
        "(kind, identity_id). A listing that appeared after the dataset froze is "
        "excluded rather than given a new identity mid-window."
    ),
    "AMBIGUOUS_TICKER": (
        "more than one research identity claims the ticker. A reused symbol is "
        "excluded rather than resolved by guesswork."
    ),
    "NOT_A_COMPANY_LISTING": (
        "the broker marks the listing as a fund or inactive, so it is not a "
        "company the frozen score describes."
    ),
}


@dataclass(frozen=True)
class ProspectiveIdentity:
    """One listing, resolved to the identity the decision ranks by.

    Attributes:
        kind: Always `COMPANY` prospectively; `HISTORICAL` cannot be listed.
        identity_id: The research dataset's primary key.
        symbol: The broker's ticker. Display metadata only, never a ranking key.
        name: The registered name, for the record.
        exchange: The listing venue, as the broker reports it.
        sector: The classification the frozen scorer reads. Never a cap or filter.
        industry: As above.
        quote_currency: What the listed share trades in.
    """

    kind: str
    identity_id: int
    symbol: str
    name: str | None = None
    exchange: str | None = None
    sector: str | None = None
    industry: str | None = None
    quote_currency: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        """The identity, as the harness addresses it."""
        return (self.kind, self.identity_id)


def resolve_identities(
    db: sqlite3.Connection, listings: Sequence[Mapping[str, Any]]
) -> tuple[list[ProspectiveIdentity], dict[str, int], dict[str, str]]:
    """Resolve broker listings to research identities.

    Args:
        db: The research database, opened read-only. Only `companies` is read.
        listings: The universe snapshot's records, each with at least `ticker`.

    Returns:
        The resolved identities in `(kind, identity_id)` order, a count per
        exclusion reason, and `{ticker: reason}` for every listing dropped.
    """
    wanted = {str(row["ticker"]).upper(): row for row in listings if row.get("ticker")}
    registry: dict[str, list[tuple[int, Any, Any, Any, Any, Any]]] = {}
    for rid, ticker, name, exchange, sector, industry, quote in db.execute(
        "SELECT id, ticker, name, exchange, sector, industry, quote_currency FROM companies"
    ):
        registry.setdefault(str(ticker).upper(), []).append(
            (int(rid), name, exchange, sector, industry, quote)
        )

    resolved: list[ProspectiveIdentity] = []
    counts: dict[str, int] = dict.fromkeys(IDENTITY_EXCLUSIONS, 0)
    dropped: dict[str, str] = {}
    for symbol, listing in sorted(wanted.items()):
        if not listing.get("is_active") or listing.get("is_fund"):
            counts["NOT_A_COMPANY_LISTING"] += 1
            dropped[symbol] = "NOT_A_COMPANY_LISTING"
            continue
        matches = registry.get(symbol, [])
        if not matches:
            counts["NO_RESEARCH_IDENTITY"] += 1
            dropped[symbol] = "NO_RESEARCH_IDENTITY"
            continue
        if len(matches) > 1:
            counts["AMBIGUOUS_TICKER"] += 1
            dropped[symbol] = "AMBIGUOUS_TICKER"
            continue
        identity_id, name, exchange, sector, industry, quote = matches[0]
        resolved.append(
            ProspectiveIdentity(
                kind=COMPANY,
                identity_id=identity_id,
                symbol=symbol,
                name=name,
                exchange=exchange,
                sector=sector,
                industry=industry,
                quote_currency=quote or listing.get("quote_currency"),
            )
        )
    resolved.sort(key=lambda row: (row.kind, row.identity_id))
    log.info(
        "prospective.identities",
        listings=len(wanted),
        resolved=len(resolved),
        dropped=len(dropped),
    )
    return resolved, counts, dropped


#: `HISTORICAL` exists in the research layer for survivorship and cannot be
#: listed prospectively. Named so the asymmetry is explicit rather than implied.
PROSPECTIVE_KINDS: tuple[str, ...] = (COMPANY,)
UNLISTABLE_KINDS: tuple[str, ...] = (HISTORICAL,)
