"""Attaching an extracted SEC cover row to a stored security, or declining to.

A cover page names a ticker on a date. This module answers "whose ticker was it
then", and it answers it with the **same** machinery the listing layer uses —
`classify_symbol` for whether the symbol ever changed hands, `holder_spans` for
who held it on a date, and `security_identifiers` for which stored security
carried that issuer's CUSIP. Phase 9L is measurement, so nothing here writes;
but a measurement produced by a second, subtly different interpretation of the
same evidence would measure the wrong system.

## What this refuses to do

- **No merges.** Two securities sharing a CIK, a title stem, or a ticker at
  different times are not thereby the same security. Seagen and Pfizer share a
  filer relationship; `SGEN` and any successor are separate stored identities,
  and this module may record a *candidate bridge* for a human to look at, never
  a join that treats them as one.
- **No "current company wins".** The live roster is reachable only for dates
  inside the most recent occupant's span. `BBBY` in 2022 is Bed Bath & Beyond;
  the company holding those letters in 2026 has no claim on it. This is the 9K.11
  defect, and re-introducing it here would corrupt every coverage figure that
  follows.
- **No tie-breaking.** Where the record holds two answers, the result is
  `AMBIGUOUS`. Picking one would manufacture the precision the whole layer
  exists to avoid claiming.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date  # noqa: TC003  — a runtime annotation on `JoinCache`
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from data_access import FtdIdentityObservationRepository
from data_access.models import Company, HistoricalSecurity, SecurityIdentifier
from domain import SymbolStatus, classify_symbol
from stock_screener.identity.resolution import as_evidence, holder_spans

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from domain import SymbolVerdict

log = structlog.get_logger(__name__)


class JoinState(StrEnum):
    """How firmly an extracted row could be attached to a stored identity."""

    EXACT = "EXACT"
    """One stored security, and the dated evidence names it."""

    AMBIGUOUS = "AMBIGUOUS"
    """More than one identity fits, or issuers' windows overlap."""

    UNRESOLVED = "UNRESOLVED"
    """No stored identity carries this symbol. Says nothing about existence."""


@dataclass(frozen=True)
class Join:
    """One extracted row's attachment to the identity layer.

    Attributes:
        state: How firm the attachment is.
        company_id: The live company, when the date falls in its tenure.
        historical_security_id: The stored delisted security, when named.
        reason: Why, in words a report can print.
    """

    state: JoinState
    company_id: int | None = None
    historical_security_id: int | None = None
    reason: str = ""


@dataclass
class JoinCache:
    """What one pass reads once instead of per row.

    A corpus pass asks this question tens of thousands of times over a few
    thousand distinct symbols, so the verdicts and the identifier intervals are
    resolved in memory. Exactly the shape `listing.attribution._Cache` uses, for
    the same reason.

    Attributes:
        verdicts: The Phase 9J verdict per symbol, computed on first use.
        intervals: Identifier intervals by CUSIP.
        live: Live tickers to company ids.
        historical: Delisted securities by ticker.
    """

    verdicts: dict[str, SymbolVerdict | None] = field(default_factory=dict)
    intervals: dict[str, list[tuple[date, date | None, int]]] = field(default_factory=dict)
    live: dict[str, int] = field(default_factory=dict)
    historical: dict[str, list[int]] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session) -> JoinCache:
        """Read the identity tables into memory."""
        intervals: dict[str, list[tuple[date, date | None, int]]] = {}
        for row in session.execute(select(SecurityIdentifier)).scalars():
            if row.cusip is None:
                continue
            intervals.setdefault(row.cusip.upper(), []).append(
                (row.valid_from, row.valid_to, row.historical_security_id)
            )
        live = {c.ticker.upper(): c.id for c in session.execute(select(Company)).scalars()}
        historical: dict[str, list[int]] = {}
        for security in session.execute(select(HistoricalSecurity)).scalars():
            historical.setdefault(security.primary_ticker.upper(), []).append(security.id)
        return cls(intervals=intervals, live=live, historical=historical)

    def security_for(self, cusip: str, moment: date) -> set[int]:
        """Every security whose interval for a CUSIP covers a date."""
        return {
            security_id
            for valid_from, valid_to, security_id in self.intervals.get(cusip.upper(), ())
            if valid_from <= moment and (valid_to is None or valid_to >= moment)
        }


def join_symbol(session: Session, symbol: str, on: date, cache: JoinCache) -> Join:
    """Decide which stored identity held a symbol on a date.

    Args:
        session: An open read-only session.
        symbol: The ticker the cover page named.
        on: The evidence date — when the SEC accepted the filing.
        cache: Identity tables, already in memory.

    Returns:
        The attachment. `UNRESOLVED` is a complete answer for a great many
        symbols: the free record names warrants, units and securities this
        project never tracked.
    """
    key = symbol.upper()
    verdict = _verdict(session, key, cache)
    candidates = cache.historical.get(key, [])

    if verdict is not None and verdict.status is SymbolStatus.AMBIGUOUS:
        return Join(JoinState.AMBIGUOUS, reason="issuers held the symbol concurrently")

    if verdict is not None and verdict.status is SymbolStatus.REUSED:
        return _join_reused(key, on, candidates, verdict, cache)

    # One issuer ever held the symbol, or the record shows none. There is no
    # handover to place, so the live row answers where there is one.
    if key in cache.live:
        return Join(JoinState.EXACT, company_id=cache.live[key], reason="sole occupant, live")
    if len(candidates) == 1:
        return Join(
            JoinState.EXACT, historical_security_id=candidates[0], reason="sole stored occupant"
        )
    if candidates:
        return Join(JoinState.AMBIGUOUS, reason=f"{len(candidates)} stored securities share it")
    return Join(JoinState.UNRESOLVED, reason="no stored identity carries this symbol")


def _join_reused(
    symbol: str,
    on: date,
    candidates: list[int],
    verdict: SymbolVerdict,
    cache: JoinCache,
) -> Join:
    """Place a date inside a symbol's sequence of occupants."""
    holders = holder_spans(verdict.spans, on)
    if len(holders) != 1:
        if not holders:
            return Join(JoinState.UNRESOLVED, reason="no issuer had taken the symbol up by then")
        return Join(JoinState.AMBIGUOUS, reason="two issuers began on the same day")

    held = {sid for cusip in holders[0].cusips for sid in cache.security_for(cusip, on)}
    if len(held) == 1:
        found = held.pop()
        if found in candidates:
            return Join(
                JoinState.EXACT,
                historical_security_id=found,
                reason="dated identifier names the holder",
            )
    if len(held) > 1:
        return Join(JoinState.AMBIGUOUS, reason="the dated identifier record holds two answers")

    latest = max(span.first_settlement for span in verdict.spans)
    if holders[0].first_settlement == latest and symbol in cache.live:
        return Join(
            JoinState.EXACT, company_id=cache.live[symbol], reason="most recent occupant, live"
        )
    # An earlier occupant no stored security matches. Handing it to the live
    # company is precisely the 9K.11 defect.
    return Join(JoinState.AMBIGUOUS, reason="earlier occupant, no stored security matches")


def _verdict(session: Session, symbol: str, cache: JoinCache) -> SymbolVerdict | None:
    """Return the Phase 9J symbol verdict, computed once per symbol per pass."""
    if symbol in cache.verdicts:
        return cache.verdicts[symbol]
    rows = FtdIdentityObservationRepository(session).for_symbol(symbol)
    verdict = classify_symbol(symbol, [as_evidence(r) for r in rows]) if rows else None
    cache.verdicts[symbol] = verdict
    return verdict
