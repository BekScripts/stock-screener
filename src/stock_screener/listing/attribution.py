"""Attaching a monthly observation to a security, or declining to.

A market-share row names a ticker. A ticker is a lease, and Phase 9J built the
machinery that says whose lease it was on a given date — `classify_symbol`,
`holder_spans`, the full-CUSIP distinctions, and the `REUSED` / `AMBIGUOUS`
verdicts. This module uses that machinery; it does not re-implement any of it and
must never route around it.

The rule that governs every decision here is the one Phase 9J's repair was
careful to preserve: **a shared ticker, a shared CIK and adjacent dates are not
together a reason to merge two securities.** `GOOG` and `GOOGL` share an issuer,
an exchange and four characters; `BKEP` and `BKEPP` share an issuer base. They
are different securities, and an attribution that pooled them would corrupt the
identity layer through a side door.

## The order is a date, not a preference

An earlier version of this module asked "is this symbol in `companies`?" first
and stopped there if it was. That reads today's roster as though it described
every month the symbol ever had: `BBBY` is Beyond, Inc. now, so Beyond was
credited with Bed Bath & Beyond's 2022 listing months — one occupant where the
record plainly holds two. The lookup was right and the question was wrong.

So the roster is consulted **last**, and only for months no earlier occupant can
claim. The boundary is the handover itself, taken from the published
fails-to-deliver record: `classify_symbol` says whether a symbol ever changed
hands, `holder_spans` says who held it in a given month, and
`security_identifiers` says which stored security carried that issuer's CUSIP.
A live company is a claimant, never a default.

`historical_securities.last_trading_date` is **not** the boundary, though it
reads like one. Of the 138 symbols carrying both a live row and a delisted row,
127 have a last trading date within days of the present: the free record's
"last trading date" is the last settlement the files happened to show, so
Leggett & Platt appears to be its own dead predecessor. Trusting it would move
those 127 symbols off the live roster wholesale. Whether the symbol *changed
hands* is a different question with a different answer, and it is the one asked
here.

Nothing is the ordinary outcome for a great many symbols — the source names
warrants, units and securities this project never tracked — and `UNRESOLVED` is a
complete answer, not a failure to be repaired.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date  # noqa: TC003  — a runtime annotation on `_Cache`
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from data_access import FtdIdentityObservationRepository
from data_access.models import (
    Company,
    HistoricalSecurity,
    ListingObservation,
    SecurityIdentifier,
)
from domain import SymbolStatus, SymbolVerdict, classify_symbol, month_start
from stock_screener.identity.resolution import as_evidence, holder_spans

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from domain import IssuerSpan

log = structlog.get_logger(__name__)

RESOLVED_COMPANY = "RESOLVED_COMPANY"
"""The symbol is a live `companies` row."""

RESOLVED_HISTORICAL = "RESOLVED_HISTORICAL"
"""The date-aware resolution names exactly one delisted security."""

REUSED = "REUSED"
"""Two or more issuers held the symbol in sequence and no stored security
matches the holder on this month. Recorded, never guessed at."""

AMBIGUOUS = "AMBIGUOUS"
"""Issuers' windows overlap by more than a handover, so no date separates them."""

UNRESOLVED = "UNRESOLVED"
"""No identity evidence carries the symbol. Says nothing about whether the
security existed."""


@dataclass
class _Cache:
    """What one pass reads once instead of per row.

    A pass walks every stored observation — 458,708 of them — so a query inside
    that loop is a query 458,708 times, and inside an autoflushing session each
    one also flushes every row the pass has already rewritten. `security_identifiers`
    is 3,482 rows and `classify_symbol` depends only on the symbol, so both are
    resolved in memory instead.

    Attributes:
        verdicts: The Phase 9J verdict per symbol, computed on first use.
        intervals: Every identifier interval by CUSIP, read once at the start.
    """

    verdicts: dict[str, SymbolVerdict | None] = field(default_factory=dict)
    intervals: dict[str, list[tuple[date, date | None, int]]] = field(default_factory=dict)

    @classmethod
    def load(cls, session: Session) -> _Cache:
        """Read the dated identity table into memory."""
        intervals: dict[str, list[tuple[date, date | None, int]]] = {}
        for row in session.execute(select(SecurityIdentifier)).scalars():
            if row.cusip is None:
                continue
            intervals.setdefault(row.cusip.upper(), []).append(
                (row.valid_from, row.valid_to, row.historical_security_id)
            )
        return cls(intervals=intervals)

    def security_for(self, cusip: str, moment: date) -> set[int]:
        """Every security whose interval for a CUSIP covers a date."""
        return {
            security_id
            for valid_from, valid_to, security_id in self.intervals.get(cusip.upper(), ())
            if valid_from <= moment and (valid_to is None or valid_to >= moment)
        }


@dataclass(frozen=True)
class Occupant:
    """One stored delisted security that carries a symbol.

    Deliberately no exit date. `historical_securities.last_trading_date` looks
    like the boundary a successor may not cross and is not one: measured over the
    138 symbols holding both a live row and a delisted row, 127 carry a date
    within days of the present, because the free record's "last trading date" is
    the last settlement the fails-to-deliver files happened to show. Leggett &
    Platt is not a dead predecessor of Leggett & Platt. The boundary comes from
    the handover evidence instead — see `_attribute`.

    Attributes:
        security_id: The stable `historical_securities.id`.
        name: The issuer name, for the audit trail.
    """

    security_id: int
    name: str


@dataclass(frozen=True)
class AttributionReport:
    """How many observations reached which identity state.

    Attributes:
        counts: `{identity_state: rows}`.
        updated: Rows whose attribution changed this run.
    """

    counts: dict[str, int]
    updated: int

    def summary(self) -> str:
        """One line for a report."""
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        return f"{sum(self.counts.values())} observations — {parts}; {self.updated} updated"


def attribute_observations(session: Session, *, limit: int | None = None) -> AttributionReport:
    """Attach each observation to a security where the record names one.

    Deterministic and re-runnable: the same evidence produces the same
    attribution, and a second run updates nothing.

    Args:
        session: An open session.
        limit: Stop after this many observations, for a partial pass.

    Returns:
        The counts by state.
    """
    live = {c.ticker.upper(): c.id for c in session.execute(select(Company)).scalars()}
    historical: dict[str, list[Occupant]] = {}
    for security in session.execute(select(HistoricalSecurity)).scalars():
        historical.setdefault(security.primary_ticker.upper(), []).append(
            Occupant(security_id=security.id, name=security.name)
        )

    statement = select(ListingObservation).order_by(
        ListingObservation.symbol, ListingObservation.observation_month
    )
    if limit is not None:
        statement = statement.limit(limit)

    counts: dict[str, int] = {}
    updated = 0
    cache = _Cache.load(session)
    for row in session.execute(statement).scalars():
        state, company_id, security_id = _attribute(session, row, live, historical, cache)
        counts[state] = counts.get(state, 0) + 1
        if (
            row.identity_state != state
            or row.company_id != company_id
            or row.historical_security_id != security_id
        ):
            row.identity_state = state
            row.company_id = company_id
            row.historical_security_id = security_id
            updated += 1
    session.flush()
    report = AttributionReport(counts=counts, updated=updated)
    log.info("listing attribution", summary=report.summary())
    return report


def _attribute(
    session: Session,
    row: ListingObservation,
    live: dict[str, int],
    historical: dict[str, list[Occupant]],
    cache: _Cache,
) -> tuple[str, int | None, int | None]:
    """Decide one observation's identity **at the month it was observed**.

    The question asked first is not "who holds this symbol now" but "did this
    symbol change hands at all". Where the published record shows one issuer, the
    live row and any stored delisted row are the same business and the live one
    answers, exactly as before. Where it shows a sequence, the month picks the
    occupant and the live roster may answer only for the most recent one.
    """
    symbol = row.symbol.upper()
    month = month_start(row.observation_month)
    candidates = historical.get(symbol, [])
    verdict = _verdict(session, symbol, cache)

    if verdict is not None and verdict.status is SymbolStatus.AMBIGUOUS:
        # Issuers held the symbol concurrently, so no date separates them.
        return AMBIGUOUS, None, None

    if verdict is not None and verdict.status is SymbolStatus.REUSED:
        return _attribute_reused(session, symbol, month, live, candidates, verdict, cache)

    # One issuer ever held the symbol, or the record shows none. Either way there
    # is no handover to place, so the live row answers where there is one — and a
    # stored delisted row for the same symbol is that same business under its
    # other name, not a predecessor to be preferred over it.
    if symbol in live:
        return RESOLVED_COMPANY, live[symbol], None
    if len(candidates) == 1:
        return RESOLVED_HISTORICAL, None, candidates[0].security_id
    return (AMBIGUOUS, None, None) if candidates else (UNRESOLVED, None, None)


def _attribute_reused(
    session: Session,
    symbol: str,
    month: date,
    live: dict[str, int],
    candidates: Sequence[Occupant],
    verdict: SymbolVerdict,
    cache: _Cache,
) -> tuple[str, int | None, int | None]:
    """Place a month inside a symbol's sequence of occupants.

    `holder_spans` names the issuer that held the symbol that month — the latest
    to have taken it up by then — and `security_identifiers` says which stored
    security carried that issuer's CUSIP, the dated identity table doing exactly
    the job it was built for.

    The live roster is reachable only from the *most recent* span. That is the
    whole point: `BBBY` in 2022 is Bed Bath & Beyond, and Beyond, Inc. holding
    the letters in 2026 gives it no claim on a month it did not trade in.
    """
    holders = holder_spans(verdict.spans, month)
    if len(holders) != 1:
        # None had taken the symbol up by this month, or two began the same day.
        return UNRESOLVED if not holders else AMBIGUOUS, None, None

    held = _security_for_holder(holders[0], month, cache)
    if held is not None and held in {c.security_id for c in candidates}:
        return RESOLVED_HISTORICAL, None, held

    latest = max(span.first_settlement for span in verdict.spans)
    if holders[0].first_settlement == latest and symbol in live:
        # The current occupant, and the live roster names it.
        return RESOLVED_COMPANY, live[symbol], None
    # An earlier occupant no stored security matches. Recorded rather than
    # guessed: handing it to the live company is the defect this closes.
    return REUSED, None, None


def _security_for_holder(holder: IssuerSpan, month: date, cache: _Cache) -> int | None:
    """Which stored security carried the holding issuer's CUSIP that month.

    Args:
        holder: The span `holder_spans` named for the month.
        month: The observation month, as its first day.
        cache: The identifier intervals, already in memory.

    Returns:
        The one security every covering interval agrees on, or None. None where
        the record is silent **and** where it holds more than one answer: a
        contradiction is not a tie to be broken, and picking a side would
        reintroduce the merge Phase 9J exists to prevent.
    """
    found = {sid for cusip in holder.cusips for sid in cache.security_for(cusip, month)}
    return found.pop() if len(found) == 1 else None


def _verdict(session: Session, symbol: str, cache: _Cache) -> SymbolVerdict | None:
    """Return the Phase 9J symbol verdict, computed once per symbol per run."""
    if symbol in cache.verdicts:
        return cache.verdicts[symbol]
    rows = FtdIdentityObservationRepository(session).for_symbol(symbol)
    verdict = classify_symbol(symbol, [as_evidence(r) for r in rows]) if rows else None
    cache.verdicts[symbol] = verdict
    return verdict
