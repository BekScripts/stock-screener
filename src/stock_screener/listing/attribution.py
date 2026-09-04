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

So the order of preference is: a live `companies` row for a symbol still trading,
then a delisted security the date-aware resolution names, then nothing. Nothing
is the ordinary outcome for a great many symbols — the source names warrants,
units and securities this project never tracked — and `UNRESOLVED` is a complete
answer, not a failure to be repaired.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from data_access import FtdIdentityObservationRepository
from data_access.models import Company, HistoricalSecurity, ListingObservation
from domain import SymbolStatus, SymbolVerdict, classify_symbol
from stock_screener.identity.resolution import as_evidence, holder_spans

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

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
    historical: dict[str, list[tuple[int, str]]] = {}
    for security in session.execute(select(HistoricalSecurity)).scalars():
        historical.setdefault(security.primary_ticker.upper(), []).append(
            (security.id, security.name)
        )

    statement = select(ListingObservation).order_by(
        ListingObservation.symbol, ListingObservation.observation_month
    )
    if limit is not None:
        statement = statement.limit(limit)

    counts: dict[str, int] = {}
    updated = 0
    verdict_cache: dict[str, SymbolVerdict | None] = {}
    for row in session.execute(statement).scalars():
        state, company_id, security_id = _attribute(session, row, live, historical, verdict_cache)
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
    historical: dict[str, list[tuple[int, str]]],
    cache: dict[str, SymbolVerdict | None],
) -> tuple[str, int | None, int | None]:
    """Decide one observation's identity, preferring the live roster."""
    symbol = row.symbol.upper()
    if symbol in live:
        return RESOLVED_COMPANY, live[symbol], None

    candidates = historical.get(symbol)
    if not candidates:
        return UNRESOLVED, None, None
    if len(candidates) == 1:
        # One stored security carries the symbol. Still requires the date-aware
        # verdict to agree that the symbol was not shared, so a reused ticker
        # cannot be attributed to whichever occupant happens to be stored.
        verdict = _verdict(session, symbol, cache)
        if verdict is None:
            return UNRESOLVED, None, None
        if verdict.status is SymbolStatus.AMBIGUOUS:
            return AMBIGUOUS, None, None
        if verdict.status is SymbolStatus.RESOLVED:
            return RESOLVED_HISTORICAL, None, candidates[0][0]
        # REUSED: the month decides which occupant, and this layer holds no map
        # from an issuer span to a stored security id. Recorded rather than
        # guessed — attributing it would be the false merge Phase 9J forbids.
        holders = holder_spans(verdict.spans, row.observation_month)
        return (REUSED, None, None) if holders else (UNRESOLVED, None, None)

    return AMBIGUOUS, None, None


def _verdict(
    session: Session, symbol: str, cache: dict[str, SymbolVerdict | None]
) -> SymbolVerdict | None:
    """Return the Phase 9J symbol verdict, computed once per symbol per run."""
    if symbol in cache:
        return cache[symbol]
    rows = FtdIdentityObservationRepository(session).for_symbol(symbol)
    verdict = classify_symbol(symbol, [as_evidence(r) for r in rows]) if rows else None
    cache[symbol] = verdict
    return verdict
