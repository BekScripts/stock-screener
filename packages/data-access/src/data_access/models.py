"""SQLAlchemy tables for companies, fundamentals, prices, scores and research.

Three tables carry Phase 1, two more carry Phase 2, and one carries Phase 3's
research reports. Their unique constraints
are load-bearing: they are what makes re-running the daily job idempotent, and
they are what the upsert helpers in `repositories` target. Removing one would not
fail a test immediately — it would slowly fill the database with duplicate
quarters that quietly double a trailing-twelve-month figure.

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
    # ISO code the company reports in. NULL means the provider did not say,
    # which the eligibility screen treats as USD.
    currency: Mapped[str | None] = mapped_column(String(3))
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
    filing_excerpts: Mapped[list[FilingExcerptRecord]] = relationship(
        back_populates="company", cascade="all, delete-orphan", passive_deletes=True
    )
    filings: Mapped[list[FilingRecord]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    research_reports: Mapped[list[StoredResearchReport]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
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
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), nullable=False
    )
    score_date: Mapped[date] = mapped_column(Date, nullable=False)
    score_version: Mapped[str] = mapped_column(String(40), nullable=False)
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
