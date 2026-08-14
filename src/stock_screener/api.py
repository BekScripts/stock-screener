"""Minimal FastAPI application.

Phase 1 needs an API foundation, not an API. Three read-only endpoints prove the
wiring — settings, database session, domain models — so Phase 4's dashboard has
somewhere to grow from, and stop there. Building the full dashboard surface now
would mean designing endpoints for a UI that does not exist.

The session is a FastAPI dependency rather than a global, so a test can override
it with an in-memory database in one line.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Annotated, Any

from fastapi import Depends, FastAPI, Query

from data_access import CompanyRepository, build_session_factory, create_engine_from_url
from stock_screener.config import get_settings
from stock_screener.scanning import scan_market

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.orm import Session, sessionmaker

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
    version="0.1.0",
    summary="Phase 1: data ingestion and the eligibility scanner.",
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
