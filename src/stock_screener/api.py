"""Minimal FastAPI application.

Read-only endpoints over what the pipeline already stored: the company list, the
eligibility scan, and the Phase 2 rankings. Nothing here calculates a score —
`/api/rankings` serves the snapshots the `score` command wrote, so the API and
the CLI can never disagree about what a company scored today.

The dashboard reads through here and nowhere else. It has no score arithmetic of
its own: every number on a screen came out of a stored snapshot, so a formula
change lands in one place rather than two. The only write in the whole surface is
the watchlist, which is the only thing a person decides rather than the pipeline.

The session is a FastAPI dependency rather than a global, so a test can override
it with an in-memory database in one line.
"""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from data_access import (
    CompanyRepository,
    WatchlistRepository,
    build_session_factory,
    create_engine_from_url,
)
from stock_screener.config import get_settings
from stock_screener.dashboard import research_view, stock_detail, watchlist_view
from stock_screener.scanning import scan_market
from stock_screener.scoring import (
    DEFAULT_RANKING_LIMIT,
    great_company_wrong_price,
    hidden_gems,
    improving_fast,
    latest_score,
    top_opportunities,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.scoring import RankingRow

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 500


@lru_cache(maxsize=1)
def _session_factory(database_url: str) -> sessionmaker[Session]:
    """Return a session factory for a URL, building the engine once.

    Cached on the URL because an engine owns a connection pool. Building one per
    request would leak a pool per request and defeat pooling entirely, which is
    exactly the mistake that makes an API fall over under load rather than in
    testing.
    """
    return build_session_factory(create_engine_from_url(database_url))


def get_session() -> Iterator[Session]:
    """Yield a database session for one request.

    Overridden in tests via `app.dependency_overrides[get_session]`.

    Yields:
        An open session, closed when the request finishes. The engine behind it
        outlives the request.
    """
    session = _session_factory(get_settings().database_url)()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated["Session", Depends(get_session)]

app = FastAPI(
    title="Compounder Radar",
    version="0.3.0",
    summary="Rankings, company detail, grounded research and a watchlist.",
)

# The dashboard is a separate process on a different port in development, which
# makes every request cross-origin. Origins are listed rather than wildcarded:
# this API has a write endpoint, and a wildcard would let any page a browser
# happens to be on add to someone's watchlist.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the process is up.

    Deliberately does not touch the database. A health check that fails when the
    database is briefly unavailable causes an orchestrator to restart a process
    that was working fine.

    Returns:
        A status object.
    """
    return {"status": "ok"}


@app.get("/api/companies")
def list_companies(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    active_only: bool = False,
) -> list[dict[str, Any]]:
    """List stored companies in ticker order.

    Args:
        session: Database session, injected.
        limit: Maximum companies to return.
        active_only: Restrict to companies currently marked active.

    Returns:
        One object per company.
    """
    companies = CompanyRepository(session).list_all(active_only=active_only)
    return [
        {
            "ticker": company.ticker,
            "name": company.name,
            "exchange": company.exchange,
            "sector": company.sector,
            "industry": company.industry,
            "market_cap": company.market_cap,
            "is_active": company.is_active,
        }
        for company in companies[:limit]
    ]


@app.get("/api/scan")
def scan(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = _DEFAULT_LIMIT,
    include_ineligible: bool = False,
) -> dict[str, Any]:
    """Run the eligibility scan over stored data and return the result.

    Args:
        session: Database session, injected.
        limit: Maximum rows to return.
        include_ineligible: Include excluded companies and their reasons.

    Returns:
        Summary counts plus the requested rows. No score is calculated — that is
        Phase 2.
    """
    settings = get_settings()
    result = scan_market(
        session, settings.eligibility_thresholds, bar_volume_basis=settings.bar_volume_basis
    )
    rows = result.rows if include_ineligible else result.eligible

    return {
        "processed": result.processed,
        "eligible": len(result.eligible),
        "rows": [
            {
                "ticker": row.ticker,
                "name": row.profile.name,
                "sector": row.profile.sector,
                "eligible": row.eligible,
                "reasons": [reason.value for reason in row.eligibility.reasons],
                "metrics": row.metrics.model_dump(),
            }
            for row in rows[:limit]
        ],
    }


@app.get("/api/rankings")
def rankings(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = DEFAULT_RANKING_LIMIT,
    min_score: Annotated[float | None, Query(ge=0, le=100)] = None,
) -> dict[str, Any]:
    """Return Top Opportunities from the most recent scoring run.

    Args:
        session: Database session, injected.
        limit: Maximum rows to return.
        min_score: Only companies at or above this final score.

    Returns:
        The ranked rows. Empty until `stock-screener score` has been run.
    """
    return _ranking_response(session, top_opportunities(session, limit=limit, min_score=min_score))


@app.get("/api/rankings/hidden-gems")
def rankings_hidden_gems(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = DEFAULT_RANKING_LIMIT,
) -> dict[str, Any]:
    """Return small, fast-growing companies that already score well.

    Args:
        session: Database session, injected.
        limit: Maximum rows to return.

    Returns:
        The ranked rows.
    """
    return _ranking_response(session, hidden_gems(session, limit=limit))


@app.get("/api/rankings/wrong-price")
def rankings_wrong_price(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = DEFAULT_RANKING_LIMIT,
) -> dict[str, Any]:
    """Return strong companies whose valuation score is poor.

    Args:
        session: Database session, injected.
        limit: Maximum rows to return.

    Returns:
        The ranked rows.
    """
    return _ranking_response(session, great_company_wrong_price(session, limit=limit))


@app.get("/api/rankings/improving")
def rankings_improving(
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_LIMIT)] = DEFAULT_RANKING_LIMIT,
    window_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> dict[str, Any]:
    """Return the companies whose score has risen most over the window.

    Args:
        session: Database session, injected.
        limit: Maximum rows to return.
        window_days: How far back to compare.

    Returns:
        The ranked rows, ordered by improvement. Empty until there is score
        history to compare against.
    """
    return _ranking_response(session, improving_fast(session, limit=limit, window_days=window_days))


@app.get("/api/companies/{ticker}/score")
def company_score(session: SessionDep, ticker: str) -> dict[str, Any]:
    """Return one company's latest score and its full breakdown.

    Args:
        session: Database session, injected.
        ticker: The symbol to look up.

    Returns:
        The score detail, including the component-by-component explanation. A
        company that could not be scored returns its status and no number,
        which is the answer to why it is missing from the ranking.

    Raises:
        HTTPException: 404 when the company is unknown or has never been scored.
    """
    detail = latest_score(session, ticker)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"no score stored for {ticker.upper()}")

    payload = asdict(detail)
    payload["score_date"] = detail.score_date.isoformat()
    return payload


@app.get("/api/stocks/{ticker}")
def stock(session: SessionDep, ticker: str) -> dict[str, Any]:
    """Return one company's overview, score breakdown and headline metrics.

    Args:
        session: Database session, injected.
        ticker: The symbol to look up.

    Returns:
        The detail view. A company that exists but was never scored comes back
        with `score` as null rather than as a 404 — "stored but unscored" is a
        real state with a real explanation.

    Raises:
        HTTPException: 404 when the company is not stored.
    """
    detail = stock_detail(session, get_settings(), ticker)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown company {ticker.upper()}")
    return asdict(detail)


@app.get("/api/stocks/{ticker}/research")
def stock_research(session: SessionDep, ticker: str) -> dict[str, Any]:
    """Return the latest validated research report for one company.

    Only what validation accepted is served; there is no endpoint that returns a
    model draft, because nothing stores one.

    Args:
        session: Database session, injected.
        ticker: The symbol to look up.

    Returns:
        The report, with its claims grouped into the thirteen sections in
        reading order.

    Raises:
        HTTPException: 404 when the company has no report yet.
    """
    payload = research_view(session, ticker)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"no research stored for {ticker.upper()}")
    return payload


@app.get("/api/watchlist")
def watchlist(session: SessionDep) -> dict[str, Any]:
    """Return the watched companies with their current scores.

    Args:
        session: Database session, injected.

    Returns:
        One entry per watched company, most recently added first.
    """
    entries = watchlist_view(session)
    return {"count": len(entries), "entries": entries}


@app.post("/api/watchlist/{ticker}", status_code=201)
def watchlist_add(
    session: SessionDep,
    ticker: str,
    note: Annotated[str | None, Body(embed=True, max_length=500)] = None,
) -> dict[str, Any]:
    """Add a company to the watchlist.

    Idempotent: adding a company already watched updates its note and returns
    the same entry, because the caller asked for a state rather than an event.

    Args:
        session: Database session, injected.
        ticker: The symbol to watch.
        note: Optional free text.

    Returns:
        The stored entry.

    Raises:
        HTTPException: 404 when the company is not stored.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        raise HTTPException(status_code=404, detail=f"unknown company {ticker.upper()}")

    entry = WatchlistRepository(session).add(company.id, note=note)
    session.commit()
    return {"ticker": company.ticker, "note": entry.note, "added_at": entry.added_at.isoformat()}


@app.delete("/api/watchlist/{ticker}")
def watchlist_remove(session: SessionDep, ticker: str) -> dict[str, Any]:
    """Remove a company from the watchlist.

    Args:
        session: Database session, injected.
        ticker: The symbol to stop watching.

    Returns:
        Whether a row was removed. Removing a company that was never watched is
        not an error — the state afterwards is what was asked for either way.

    Raises:
        HTTPException: 404 when the company is not stored.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        raise HTTPException(status_code=404, detail=f"unknown company {ticker.upper()}")

    removed = WatchlistRepository(session).remove(company.id)
    session.commit()
    return {"ticker": company.ticker, "removed": removed}


def _ranking_response(session: Session, rows: list[RankingRow]) -> dict[str, Any]:
    """Wrap ranking rows in a response envelope, marking the watched ones.

    The flag is resolved here rather than per row in the frontend: a dashboard
    that fetched the watchlist separately and joined it client-side would show a
    row as unwatched for as long as the second request took.
    """
    watched = WatchlistRepository(session).watched_company_ids()
    companies = CompanyRepository(session)
    payload = []
    for row in rows:
        company = companies.get_by_ticker(row.ticker)
        entry = asdict(row)
        entry["watched"] = company is not None and company.id in watched
        payload.append(entry)
    return {"count": len(payload), "rows": payload}
