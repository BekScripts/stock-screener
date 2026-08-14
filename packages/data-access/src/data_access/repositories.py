"""Reading and writing the three Phase 1 tables.

Every write is an upsert. The daily job re-fetches overlapping data by design —
a restated quarter has to replace the old one, and the last few price bars are
re-requested because a provider may correct them — so "insert if new, update if
seen" is the only write pattern that leaves the database correct after a second
run.

The upsert is dialect-aware. PostgreSQL and SQLite both support
`INSERT ... ON CONFLICT DO UPDATE`, and SQLAlchemy exposes each through its own
`insert()`. Doing it in one statement rather than select-then-write also removes
the race between the two, and cuts a full-market ingest from hundreds of
thousands of round trips to a few thousand.

Repositories take a `Session`; they never create one. That is what lets the
ingestion tests run against in-memory SQLite with no patching.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from data_access.models import Company, FinancialSnapshot, PriceHistory, _utcnow

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import CompanyProfile, FinancialPeriod, PriceBar


class UnsupportedDialectError(RuntimeError):
    """The database in use has no upsert implementation here.

    Raised rather than silently falling back to select-then-insert, which would
    reintroduce the duplicate-row race this module exists to prevent.
    """


def _insert_for(session: Session) -> Any:
    """Return the dialect-specific `insert()` construct for this session."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return postgres_insert
    if dialect == "sqlite":
        return sqlite_insert
    raise UnsupportedDialectError(
        f"no upsert implementation for dialect {dialect!r}; "
        "use postgresql or sqlite, or add one here"
    )


class CompanyRepository:
    """Reads and writes the `companies` table.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_profile(self, profile: CompanyProfile) -> None:
        """Insert or update one company from a provider profile.

        Fields the provider left as None are **not** written over existing
        values. Alpaca knows a company's exchange but not its sector, and FMP the
        reverse; overwriting with None would mean whichever provider ran last
        erased the other's contribution.

        Args:
            profile: The normalised profile to store.
        """
        insert = _insert_for(self._session)
        values: dict[str, Any] = {
            "ticker": profile.ticker.upper(),
            "name": profile.name,
            "exchange": profile.exchange,
            "sector": profile.sector,
            "industry": profile.industry,
            "market_cap": profile.market_cap,
            "average_volume": profile.average_volume,
            "currency": profile.currency,
            "is_active": profile.is_active,
        }
        updatable = [key for key, value in values.items() if value is not None and key != "ticker"]

        statement = insert(Company).values(**values)
        set_: dict[str, Any] = {key: statement.excluded[key] for key in updatable}
        # `is_active` is a real observation even when False, so it always applies.
        set_["is_active"] = statement.excluded["is_active"]
        # An ORM-level `onupdate` does not fire for a Core INSERT ... ON CONFLICT,
        # so the timestamp is set here or it never moves after the first write.
        set_["updated_at"] = _utcnow()

        self._session.execute(
            statement.on_conflict_do_update(index_elements=[Company.ticker], set_=set_)
        )

    def get_by_ticker(self, ticker: str) -> Company | None:
        """Return one company by ticker, or None when it is not stored."""
        return self._session.scalars(
            select(Company).where(Company.ticker == ticker.upper())
        ).one_or_none()

    def list_all(self, *, active_only: bool = False) -> list[Company]:
        """Return every stored company, ordered by ticker.

        Args:
            active_only: Restrict to companies currently marked active.

        Returns:
            Companies in ticker order, so output is stable between runs.
        """
        statement = select(Company).order_by(Company.ticker)
        if active_only:
            statement = statement.where(Company.is_active.is_(True))
        return list(self._session.scalars(statement))

    def count(self) -> int:
        """Return how many companies are stored."""
        return self._session.scalar(select(func.count()).select_from(Company)) or 0


class FinancialSnapshotRepository:
    """Reads and writes the `financial_snapshots` table.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_periods(self, company_id: int, periods: Iterable[FinancialPeriod]) -> int:
        """Insert or replace financial periods for one company.

        Args:
            company_id: The owning company's primary key.
            periods: Normalised periods to store.

        Returns:
            How many periods were written. Re-running with the same data returns
            the same count but leaves the row count unchanged — the constraint on
            `(company_id, period_end)` turns the repeat into an update.
        """
        rows = [
            {
                "company_id": company_id,
                "period_end": period.period_end,
                "revenue": period.revenue,
                "gross_profit": period.gross_profit,
                "operating_income": period.operating_income,
                "operating_cash_flow": period.operating_cash_flow,
                "capital_expenditure": period.capital_expenditure,
                "free_cash_flow": period.free_cash_flow,
                "cash": period.cash,
                "total_debt": period.total_debt,
                "shares_outstanding": period.shares_outstanding,
                "reported_currency": period.reported_currency,
                "source": period.source,
            }
            for period in periods
        ]
        if not rows:
            return 0

        insert = _insert_for(self._session)
        statement = insert(FinancialSnapshot).values(rows)
        updatable = [key for key in rows[0] if key not in {"company_id", "period_end"}]
        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[FinancialSnapshot.company_id, FinancialSnapshot.period_end],
                set_={key: statement.excluded[key] for key in updatable},
            )
        )
        return len(rows)

    def latest_period_end(self, company_id: int) -> date | None:
        """Return the most recent stored period end, or None when there is none.

        Ingestion uses this to skip a provider round trip when the vendor's
        newest quarter is one already held.
        """
        return self._session.scalar(
            select(func.max(FinancialSnapshot.period_end)).where(
                FinancialSnapshot.company_id == company_id
            )
        )

    def list_for_company(self, company_id: int) -> list[FinancialSnapshot]:
        """Return every stored period for one company, oldest first."""
        return list(
            self._session.scalars(
                select(FinancialSnapshot)
                .where(FinancialSnapshot.company_id == company_id)
                .order_by(FinancialSnapshot.period_end)
            )
        )

    def count(self) -> int:
        """Return how many snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(FinancialSnapshot)) or 0


class PriceHistoryRepository:
    """Reads and writes the `price_history` table.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_bars(self, company_id: int, bars: Sequence[PriceBar]) -> int:
        """Insert or replace daily bars for one company.

        Duplicate dates within a single call are collapsed before the write.
        PostgreSQL rejects an `ON CONFLICT` statement that touches the same row
        twice, so a provider echoing a bar in two pages would otherwise abort the
        whole batch.

        Args:
            company_id: The owning company's primary key.
            bars: Bars to store, in any order.

        Returns:
            How many distinct sessions were written.
        """
        deduplicated = {bar.date: bar for bar in bars}
        rows = [
            {
                "company_id": company_id,
                "date": bar.date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in sorted(deduplicated.values(), key=lambda bar: bar.date)
        ]
        if not rows:
            return 0

        insert = _insert_for(self._session)
        statement = insert(PriceHistory).values(rows)
        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[PriceHistory.company_id, PriceHistory.date],
                set_={
                    key: statement.excluded[key]
                    for key in ("open", "high", "low", "close", "volume")
                },
            )
        )
        return len(rows)

    def latest_date(self, company_id: int) -> date | None:
        """Return the most recent stored session, or None when there is none.

        Ingestion fetches from the day after this rather than re-downloading a
        year of unchanged bars on every run.
        """
        return self._session.scalar(
            select(func.max(PriceHistory.date)).where(PriceHistory.company_id == company_id)
        )

    def list_for_company(self, company_id: int, *, since: date | None = None) -> list[PriceHistory]:
        """Return stored bars for one company, oldest first.

        Args:
            company_id: The owning company's primary key.
            since: Only return sessions on or after this date.

        Returns:
            Bars in date order.
        """
        statement = (
            select(PriceHistory)
            .where(PriceHistory.company_id == company_id)
            .order_by(PriceHistory.date)
        )
        if since is not None:
            statement = statement.where(PriceHistory.date >= since)
        return list(self._session.scalars(statement))

    def count(self) -> int:
        """Return how many bars are stored."""
        return self._session.scalar(select(func.count()).select_from(PriceHistory)) or 0
