"""Reading and writing the tables.

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
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from data_access.models import (
    BenchmarkPrice,
    Company,
    FilingExcerptRecord,
    FilingRecord,
    FinancialSnapshot,
    FxRate,
    JobRecord,
    PriceHistory,
    ScoreSnapshot,
    StoredDeepResearchReport,
    StoredResearchReport,
    WatchlistEntry,
    _utcnow,
)
from domain import ScoringStatus

if TYPE_CHECKING:
    from domain import FxConversion

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from deep_research import DeepResearchReport
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

MARKET_COVERAGE = "MARKET"
"""A scoring run that covered the whole stored universe.

The only kind a ranking may be built from. A ranking is a comparison, and a day
holding two companies is not one.
"""

SINGLE_COVERAGE = "SINGLE"
"""A scoring run for one company, from single-stock preparation.

A real snapshot, stored and readable like any other — a deep research report has
to explain a score that exists. It is simply not a day the market was ranked, so
`latest_score_date` does not see it.
"""

JOB_RUNNING = "RUNNING"
"""A spawned command with a live process behind it."""

JOB_SUCCEEDED = "SUCCEEDED"
"""A command whose process exited zero."""

JOB_FAILED = "FAILED"
"""A command whose process exited non-zero. Its log says why."""

EXTERNAL_FRESH = "FRESH"
"""External evidence gathered by this run's own searches."""

EXTERNAL_REUSED = "REUSED"
"""External evidence carried over from a recent collection, unsearched."""

EXTERNAL_DEGRADED = "DEGRADED"
"""External evidence gathered by searches that partly failed.

Never reused. The set is thin because the collection went wrong, and treating
that as a healthy cache would hold the gap open for the whole window.
"""

JOB_UNKNOWN = "UNKNOWN"
"""A run whose process is gone without ever being closed.

Distinct from `JOB_FAILED` on purpose. The API restarting mid-run leaves this
behind, and the command itself may well have completed — calling that a failure
would be asserting something nobody observed.
"""

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
            "consolidated_avg_volume": profile.consolidated_avg_volume,
            "volume_source": profile.volume_source,
            "statement_profile": profile.statement_profile,
            "reporting_currency": profile.reporting_currency,
            "quote_currency": profile.quote_currency,
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

    def set_consolidated_volume(self, company_id: int, volume: float, *, source: str) -> None:
        """Record the consolidated average volume the liquidity screen reads.

        Deliberately not `upsert_profile`: the pass that calls this observes two
        fields and nothing else, and building a whole profile to carry them would
        mean supplying a `name` it does not know, which would then be written
        over the real one.

        Args:
            company_id: The company to update.
            volume: Average daily share volume across every venue.
            source: Which feed produced it, so a liquidity decision stays
                traceable to the tape behind it.
        """
        self._session.execute(
            update(Company)
            .where(Company.id == company_id)
            .values(consolidated_avg_volume=volume, volume_source=source, updated_at=_utcnow())
        )

    def get_by_ticker(self, ticker: str) -> Company | None:
        """Return one company by ticker, or None when it is not stored."""
        return self._session.scalars(
            select(Company).where(Company.ticker == ticker.upper())
        ).one_or_none()

    def search(self, query: str, *, limit: int = 20) -> list[Company]:
        """Find companies whose ticker or name matches a fragment.

        Ordered so an exact ticker wins, then a ticker prefix, then everything
        else alphabetically. Typing `MU` should reach Micron before every
        company with "mu" somewhere in its name.

        Args:
            query: Ticker or name fragment. Case-insensitive.
            limit: Maximum companies to return.

        Returns:
            The matches, best first. Empty when the fragment is blank.
        """
        fragment = query.strip()
        if not fragment:
            return []

        upper = fragment.upper()
        pattern = f"%{fragment}%"
        rank = case(
            (Company.ticker == upper, 0),
            (Company.ticker.istartswith(fragment), 1),
            else_=2,
        )

        return list(
            self._session.scalars(
                select(Company)
                .where(or_(Company.ticker.icontains(fragment), Company.name.ilike(pattern)))
                .order_by(rank, Company.ticker)
                .limit(limit)
            )
        )

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
                "period_start": period.period_start,
                "cadence": period.cadence.value,
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


class FxRateRepository:
    """Reads and writes the `fx_rates` table.

    Small on purpose. Scoring needs to ask one question — what was this pair
    worth on or shortly before this date — and to record the answer so the same
    question gets the same answer tomorrow.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_on_or_before(
        self,
        base: str,
        quote: str,
        as_of: date,
        *,
        max_age_days: int,
    ) -> FxRate | None:
        """Return the newest stored rate for a pair at or before a date.

        **Never looks forward.** A rate published after the score date did not
        exist when the score was computed, and using one would restate a
        historical valuation in money from the future — the specific error that
        makes a stored score irreproducible.

        Args:
            base: Currency converted from.
            quote: Currency converted to.
            as_of: The date wanted.
            max_age_days: How far back to accept. A rate older than this is
                refused rather than returned, because a fixing series has no
                ordinary gaps longer than a holiday weekend and a stale rate is
                a worse answer than no rate.

        Returns:
            The newest acceptable observation, or None. Where two providers
            published the same date, the one whose name sorts first is taken so
            the choice is deterministic rather than dependent on insert order.
        """
        oldest = as_of - timedelta(days=max_age_days)
        return self._session.scalars(
            select(FxRate)
            .where(
                FxRate.base_currency == base,
                FxRate.quote_currency == quote,
                FxRate.rate_date <= as_of,
                FxRate.rate_date >= oldest,
            )
            .order_by(FxRate.rate_date.desc(), FxRate.provider.asc())
            .limit(1)
        ).first()

    def save(self, conversion: FxConversion) -> None:
        """Store one observation, replacing any earlier fetch of the same one.

        Idempotent on `(base, quote, rate_date, provider)`, so re-running a
        scoring run does not append a second copy of a rate that cannot have
        changed — a past day's fixing is final.

        Args:
            conversion: The rate to store.
        """
        insert = _insert_for(self._session)
        statement = insert(FxRate).values(
            base_currency=conversion.base,
            quote_currency=conversion.quote,
            rate_date=conversion.rate_date,
            rate=conversion.rate,
            provider=conversion.provider,
            retrieved_at=conversion.retrieved_at or _utcnow(),
        )
        self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    FxRate.base_currency,
                    FxRate.quote_currency,
                    FxRate.rate_date,
                    FxRate.provider,
                ],
                set_={
                    "rate": statement.excluded.rate,
                    "retrieved_at": statement.excluded.retrieved_at,
                },
            )
        )

    def count(self) -> int:
        """Return how many rate observations are stored."""
        return self._session.scalar(select(func.count()).select_from(FxRate)) or 0


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
        exclusion_reasons: Every eligibility check the security failed, in the
            order the screen reports them. Empty for a company that passed.
            Stored because `NOT_ELIGIBLE` on its own cannot tell a company that
            reports in a currency this system will not mix from one that is
            delisted, and a reader looking at a blank row deserves the
            difference.
    """

    company_id: int
    score: CompanyScore
    metrics: CompanyMetrics | None = None
    ranking_state: str = PRELIMINARY
    exclusion_reasons: tuple[str, ...] = ()


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
        "exclusion_reasons",
    )

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_scores(
        self,
        records: Sequence[ScoreRecord],
        score_date: date,
        *,
        coverage: str = MARKET_COVERAGE,
    ) -> int:
        """Insert or replace one day's scores.

        Re-running the scoring command on the same day replaces that day's rows
        rather than appending a second set, so the score history stays one row
        per company per day per version however many times the job is run.

        Args:
            records: The scores to store.
            score_date: The day the scores describe.
            coverage: Whether this run covered the market or a single company.
                Only market-wide runs are eligible to become "the latest
                ranking"; see `latest_score_date`.

        Returns:
            How many rows were written.
        """
        rows = [_score_values(record, score_date, coverage) for record in records]
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

    def latest_score_date(
        self, *, score_version: str, coverage: str = MARKET_COVERAGE
    ) -> date | None:
        """Return the most recent day the market was scored, or None.

        Filtered to market-wide runs by default, which is what makes the ranking
        views safe from single-stock preparation. A per-ticker run writes a real
        snapshot on whatever date its data reaches; without this filter the
        newest date would be that one company, and every ranking would show a
        list of one.

        Args:
            score_version: The formula version to read.
            coverage: Which kind of run to consider. Pass `SINGLE_COVERAGE` only
                to ask when a per-ticker run last happened.

        Returns:
            The date, or None when nothing of that kind has been scored.
        """
        return self._session.scalar(
            select(func.max(ScoreSnapshot.score_date)).where(
                ScoreSnapshot.score_version == score_version,
                ScoreSnapshot.coverage == coverage,
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

    def latest_scored_population(self, *, score_version: str) -> list[tuple[int, float]]:
        """Return every company's most recent scored value, one row per company.

        Ranking on a single `score_date` is right for the dashboard views, where
        a nightly run scored the whole market on one day. It became wrong the
        moment single-stock preparation existed: scoring one ticker writes a
        snapshot on today's date, and that date then holds exactly one company —
        so "rank 1 of 1" is technically true and completely useless.

        This reads each company's newest scored snapshot instead, whatever day it
        landed on, which is what a person means by "where does this company
        stand". A company rescored this morning is compared against the rest of
        the market as most recently known, rather than against whoever happened
        to be rescored alongside it.

        Args:
            score_version: The formula version to read. Never crosses versions,
                for the same reason nothing else here does.

        Returns:
            `(company_id, final_score)` for every company with a scored snapshot,
            unordered. Empty when nothing has been scored under this version.
        """
        newest = (
            select(
                ScoreSnapshot.company_id.label("company_id"),
                func.max(ScoreSnapshot.score_date).label("score_date"),
            )
            .where(
                ScoreSnapshot.score_version == score_version,
                ScoreSnapshot.scoring_status == _SCORED,
            )
            .group_by(ScoreSnapshot.company_id)
            .subquery()
        )

        statement = (
            select(ScoreSnapshot.company_id, ScoreSnapshot.final_score)
            .join(
                newest,
                and_(
                    ScoreSnapshot.company_id == newest.c.company_id,
                    ScoreSnapshot.score_date == newest.c.score_date,
                ),
            )
            .where(
                ScoreSnapshot.score_version == score_version,
                ScoreSnapshot.scoring_status == _SCORED,
                ScoreSnapshot.final_score.is_not(None),
            )
        )

        return [
            (int(company_id), float(final_score))
            for company_id, final_score in self._session.execute(statement)
        ]

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


class JobRepository:
    """Reads and writes the `jobs` table.

    Rows are append-only in spirit: `start` adds one, `finish` closes it, and
    nothing else edits it. A job is never deleted, because the history is the
    feature — "when did the last daily run happen and did it work" is not
    answerable from a table that keeps only what is in flight.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(
        self, kind: str, *, target: str | None = None, pid: int | None = None, log_path: str | None
    ) -> JobRecord:
        """Record a job that has just been spawned.

        Args:
            kind: Which command is running.
            target: The ticker, for a per-company job.
            pid: The spawned process, used later to tell a live run from a
                stale row.
            log_path: Where the process's output is being written.

        Returns:
            The stored row, with its id populated.
        """
        record = JobRecord(
            kind=kind,
            target=target,
            status=JOB_RUNNING,
            pid=pid,
            log_path=log_path,
        )
        self._session.add(record)
        self._session.flush()
        return record

    def finish(self, job_id: int, *, exit_code: int) -> JobRecord | None:
        """Close a job with the exit code its process returned.

        Args:
            job_id: The job to close.
            exit_code: The process's exit status. Zero is a success.

        Returns:
            The updated row, or None when no such job exists.
        """
        record = self.get(job_id)
        if record is None:
            return None

        record.exit_code = exit_code
        record.status = JOB_SUCCEEDED if exit_code == 0 else JOB_FAILED
        record.finished_at = datetime.now(UTC)
        self._session.flush()
        return record

    def abandon(self, job_id: int) -> JobRecord | None:
        """Mark a job whose process is gone but which was never closed.

        A run interrupted by an API restart leaves `RUNNING` behind with nothing
        attached to it. That is not a failure of the command — it may well have
        finished — so it gets its own status rather than being called one.

        Args:
            job_id: The job to abandon.

        Returns:
            The updated row, or None when no such job exists.
        """
        record = self.get(job_id)
        if record is None:
            return None

        record.status = JOB_UNKNOWN
        record.finished_at = datetime.now(UTC)
        self._session.flush()
        return record

    def get(self, job_id: int) -> JobRecord | None:
        """Return one job, or None when the id is unknown."""
        return self._session.get(JobRecord, job_id)

    def running(self, kind: str, *, target: str | None = None) -> JobRecord | None:
        """Return the running job for a kind, if there is one.

        Args:
            kind: The command to look for.
            target: The ticker, for a per-company job. A research run on one
                company does not block a research run on another.

        Returns:
            The in-flight job, or None.
        """
        return self._session.scalars(
            select(JobRecord)
            .where(
                JobRecord.kind == kind,
                JobRecord.target == target,
                JobRecord.status == JOB_RUNNING,
            )
            .order_by(JobRecord.started_at.desc())
            .limit(1)
        ).one_or_none()

    def all_running(self) -> list[JobRecord]:
        """Return every job currently marked running, newest first."""
        return list(
            self._session.scalars(
                select(JobRecord)
                .where(JobRecord.status == JOB_RUNNING)
                .order_by(JobRecord.started_at.desc())
            )
        )

    def recent(self, *, limit: int = 20) -> list[JobRecord]:
        """Return the most recently started jobs, newest first."""
        return list(
            self._session.scalars(
                select(JobRecord).order_by(JobRecord.started_at.desc()).limit(limit)
            )
        )


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


class DeepResearchReportRepository:
    """Reads and writes the `deep_research_reports` table.

    Three things this repository does differently from `ResearchReportRepository`,
    all deliberate.

    It **appends, never overwrites.** `save` always inserts. Re-running deep
    research over identical evidence produces a second row rather than replacing
    the first, because a deep report is a dated investigation and the record of
    what was concluded, when, and on what evidence is the thing worth keeping.
    Nothing here updates or deletes a stored report.

    It **caches on the newest match** rather than on a unique row. `find_cached`
    returns the most recently generated report for a cache key, which is what
    lets history accumulate without a stale row being served ahead of a fresh
    one.

    It **only accepts a validated report.** `save` takes a
    `deep_research.DeepResearchReport`, the type only deep validation produces. A
    `DeepResearchDraft` — unchecked model output — does not type-check here and
    does not have the fields this table needs.

    As with Phase 3, there is no write path from here to `score_snapshots`. A
    deep report explains a score and cannot revise one, and neither can anything
    in the `W.` namespace that fed it.

    Args:
        session: The session to operate in. Not owned; the caller commits.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def latest_collection(
        self,
        company_id: int,
        *,
        deterministic_fingerprint: str,
        since: datetime,
    ) -> StoredDeepResearchReport | None:
        """Return the newest healthy external collection still inside its window.

        The read behind external-evidence reuse. Three conditions, and each one
        is there for a reason:

        The deterministic fingerprint must match, because reusing yesterday's
        news beside today's rescored fundamentals would produce a brief that
        never existed.

        The collection must be recent, because a reuse window is a bound on
        staleness rather than a licence to stop looking.

        And it must have been `FRESH` — a collection whose searches partly
        failed produced a thin set on purpose, and caching that would freeze the
        gap in place for the length of the window.

        Args:
            company_id: The company.
            deterministic_fingerprint: The deterministic half of the current
                brief, which the stored collection must have been gathered
                against.
            since: Earliest collection time still considered fresh.

        Returns:
            The newest qualifying row, or None when the evidence must be
            collected again.
        """
        return self._session.scalars(
            select(StoredDeepResearchReport)
            .where(
                StoredDeepResearchReport.company_id == company_id,
                StoredDeepResearchReport.deterministic_fingerprint == deterministic_fingerprint,
                StoredDeepResearchReport.external_state == EXTERNAL_FRESH,
                StoredDeepResearchReport.external_collected_at.is_not(None),
                StoredDeepResearchReport.external_collected_at >= since,
            )
            .order_by(
                StoredDeepResearchReport.external_collected_at.desc(),
                StoredDeepResearchReport.id.desc(),
            )
            .limit(1)
        ).first()

    def save(
        self,
        company_id: int,
        report: DeepResearchReport,
        *,
        external_state: str = EXTERNAL_FRESH,
        external_collected_at: datetime | None = None,
        collected_external: Sequence[Mapping[str, Any]] = (),
    ) -> StoredDeepResearchReport:
        """Insert one report, keeping every earlier one.

        Args:
            company_id: The company the report is about.
            report: A validated report. There is no way to pass an unvalidated
                one: only deep validation constructs this type.
            external_state: Whether the external evidence behind this report was
                freshly collected, reused from a recent collection, or degraded.
            external_collected_at: When that evidence was actually gathered —
                which is not when this report was generated, if it was reused.
            collected_external: The **whole** accepted external set, not just
                the sources the claims cite. A rerun rebuilds its brief from
                this, and a subset would fingerprint differently.

        Returns:
            The newly inserted row. Never an updated one — a caller wanting to
            know whether an equivalent report already existed asks `find_cached`
            first.
        """
        row = StoredDeepResearchReport(
            **_deep_research_values(company_id, report),
            external_state=external_state,
            external_collected_at=external_collected_at,
            collected_external_json=list(collected_external),
        )
        self._session.add(row)
        self._session.flush()
        return row

    def find_cached(
        self,
        company_id: int,
        *,
        deterministic_fingerprint: str,
        evidence_fingerprint: str,
        prompt_version: str,
    ) -> StoredDeepResearchReport | None:
        """Return the newest report for one cache key, or None.

        Args:
            company_id: The company.
            deterministic_fingerprint: Hash of the calculated and filed evidence.
            evidence_fingerprint: Hash of that plus the external evidence.
            prompt_version: The prompt that produced it.

        Returns:
            The most recently generated matching row, whatever its status.
            Deciding whether a `FAILED` row counts as a hit is the caller's
            judgement, not the storage layer's.
        """
        return self._session.scalars(
            select(StoredDeepResearchReport)
            .where(
                StoredDeepResearchReport.company_id == company_id,
                StoredDeepResearchReport.deterministic_fingerprint == deterministic_fingerprint,
                StoredDeepResearchReport.evidence_fingerprint == evidence_fingerprint,
                StoredDeepResearchReport.prompt_version == prompt_version,
            )
            .order_by(
                StoredDeepResearchReport.generated_at.desc(),
                StoredDeepResearchReport.id.desc(),
            )
            .limit(1)
        ).first()

    def latest_for_company(self, company_id: int) -> StoredDeepResearchReport | None:
        """Return a company's most recently generated deep report.

        Not filtered by score version, unlike the Phase 3 equivalent. A deep
        report is asked for by ticker rather than read off a ranking, so "the
        last thing we concluded about this company" is a question worth
        answering across versions — the row carries its own `score_version` for a
        caller that cares.

        Args:
            company_id: The company.

        Returns:
            The newest row, or None when the company has never been researched.
        """
        return self._session.scalars(
            select(StoredDeepResearchReport)
            .where(StoredDeepResearchReport.company_id == company_id)
            .order_by(
                StoredDeepResearchReport.generated_at.desc(),
                StoredDeepResearchReport.id.desc(),
            )
            .limit(1)
        ).first()

    def history_for_company(
        self, company_id: int, *, limit: int = 20
    ) -> tuple[StoredDeepResearchReport, ...]:
        """Return a company's deep reports, newest first.

        The reason the table appends. Reading how a thesis changed across runs is
        only possible because nothing overwrote the earlier ones.

        Args:
            company_id: The company.
            limit: How many to return, newest first.

        Returns:
            The reports, newest first, empty when there are none.
        """
        return tuple(
            self._session.scalars(
                select(StoredDeepResearchReport)
                .where(StoredDeepResearchReport.company_id == company_id)
                .order_by(
                    StoredDeepResearchReport.generated_at.desc(),
                    StoredDeepResearchReport.id.desc(),
                )
                .limit(limit)
            ).all()
        )

    def count(self) -> int:
        """Return how many deep research reports are stored."""
        return self._session.scalar(select(func.count()).select_from(StoredDeepResearchReport)) or 0


def _deep_research_values(company_id: int, report: DeepResearchReport) -> dict[str, Any]:
    """Flatten one validated deep report into the table's column layout.

    The whole report document goes into `validated_report_json`, external sources
    included, so a stored row resolves its own `W.` citations without a join.
    """
    return {
        "company_id": company_id,
        "ticker": report.ticker,
        "as_of": report.as_of,
        "score_version": report.score_version,
        "deterministic_fingerprint": report.deterministic_fingerprint,
        "evidence_fingerprint": report.evidence_fingerprint,
        "contract_version": report.contract_version,
        "prompt_version": report.prompt_version,
        "model_id": report.model_id,
        "status": report.status.value,
        "confidence": report.confidence.level.value,
        "generated_at": report.generated_at,
        "validated_report_json": report.model_dump(mode="json"),
        "validation_issues_json": [issue.model_dump(mode="json") for issue in report.issues],
    }


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


def _score_values(record: ScoreRecord, score_date: date, coverage: str) -> dict[str, Any]:
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
        "coverage": coverage,
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
        # Pipe-separated rather than JSON: the set is small, closed and ordered,
        # and the scan report already writes it this way.
        "exclusion_reasons": "|".join(record.exclusion_reasons) or None,
    }
