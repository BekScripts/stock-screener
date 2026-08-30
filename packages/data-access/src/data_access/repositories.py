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
from sqlalchemy import insert as sa_insert
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from data_access.models import (
    BenchmarkPrice,
    Company,
    CorporateAction,
    Delisting,
    FilingAcceptance,
    FilingExcerptRecord,
    FilingRecord,
    FinancialSnapshot,
    ForwardReturnRecord,
    FundamentalObservation,
    FxRate,
    JobRecord,
    PitCaptureRun,
    PitFeatureSnapshot,
    PitScoreSnapshot,
    PitSignalSnapshot,
    PriceHistory,
    ScoreSnapshot,
    StoredDeepResearchReport,
    StoredResearchReport,
    UniverseEvent,
    WatchlistEntry,
    _utcnow,
)
from domain import Freshness, ScoringStatus

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

    def get(self, company_id: int) -> Company | None:
        """Return one company by primary key, or None when it is not stored."""
        return self._session.get(Company, company_id)

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

    def list_bars(
        self, symbol: str, *, since: date | None = None, until: date | None = None
    ) -> list[BenchmarkPrice]:
        """Return stored bars for one benchmark, oldest first.

        Args:
            symbol: The benchmark's ticker.
            since: Only return sessions on or after this date.
            until: Only return sessions on or before this date. What a
                point-in-time reader passes so a market-relative figure cannot
                be computed against a session that had not happened yet.

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
        if until is not None:
            statement = statement.where(BenchmarkPrice.date <= until)
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
        freshness: Whether the fundamentals behind the score were current. A
            stale score keeps its number everywhere the number describes the
            company, and is kept out of current rankings only.
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
    freshness: Freshness = Freshness.CURRENT


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

    def list_for_date(self, *, score_date: date, score_version: str) -> list[ScoreSnapshot]:
        """Return every snapshot stored for one day, whatever its status.

        Unlike `list_scored`, this filters nothing: an ineligible company, an
        unsupported sector and a company with too little history are all
        returned, carrying the status that says why they have no number. A
        point-in-time record wants the whole day — "this company was excluded
        then" is exactly the kind of fact a later study needs and cannot
        reconstruct.

        Args:
            score_date: The day to read.
            score_version: The formula version to read.

        Returns:
            Every stored row for that day and version, by company id.
        """
        return list(
            self._session.scalars(
                select(ScoreSnapshot)
                .where(
                    ScoreSnapshot.score_date == score_date,
                    ScoreSnapshot.score_version == score_version,
                )
                .order_by(ScoreSnapshot.company_id)
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
        rank_eligible_only: bool = True,
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
            rank_eligible_only: Drop snapshots that may not appear in a *current*
                ranking — under V1.2, those built on stale fundamentals. On by
                default because that is what every ranking view wants and
                forgetting it would silently readmit them. Pass False to read the
                complete set, which is what a score-change comparison needs: a
                company that went stale did not have its history deleted.
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

        if rank_eligible_only:
            # NULL is CURRENT. Every row written before V1.2 has one, and those
            # rows were ranked under rules where freshness did not bear on
            # ranking — reading them as stale now would rewrite history.
            statement = statement.where(
                or_(
                    ScoreSnapshot.freshness.is_(None),
                    ScoreSnapshot.freshness != Freshness.STALE.value,
                )
            )

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
        "freshness": record.freshness.value,
    }


#: Statuses a capture run passes through. Only COMPLETE may be read as a day's
#: record; the other two exist so an operator can see what happened.
RUN_RUNNING = "RUNNING"
RUN_COMPLETE = "COMPLETE"
RUN_FAILED = "FAILED"

#: How a universe row came to be written. BASELINE is the state as first seen
#: and dates nothing; OBSERVED is a transition the system actually watched.
OBSERVATION_BASELINE = "BASELINE"
OBSERVATION_OBSERVED = "OBSERVED"

#: Universe transitions.
EVENT_ADDED = "ADDED"
EVENT_ACTIVE = "ACTIVE"
EVENT_DELISTED = "DELISTED"
EVENT_REACTIVATED = "REACTIVATED"


class ImmutableSnapshotError(Exception):
    """A point-in-time snapshot already exists for this company, date and version.

    Raised rather than silently updating. The whole value of these tables is
    that a row records what was believed on a date; overwriting one would
    quietly replace history with hindsight, which is the failure the tables
    exist to prevent.
    """


class PitCaptureRunRepository:
    """Reads and writes `pit_capture_runs`.

    The run is what makes a day's capture atomic and idempotent. `start` opens
    one, `complete` closes it, and nothing reads a day whose run never reached
    COMPLETE — so a crash halfway through leaves a day that is visibly absent
    rather than one that is quietly partial.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def start(
        self, snapshot_date: date, *, score_version: str, feature_version: str
    ) -> PitCaptureRun:
        """Open a run for one day.

        Args:
            snapshot_date: The day being captured.
            score_version: The scoring rules in force.
            feature_version: The feature definitions in force.

        Returns:
            The open run, already flushed so its id can be used as a foreign key.
        """
        run = PitCaptureRun(
            snapshot_date=snapshot_date,
            status=RUN_RUNNING,
            score_version=score_version,
            feature_version=feature_version,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def complete(
        self,
        run: PitCaptureRun,
        *,
        scores: int,
        features: int,
        universe_events: int,
    ) -> None:
        """Mark a run complete and record what it wrote."""
        run.status = RUN_COMPLETE
        run.completed_at = datetime.now(UTC)
        run.scores_captured = scores
        run.features_captured = features
        run.universe_events_recorded = universe_events
        self._session.flush()

    def fail(
        self, snapshot_date: date, *, detail: str, score_version: str, feature_version: str
    ) -> None:
        """Record that a capture for this day failed.

        Written in its own transaction after the data transaction has been
        rolled back, so the failure is visible and no snapshot survives it.
        """
        self._session.add(
            PitCaptureRun(
                snapshot_date=snapshot_date,
                status=RUN_FAILED,
                score_version=score_version,
                feature_version=feature_version,
                completed_at=datetime.now(UTC),
                detail=detail[:2000],
            )
        )
        self._session.flush()

    def completed_run(self, snapshot_date: date) -> PitCaptureRun | None:
        """Return the completed run for a day, or None if there is not one."""
        return self._session.scalars(
            select(PitCaptureRun)
            .where(
                PitCaptureRun.snapshot_date == snapshot_date,
                PitCaptureRun.status == RUN_COMPLETE,
            )
            .order_by(PitCaptureRun.id.desc())
            .limit(1)
        ).first()

    def latest(self) -> PitCaptureRun | None:
        """Return the most recent completed run, or None."""
        return self._session.scalars(
            select(PitCaptureRun)
            .where(PitCaptureRun.status == RUN_COMPLETE)
            .order_by(PitCaptureRun.snapshot_date.desc(), PitCaptureRun.id.desc())
            .limit(1)
        ).first()

    def count(self) -> int:
        """Return how many runs are stored."""
        return self._session.scalar(select(func.count()).select_from(PitCaptureRun)) or 0


class PitScoreSnapshotRepository:
    """Reads and writes `pit_score_snapshots`.

    There is no update method and no upsert, deliberately. A second write for a
    company, date and version raises `ImmutableSnapshotError` rather than
    replacing the row: re-running a capture must be a no-op, not a revision.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert score snapshots, refusing to overwrite an existing one.

        Args:
            rows: Column mappings, each carrying at least `company_id`,
                `snapshot_date` and `score_version`.

        Returns:
            How many rows were inserted.

        Raises:
            ImmutableSnapshotError: If any row already exists.
        """
        if not rows:
            return 0

        snapshot_date = rows[0]["snapshot_date"]
        version = rows[0]["score_version"]
        existing = set(
            self._session.scalars(
                select(PitScoreSnapshot.company_id).where(
                    PitScoreSnapshot.snapshot_date == snapshot_date,
                    PitScoreSnapshot.score_version == version,
                    PitScoreSnapshot.company_id.in_([row["company_id"] for row in rows]),
                )
            )
        )
        if existing:
            raise ImmutableSnapshotError(
                f"{len(existing)} score snapshot(s) already stored for {snapshot_date} "
                f"under {version}; a point-in-time row is never rewritten"
            )

        for chunk in _chunks(list(rows), _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(PitScoreSnapshot), list(chunk))
        return len(rows)

    def for_date(self, snapshot_date: date, *, score_version: str) -> list[PitScoreSnapshot]:
        """Return every score snapshot captured on a day, best score first."""
        return list(
            self._session.scalars(
                select(PitScoreSnapshot)
                .where(
                    PitScoreSnapshot.snapshot_date == snapshot_date,
                    PitScoreSnapshot.score_version == score_version,
                )
                .order_by(
                    PitScoreSnapshot.final_score.desc().nullslast(),
                    PitScoreSnapshot.ticker,
                )
            )
        )

    def history(self, company_id: int, *, score_version: str) -> list[PitScoreSnapshot]:
        """Return one company's captured scores, oldest first."""
        return list(
            self._session.scalars(
                select(PitScoreSnapshot)
                .where(
                    PitScoreSnapshot.company_id == company_id,
                    PitScoreSnapshot.score_version == score_version,
                )
                .order_by(PitScoreSnapshot.snapshot_date)
            )
        )

    def count(self) -> int:
        """Return how many score snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(PitScoreSnapshot)) or 0


class PitFeatureSnapshotRepository:
    """Reads and writes `pit_feature_snapshots`. Insert-only, like its sibling."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert feature snapshots, refusing to overwrite an existing one.

        Args:
            rows: Column mappings, each carrying at least `company_id`,
                `snapshot_date` and `feature_version`.

        Returns:
            How many rows were inserted.

        Raises:
            ImmutableSnapshotError: If any row already exists.
        """
        if not rows:
            return 0

        snapshot_date = rows[0]["snapshot_date"]
        version = rows[0]["feature_version"]
        existing = set(
            self._session.scalars(
                select(PitFeatureSnapshot.company_id).where(
                    PitFeatureSnapshot.snapshot_date == snapshot_date,
                    PitFeatureSnapshot.feature_version == version,
                    PitFeatureSnapshot.company_id.in_([row["company_id"] for row in rows]),
                )
            )
        )
        if existing:
            raise ImmutableSnapshotError(
                f"{len(existing)} feature snapshot(s) already stored for {snapshot_date} "
                f"under {version}; a point-in-time row is never rewritten"
            )

        for chunk in _chunks(list(rows), _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(PitFeatureSnapshot), list(chunk))
        return len(rows)

    def for_date(self, snapshot_date: date, *, feature_version: str) -> list[PitFeatureSnapshot]:
        """Return every feature snapshot captured on a day, by ticker."""
        return list(
            self._session.scalars(
                select(PitFeatureSnapshot)
                .where(
                    PitFeatureSnapshot.snapshot_date == snapshot_date,
                    PitFeatureSnapshot.feature_version == feature_version,
                )
                .order_by(PitFeatureSnapshot.ticker)
            )
        )

    def latest_snapshot_date(self, *, feature_version: str) -> date | None:
        """Return the most recent day features were captured for, or None.

        What a derivation pass defaults to when no date is given: the newest day
        there is anything to read.
        """
        return self._session.scalar(
            select(func.max(PitFeatureSnapshot.snapshot_date)).where(
                PitFeatureSnapshot.feature_version == feature_version
            )
        )

    def history(self, company_id: int, *, feature_version: str) -> list[PitFeatureSnapshot]:
        """Return one company's captured features, oldest first."""
        return list(
            self._session.scalars(
                select(PitFeatureSnapshot)
                .where(
                    PitFeatureSnapshot.company_id == company_id,
                    PitFeatureSnapshot.feature_version == feature_version,
                )
                .order_by(PitFeatureSnapshot.snapshot_date)
            )
        )

    def count(self) -> int:
        """Return how many feature snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(PitFeatureSnapshot)) or 0


class FundamentalObservationRepository:
    """Reads and writes `fundamental_observations`, the append-only filing record.

    Two things distinguish this from every other write path in the package.
    It never updates: a revision of a period appends a second row and the first
    survives. And it selects on `filed_date`, not `period_end`, so a caller
    reconstructing a past decision sees only what had actually been filed by
    then.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, company_id: int, observations: Sequence[Mapping[str, Any]]) -> int:
        """Append observations, skipping any already stored.

        Idempotent by fingerprint: re-reading the same filing writes nothing,
        while a genuine revision hashes differently and appends.

        Args:
            company_id: The owning company's primary key.
            observations: Column mappings carrying `observation_fingerprint`.

        Returns:
            How many rows were newly inserted.
        """
        if not observations:
            return 0

        fingerprints = [row["observation_fingerprint"] for row in observations]
        seen = set(
            self._session.scalars(
                select(FundamentalObservation.observation_fingerprint).where(
                    FundamentalObservation.company_id == company_id,
                    FundamentalObservation.observation_fingerprint.in_(fingerprints),
                )
            )
        )
        fresh = [
            {**row, "company_id": company_id}
            for row in observations
            if row["observation_fingerprint"] not in seen
        ]
        if not fresh:
            return 0

        for chunk in _chunks(fresh, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(FundamentalObservation), list(chunk))
        return len(fresh)

    def as_of(
        self, company_id: int, as_of: date, *, concepts: Sequence[str] = ()
    ) -> list[FundamentalObservation]:
        """Return what had been filed for each period by a date.

        The point-in-time primitive. For every `(concept, period_end)` it
        returns the observation with the latest `filed_date` that is on or
        before `as_of` — the figure a decision made that day would have seen.

        An observation with no `filed_date` is **excluded**. Its availability is
        unknown, and falling back to `period_end` would be exactly the
        look-ahead this method exists to prevent.

        Args:
            company_id: The owning company's primary key.
            as_of: The decision date. Nothing filed after it is visible.
            concepts: Restrict to these concepts. Empty returns all.

        Returns:
            One observation per concept and period, newest period first.
        """
        conditions = [
            FundamentalObservation.company_id == company_id,
            FundamentalObservation.filed_date.is_not(None),
            FundamentalObservation.filed_date <= as_of,
        ]
        if concepts:
            conditions.append(FundamentalObservation.concept.in_(list(concepts)))

        latest = (
            select(
                FundamentalObservation.concept.label("concept"),
                FundamentalObservation.period_end.label("period_end"),
                func.max(FundamentalObservation.filed_date).label("filed_date"),
            )
            .where(*conditions)
            .group_by(FundamentalObservation.concept, FundamentalObservation.period_end)
            .subquery()
        )
        rows = self._session.scalars(
            select(FundamentalObservation)
            .join(
                latest,
                and_(
                    FundamentalObservation.concept == latest.c.concept,
                    FundamentalObservation.period_end == latest.c.period_end,
                    FundamentalObservation.filed_date == latest.c.filed_date,
                ),
            )
            .where(FundamentalObservation.company_id == company_id)
            .order_by(
                FundamentalObservation.period_end.desc(),
                FundamentalObservation.concept,
                FundamentalObservation.id.desc(),
            )
        )
        # Two filings can share a filed date for one period — a 10-K and its
        # amendment, filed together. The newest row wins, and the pair is
        # collapsed here rather than in SQL so the rule is visible.
        chosen: dict[tuple[str, date], FundamentalObservation] = {}
        for row in rows:
            chosen.setdefault((row.concept, row.period_end), row)
        return list(chosen.values())

    def available_as_of(
        self,
        company_id: int,
        as_of: date,
        *,
        accepted_by: datetime | None = None,
    ) -> list[FundamentalObservation]:
        """Return every observation that had been filed by a date.

        The retrieval half of the point-in-time metric engine. Unlike `as_of`,
        this collapses nothing: every competing observation public by the cutoff
        comes back, and choosing between them is left to the normalisation that
        consumes them.

        That distinction is load-bearing. `as_of` groups by concept and period
        end, which is right for inspecting one figure's history but drops a
        second unit — a filer tagging revenue in both TWD and USD would lose
        one, and currency detection reads exactly that. Handing the whole
        available set to the provider's own normalisation avoids the question.

        An observation with no `filed_date` is excluded. Its availability is
        unknown, and treating `period_end` as a proxy is the look-ahead this
        table exists to prevent.

        **Two availability tests, and only the instant-level one is correct.**
        Passing `accepted_by` compares each filing's stored acceptance instant
        against the moment the decision was taken, which is the only test that
        can settle whether a document was public. Omitting it compares dates,
        which cannot: measured across XBRL-bearing filings, 45.7% of those
        accepted on their stated filing date were accepted after the 16:00 New
        York close, so a date comparison admits them to a close-of-day decision
        that could not have seen them.

        Where `accepted_by` is given but a filing has no stored acceptance time,
        the filing must predate `as_of` outright - treated as though it arrived
        after that day's close. Conservative by design: it can withhold a filing
        for a day and can never reveal one early.

        Args:
            company_id: The owning company's primary key.
            as_of: The decision date. Nothing filed after it is visible.
            accepted_by: The instant the decision was taken. Supply it for a
                correct availability test; omit it only for a deliberately
                approximate, date-level read.

        Returns:
            Every observation available at that point, oldest filing first.
        """
        # Normalised to UTC before it reaches the query. Stored timestamps are
        # written by `_utcnow()`, and SQLite records a datetime's own wall clock
        # with the offset discarded — so a cutoff expressed in New York time
        # would be compared as though it were UTC and land four or five hours
        # early. Converting here keeps every caller's timezone honest.
        cutoff = accepted_by.astimezone(UTC) if accepted_by is not None else None

        statement = select(FundamentalObservation).where(
            FundamentalObservation.company_id == company_id,
            FundamentalObservation.filed_date.is_not(None),
        )
        if cutoff is None:
            statement = statement.where(FundamentalObservation.filed_date <= as_of)
        else:
            statement = statement.outerjoin(
                FilingAcceptance,
                FilingAcceptance.accession == FundamentalObservation.accession,
            ).where(
                or_(
                    FilingAcceptance.accepted_at <= cutoff,
                    and_(
                        FilingAcceptance.accepted_at.is_(None),
                        FundamentalObservation.filed_date < as_of,
                    ),
                )
            )
        return list(
            self._session.scalars(
                statement.order_by(
                    FundamentalObservation.filed_date,
                    FundamentalObservation.id,
                )
            )
        )

    def unresolved_accessions(self, company_id: int) -> list[str]:
        """Return this company's accessions that have no acceptance time yet.

        What makes a re-run of the acceptance pass cheap: a company whose
        filings are all resolved is skipped without a request.
        """
        return list(
            self._session.scalars(
                select(FundamentalObservation.accession)
                .outerjoin(
                    FilingAcceptance,
                    FilingAcceptance.accession == FundamentalObservation.accession,
                )
                .where(
                    FundamentalObservation.company_id == company_id,
                    FundamentalObservation.accession.is_not(None),
                    FilingAcceptance.accession.is_(None),
                )
                .group_by(FundamentalObservation.accession)
            )
        )

    def revisions(
        self, company_id: int, period_end: date, concept: str
    ) -> list[FundamentalObservation]:
        """Return every stored observation of one concept for one period.

        Oldest filing first, so a reader can watch a figure being restated.
        """
        return list(
            self._session.scalars(
                select(FundamentalObservation)
                .where(
                    FundamentalObservation.company_id == company_id,
                    FundamentalObservation.period_end == period_end,
                    FundamentalObservation.concept == concept,
                )
                .order_by(
                    FundamentalObservation.filed_date.nullsfirst(),
                    FundamentalObservation.id,
                )
            )
        )

    def count(self) -> int:
        """Return how many observations are stored."""
        return self._session.scalar(select(func.count()).select_from(FundamentalObservation)) or 0


class UniverseEventRepository:
    """Reads and writes `universe_events`, the append-only membership record."""

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, events: Sequence[Mapping[str, Any]]) -> int:
        """Append events, skipping any already stored.

        Args:
            events: Column mappings carrying `company_id` and
                `event_fingerprint`.

        Returns:
            How many rows were newly inserted.
        """
        if not events:
            return 0

        pairs = {(row["company_id"], row["event_fingerprint"]) for row in events}
        seen = {
            (company_id, fingerprint)
            for company_id, fingerprint in self._session.execute(
                select(UniverseEvent.company_id, UniverseEvent.event_fingerprint).where(
                    UniverseEvent.company_id.in_({company for company, _ in pairs}),
                    UniverseEvent.event_fingerprint.in_({key for _, key in pairs}),
                )
            )
        }
        fresh = [row for row in events if (row["company_id"], row["event_fingerprint"]) not in seen]
        if not fresh:
            return 0

        for chunk in _chunks(fresh, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(UniverseEvent), list(chunk))
        return len(fresh)

    def latest_state(self, company_ids: Sequence[int]) -> dict[int, UniverseEvent]:
        """Return each company's most recent membership event.

        What a capture compares against to decide whether anything changed. A
        company absent from the result has never been recorded, and its first
        row will be a BASELINE.
        """
        found: dict[int, UniverseEvent] = {}
        for chunk in _chunks(list(company_ids), _INSERT_CHUNK_ROWS):
            rows = self._session.scalars(
                select(UniverseEvent)
                .where(UniverseEvent.company_id.in_(list(chunk)))
                .order_by(UniverseEvent.company_id, UniverseEvent.id)
            )
            for row in rows:
                found[row.company_id] = row
        return found

    def history(self, company_id: int) -> list[UniverseEvent]:
        """Return one company's membership events, oldest first."""
        return list(
            self._session.scalars(
                select(UniverseEvent)
                .where(UniverseEvent.company_id == company_id)
                .order_by(UniverseEvent.effective_date, UniverseEvent.id)
            )
        )

    def count(self) -> int:
        """Return how many events are stored."""
        return self._session.scalar(select(func.count()).select_from(UniverseEvent)) or 0


class CorporateActionRepository:
    """Reads and writes `corporate_actions`, the append-only action record.

    No ingestion writes to this table yet. It exists so that when a source is
    wired up the shape is already settled, and so that the absence of factors is
    an empty table rather than an unasked question. Nothing here derives an
    action from a price movement, and nothing ever should.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, actions: Sequence[Mapping[str, Any]]) -> int:
        """Append actions, skipping any already stored.

        Args:
            actions: Column mappings carrying `company_id` and
                `action_fingerprint`.

        Returns:
            How many rows were newly inserted.
        """
        if not actions:
            return 0

        pairs = {(row["company_id"], row["action_fingerprint"]) for row in actions}
        seen = {
            (company_id, fingerprint)
            for company_id, fingerprint in self._session.execute(
                select(CorporateAction.company_id, CorporateAction.action_fingerprint).where(
                    CorporateAction.company_id.in_({company for company, _ in pairs}),
                    CorporateAction.action_fingerprint.in_({key for _, key in pairs}),
                )
            )
        }
        # Deduplicated against the argument as well as the table. A caller
        # batching a hundred symbols has no cheap way to know it was handed the
        # same action twice, and an integrity error mid-batch would lose every
        # row around it — including the ones that were fine.
        fresh: list[Mapping[str, Any]] = []
        for row in actions:
            key = (row["company_id"], row["action_fingerprint"])
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if not fresh:
            return 0

        for chunk in _chunks(fresh, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(CorporateAction), list(chunk))
        return len(fresh)

    def for_company(self, company_id: int, *, until: date | None = None) -> list[CorporateAction]:
        """Return one company's actions, oldest ex-date first.

        Args:
            company_id: The owning company's primary key.
            until: Only actions with an ex-date on or before this day, so a
                caller rebuilding a past price series does not apply a factor
                that had not happened yet.

        Returns:
            The stored actions.
        """
        conditions = [CorporateAction.company_id == company_id]
        if until is not None:
            conditions.append(CorporateAction.ex_date <= until)
        return list(
            self._session.scalars(
                select(CorporateAction)
                .where(*conditions)
                .order_by(CorporateAction.ex_date, CorporateAction.id)
            )
        )

    def count(self) -> int:
        """Return how many actions are stored."""
        return self._session.scalar(select(func.count()).select_from(CorporateAction)) or 0


class DelistingRepository:
    """Reads and writes `delistings`, the record of securities that ceased.

    Insert-only and identified by the source's own id, so a re-run of a window
    already discovered stores nothing.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Append delistings, skipping any already stored.

        Args:
            rows: Column mappings carrying `source` and `source_id`.

        Returns:
            How many rows were newly inserted.
        """
        if not rows:
            return 0

        seen = {
            (source, source_id)
            for source, source_id in self._session.execute(
                select(Delisting.source, Delisting.source_id).where(
                    Delisting.source.in_({row["source"] for row in rows})
                )
            )
        }
        fresh: list[Mapping[str, Any]] = []
        for row in rows:
            key = (row["source"], row["source_id"])
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if not fresh:
            return 0

        for chunk in _chunks(fresh, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(Delisting), list(chunk))
        return len(fresh)

    def by_ticker(self, tickers: Sequence[str]) -> dict[str, Delisting]:
        """Return the earliest delisting for each of these tickers.

        Earliest, because a ticker can be reused: 94 of the provider's inactive
        symbols are also live tickers belonging to different companies today.
        A caller resolving what happened to a security it held in 2022 wants the
        event that ended *that* security, not one recorded for its namesake.

        Args:
            tickers: Symbols to look up.

        Returns:
            One row per ticker that has one.
        """
        if not tickers:
            return {}
        found: dict[str, Delisting] = {}
        rows = self._session.scalars(
            select(Delisting)
            .where(Delisting.ticker.in_({ticker.upper() for ticker in tickers}))
            .order_by(Delisting.effective_date)
        )
        for row in rows:
            found.setdefault(row.ticker, row)
        return found

    def count(self) -> int:
        """Return how many delistings are stored."""
        return self._session.scalar(select(func.count()).select_from(Delisting)) or 0


class ForwardReturnRepository:
    """Reads and writes `forward_returns`. Insert-only, like its PIT siblings.

    A horizon already measured for a date is refused rather than replaced. There
    is no update path and no upsert, because a realised return is arithmetic
    over two published prices: a second run producing a different number would
    mean an input changed underneath, which is exactly the event worth failing
    on rather than absorbing.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Append measurements, skipping any horizon already recorded.

        Args:
            rows: Column mappings carrying `company_id`, `observation_date` and
                `horizon_sessions`.

        Returns:
            How many rows were newly inserted.
        """
        if not rows:
            return 0

        companies = {row["company_id"] for row in rows}
        dates = {row["observation_date"] for row in rows}
        seen = {
            (company_id, observation_date, horizon)
            for company_id, observation_date, horizon in self._session.execute(
                select(
                    ForwardReturnRecord.company_id,
                    ForwardReturnRecord.observation_date,
                    ForwardReturnRecord.horizon_sessions,
                ).where(
                    ForwardReturnRecord.company_id.in_(companies),
                    ForwardReturnRecord.observation_date.in_(dates),
                )
            )
        }
        fresh: list[Mapping[str, Any]] = []
        for row in rows:
            key = (row["company_id"], row["observation_date"], row["horizon_sessions"])
            if key in seen:
                continue
            seen.add(key)
            fresh.append(row)
        if not fresh:
            return 0

        for chunk in _chunks(fresh, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(ForwardReturnRecord), list(chunk))
        return len(fresh)

    def measured_horizons(self, observation_date: date) -> set[tuple[int, int]]:
        """Return the `(company_id, horizon)` pairs already measured for a date.

        Lets a re-run skip work rather than compute it and discard it, which
        matters when a horizon is measured over five thousand companies.

        Args:
            observation_date: The date to look up.

        Returns:
            The pairs already stored.
        """
        return {
            (company_id, horizon)
            for company_id, horizon in self._session.execute(
                select(ForwardReturnRecord.company_id, ForwardReturnRecord.horizon_sessions).where(
                    ForwardReturnRecord.observation_date == observation_date
                )
            )
        }

    def count(self) -> int:
        """Return how many measurements are stored."""
        return self._session.scalar(select(func.count()).select_from(ForwardReturnRecord)) or 0


class PitSignalSnapshotRepository:
    """Reads and writes `pit_signal_snapshots`. Insert-only, like its siblings.

    A day already derived under a version is refused rather than replaced. That
    is what lets a reader six months from now trust that a stored signal is what
    the system said at the time and not what today's rules would say about that
    day.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Insert signal snapshots, refusing to overwrite an existing one.

        Args:
            rows: Column mappings, each carrying at least `company_id`,
                `snapshot_date` and `signal_version`.

        Returns:
            How many rows were inserted.

        Raises:
            ImmutableSnapshotError: If any row already exists for that company,
                date and version.
        """
        if not rows:
            return 0

        snapshot_date = rows[0]["snapshot_date"]
        version = rows[0]["signal_version"]
        existing = set(
            self._session.scalars(
                select(PitSignalSnapshot.company_id).where(
                    PitSignalSnapshot.snapshot_date == snapshot_date,
                    PitSignalSnapshot.signal_version == version,
                    PitSignalSnapshot.company_id.in_([row["company_id"] for row in rows]),
                )
            )
        )
        if existing:
            raise ImmutableSnapshotError(
                f"{len(existing)} signal snapshot(s) already stored for {snapshot_date} "
                f"under {version}; a point-in-time row is never rewritten"
            )

        for chunk in _chunks(list(rows), _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(PitSignalSnapshot), list(chunk))
        return len(rows)

    def for_date(self, snapshot_date: date, *, signal_version: str) -> list[PitSignalSnapshot]:
        """Return every signal derived for a day, by ticker.

        Deliberately ordered by ticker rather than by any signal value: this
        table holds no ranking, and returning it sorted by, say, a percentile
        would invite a caller to read the first row as the best one.
        """
        return list(
            self._session.scalars(
                select(PitSignalSnapshot)
                .where(
                    PitSignalSnapshot.snapshot_date == snapshot_date,
                    PitSignalSnapshot.signal_version == signal_version,
                )
                .order_by(PitSignalSnapshot.ticker)
            )
        )

    def history(self, company_id: int, *, signal_version: str) -> list[PitSignalSnapshot]:
        """Return one company's derived signals, oldest first."""
        return list(
            self._session.scalars(
                select(PitSignalSnapshot)
                .where(
                    PitSignalSnapshot.company_id == company_id,
                    PitSignalSnapshot.signal_version == signal_version,
                )
                .order_by(PitSignalSnapshot.snapshot_date)
            )
        )

    def versions_for_date(self, snapshot_date: date) -> list[str]:
        """Return every signal version derived for a day, oldest row first.

        More than one is normal and healthy: re-deriving under new rules appends
        a second version beside the first.
        """
        return list(
            self._session.scalars(
                select(PitSignalSnapshot.signal_version)
                .where(PitSignalSnapshot.snapshot_date == snapshot_date)
                .group_by(PitSignalSnapshot.signal_version)
                .order_by(func.min(PitSignalSnapshot.id))
            )
        )

    def count(self) -> int:
        """Return how many signal snapshots are stored."""
        return self._session.scalar(select(func.count()).select_from(PitSignalSnapshot)) or 0


def _as_utc(moment: datetime) -> datetime:
    """Return an instant in UTC, so stored and compared values agree.

    SQLite writes a datetime's own wall clock and discards the offset, so an
    instant handed over in another zone would be stored as that zone's clock
    reading and compare wrongly against everything written by `_utcnow()`.
    """
    return moment.astimezone(UTC) if moment.tzinfo is not None else moment


class FilingAcceptanceRepository:
    """Reads and writes `filing_acceptance`, the append-only acceptance record.

    No update path. An acceptance time is a fact about when a document became
    public, and the SEC does not revise it.
    """

    def __init__(self, session: Session) -> None:
        """Bind the repository to a session."""
        self._session = session

    def add_all(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Append acceptance times, skipping accessions already stored.

        Args:
            rows: Column mappings carrying `accession`.

        Returns:
            How many rows were newly inserted.
        """
        if not rows:
            return 0

        wanted = {row["accession"] for row in rows}
        seen: set[str] = set()
        for chunk in _chunks(sorted(wanted), _INSERT_CHUNK_ROWS):
            seen.update(
                self._session.scalars(
                    select(FilingAcceptance.accession).where(
                        FilingAcceptance.accession.in_(list(chunk))
                    )
                )
            )
        fresh = [row for row in rows if row["accession"] not in seen]
        if not fresh:
            return 0

        normalised = [{**row, "accepted_at": _as_utc(row["accepted_at"])} for row in fresh]
        for chunk in _chunks(normalised, _INSERT_CHUNK_ROWS):
            self._session.execute(sa_insert(FilingAcceptance), list(chunk))
        return len(fresh)

    def known_accessions(self, accessions: Sequence[str]) -> set[str]:
        """Return which of these accessions already have an acceptance time."""
        found: set[str] = set()
        for chunk in _chunks(list(accessions), _INSERT_CHUNK_ROWS):
            found.update(
                self._session.scalars(
                    select(FilingAcceptance.accession).where(
                        FilingAcceptance.accession.in_(list(chunk))
                    )
                )
            )
        return found

    def count(self) -> int:
        """Return how many acceptance times are stored."""
        return self._session.scalar(select(func.count()).select_from(FilingAcceptance)) or 0
