"""Translation between stored rows and domain models.

The metric engine works on `domain` models and knows nothing about SQLAlchemy;
persistence knows nothing about how a margin is calculated. These functions are
the join between the two, and keeping them here means the ORM classes never
travel past this package.

Column names deliberately match the domain field names, so the translation is
mechanical and a mismatch shows up as a type error rather than a wrong number.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain import CompanyProfile, FinancialPeriod, PriceBar

if TYPE_CHECKING:
    from data_access.models import Company, FinancialSnapshot, PriceHistory


def to_company_profile(company: Company) -> CompanyProfile:
    """Convert a stored company row into a domain profile.

    Args:
        company: The stored row.

    Returns:
        The equivalent `CompanyProfile`.
    """
    return CompanyProfile(
        ticker=company.ticker,
        name=company.name,
        exchange=company.exchange,
        sector=company.sector,
        industry=company.industry,
        market_cap=company.market_cap,
        average_volume=company.average_volume,
        currency=company.currency,
        is_active=company.is_active,
    )


def to_financial_period(snapshot: FinancialSnapshot) -> FinancialPeriod:
    """Convert a stored snapshot row into a domain period.

    Args:
        snapshot: The stored row.

    Returns:
        The equivalent `FinancialPeriod`, with NULL columns preserved as None.
    """
    return FinancialPeriod(
        period_end=snapshot.period_end,
        revenue=snapshot.revenue,
        gross_profit=snapshot.gross_profit,
        operating_income=snapshot.operating_income,
        operating_cash_flow=snapshot.operating_cash_flow,
        capital_expenditure=snapshot.capital_expenditure,
        free_cash_flow=snapshot.free_cash_flow,
        cash=snapshot.cash,
        total_debt=snapshot.total_debt,
        shares_outstanding=snapshot.shares_outstanding,
        reported_currency=snapshot.reported_currency,
        source=snapshot.source,
    )


def to_price_bar(row: PriceHistory) -> PriceBar:
    """Convert a stored price row into a domain bar.

    Args:
        row: The stored row.

    Returns:
        The equivalent `PriceBar`.
    """
    return PriceBar(
        date=row.date,
        open=row.open,
        high=row.high,
        low=row.low,
        close=row.close,
        volume=row.volume,
    )
