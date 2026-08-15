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
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from data_access.models import (
    BenchmarkPrice,
    Company,
    FinancialSnapshot,
    PriceHistory,
    ScoreSnapshot,
    _utcnow,
)
from domain import ScoringStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import CompanyMetrics, CompanyProfile, CompanyScore, FinancialPeriod, PriceBar


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

    def count(self) -> int:
        """Return how many score snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(ScoreSnapshot)) or 0


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
