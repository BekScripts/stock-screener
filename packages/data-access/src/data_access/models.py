"""SQLAlchemy tables for companies, fundamentals and price history.

Three tables carry Phase 1. Their unique constraints are load-bearing: they are
what makes re-running the daily job idempotent, and they are what the upsert
helpers in `repositories` target. Removing one would not fail a test immediately
— it would slowly fill the database with duplicate quarters that quietly double a
trailing-twelve-month figure.

Money is stored as `Float` rather than `Numeric`. Everything downstream is a
ratio or a ranking, where double precision is ample, and floats avoid `Decimal`
conversions on every read. This would be the wrong call for a ledger; it is the
right one for a screener.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
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
    operating_income: Mapped[float | None] = mapped_column(Float)
    operating_cash_flow: Mapped[float | None] = mapped_column(Float)
    capital_expenditure: Mapped[float | None] = mapped_column(Float)
    free_cash_flow: Mapped[float | None] = mapped_column(Float)
    cash: Mapped[float | None] = mapped_column(Float)
    total_debt: Mapped[float | None] = mapped_column(Float)
    shares_outstanding: Mapped[float | None] = mapped_column(Float)
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
