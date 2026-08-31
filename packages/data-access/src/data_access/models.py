"""SQLAlchemy tables for companies, fundamentals, prices, scores and research.

Three tables carry Phase 1, two more carry Phase 2, one carries Phase 3's
research reports, and one carries Phase 6's deep research reports. Their unique
constraints
are load-bearing: they are what makes re-running the daily job idempotent, and
they are what the upsert helpers in `repositories` target. Removing one would not
fail a test immediately — it would slowly fill the database with duplicate
quarters that quietly double a trailing-twelve-month figure.

`deep_research_reports` is the one deliberate exception. It has no unique
constraint and appends rather than upserting, because a deep report is a dated
investigation and its history is the point. See the class for why.

Scores live in their own table rather than as columns on `financial_snapshots`.
That table holds reported facts; a score is an opinion derived from them under a
particular set of rules. Mixed together, a restatement and a re-score would be
indistinguishable, and the daily score history that makes "improving fast"
possible would have nowhere to live.

Money is stored as `Float` rather than `Numeric`. Everything downstream is a
ratio or a ranking, where double precision is ample, and floats avoid `Decimal`
conversions on every read. This would be the wrong call for a ledger; it is the
right one for a screener.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base for every table in this package.

    Alembic's `env.py` reads `Base.metadata` to autogenerate migrations, so a
    model that does not inherit from this is invisible to migrations.
    """


class Company(Base):
    """One listed security.

    Ticker is unique. The Phase 1 specification is explicit that historical
    ticker reassignment — the machinery an institutional platform needs to know
    that `FB` and `META` are the same company — is out of scope.
    """

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticker: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(50))
    sector: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(160))
    market_cap: Mapped[float | None] = mapped_column(Float)
    # Consolidated average daily share volume, when a provider supplies one.
    # The liquidity threshold is calibrated against this, not against volume
    # derived from a single-exchange price feed.
    average_volume: Mapped[float | None] = mapped_column(Float)
    # The same measure taken from the consolidated tape over a window this
    # project chose, rather than one a vendor declines to publish. Preferred
    # where both exist; `volume_source` records which feed produced it.
    consolidated_avg_volume: Mapped[float | None] = mapped_column(Float)
    volume_source: Mapped[str | None] = mapped_column(String(20))
    # Which accounting model the filing follows, classified from the concepts it
    # tags. NULL reads as GENERAL. This is the only durable record of statement
    # shape: the concept names are discarded after ingestion, and by scoring time
    # a bank's normalised figures look exactly like a shop's.
    statement_profile: Mapped[str | None] = mapped_column(String(30))
    # ISO code the company states its *financial statements* in, read from the
    # filing. NULL means nobody said, which is treated as USD.
    reporting_currency: Mapped[str | None] = mapped_column(String(3))
    # ISO code the *listed security* trades in, and therefore what `market_cap`
    # and every price are quoted in. Kept apart from the column above because
    # the two were one field and their two sources disagreed about its meaning:
    # EDGAR wrote the filing's currency, a market-data vendor wrote the trading
    # currency, and whichever arrived last won.
    quote_currency: Mapped[str | None] = mapped_column(String(3))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    snapshots: Mapped[list[FinancialSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    prices: Mapped[list[PriceHistory]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    scores: Mapped[list[ScoreSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    watchlist_entry: Mapped[WatchlistEntry | None] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    filing_excerpts: Mapped[list[FilingExcerptRecord]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    filings: Mapped[list[FilingRecord]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    research_reports: Mapped[list[StoredResearchReport]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    deep_research_reports: Mapped[list[StoredDeepResearchReport]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    pit_scores: Mapped[list[PitScoreSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    pit_features: Mapped[list[PitFeatureSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    pit_signals: Mapped[list[PitSignalSnapshot]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    fundamental_observations: Mapped[list[FundamentalObservation]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    universe_events: Mapped[list[UniverseEvent]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    corporate_actions: Mapped[list[CorporateAction]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    forward_returns: Mapped[list[ForwardReturnRecord]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )


class FinancialSnapshot(Base):
    """One reporting period of normalised fundamentals for one company.

    Every financial column is nullable on purpose. A provider that did not report
    operating cash flow leaves NULL, which reads back as `None` and propagates as
    a missing metric. A `NOT NULL DEFAULT 0` here would silently convert "not
    reported" into "reported as zero" at the storage layer, defeating the care
    taken everywhere else.
    """

    __tablename__ = "financial_snapshots"
    __table_args__ = (
        UniqueConstraint("company_id", "period_end", name="uq_financial_snapshot_period"),
        Index("ix_financial_snapshots_company_period", "company_id", "period_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # First day of the period, and how long it covers. A period is whatever the
    # company reported — a quarter, a half-year or a full year — and the cadence
    # is what stops a year of revenue being read as a quarter of it.
    period_start: Mapped[date | None] = mapped_column(Date)
    cadence: Mapped[str] = mapped_column(String(12), nullable=False, default="UNKNOWN")

    revenue: Mapped[float | None] = mapped_column(Float)
    gross_profit: Mapped[float | None] = mapped_column(Float)
    # Which concept produced the gross profit: reported directly, or the
    # cost concept it was derived from. A cost basis excluding depreciation
    # yields a higher margin than one including it.
    gross_profit_basis: Mapped[str | None] = mapped_column(String(80))
    operating_income: Mapped[float | None] = mapped_column(Float)
    operating_cash_flow: Mapped[float | None] = mapped_column(Float)
    capital_expenditure: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    # Weighted-average diluted shares, for dilution.
    shares_outstanding: Mapped[float | None] = mapped_column(Float)
    # Cover-page shares outstanding at a point in time, for market cap. The two
    # are different concepts and must not be substituted for one another.
    common_shares_outstanding: Mapped[float | None] = mapped_column(Float)
    reported_currency: Mapped[str | None] = mapped_column(String(3))

    source: Mapped[str] = mapped_column(String(50), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="snapshots")


class PriceHistory(Base):
    """One daily OHLCV bar for one company."""

    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("company_id", "date", name="uq_price_history_session"),
        Index("ix_price_history_company_date", "company_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    company: Mapped[Company] = relationship(back_populates="prices")


class BenchmarkPrice(Base):
    """One daily bar for a broad-market benchmark, e.g. SPY.

    Deliberately not a row in `companies` with its history in `price_history`.
    The benchmark is an ETF, and putting it in the company universe would mean
    every scan had to remember to exclude it — the kind of filter that gets
    forgotten once and puts an index fund in a list of research candidates.
    """

    __tablename__ = "benchmark_prices"
    __table_args__ = (
        UniqueConstraint("symbol", "date", name="uq_benchmark_price_session"),
        Index("ix_benchmark_prices_symbol_date", "symbol", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)

    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)


class FxRate(Base):
    """One exchange rate observation, kept so a score stays reproducible.

    Rates are stored rather than fetched-and-forgotten for the same reason
    scores are: a valuation computed from a rate nobody wrote down cannot be
    checked afterwards, and re-deriving it later would use a different rate and
    quietly produce a different answer to the same question.

    The unique constraint spans the provider as well as the pair and the date,
    because two sources publishing the same pair on the same day are two
    observations rather than a conflict — the ECB's official fixing and a
    broader dataset's figure differ by a few tenths of a percent, and the row
    should say which one a score used rather than averaging them into a number
    neither published.
    """

    __tablename__ = "fx_rates"
    __table_args__ = (
        UniqueConstraint(
            "base_currency",
            "quote_currency",
            "rate_date",
            "provider",
            name="uq_fx_rate_observation",
        ),
        Index("ix_fx_rates_pair_date", "base_currency", "quote_currency", "rate_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # The date the rate is *for*, which on a weekend is the preceding business
    # day rather than the date it was asked for.
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[float] = mapped_column(Float, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=_utcnow, server_default=func.current_timestamp()
    )


class ScoreSnapshot(Base):
    """One company's CompounderScore on one day under one set of rules.

    The unique constraint spans `(company_id, score_date, score_version)`, which
    is what makes re-running the scoring command idempotent and what keeps a
    score computed under v1 from being overwritten by one computed under v2.
    Comparing across versions would measure the formula rather than the
    business, so history is kept per version and read per version.

    Every score column is nullable because not every company gets a number. A
    bank, an ineligible security and a company with two quarters of history all
    have a row here — carrying the status that says why there is no score, which
    is the only way to answer "why is X not in the ranking" without re-running
    anything.
    """

    __tablename__ = "score_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "score_date", "score_version", name="uq_score_snapshot_daily"
        ),
        Index("ix_score_snapshots_company_date", "company_id", "score_date"),
        Index("ix_score_snapshots_ranking", "score_version", "score_date", "final_score"),
        # Serves `latest_score_date`, which asks for the newest *market-wide*
        # day. Without the coverage column in the index that lookup scans every
        # snapshot ever written.
        Index("ix_score_snapshots_coverage_date", "score_version", "coverage", "score_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    score_date: Mapped[date] = mapped_column(Date, nullable=False)
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)
    # Whether the run that wrote this row covered the market or one company.
    # Ranking views read the newest MARKET day, so a per-ticker run — which
    # scores one company on whatever date its data reaches — cannot become
    # "the latest ranking" and reduce the dashboard to a single row.
    coverage: Mapped[str] = mapped_column(String(10), nullable=False, default="MARKET")
    calculated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    scoring_status: Mapped[str] = mapped_column(String(30), nullable=False)

    growth_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    valuation_score: Mapped[float | None] = mapped_column(Float)
    momentum_score: Mapped[float | None] = mapped_column(Float)
    raw_score: Mapped[float | None] = mapped_column(Float)
    risk_penalty: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)

    risk_level: Mapped[str | None] = mapped_column(String(20))
    score_category: Mapped[str | None] = mapped_column(String(40))
    valuation_basis: Mapped[str | None] = mapped_column(String(20))
    data_coverage: Mapped[float | None] = mapped_column(Float)
    risk_coverage: Mapped[float | None] = mapped_column(Float)

    # Whether this row's inputs have been through candidate enrichment, and
    # where its market capitalisation came from. A preliminary row scored on a
    # calculated market cap and unverified liquidity is a different claim from
    # one scored on a provider's figures, and a ranking that hid the difference
    # would present both as equally checked.
    ranking_state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="PRELIMINARY", server_default="PRELIMINARY"
    )
    market_cap_source: Mapped[str | None] = mapped_column(String(20))
    # What the liquidity figure behind this score represented. A ranking row
    # showing single-exchange volume must not be read as a verified one.
    volume_basis: Mapped[str | None] = mapped_column(String(20))

    # Every eligibility check the security failed, pipe-separated, or None where
    # it passed. `scoring_status` says a company was not scored; this says why,
    # and the difference is the whole distance between a screen full of dashes
    # and a screen that explains itself. A company reporting in TWD, a fund, a
    # delisted shell and a stock that trades too thinly all arrive as
    # `NOT_ELIGIBLE` and are four very different things.
    exclusion_reasons: Mapped[str | None] = mapped_column(String(200))

    # Whether the fundamentals behind this score were current when it was
    # computed. Recorded rather than derived: the staleness bound depends on the
    # company's reporting cadence, so a reader comparing dates against a fixed
    # window would disagree with the screen that produced the row. V1.2 keeps
    # stale scores out of current rankings and nowhere else. NULL reads as
    # CURRENT, which is what every pre-V1.2 row meant.
    freshness: Mapped[str | None] = mapped_column(String(10))

    # The inputs a ranking displays beside the score. Copied here so a ranking
    # is one query rather than a re-scan, and so the row records the figures the
    # score was actually computed from even after the company restates them.
    market_cap: Mapped[float | None] = mapped_column(Float)
    revenue_growth_yoy: Mapped[float | None] = mapped_column(Float)
    revenue_growth_acceleration: Mapped[float | None] = mapped_column(Float)
    enterprise_value: Mapped[float | None] = mapped_column(Float)

    # The full explanation, as produced by `CompanyScore.model_dump`. One JSON
    # column rather than a table per component: the breakdown is read whole, by
    # a person or an API response, and never queried field by field.
    breakdown: Mapped[dict[str, object] | None] = mapped_column(JSON)

    company: Mapped[Company] = relationship(back_populates="scores")


class FilingRecord(Base):
    """One regulatory filing's index entry.

    Metadata only — no document text. The table exists so a research brief can
    cite a filing without any provider being called while the brief is being
    assembled, which is the property that keeps a research run working from what
    the nightly scan already fetched.

    Named `FilingRecord` rather than `Filing` because `domain.Filing` is the
    value object this row translates to, and a module holding both should not
    have to disambiguate them.

    Accession numbers are unique SEC-wide, but the constraint is scoped to the
    company as well: it is the pair that a re-run upserts on, and scoping it that
    way keeps a filer that appears twice under two tickers from silently
    colliding.
    """

    __tablename__ = "filings"
    __table_args__ = (
        UniqueConstraint("company_id", "accession", name="uq_filing_accession"),
        Index("ix_filings_company_filed", "company_id", "filed"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    accession: Mapped[str] = mapped_column(String(30), nullable=False)
    form: Mapped[str] = mapped_column(String(20), nullable=False)
    filed: Mapped[date] = mapped_column(Date, nullable=False)
    # The period the filing reports on, when it states one. An 8-K usually does
    # not, so NULL here is normal rather than missing data.
    period_end: Mapped[date | None] = mapped_column(Date)
    primary_document: Mapped[str | None] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    source: Mapped[str] = mapped_column(String(50), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="filings")


class WatchlistEntry(Base):
    """One company a person chose to keep an eye on.

    The whole feature. A ticker, when it was added, and an optional note — no
    target price, no position size, no folders. Those belong to a portfolio
    tool, and adding them here would turn a shortlist into an accounting system
    nobody asked for.

    One row per company, enforced by the database rather than by the caller, so
    adding a company twice is idempotent instead of a duplicate.
    """

    __tablename__ = "watchlist"
    __table_args__ = (UniqueConstraint("company_id", name="uq_watchlist_company"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(500))
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="watchlist_entry")


class JobRecord(Base):
    """One pipeline command started from outside the CLI.

    The dashboard runs commands by spawning them, and this is the record of
    that: what was asked for, whether it is still going, and where its output
    went. It stores no results — every command already persists what it
    produces, so a finished job is read through the endpoint that serves the
    thing it produced, never from here.

    `pid` is what makes a stale row recoverable. A run interrupted by an API
    restart leaves `RUNNING` behind with no process attached, and the pid is
    the only way to tell that from a run still in progress.

    Rows accumulate deliberately: "when did this last run, and did it work" is
    the question the history answers, and it cannot be answered by a table that
    keeps only what is in flight.
    """

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_status", "status"),
        Index("ix_jobs_recent", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    #: The ticker for a per-company job, None for one that runs over everything.
    target: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    pid: Mapped[int | None] = mapped_column(Integer)
    exit_code: Mapped[int | None] = mapped_column(Integer)
    log_path: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class FilingExcerptRecord(Base):
    """Verbatim text extracted from one section of one filing.

    A separate table from `filings`, not a column on it, because the two answer
    different questions and one filing yields several sections. The pair that
    identifies an excerpt is the accession and the section slug, scoped to the
    company — re-running extraction over a filing already read replaces its rows
    rather than accumulating copies of the same paragraphs.

    Nothing here is summarised. The text is what the filer wrote, cut to a bound,
    and the row exists so that a research claim about a business can cite
    something a person can go and read.
    """

    __tablename__ = "filing_excerpts"
    __table_args__ = (
        UniqueConstraint("company_id", "accession", "section", name="uq_filing_excerpt_section"),
        Index("ix_filing_excerpts_company_filed", "company_id", "filed"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    accession: Mapped[str] = mapped_column(String(30), nullable=False)
    form: Mapped[str] = mapped_column(String(20), nullable=False)
    # `business`, `risk_factors`, `mda`, or `item_2.02` for an 8-K item. Part of
    # the citation handle a report quotes, so it may not drift.
    section: Mapped[str] = mapped_column(String(40), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    filed: Mapped[date] = mapped_column(Date, nullable=False)
    url: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    source: Mapped[str] = mapped_column(String(50), nullable=False, default="unknown")
    # When the text was pulled, as distinct from when the filing was filed. A
    # re-extraction under a changed extractor moves this and not `filed`.
    extracted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="filing_excerpts")


class StoredResearchReport(Base):
    """One validated AI research report about one company.

    Named for what it is rather than for the contract it carries, because
    `research.ResearchReport` is the validated value object and this is the row
    it was written to. Code that touches both should never have to wonder which
    one it is holding.

    The unique constraint spans `(company_id, score_version, brief_fingerprint,
    prompt_version)`, and those four together are the cache key: the company, the
    scoring rules it was explained under, a hash of every piece of evidence
    supplied, and the prompt that turned that evidence into prose. Change any one
    and the report is a different reading of a different thing. Change none and
    regenerating it would spend a model call to produce what is already stored.

    The report is kept as JSON rather than as a column per section. It is read
    whole — by a person, or by an API response — and never queried section by
    section, which is exactly the case `score_snapshots.breakdown` already makes.
    `issues` is stored beside it, denormalised out of the same document, because
    "which reports had claims dropped" is a question worth answering without
    parsing every report in the table.
    """

    __tablename__ = "research_reports"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "score_version",
            "brief_fingerprint",
            "prompt_version",
            name="uq_research_report_cache_key",
        ),
        Index("ix_research_reports_company_generated", "company_id", "generated_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )

    # Which score the report explains. Carried so a report can be paired with
    # the exact snapshot it was written about, and so a report produced under one
    # formula version is never shown beside a score from another.
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)
    score_date: Mapped[date] = mapped_column(Date, nullable=False)

    # The evidence, the contract and the prompt that produced it.
    brief_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)

    # COMPLETE, PARTIAL or FAILED. A failed row records that a run tried and
    # could not, which is why it is stored rather than swallowed — and why the
    # cache treats it as a miss.
    status: Mapped[str] = mapped_column(String(20), nullable=False)

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    report: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    issues: Mapped[list[object] | None] = mapped_column(JSON)

    company: Mapped[Company] = relationship(back_populates="research_reports")


class StoredDeepResearchReport(Base):
    """One validated deep research report about one company.

    A separate table from `research_reports`, not a status column on it. The two
    hold different contracts — different sections, different bases, a different
    evidence namespace — and the moment they shared a table, every read would
    have to filter on which kind it was holding and every schema change to one
    would risk the other.

    **This table appends; it never overwrites.** `research_reports` carries a
    unique constraint on its cache key and upserts into it, which is right for a
    report explaining a stored snapshot: re-running the same evidence should not
    accumulate rows. A deep report is a dated investigation of a company at a
    moment, and the history of what was concluded and on what evidence is the
    interesting part. Two runs a month apart over identical evidence are two
    facts about what this system said, and both are kept.

    Caching is therefore "the newest row matching the key" rather than "the row",
    and the key is `(company_id, deterministic_fingerprint, evidence_fingerprint,
    prompt_version)` — the company, a hash of everything calculated or filed, a
    hash of that plus the external sources, and the prompt that turned it into
    prose. `ix_deep_research_reports_cache` serves that lookup.

    Two fingerprints rather than one, because there are two reasons to regenerate
    and they are worth telling apart: the fundamentals moved, or somebody
    published something. A caller can ask "is the deterministic half of this
    report still current?" without rehashing the news.

    `ticker` is denormalised alongside `company_id` deliberately, in the same
    spirit as `jobs.target`: a report is a record of what was said about a symbol
    on a date, and it stays readable after the company row is renamed.

    The report is kept as JSON rather than as a column per section — it is read
    whole, by a person or an API response, and never queried section by section.
    `status` and `confidence` are denormalised out of that document because
    "which reports came back thin" is worth answering without parsing every row.
    """

    __tablename__ = "deep_research_reports"
    __table_args__ = (
        Index(
            "ix_deep_research_reports_cache",
            "company_id",
            "deterministic_fingerprint",
            "evidence_fingerprint",
            "prompt_version",
        ),
        Index("ix_deep_research_reports_company_generated", "company_id", "generated_at"),
        Index(
            "ix_deep_research_reports_collection",
            "company_id",
            "deterministic_fingerprint",
            "external_collected_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)

    # The score date the report describes. Every deterministic figure in it
    # belongs to this date, and external evidence was read against it.
    as_of: Mapped[date] = mapped_column(Date, nullable=False)
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)

    # The two halves of the cache key. The first covers everything this system
    # calculated or the company filed; the second covers that plus the external
    # sources. A change in one and not the other says which kind of staleness
    # this is.
    deterministic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    contract_version: Mapped[str] = mapped_column(String(40), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)

    # COMPLETE, PARTIAL or FAILED. A failed row records that a run tried and
    # could not, which is why it is stored rather than swallowed.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # LOW, MEDIUM or HIGH, after validation lowered it. Denormalised so a thin
    # report can be found without opening the document.
    confidence: Mapped[str] = mapped_column(String(20), nullable=False)

    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    # The validated report, including the external sources its claims cite —
    # so a `W.` citation still resolves to a title, publisher, URL and date long
    # after the brief that produced it is gone. No draft is ever stored here:
    # only a validated report has this shape.
    validated_report_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    validation_issues_json: Mapped[list[object] | None] = mapped_column(JSON)

    # The external evidence this report was collected from, whole — not just the
    # sources its claims ended up citing. A rerun rebuilds its brief from this,
    # and a subset would produce a different fingerprint and miss the cache it
    # was trying to hit.
    #
    # `external_state` guards the reuse decision: FRESH is safe to reuse,
    # DEGRADED is a collection whose searches partly failed and whose thinness
    # must not be frozen in place for the length of the window.
    external_state: Mapped[str | None] = mapped_column(String(20))
    external_collected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    collected_external_json: Mapped[list[object] | None] = mapped_column(JSON)

    company: Mapped[Company] = relationship(back_populates="deep_research_reports")


class PitCaptureRun(Base):
    """One attempt to record what the system knew on one day.

    Phase 8B's snapshot tables answer "what did we know then". This table
    answers "and was that record complete", which is the question that decides
    whether the day may be used at all. A run reaching `COMPLETE` is a promise
    that every company the capture examined was written; a run that failed
    leaves a `FAILED` row and **no** snapshot rows, because the snapshots are
    written in the same transaction that marks the run complete.

    There is deliberately no unique constraint on `snapshot_date`. Attempts are
    history — a failed run followed by a successful one is two rows, and hiding
    the first would hide exactly the operational fact this table exists to
    record. Uniqueness that matters lives on the snapshot tables, one row per
    company per date per version.
    """

    __tablename__ = "pit_capture_runs"
    __table_args__ = (Index("ix_pit_capture_runs_date_status", "snapshot_date", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The trading day the capture describes. UTC calendar date, matching
    # `score_snapshots.score_date`; see the module docstring in
    # `stock_screener.pit` for why the two must agree.
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    # When the process ran. Distinct from `snapshot_date` on purpose: a capture
    # for Friday started at 00:05 on Saturday describes Friday, and conflating
    # the two is how a dataset silently acquires look-ahead.
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # RUNNING, COMPLETE or FAILED. Only COMPLETE may be read as a day's record.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_version: Mapped[str] = mapped_column(String(40), nullable=False)
    scores_captured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    features_captured: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    universe_events_recorded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Why a run failed, when it did. Free text for an operator, never parsed.
    detail: Mapped[str | None] = mapped_column(Text)

    scores: Mapped[list[PitScoreSnapshot]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    features: Mapped[list[PitFeatureSnapshot]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )
    signals: Mapped[list[PitSignalSnapshot]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )


class PitScoreSnapshot(Base):
    """What the scoring system believed about one company on one day.

    A copy, not a view. `score_snapshots` is the operational table and it
    upserts: re-running the score command replaces the day's rows with better
    information, which is right for a screener and fatal for a backtest. This
    table is written once and never updated, so a strategy calibrated six months
    from now reads the number that was actually on the screen, not the number
    today's data would produce for that date.

    `source_score_date` records which operational row was copied. It is normally
    equal to `snapshot_date`, and when it is not, the capture read an older
    score because the day's scoring had not run — a fact a reader must be able
    to see rather than infer.
    """

    __tablename__ = "pit_score_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "snapshot_date", "score_version", name="uq_pit_score_snapshot"
        ),
        Index("ix_pit_score_snapshots_date_version", "snapshot_date", "score_version"),
        Index("ix_pit_score_snapshots_company_date", "company_id", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pit_capture_runs.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # The symbol as it stood on the capture date. Denormalised deliberately: a
    # ticker is reassigned when a company delists, and a join through
    # `companies` months later would report today's owner of the symbol.
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    source_score_date: Mapped[date] = mapped_column(Date, nullable=False)
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)

    scoring_status: Mapped[str] = mapped_column(String(30), nullable=False)
    final_score: Mapped[float | None] = mapped_column(Float)
    raw_score: Mapped[float | None] = mapped_column(Float)
    growth_score: Mapped[float | None] = mapped_column(Float)
    quality_score: Mapped[float | None] = mapped_column(Float)
    valuation_score: Mapped[float | None] = mapped_column(Float)
    momentum_score: Mapped[float | None] = mapped_column(Float)
    risk_penalty: Mapped[float | None] = mapped_column(Float)
    risk_level: Mapped[str | None] = mapped_column(String(20))
    score_category: Mapped[str | None] = mapped_column(String(40))
    valuation_basis: Mapped[str | None] = mapped_column(String(20))
    data_coverage: Mapped[float | None] = mapped_column(Float)
    risk_coverage: Mapped[float | None] = mapped_column(Float)
    market_cap: Mapped[float | None] = mapped_column(Float)
    market_cap_source: Mapped[str | None] = mapped_column(String(20))
    volume_basis: Mapped[str | None] = mapped_column(String(20))
    revenue_growth_yoy: Mapped[float | None] = mapped_column(Float)
    revenue_growth_acceleration: Mapped[float | None] = mapped_column(Float)
    enterprise_value: Mapped[float | None] = mapped_column(Float)
    freshness: Mapped[str | None] = mapped_column(String(10))
    coverage: Mapped[str] = mapped_column(String(10), nullable=False)
    ranking_state: Mapped[str | None] = mapped_column(String(20))
    exclusion_reasons: Mapped[str | None] = mapped_column(String(200))
    # Whether this row was admissible to a *current* ranking on the day, under
    # the rules of its own score version. Recorded rather than derived, because
    # the rule is versioned and a later reader would apply a later rule.
    rank_eligible: Mapped[bool] = mapped_column(Boolean, nullable=False)

    run: Mapped[PitCaptureRun] = relationship(back_populates="scores")
    company: Mapped[Company] = relationship(back_populates="pit_scores")


class PitFeatureSnapshot(Base):
    """The market state observed for one company on one day.

    Structured features, never a score and never a ranking. Phase 8A established
    that fundamental attractiveness and tradability are close to orthogonal, and
    that averaging anti-correlated dimensions into one number produces something
    meaningless; so this table stores the dimensions and leaves the judgement to
    whatever later phase earns the right to make it.

    Windows are strict — a field named for 60 sessions used exactly 60 or is
    NULL — so two rows of this table are comparable. `bars_available` and
    `feature_as_of` record what the computation actually had: `feature_as_of` is
    the date of the newest bar used, which is earlier than `snapshot_date` for a
    security that has stopped trading.
    """

    __tablename__ = "pit_feature_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "snapshot_date", "feature_version", name="uq_pit_feature_snapshot"
        ),
        Index("ix_pit_feature_snapshots_date_version", "snapshot_date", "feature_version"),
        Index("ix_pit_feature_snapshots_company_date", "company_id", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pit_capture_runs.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    feature_version: Mapped[str] = mapped_column(String(40), nullable=False)
    # Date of the newest bar the features were computed from — the data
    # watermark. Equal to `snapshot_date` for an actively trading security.
    feature_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    bars_available: Mapped[int] = mapped_column(Integer, nullable=False)
    benchmark_symbol: Mapped[str | None] = mapped_column(String(20))

    last_close: Mapped[float | None] = mapped_column(Float)
    atr_pct_14: Mapped[float | None] = mapped_column(Float)
    atr_pct_20: Mapped[float | None] = mapped_column(Float)
    atr_pct_60: Mapped[float | None] = mapped_column(Float)
    rvol_5_pct: Mapped[float | None] = mapped_column(Float)
    rvol_20_pct: Mapped[float | None] = mapped_column(Float)
    rvol_60_pct: Mapped[float | None] = mapped_column(Float)
    rvol_252_pct: Mapped[float | None] = mapped_column(Float)
    vol_expansion_ratio: Mapped[float | None] = mapped_column(Float)
    avg_abs_ret_20_pct: Mapped[float | None] = mapped_column(Float)
    avg_abs_ret_60_pct: Mapped[float | None] = mapped_column(Float)
    avg_abs_ret_252_pct: Mapped[float | None] = mapped_column(Float)
    median_abs_ret_252_pct: Mapped[float | None] = mapped_column(Float)
    max_abs_ret_60_pct: Mapped[float | None] = mapped_column(Float)
    max_abs_ret_252_pct: Mapped[float | None] = mapped_column(Float)
    days_over_1pct_share: Mapped[float | None] = mapped_column(Float)
    days_over_2pct_share: Mapped[float | None] = mapped_column(Float)
    days_over_3pct_share: Mapped[float | None] = mapped_column(Float)
    median_range_pct: Mapped[float | None] = mapped_column(Float)
    range_over_1pct_share: Mapped[float | None] = mapped_column(Float)
    range_over_2pct_share: Mapped[float | None] = mapped_column(Float)
    # The two markers Phase 8A found actually identify an untradable security.
    # Neither is a volatility measure: a name can carry a high average true
    # range from overnight gaps while printing an unchanged bar most sessions.
    flat_bar_share: Mapped[float | None] = mapped_column(Float)
    zero_move_share: Mapped[float | None] = mapped_column(Float)
    closing_band_pct: Mapped[float | None] = mapped_column(Float)
    ret_5d_pct: Mapped[float | None] = mapped_column(Float)
    ret_20d_pct: Mapped[float | None] = mapped_column(Float)
    ret_60d_pct: Mapped[float | None] = mapped_column(Float)
    ret_252d_pct: Mapped[float | None] = mapped_column(Float)
    trend_r2: Mapped[float | None] = mapped_column(Float)
    trend_slope: Mapped[float | None] = mapped_column(Float)
    above_sma20_share: Mapped[float | None] = mapped_column(Float)
    high_52w: Mapped[float | None] = mapped_column(Float)
    low_52w: Mapped[float | None] = mapped_column(Float)
    distance_from_52w_high: Mapped[float | None] = mapped_column(Float)
    gap_over_1pct_share: Mapped[float | None] = mapped_column(Float)
    gap_over_2pct_share: Mapped[float | None] = mapped_column(Float)
    mean_abs_gap_pct: Mapped[float | None] = mapped_column(Float)
    max_abs_gap_pct: Mapped[float | None] = mapped_column(Float)
    # Named for the venue, not for liquidity. Stored bars come from a single
    # exchange and are a small, highly variable fraction of the consolidated
    # tape, so these are a floor on real turnover and never an estimate of it.
    venue_dollar_volume_median: Mapped[float | None] = mapped_column(Float)
    venue_dollar_volume_mean: Mapped[float | None] = mapped_column(Float)
    venue_volume_stability: Mapped[float | None] = mapped_column(Float)
    venue_volume_ratio: Mapped[float | None] = mapped_column(Float)
    beta_60: Mapped[float | None] = mapped_column(Float)
    correlation_60: Mapped[float | None] = mapped_column(Float)
    rel_strength_20_pct: Mapped[float | None] = mapped_column(Float)
    rel_strength_60_pct: Mapped[float | None] = mapped_column(Float)
    # A 40%+ single session. An integrity flag for a reader, **not** a corporate
    # action: this project never infers a split from a price movement.
    has_extreme_move: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[PitCaptureRun] = relationship(back_populates="features")
    company: Mapped[Company] = relationship(back_populates="pit_features")


class FundamentalObservation(Base):
    """One reported figure, as one filing stated it, kept forever.

    `financial_snapshots` holds the current normalised view and overwrites on
    `(company_id, period_end)`: a restatement replaces the original and the
    original is gone. That is correct for scoring — the newest figure is the
    best estimate of the truth — and it destroys the only thing a backtest
    needs, which is the figure that was on the wire at the time.

    So this table appends. A revision of the same period arrives as a second row
    with a later `filed_date`, and both survive. Selection is by
    `filed_date <= as_of`, never by `period_end`: a quarter ending in December
    that was filed in February was not knowable in January, and treating the
    period end as the availability date is the single most common way a
    backtest quietly reads the future.

    `observation_fingerprint` is what makes re-ingestion idempotent. It hashes
    the identity of the observation — period, concept, filing, value — so
    re-reading the same filing writes nothing, while a genuine revision hashes
    differently and appends. A unique constraint over the raw columns could not
    do this: `filed_date` and `accession` are nullable for sources that do not
    supply them, and SQL treats two NULLs as distinct, so repeated ingestion
    would accumulate duplicates.
    """

    __tablename__ = "fundamental_observations"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "observation_fingerprint", name="uq_fundamental_observation"
        ),
        Index("ix_fundamental_observations_pit", "company_id", "concept", "filed_date"),
        Index("ix_fundamental_observations_period", "company_id", "period_end", "filed_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # Hex SHA-256 over the observation's identity. See the class docstring.
    observation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # The XBRL namespace the concept was tagged in — `us-gaap`, `ifrs-full`,
    # `dei`. Kept because the same economic quantity has different names in each
    # and a reader must be able to tell which table produced a figure.
    taxonomy: Mapped[str] = mapped_column(String(40), nullable=False)
    concept: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    # The unit key the filer tagged, verbatim — `USD`, `TWD`, `shares`. The
    # filing's unit is authoritative; a vendor's assumption is not.
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL only where the source genuinely does not supply one. A NULL here is
    # not a licence to fall back on `period_end` — it means the observation
    # cannot participate in point-in-time selection at all.
    filed_date: Mapped[date | None] = mapped_column(Date)
    accession: Mapped[str | None] = mapped_column(String(30))
    form: Mapped[str | None] = mapped_column(String(20))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(10))
    frame: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="fundamental_observations")


class UniverseEvent(Base):
    """An observed change in whether a security is part of the investable set.

    Append-only, and forward-looking only. Phase 8A found the universe was
    created in a single day with no membership history, so any historical study
    over it is survivorship-biased. This table starts fixing that from the day
    it is first written — it does not, and must not, invent what happened
    before.

    `observation_kind` is the field that keeps this honest. `BASELINE` is the
    state as first seen, carrying the capture date and asserting nothing about
    when it became true. `OBSERVED` is a transition the system actually watched
    happen, and only those rows may be read as events with meaningful dates. A
    delisting inferred from a guessed date would be indistinguishable from one
    that was witnessed, which is why the distinction is a column rather than a
    convention.
    """

    __tablename__ = "universe_events"
    __table_args__ = (
        UniqueConstraint("company_id", "event_fingerprint", name="uq_universe_event"),
        Index("ix_universe_events_company_date", "company_id", "effective_date"),
        Index("ix_universe_events_date", "effective_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    event_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    # ADDED, ACTIVE, DELISTED or REACTIVATED.
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # BASELINE for state as first seen, OBSERVED for a witnessed transition.
    observation_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    # For an OBSERVED row, the date the change was seen. For a BASELINE row, the
    # date the state was first recorded — never a guess at when it began.
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exchange: Mapped[str | None] = mapped_column(String(50))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="universe_events")


class CorporateAction(Base):
    """A split or a distribution, as a source reported it.

    Stored prices are adjusted retroactively — the close recorded for a session
    last year is a function of every corporate action since — so the series as
    it appeared on the day cannot be rebuilt without the factors. This table is
    where they belong.

    It is deliberately empty until a source is wired to it. Phase 8A established
    that a split cannot be inferred from a price movement: an unadjusted 2-for-1
    and a genuine halving are the same bar, and a project that guessed would
    manufacture exactly the fake history this phase exists to prevent. An empty
    table that says "no source yet" is worth more than a populated one that says
    something untrue.
    """

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint("company_id", "action_fingerprint", name="uq_corporate_action"),
        Index("ix_corporate_actions_company_date", "company_id", "ex_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    action_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    # SPLIT, REVERSE_SPLIT, CASH_DIVIDEND, STOCK_DIVIDEND or SPINOFF.
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # The date the action takes effect in the price — the first session trading
    # without it. Never derived from where a price happens to jump.
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    # New shares per old share: 2.0 for a 2-for-1, 0.1 for a 1-for-10 reverse.
    # NULL for a cash distribution.
    split_factor: Mapped[float | None] = mapped_column(Float)
    # Cash per share, in `currency`. NULL for a split.
    cash_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    record_date: Mapped[date | None] = mapped_column(Date)
    payable_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    # The identifier the source itself uses, so a row can be traced back.
    source_id: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="corporate_actions")


class Delisting(Base):
    """A security's last day, and what a holder was left with.

    Keyed by ticker and **not** by `company_id`, which is the whole point of the
    table. Nearly every row here is a security the `companies` table has never
    contained: it holds what is investable now, so a study run over it cannot
    see a single company that was acquired or wound up, and measured over 4,599
    companies across 120 sessions of 2024 it saw exactly zero. That is not a
    quiet distortion, it is the survivorship bias in its pure form.

    Giving these rows company records instead would fix the join and break
    everything else — thousands of untradeable tickers would enter the table the
    scanner, the rankings and the dashboard all read as the investable set.

    Append-only, identified by the source's own id.
    """

    __tablename__ = "delistings"
    __table_args__ = (
        UniqueConstraint("source", "source_id", name="uq_delisting_source"),
        Index("ix_delistings_ticker", "ticker"),
        Index("ix_delistings_date", "effective_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The acquiree — the security that ceased. Never the acquirer, which
    # carries on and must not be recorded as having ended.
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    # CASH_MERGER, STOCK_MERGER, STOCK_AND_CASH_MERGER or WORTHLESS_REMOVAL.
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Cash received per share. NULL for a pure stock merger, and NULL rather
    # than 0.0 for a worthless removal, which reports its nothing through
    # `kind` — a stored zero would be indistinguishable from an unknown.
    cash_per_share: Mapped[float | None] = mapped_column(Float)
    acquirer_ticker: Mapped[str | None] = mapped_column(String(20))
    acquirer_rate: Mapped[float | None] = mapped_column(Float)
    acquiree_rate: Mapped[float | None] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class ForwardReturnRecord(Base):
    """What a security did after a point-in-time observation date.

    The other half of a research question. A snapshot says what was knowable;
    this says what happened next, and the two together are what any study of
    whether the score predicts anything has to join.

    Append-only and versionless, because it is a measurement rather than a
    policy: `1, 5, 20, 60, 120` sessions from an observation date is the same
    fact whoever computes it, and a re-run of a realised horizon reproduces the
    number exactly. The unique constraint is what enforces that — a horizon
    already measured for a date is refused, never replaced.

    **A row with no return is the point of the table, not a defect in it.**
    `status` distinguishes a horizon that has not elapsed from a security that
    stopped trading inside it, and dropping either kind would rebuild the
    survivorship bias the point-in-time record exists to remove.
    """

    __tablename__ = "forward_returns"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "observation_date",
            "horizon_sessions",
            name="uq_forward_return",
        ),
        Index("ix_forward_returns_date", "observation_date", "horizon_sessions"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    # The date the snapshot describes. The entry is the session *after* it.
    observation_date: Mapped[date] = mapped_column(Date, nullable=False)
    horizon_sessions: Mapped[int] = mapped_column(Integer, nullable=False)
    # REALISED, TRUNCATED, INTERRUPTED or NO_ENTRY.
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    entry_date: Mapped[date | None] = mapped_column(Date)
    entry_price: Mapped[float | None] = mapped_column(Float)
    exit_date: Mapped[date | None] = mapped_column(Date)
    exit_price: Mapped[float | None] = mapped_column(Float)
    # `exit / entry - 1` on the adjusted basis, so distributions and splits are
    # already in it. NULL unless `status` is REALISED — never zero, which would
    # claim the security went nowhere.
    total_return: Mapped[float | None] = mapped_column(Float)
    # Where an interrupted security was last priced. Evidence about what
    # happened, not a substitute for the exit that never came.
    last_seen_date: Mapped[date | None] = mapped_column(Date)
    last_seen_price: Mapped[float | None] = mapped_column(Float)
    measured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="forward_returns")


class PitSignalSnapshot(Base):
    """One security's market state on one date, under one set of signal rules.

    Derived from `pit_feature_snapshots` and stored rather than recomputed on
    read. The features are immutable, so a signal could in principle be
    rebuilt from them at any time — but only by running the code that built it,
    and that code changes. Storing the result decouples the record from the
    repository's history, exactly as `score_snapshots` is stored rather than
    recomputed from `financial_snapshots`. See ADR-0011.

    Immutable and unique on `(company_id, snapshot_date, signal_version)`. A
    later version of the rules run over the same day appends a second row
    beside the first; neither overwrites the other, so a change in definitions
    can never silently restate what the system said at the time.

    `run_id` points at the capture run whose features were read. It is the
    provenance link that makes a signal reproducible: the exact feature rows are
    identified, not merely the date.

    This table holds no ranking and no composite. Phase 8A established that the
    dimensions here are anti-correlated and that averaging them produces a
    number which looks meaningful and is not.
    """

    __tablename__ = "pit_signal_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "company_id", "snapshot_date", "signal_version", name="uq_pit_signal_snapshot"
        ),
        Index("ix_pit_signal_snapshots_date_version", "snapshot_date", "signal_version"),
        Index("ix_pit_signal_snapshots_company_date", "company_id", "snapshot_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("pit_capture_runs.id", ondelete="CASCADE"), nullable=False
    )
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    signal_version: Mapped[str] = mapped_column(String(40), nullable=False)
    # The feature definitions the signals were derived from. Two versions travel
    # together because a signal is only meaningful against the features it read.
    feature_version: Mapped[str] = mapped_column(String(40), nullable=False)
    feature_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    bars_available: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many companies the cross-sectional percentiles were measured against.
    # Stored because a percentile is meaningless without its population.
    peer_population: Mapped[int] = mapped_column(Integer, nullable=False)

    compression_ratio: Mapped[float | None] = mapped_column(Float)
    volatility_expansion_ratio: Mapped[float | None] = mapped_column(Float)
    distance_from_52w_high: Mapped[float | None] = mapped_column(Float)
    distance_from_52w_low: Mapped[float | None] = mapped_column(Float)
    stretch_20d: Mapped[float | None] = mapped_column(Float)
    trend_direction: Mapped[str] = mapped_column(String(10), nullable=False)
    trend_r2: Mapped[float | None] = mapped_column(Float)

    # Percentile per ranked feature. JSON because the key set belongs to the
    # signal version rather than to the schema, and a new version may rank a
    # different set without a migration.
    percentiles_json: Mapped[dict[str, object] | None] = mapped_column(JSON)

    # Pipe-separated, as `exclusion_reasons` is: the set is small, closed and
    # ordered, and a reader can filter on it with LIKE without parsing JSON.
    # Multi-label — a security can compress while trending.
    setups: Mapped[str] = mapped_column(String(120), nullable=False)
    setup_evidence_json: Mapped[list[object] | None] = mapped_column(JSON)

    # TRADABLE, UNTRADABLE or UNVERIFIED. Three states, never a boolean: the
    # honest answer for much of this universe is that nobody can tell, and
    # collapsing that into either neighbour invents a certainty.
    tradability: Mapped[str] = mapped_column(String(20), nullable=False)
    tradability_reasons_json: Mapped[list[object] | None] = mapped_column(JSON)
    # Whether stored venue volume established a *minimum* consolidated turnover.
    # False means unproven, never illiquid.
    liquidity_floor_proven: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[PitCaptureRun] = relationship(back_populates="signals")
    company: Mapped[Company] = relationship(back_populates="pit_signals")


class HistoricalSecurity(Base):
    """A security that has left the market, kept addressable after delisting.

    Deliberately **not** a `companies` row. `companies` is the current investable
    universe — what the scanner, rankings and dashboard read — and a delisted
    security must never enter it. This table is the parallel record for names
    that no longer trade, so the two sets cannot contaminate one another: there
    is no foreign key between them and no shared identity.

    Identity is the **CIK**, not the ticker. The CIK is the SEC's stable filer
    identifier, and it is the right key for three reasons this system relies on:
    a ticker change (FB→META) keeps one CIK, so the economic entity is not split
    in two; a reused ticker (BBBY today belongs to a different filer) is a
    different CIK, so the old security can never be joined to the new company;
    and an acquisition gives acquiree and acquirer separate CIKs, so a merger
    does not merge their histories. `primary_ticker` is how prices are fetched
    from a ticker-keyed market feed — an attribute that can change over time,
    never the identity.

    Every field the free sources cannot establish is left NULL rather than
    guessed. `terminal_value_per_share` is set only when a source states it (a
    cash merger); a receivership whose equity value no authoritative source
    gives keeps it NULL, which a reader must read as UNKNOWN and never as zero.
    """

    __tablename__ = "historical_securities"
    __table_args__ = (
        UniqueConstraint("cik", name="uq_historical_security_cik"),
        Index("ix_historical_securities_ticker", "primary_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The SEC filer identifier. The stable identity of the historical security;
    # see the class docstring for why the ticker is not.
    cik: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # The last-known trading symbol, used to fetch prices from a ticker-keyed
    # feed. An attribute, not the identity — a ticker change updates it.
    primary_ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    # The venue the security last traded on, for the eligibility screen. NULL
    # where no free source establishes it, which the screen reads as an
    # unsupported venue rather than assuming a good one.
    exchange: Mapped[str | None] = mapped_column(String(50))
    # Why the security left: a `DelistingReason` value — ACQUIRED, DELISTED,
    # BANKRUPT, RECEIVERSHIP or UNKNOWN. UNKNOWN is the honest default.
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="UNKNOWN")
    # Classification, for the frozen scoring gates. `statement_profile` is read
    # from the filing concepts at ingestion, so a delisted bank is declined the
    # score exactly as a live one is; sector and industry are NULL where no free
    # source establishes them, which the label gate treats as "not unsupported".
    sector: Mapped[str | None] = mapped_column(String(120))
    industry: Mapped[str | None] = mapped_column(String(160))
    statement_profile: Mapped[str | None] = mapped_column(String(30))
    first_filing_date: Mapped[date | None] = mapped_column(Date)
    last_filing_date: Mapped[date | None] = mapped_column(Date)
    # The security's final stored trading session, when one is known.
    last_trading_date: Mapped[date | None] = mapped_column(Date)
    # Cash a holder received per share, when a source states it — a cash merger.
    # NULL is UNKNOWN, never a guessed 0.0: a receivership with no authoritative
    # value keeps it NULL, and reporting zero would libel a name whose equity a
    # source simply never priced.
    terminal_value_per_share: Mapped[float | None] = mapped_column(Float)
    terminal_value_source: Mapped[str | None] = mapped_column(String(50))
    # The currency the *statements* are filed in, and the currency the *shares*
    # traded in. Kept apart for the reason `companies` keeps them apart: one
    # field held both and the two sources disagreed about its meaning.
    reporting_currency: Mapped[str | None] = mapped_column(String(3))
    quote_currency: Mapped[str | None] = mapped_column(String(3))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    observations: Mapped[list[HistoricalObservation]] = relationship(
        back_populates="security", cascade="all, delete-orphan", passive_deletes=True
    )
    prices: Mapped[list[HistoricalPrice]] = relationship(
        back_populates="security", cascade="all, delete-orphan", passive_deletes=True
    )
    corporate_actions: Mapped[list[HistoricalCorporateAction]] = relationship(
        back_populates="security", cascade="all, delete-orphan", passive_deletes=True
    )


class HistoricalObservation(Base):
    """One reported figure as one filing stated it, for a historical security.

    The exact mirror of `fundamental_observations`, keyed to a
    `historical_security` instead of a company. It is append-only and selected
    on `filed_date`, so a reconstruction of a past decision sees only what had
    actually been filed by then and a restatement is invisible until its own
    filing date — the same point-in-time guarantee the company record makes,
    for a security the company record cannot hold.
    """

    __tablename__ = "historical_observations"
    __table_args__ = (
        UniqueConstraint(
            "historical_security_id",
            "observation_fingerprint",
            name="uq_historical_observation",
        ),
        Index(
            "ix_historical_observations_pit",
            "historical_security_id",
            "concept",
            "filed_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    historical_security_id: Mapped[int] = mapped_column(
        ForeignKey("historical_securities.id", ondelete="CASCADE"), nullable=False
    )
    observation_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    taxonomy: Mapped[str] = mapped_column(String(40), nullable=False)
    concept: Mapped[str] = mapped_column(String(120), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    unit: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[date | None] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    # NULL only where the source gives no filing date, and then the observation
    # cannot take part in point-in-time selection at all — `period_end` is never
    # a proxy for it.
    filed_date: Mapped[date | None] = mapped_column(Date)
    accession: Mapped[str | None] = mapped_column(String(30))
    form: Mapped[str | None] = mapped_column(String(20))
    fiscal_year: Mapped[int | None] = mapped_column(Integer)
    fiscal_period: Mapped[str | None] = mapped_column(String(10))
    frame: Mapped[str | None] = mapped_column(String(40))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    security: Mapped[HistoricalSecurity] = relationship(back_populates="observations")


class HistoricalPrice(Base):
    """One daily bar for a historical security, through its last session.

    Mirrors `price_history`, keyed to a `historical_security`. Free market data
    serves a delisted name's bars up to its final trading day, and this is where
    they live so a point-in-time market capitalisation and a forward outcome can
    be computed for a security that no longer exists.
    """

    __tablename__ = "historical_prices"
    __table_args__ = (
        UniqueConstraint("historical_security_id", "date", name="uq_historical_price"),
        Index("ix_historical_prices_security_date", "historical_security_id", "date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    historical_security_id: Mapped[int] = mapped_column(
        ForeignKey("historical_securities.id", ondelete="CASCADE"), nullable=False
    )
    date: Mapped[date] = mapped_column(Date, nullable=False)
    open: Mapped[float] = mapped_column(Float, nullable=False)
    high: Mapped[float] = mapped_column(Float, nullable=False)
    low: Mapped[float] = mapped_column(Float, nullable=False)
    close: Mapped[float] = mapped_column(Float, nullable=False)
    volume: Mapped[float] = mapped_column(Float, nullable=False)

    security: Mapped[HistoricalSecurity] = relationship(back_populates="prices")


class HistoricalCorporateAction(Base):
    """A split or distribution for a historical security, as a source reported it.

    Mirrors `corporate_actions`, keyed to a `historical_security`. Stored bars
    are adjusted retroactively, so recovering the price a holder actually saw on
    a past date needs the factor and the ex-date it applied on; a split before a
    delisting is exactly such a factor, and it lives here rather than being
    inferred from where a price happens to jump.
    """

    __tablename__ = "historical_corporate_actions"
    __table_args__ = (
        UniqueConstraint(
            "historical_security_id", "action_fingerprint", name="uq_historical_corporate_action"
        ),
        Index(
            "ix_historical_corporate_actions_security_date",
            "historical_security_id",
            "ex_date",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    historical_security_id: Mapped[int] = mapped_column(
        ForeignKey("historical_securities.id", ondelete="CASCADE"), nullable=False
    )
    action_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    ticker: Mapped[str] = mapped_column(String(20), nullable=False)
    action_type: Mapped[str] = mapped_column(String(30), nullable=False)
    ex_date: Mapped[date] = mapped_column(Date, nullable=False)
    split_factor: Mapped[float | None] = mapped_column(Float)
    cash_amount: Mapped[float | None] = mapped_column(Float)
    currency: Mapped[str | None] = mapped_column(String(3))
    record_date: Mapped[date | None] = mapped_column(Date)
    payable_date: Mapped[date | None] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(80))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    security: Mapped[HistoricalSecurity] = relationship(back_populates="corporate_actions")


class FilingAcceptance(Base):
    """When the SEC actually accepted one filing, to the second.

    `companyfacts` publishes a filing *date* and nothing finer, which is not
    enough to decide availability. Measured across 2,546 XBRL-bearing filings,
    45.7% of those accepted on their stated filing date were accepted **after**
    the 16:00 ET close — so treating the date as availability shows a
    close-of-day decision roughly half of that day's filings before they were
    public. Treating it as unavailable until the next day is wrong the other
    way, hiding the 54.3% that genuinely were.

    The submissions endpoint publishes `acceptanceDateTime` per accession, and
    every observation carries an accession, so the two join exactly.

    Keyed by accession alone rather than by company: an accession is unique
    across all filers, and one row then serves every observation that cites it —
    a filing carrying two hundred facts is stored once.

    Append-only, like the observation record it qualifies. A re-fetch of the
    same accession writes nothing; the SEC does not revise an acceptance time,
    and if it ever did, the first observation is the one a past decision used.
    """

    __tablename__ = "filing_acceptance"
    __table_args__ = (
        UniqueConstraint("accession", name="uq_filing_acceptance"),
        Index("ix_filing_acceptance_accepted", "accepted_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    accession: Mapped[str] = mapped_column(String(30), nullable=False)
    # Aware UTC. The SEC publishes this as an ISO instant with a `Z` suffix;
    # verified against known filings — a 10-Q accepted at 20:00Z corresponds to
    # 16:00 New York, so the offset is real and not an Eastern time mislabelled.
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # What the submissions index calls `filingDate`, kept so the date-level and
    # instant-level views can be reconciled and disagreements found.
    filed_date: Mapped[date | None] = mapped_column(Date)
    form: Mapped[str | None] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
