"""Reading and writing the five tables.

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

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from data_access.models import (
    BenchmarkPrice,
    Company,
    FilingExcerptRecord,
    FilingRecord,
    FinancialSnapshot,
    PriceHistory,
    ScoreSnapshot,
    StoredResearchReport,
    WatchlistEntry,
    _utcnow,
)
from domain import ScoringStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import (
        CompanyMetrics,
        CompanyProfile,
        CompanyScore,
        Filing,
        FilingExcerpt,
        FinancialPeriod,
        PriceBar,
    )
    from research import ResearchReport


#: Rows per `INSERT`, and identifiers per `IN` clause.
#:
#: A market-wide scoring run writes one row per company — several thousand — and
#: every column of every row is a bind parameter. PostgreSQL's wire protocol
#: caps a statement at 65,535 of them, and SQLite builds vary, so a single
#: statement for the whole market would fail on the day the universe grew rather
#: than in testing. Chunking costs a handful of round trips a night.
_INSERT_CHUNK_ROWS = 500
_IN_CLAUSE_CHUNK = 5_000

PRELIMINARY = "PRELIMINARY"
"""A score computed from broad-scan data alone.

Market capitalisation may be calculated rather than supplied, and liquidity may
be unverified. Ranked, because a ranking nobody can produce is worth nothing —
but labelled, because it has not been checked against a second source.
"""

FINAL = "FINAL"
"""A score whose company has been through candidate enrichment."""

_SCORED = ScoringStatus.SCORED.value
"""The only status a ranking or a score comparison reads.

Rows exist for companies that could not be scored, deliberately — but a ranking
that included them would be sorting on absent numbers, and a score change
measured against a day the company had no score would be measuring nothing.
"""


class UnsupportedDialectError(RuntimeError):
    """The database in use has no upsert implementation here.

    Raised rather than silently falling back to select-then-insert, which would
    reintroduce the duplicate-row race this module exists to prevent.
    """


def _chunks(items: Sequence[Any], size: int) -> list[Sequence[Any]]:
    """Split a sequence into chunks of at most `size`."""
    return [items[index : index + size] for index in range(0, len(items), size)]


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
                "gross_profit_basis": period.gross_profit_basis,
                "operating_income": period.operating_income,
                "operating_cash_flow": period.operating_cash_flow,
                "capital_expenditure": period.capital_expenditure,
                "free_cash_flow": period.free_cash_flow,
                "cash": period.cash,
                "total_debt": period.total_debt,
                "shares_outstanding": period.shares_outstanding,
                "common_shares_outstanding": period.common_shares_outstanding,
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

    def period_counts(self, company_ids: Sequence[int]) -> dict[int, int]:
        """Return how many stored periods each company has.

        Counted in the database rather than by loading the rows: the callers
        that need this — the research candidate filter, for one — ask about
        dozens of companies at a time and care only about the number.

        Args:
            company_ids: The companies to count for.

        Returns:
            A count per company. A company with no stored periods is absent
            rather than present with a zero, so a caller must use `.get(id, 0)`.
        """
        if not company_ids:
            return {}

        counts: dict[int, int] = {}
        for chunk in _chunks(company_ids, _IN_CLAUSE_CHUNK):
            rows = self._session.execute(
                select(FinancialSnapshot.company_id, func.count())
                .where(FinancialSnapshot.company_id.in_(chunk))
                .group_by(FinancialSnapshot.company_id)
            )
            counts.update({row[0]: row[1] for row in rows})
        return counts

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

    def list_for_company(
        self, company_id: int, *, since: date | None = None, until: date | None = None
    ) -> list[PriceHistory]:
        """Return stored bars for one company, oldest first.

        Args:
            company_id: The owning company's primary key.
            since: Only return sessions on or after this date.
            until: Only return sessions on or before this date. A research brief
                explaining a score passes the score's own date, so the metrics
                behind the brief cannot be calculated from bars that did not
                exist when the score was.

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
        if until is not None:
            statement = statement.where(PriceHistory.date <= until)
        return list(self._session.scalars(statement))

    def count(self) -> int:
        """Return how many bars are stored."""
        return self._session.scalar(select(func.count()).select_from(PriceHistory)) or 0


class BenchmarkPriceRepository:
    """Reads and writes the `benchmark_prices` table.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_bars(self, symbol: str, bars: Sequence[PriceBar]) -> int:
        """Insert or replace daily bars for one benchmark.

        Args:
            symbol: The benchmark's ticker, e.g. `SPY`.
            bars: Bars to store, in any order.

        Returns:
            How many distinct sessions were written.
        """
        deduplicated = {bar.date: bar for bar in bars}
        rows = [
            {
                "symbol": symbol.upper(),
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
        statement = insert(BenchmarkPrice).values(rows)
        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[BenchmarkPrice.symbol, BenchmarkPrice.date],
                set_={
                    key: statement.excluded[key]
                    for key in ("open", "high", "low", "close", "volume")
                },
            )
        )
        return len(rows)

    def latest_date(self, symbol: str) -> date | None:
        """Return the most recent stored session for a benchmark, or None."""
        return self._session.scalar(
            select(func.max(BenchmarkPrice.date)).where(BenchmarkPrice.symbol == symbol.upper())
        )

    def list_bars(self, symbol: str, *, since: date | None = None) -> list[BenchmarkPrice]:
        """Return stored bars for one benchmark, oldest first.

        Args:
            symbol: The benchmark's ticker.
            since: Only return sessions on or after this date.

        Returns:
            Bars in date order.
        """
        statement = (
            select(BenchmarkPrice)
            .where(BenchmarkPrice.symbol == symbol.upper())
            .order_by(BenchmarkPrice.date)
        )
        if since is not None:
            statement = statement.where(BenchmarkPrice.date >= since)
        return list(self._session.scalars(statement))

    def count(self) -> int:
        """Return how many benchmark bars are stored."""
        return self._session.scalar(select(func.count()).select_from(BenchmarkPrice)) or 0


@dataclass(frozen=True, slots=True)
class ScoreRecord:
    """One company's score, ready to persist.

    Attributes:
        company_id: The owning company's primary key.
        score: The calculated score, including the statuses that explain an
            absent number.
        metrics: The metrics the score was calculated from. A few of them are
            copied onto the row so a ranking can be rendered without recomputing
            anything.
        ranking_state: Whether this row's inputs have been through candidate
            enrichment. `PRELIMINARY` until they have.
    """

    company_id: int
    score: CompanyScore
    metrics: CompanyMetrics | None = None
    ranking_state: str = PRELIMINARY


class ScoreSnapshotRepository:
    """Reads and writes the `score_snapshots` table.

    Every read takes a `score_version`. Scores produced by different versions of
    the formula are different measurements, and subtracting one from another
    would report a change in the rules as a change in the business.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    #: Columns the daily upsert overwrites. `company_id`, `score_date` and
    #: `score_version` identify the row and are therefore not among them.
    _UPDATABLE = (
        "calculated_at",
        "scoring_status",
        "growth_score",
        "quality_score",
        "valuation_score",
        "momentum_score",
        "raw_score",
        "risk_penalty",
        "final_score",
        "risk_level",
        "score_category",
        "valuation_basis",
        "data_coverage",
        "risk_coverage",
        "market_cap",
        "revenue_growth_yoy",
        "revenue_growth_acceleration",
        "enterprise_value",
        "ranking_state",
        "market_cap_source",
        "volume_basis",
        "breakdown",
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_scores(self, records: Sequence[ScoreRecord], score_date: date) -> int:
        """Insert or replace one day's scores.

        Re-running the scoring command on the same day replaces that day's rows
        rather than appending a second set, so the score history stays one row
        per company per day per version however many times the job is run.

        Args:
            records: The scores to store.
            score_date: The day the scores describe.

        Returns:
            How many rows were written.
        """
        rows = [_score_values(record, score_date) for record in records]
        if not rows:
            return 0

        for chunk in _chunks(rows, _INSERT_CHUNK_ROWS):
            self._insert_chunk(chunk)
        return len(rows)

    def _insert_chunk(self, rows: Sequence[dict[str, Any]]) -> None:
        """Write one chunk of score rows in a single upsert."""
        insert = _insert_for(self._session)
        statement = insert(ScoreSnapshot).values(list(rows))
        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    ScoreSnapshot.company_id,
                    ScoreSnapshot.score_date,
                    ScoreSnapshot.score_version,
                ],
                set_={key: statement.excluded[key] for key in self._UPDATABLE},
            )
        )

    def latest_score_date(self, *, score_version: str) -> date | None:
        """Return the most recent day scores were stored for, or None."""
        return self._session.scalar(
            select(func.max(ScoreSnapshot.score_date)).where(
                ScoreSnapshot.score_version == score_version
            )
        )

    def latest_for_company(self, company_id: int, *, score_version: str) -> ScoreSnapshot | None:
        """Return a company's most recent snapshot, whatever its status."""
        return self._session.scalars(
            select(ScoreSnapshot)
            .where(
                ScoreSnapshot.company_id == company_id,
                ScoreSnapshot.score_version == score_version,
            )
            .order_by(ScoreSnapshot.score_date.desc())
            .limit(1)
        ).one_or_none()

    def snapshots_on_or_before(
        self,
        company_ids: Sequence[int],
        target: date,
        *,
        score_version: str,
        scoring_status: str = _SCORED,
    ) -> dict[int, ScoreSnapshot]:
        """Return each company's newest snapshot at or before a date.

        Score history has gaps — a weekend, a failed run, a company that only
        became scoreable last month — so a score change is measured against the
        nearest earlier snapshot rather than requiring one exactly 30 days back.

        Args:
            company_ids: Companies to look up. An empty sequence returns `{}`
                without a query.
            target: The date to look back from, inclusive.
            score_version: Only snapshots from this version are considered.
            scoring_status: Only snapshots with this status are considered, so a
                comparison is never made against a day the company had no score.

        Returns:
            A mapping of company id to snapshot, omitting companies with none.
        """
        found: dict[int, ScoreSnapshot] = {}
        for chunk in _chunks(company_ids, _IN_CLAUSE_CHUNK):
            newest = (
                select(
                    ScoreSnapshot.company_id.label("company_id"),
                    func.max(ScoreSnapshot.score_date).label("score_date"),
                )
                .where(
                    ScoreSnapshot.company_id.in_(chunk),
                    ScoreSnapshot.score_version == score_version,
                    ScoreSnapshot.scoring_status == scoring_status,
                    ScoreSnapshot.score_date <= target,
                )
                .group_by(ScoreSnapshot.company_id)
                .subquery()
            )

            statement = select(ScoreSnapshot).join(
                newest,
                and_(
                    ScoreSnapshot.company_id == newest.c.company_id,
                    ScoreSnapshot.score_date == newest.c.score_date,
                    ScoreSnapshot.score_version == score_version,
                ),
            )
            found.update({row.company_id: row for row in self._session.scalars(statement)})

        return found

    def list_scored(
        self,
        *,
        score_version: str,
        score_date: date | None = None,
        scoring_status: str = _SCORED,
        min_final_score: float | None = None,
        max_market_cap: float | None = None,
        min_revenue_growth: float | None = None,
        min_growth_score: float | None = None,
        min_quality_score: float | None = None,
        max_valuation_score: float | None = None,
        exclude_risk_levels: Sequence[str] = (),
        limit: int | None = None,
    ) -> list[tuple[ScoreSnapshot, Company]]:
        """Return ranked snapshots with their companies, best first.

        The ordering is fully deterministic — final score, then raw score, then
        growth, then size, then ticker — so two runs over unchanged data produce
        the same ranking and a diff between days means something changed.

        Args:
            score_version: The formula version to read.
            score_date: The day to rank. Defaults to the most recent day stored
                for this version.
            scoring_status: Which status to include. Defaults to scored
                companies only, which is what a ranking means.
            min_final_score: Lower bound on the final score.
            max_market_cap: Upper bound on market capitalisation.
            min_revenue_growth: Lower bound on latest year-over-year growth.
            min_growth_score: Lower bound on the growth component.
            min_quality_score: Lower bound on the quality component.
            max_valuation_score: Upper bound on the valuation component, for
                finding good companies at a poor price.
            exclude_risk_levels: Risk labels to leave out.
            limit: Maximum rows to return.

        Returns:
            `(snapshot, company)` pairs in ranking order. Empty when nothing has
            been scored for this version.
        """
        as_of = score_date or self.latest_score_date(score_version=score_version)
        if as_of is None:
            return []

        statement = (
            select(ScoreSnapshot, Company)
            .join(Company, Company.id == ScoreSnapshot.company_id)
            .where(
                ScoreSnapshot.score_version == score_version,
                ScoreSnapshot.score_date == as_of,
                ScoreSnapshot.scoring_status == scoring_status,
            )
        )

        if min_final_score is not None:
            statement = statement.where(ScoreSnapshot.final_score >= min_final_score)
        if max_market_cap is not None:
            statement = statement.where(ScoreSnapshot.market_cap <= max_market_cap)
        if min_revenue_growth is not None:
            statement = statement.where(ScoreSnapshot.revenue_growth_yoy >= min_revenue_growth)
        if min_growth_score is not None:
            statement = statement.where(ScoreSnapshot.growth_score >= min_growth_score)
        if min_quality_score is not None:
            statement = statement.where(ScoreSnapshot.quality_score >= min_quality_score)
        if max_valuation_score is not None:
            statement = statement.where(ScoreSnapshot.valuation_score <= max_valuation_score)

        if exclude_risk_levels:
            statement = statement.where(ScoreSnapshot.risk_level.not_in(exclude_risk_levels))

        statement = statement.order_by(
            ScoreSnapshot.final_score.desc(),
            ScoreSnapshot.raw_score.desc(),
            ScoreSnapshot.growth_score.desc(),
            ScoreSnapshot.market_cap.desc(),
            Company.ticker,
        )
        if limit is not None:
            statement = statement.limit(limit)

        return [(row[0], row[1]) for row in self._session.execute(statement)]

    def history_for_company(
        self, company_id: int, *, score_version: str, limit: int | None = None
    ) -> list[ScoreSnapshot]:
        """Return one company's score history, newest first.

        Filtered to a single `score_version`, like every other read here. A
        history that mixed versions would show the day the formula changed as a
        jump in the business.

        Args:
            company_id: The company to read.
            score_version: The formula version to read.
            limit: Most snapshots to return. None returns all of them.

        Returns:
            The snapshots, newest first.
        """
        statement = (
            select(ScoreSnapshot)
            .where(
                ScoreSnapshot.company_id == company_id,
                ScoreSnapshot.score_version == score_version,
            )
            .order_by(ScoreSnapshot.score_date.desc())
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self._session.scalars(statement))

    def count(self) -> int:
        """Return how many score snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(ScoreSnapshot)) or 0


class FilingRepository:
    """Reads and writes the `filings` table.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    _UPDATABLE = ("form", "filed", "period_end", "primary_document", "url", "source")

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_filings(self, company_id: int, filings: Iterable[Filing]) -> int:
        """Insert or replace one company's filing index entries.

        Upserted rather than appended because the SEC's recent-filings index
        overlaps heavily between runs, and because a filer occasionally corrects
        a period or a document name on an accession already stored.

        Args:
            company_id: The owning company.
            filings: Index entries to store.

        Returns:
            How many rows were written.
        """
        rows = [
            {
                "company_id": company_id,
                "accession": filing.accession,
                "form": filing.form,
                "filed": filing.filed,
                "period_end": filing.period_end,
                "primary_document": filing.primary_document,
                "url": filing.url,
                "source": filing.source,
            }
            for filing in filings
        ]
        if not rows:
            return 0

        for chunk in _chunks(rows, _INSERT_CHUNK_ROWS):
            insert = _insert_for(self._session)
            statement = insert(FilingRecord).values(list(chunk))
            self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[FilingRecord.company_id, FilingRecord.accession],
                    set_={key: statement.excluded[key] for key in self._UPDATABLE},
                )
            )
        return len(rows)

    def list_for_company(
        self, company_id: int, *, until: date | None = None, limit: int | None = None
    ) -> list[FilingRecord]:
        """Return one company's filings, newest first.

        Args:
            company_id: The company to read.
            until: Latest filing date to include. A research brief explaining a
                score passes the score's own date here, so the brief cannot cite
                a filing that did not exist when the score was calculated.
            limit: Most filings to return.

        Returns:
            The filings, newest first.
        """
        statement = select(FilingRecord).where(FilingRecord.company_id == company_id)
        if until is not None:
            statement = statement.where(FilingRecord.filed <= until)
        statement = statement.order_by(FilingRecord.filed.desc(), FilingRecord.accession.desc())
        if limit is not None:
            statement = statement.limit(limit)
        return list(self._session.scalars(statement))

    def latest_filed(self, company_id: int) -> date | None:
        """Return the most recent filing date stored for one company, or None."""
        return self._session.scalar(
            select(func.max(FilingRecord.filed)).where(FilingRecord.company_id == company_id)
        )

    def count(self) -> int:
        """Return how many filings are stored."""
        return self._session.scalar(select(func.count()).select_from(FilingRecord)) or 0


class WatchlistRepository:
    """Reads and writes the `watchlist` table.

    Adding is an upsert on the company, so pressing the button twice is the same
    as pressing it once. Removing a company that is not on the list is not an
    error either — both operations answer "is this company watched", and the
    answer afterwards is what the caller asked for.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, company_id: int, *, note: str | None = None) -> WatchlistEntry:
        """Add a company to the watchlist, or update the note on one already there.

        Args:
            company_id: The company to watch.
            note: Optional free text. Replaces any existing note when given, and
                leaves it alone when None, so adding twice cannot silently erase
                what was written the first time.

        Returns:
            The stored entry.
        """
        existing = self.get(company_id)
        if existing is not None:
            if note is not None:
                existing.note = note
            return existing

        entry = WatchlistEntry(company_id=company_id, note=note)
        self._session.add(entry)
        self._session.flush()
        return entry

    def remove(self, company_id: int) -> bool:
        """Remove a company from the watchlist.

        Args:
            company_id: The company to stop watching.

        Returns:
            Whether a row was removed. False means it was never on the list,
            which is not a failure.
        """
        entry = self.get(company_id)
        if entry is None:
            return False
        self._session.delete(entry)
        self._session.flush()
        return True

    def get(self, company_id: int) -> WatchlistEntry | None:
        """Return one company's entry, or None when it is not watched."""
        return self._session.scalars(
            select(WatchlistEntry).where(WatchlistEntry.company_id == company_id)
        ).one_or_none()

    def list_all(self) -> list[WatchlistEntry]:
        """Return every entry, most recently added first."""
        return list(
            self._session.scalars(select(WatchlistEntry).order_by(WatchlistEntry.added_at.desc()))
        )

    def watched_company_ids(self) -> set[int]:
        """Return the ids of every watched company, for marking a ranking."""
        return set(self._session.scalars(select(WatchlistEntry.company_id)))

    def count(self) -> int:
        """Return how many companies are watched."""
        return self._session.scalar(select(func.count()).select_from(WatchlistEntry)) or 0


class FilingExcerptRepository:
    """Reads and writes the `filing_excerpts` table.

    The store behind "what does this filing actually say". Writes are upserts on
    the company, accession and section, so re-extracting a filing is idempotent:
    a second pass over the same document rewrites the same rows rather than
    stacking near-duplicate paragraphs a brief would then have to deduplicate.
    """

    _UPDATABLE = ("form", "text", "filed", "url", "source", "extracted_at")

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_excerpts(self, company_id: int, excerpts: Iterable[FilingExcerpt]) -> int:
        """Insert or replace extracted text for one company.

        Args:
            company_id: The owning company.
            excerpts: Extracted sections to store.

        Returns:
            How many rows were written.
        """
        now = datetime.now(UTC)
        rows = [
            {
                "company_id": company_id,
                "accession": excerpt.accession,
                "form": excerpt.form,
                "section": excerpt.section,
                "text": excerpt.text,
                "filed": excerpt.filed,
                "url": excerpt.url,
                "source": excerpt.source,
                "extracted_at": now,
            }
            for excerpt in excerpts
        ]
        if not rows:
            return 0

        for chunk in _chunks(rows, _INSERT_CHUNK_ROWS):
            insert = _insert_for(self._session)
            statement = insert(FilingExcerptRecord).values(list(chunk))
            self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        FilingExcerptRecord.company_id,
                        FilingExcerptRecord.accession,
                        FilingExcerptRecord.section,
                    ],
                    set_={key: statement.excluded[key] for key in self._UPDATABLE},
                )
            )
        return len(rows)

    def list_for_company(
        self, company_id: int, *, until: date | None = None, limit: int | None = None
    ) -> list[FilingExcerptRecord]:
        """Return one company's extracted sections, newest filing first.

        Args:
            company_id: The company to read.
            until: Latest filing date to include. A brief explaining a score
                passes the score's own date, so it cannot quote a filing that
                did not exist when the score was calculated.
            limit: Most rows to return.

        Returns:
            The excerpts, newest first, ordered within a filing by section so a
            brief assembled twice from the same data is byte-identical.
        """
        statement = select(FilingExcerptRecord).where(FilingExcerptRecord.company_id == company_id)
        if until is not None:
            statement = statement.where(FilingExcerptRecord.filed <= until)
        statement = statement.order_by(
            FilingExcerptRecord.filed.desc(),
            FilingExcerptRecord.accession.desc(),
            FilingExcerptRecord.section.asc(),
        )
        if limit is not None:
            statement = statement.limit(limit)
        return list(self._session.scalars(statement))

    def accessions_with_text(self, company_id: int) -> set[str]:
        """Return the accessions already extracted, so a pass can skip them."""
        return set(
            self._session.scalars(
                select(FilingExcerptRecord.accession).where(
                    FilingExcerptRecord.company_id == company_id
                )
            )
        )

    def count(self) -> int:
        """Return how many excerpts are stored."""
        return self._session.scalar(select(func.count()).select_from(FilingExcerptRecord)) or 0


class ResearchReportRepository:
    """Reads and writes the `research_reports` table.

    Two things this repository will not do, both deliberate.

    It **only accepts a validated report.** `save` takes a
    `research.ResearchReport`, which is the type `research.validate_report`
    produces and nothing else does. A `DraftReport` — unchecked model output —
    is not merely discouraged here, it does not type-check and does not have the
    fields this table needs. That is the whole reason the contract splits the
    two.

    It **never writes to `score_snapshots`.** A research report explains a score;
    it cannot revise one. Phase 3 has no write path to the scoring tables at all.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def save(self, company_id: int, report: ResearchReport) -> StoredResearchReport:
        """Insert or replace one report, keyed on the cache key.

        Re-validating the same draft against the same brief writes the same row
        rather than a second one, which is what makes a re-run idempotent.

        Args:
            company_id: The company the report is about.
            report: A validated report. There is no way to pass an unvalidated
                one: only `validate_report` and `failed_report` construct this
                type.

        Returns:
            The stored row.
        """
        existing = self.find(
            company_id,
            score_version=report.score_version,
            brief_fingerprint=report.brief_fingerprint,
            prompt_version=report.prompt_version,
        )
        values = _research_values(company_id, report)

        if existing is None:
            row = StoredResearchReport(**values)
            self._session.add(row)
            self._session.flush()
            return row

        for key, value in values.items():
            setattr(existing, key, value)
        self._session.flush()
        return existing

    def find(
        self,
        company_id: int,
        *,
        score_version: str,
        brief_fingerprint: str,
        prompt_version: str,
    ) -> StoredResearchReport | None:
        """Return the row for one cache key, or None.

        Args:
            company_id: The company.
            score_version: The formula version the report explains.
            brief_fingerprint: Hash of the evidence it was written from.
            prompt_version: The prompt that produced it.

        Returns:
            The stored row, whatever its status. Deciding whether a `FAILED` row
            counts as a hit is the caller's judgement, not the storage layer's.
        """
        return self._session.scalars(
            select(StoredResearchReport).where(
                StoredResearchReport.company_id == company_id,
                StoredResearchReport.score_version == score_version,
                StoredResearchReport.brief_fingerprint == brief_fingerprint,
                StoredResearchReport.prompt_version == prompt_version,
            )
        ).one_or_none()

    def latest_for_company(
        self, company_id: int, *, score_version: str
    ) -> StoredResearchReport | None:
        """Return a company's most recently generated report under one version."""
        return self._session.scalars(
            select(StoredResearchReport)
            .where(
                StoredResearchReport.company_id == company_id,
                StoredResearchReport.score_version == score_version,
            )
            .order_by(StoredResearchReport.generated_at.desc())
            .limit(1)
        ).one_or_none()

    def count(self) -> int:
        """Return how many research reports are stored."""
        return self._session.scalar(select(func.count()).select_from(StoredResearchReport)) or 0


def _research_values(company_id: int, report: ResearchReport) -> dict[str, Any]:
    """Flatten one validated report into the `research_reports` column layout."""
    return {
        "company_id": company_id,
        "score_version": report.score_version,
        "score_date": report.score_date,
        "brief_fingerprint": report.brief_fingerprint,
        "contract_version": report.contract_version,
        "prompt_version": report.prompt_version,
        "model_id": report.model_id,
        "status": report.status.value,
        "generated_at": report.generated_at,
        "report": report.model_dump(mode="json"),
        "issues": [issue.model_dump(mode="json") for issue in report.issues],
    }


def _score_values(record: ScoreRecord, score_date: date) -> dict[str, Any]:
    """Flatten one score into the `score_snapshots` column layout.

    Component scores are None whenever the status is not `SCORED`. They are
    never written as zero: a ranking sorts on these columns, and a fabricated
    zero would place a company that could not be judged below one that was
    judged and found wanting.
    """
    score = record.score
    metrics = record.metrics
    risk = score.risk

    return {
        "company_id": record.company_id,
        "score_date": score_date,
        "score_version": score.score_version,
        "calculated_at": _utcnow(),
        "scoring_status": score.status.value,
        "growth_score": score.growth.score if score.growth else None,
        "quality_score": score.quality.score if score.quality else None,
        "valuation_score": score.valuation.score if score.valuation else None,
        "momentum_score": score.momentum.score if score.momentum else None,
        "raw_score": score.raw_score,
        "risk_penalty": risk.total_penalty if risk else None,
        "final_score": score.final_score,
        "risk_level": risk.level.value if risk else None,
        "score_category": score.category.value if score.category else None,
        "valuation_basis": score.valuation_basis.value,
        "data_coverage": score.data_coverage,
        "risk_coverage": risk.coverage if risk else None,
        "market_cap": metrics.market_cap if metrics else None,
        "revenue_growth_yoy": metrics.revenue_growth_yoy if metrics else None,
        "revenue_growth_acceleration": metrics.revenue_growth_acceleration if metrics else None,
        "enterprise_value": metrics.enterprise_value if metrics else None,
        "ranking_state": record.ranking_state,
        "market_cap_source": metrics.market_cap_source.value if metrics else None,
        "volume_basis": metrics.liquidity_basis.value if metrics else None,
        "breakdown": score.model_dump(mode="json"),
    }
