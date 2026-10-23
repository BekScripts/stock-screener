"""Ingest the corporate-action record, so a past price can be read back.

Stored bars carry `adjustment=all`: every historical close is expressed in
today's share basis. That is correct for a return and wrong for a market
capitalisation, and the difference is not small — AAPL's close for 2020-08-28 is
stored as 120.98 and the security traded near 499 that afternoon.

Phase 8A established that the factor cannot be inferred from the prices
themselves, because an unadjusted 2-for-1 and a genuine halving are the same
bar. So the factors have to come from a source that states them. This pass reads
them from the market-data provider and appends them.

Two properties are worth stating:

**Appending, never replacing.** A fingerprint of kind, ex-date and factor makes a
re-run idempotent without an update path. An action that is genuinely revised
arrives as a second row rather than overwriting the first, and both are visible.

**Optional, and never fatal.** A provider without an action endpoint costs the
project its as-traded prices and nothing else. Scanning, scoring and ranking
never touch this table.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from api_clients.errors import ProviderError
from data_access import CompanyRepository, CorporateActionRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import CorporateActionRecord

log = structlog.get_logger(__name__)

#: How many symbols to ask for at once. The endpoint takes a symbol list and
#: pages over the result, so the bound here is URL length, not rate limit.
_SYMBOLS_PER_REQUEST = 100


@runtime_checkable
class CorporateActionSource(Protocol):
    """A provider that can report splits and distributions.

    Checked at runtime rather than required, exactly like the acceptance source.
    A provider that serves bars and nothing else is a legitimate configuration;
    it just cannot supply factors, and this pass says so instead of failing.
    """

    def get_corporate_actions(
        self, symbols: Sequence[str], start: date, end: date
    ) -> list[CorporateActionRecord]:
        """Return the actions for these symbols in this window."""
        ...


@dataclass(frozen=True)
class ActionIngestResult:
    """What one ingestion pass did.

    Attributes:
        companies: How many companies were asked about.
        fetched: How many actions the provider returned.
        stored: How many were new. The gap from `fetched` is re-runs, which is
            the normal case and not a problem.
        unmatched: Symbols the provider reported an action for that no company
            in the database claims. Usually a ticker that has since been
            reassigned; recorded rather than silently dropped.
        failures: `(batch, reason)` for each batch that could not be read.
    """

    companies: int = 0
    fetched: int = 0
    stored: int = 0
    unmatched: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = field(default=())

    def summary(self) -> str:
        """Return a one-line summary for the CLI."""
        return (
            f"{self.companies} companies, {self.fetched} actions fetched, "
            f"{self.stored} new, {len(self.unmatched)} unmatched symbols, "
            f"{len(self.failures)} failed batches"
        )


def _fingerprint(action: CorporateActionRecord) -> str:
    """Return the identity of an action, independent of when it was fetched.

    The source's own identifier where there is one. It is stable across fetches,
    which is what makes a re-run store nothing, and it is unique per record,
    which matters more than it sounds: ACNB has two records for a $0.34 dividend
    with the same ex-date and different payable dates. A composite of kind, date
    and amount cannot tell them apart, and collapsing them would silently decide
    that one of a source's two statements is the true one.

    Falling back to that composite for a source with no identifier keeps a
    re-run idempotent, at the cost of that distinction.
    """
    if action.source_id:
        return action.source_id
    amount = action.split_factor if action.split_factor is not None else action.cash_amount
    return f"{action.kind.value}:{action.ex_date.isoformat()}:{amount!r}"


def ingest_corporate_actions(
    session: Session,
    provider: object,
    *,
    start: date,
    end: date,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
) -> ActionIngestResult:
    """Fetch and append corporate actions for the stored universe.

    Args:
        session: Open database session.
        provider: Market-data provider. Must satisfy `CorporateActionSource` or
            the pass returns an empty result without touching the database.
        start: Earliest ex-date to ask for.
        end: Latest ex-date to ask for.
        tickers: Restrict to these companies. None means every stored company.
        limit: Cap on how many companies to ask about, for a rehearsal run.

    Returns:
        What was fetched and what was stored.
    """
    if not isinstance(provider, CorporateActionSource):
        log.warning(
            "provider reports no corporate actions, skipping",
            provider=type(provider).__name__,
        )
        return ActionIngestResult()

    companies = CompanyRepository(session).list_all()
    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    if wanted is not None:
        companies = [company for company in companies if company.ticker in wanted]
    companies = sorted(companies, key=lambda company: company.ticker)
    if limit is not None:
        companies = companies[:limit]
    if not companies:
        return ActionIngestResult()

    by_ticker = {company.ticker: company.id for company in companies}
    symbols = sorted(by_ticker)
    repository = CorporateActionRepository(session)

    fetched = 0
    stored = 0
    unmatched: set[str] = set()
    failures: list[tuple[str, str]] = []

    for index in range(0, len(symbols), _SYMBOLS_PER_REQUEST):
        batch = symbols[index : index + _SYMBOLS_PER_REQUEST]
        label = f"{batch[0]}..{batch[-1]}"
        try:
            actions = provider.get_corporate_actions(batch, start, end)
        except ProviderError as exc:
            # One unreachable batch costs its symbols their factors. The rest of
            # the universe still gets them, and the batch is named for a re-run.
            log.warning("corporate actions unavailable", batch=label, error=str(exc))
            failures.append((label, str(exc)))
            continue

        fetched += len(actions)
        rows: list[dict[str, Any]] = []
        for action in actions:
            company_id = by_ticker.get(action.symbol)
            if company_id is None:
                unmatched.add(action.symbol)
                continue
            rows.append(
                {
                    "company_id": company_id,
                    "action_fingerprint": _fingerprint(action),
                    "ticker": action.symbol,
                    "action_type": action.kind.value,
                    "ex_date": action.ex_date,
                    "split_factor": action.split_factor,
                    "cash_amount": action.cash_amount,
                    "currency": action.currency,
                    "record_date": action.record_date,
                    "payable_date": action.payable_date,
                    "source": action.source,
                    "source_id": action.source_id,
                }
            )
        stored += repository.add_all(rows)

    return ActionIngestResult(
        companies=len(companies),
        fetched=fetched,
        stored=stored,
        unmatched=tuple(sorted(unmatched)),
        failures=tuple(failures),
    )
