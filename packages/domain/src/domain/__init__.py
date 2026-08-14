"""Screening rules and filter models, free of I/O.

This module is the package's public API. Consumers import from the package root
only::

    from domain import build_company_metrics, evaluate_eligibility

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package holds three things: the normalised models every other layer speaks in
(`models`), the deterministic metric engine (`metrics`), and the eligibility
screen with its universe rules (`eligibility`, `universe`). None of them perform
I/O, so all of them are testable against hand-worked numbers.
"""

from domain.eligibility import evaluate_eligibility
from domain.metrics import (
    DEFAULT_LIQUIDITY_WINDOW,
    average_dollar_volume,
    build_company_metrics,
    distance_from_52w_high,
    fcf_margin,
    free_cash_flow,
    gross_margin,
    gross_profit_growth_yoy,
    growth_acceleration,
    high_52w,
    latest_close,
    low_52w,
    net_cash,
    operating_margin,
    previous_yoy_revenue_growth,
    resolve_liquidity,
    return_6m,
    return_12m,
    revenue_cagr_3y,
    share_count_growth_yoy,
    total_return,
    trading_days_used,
    ttm_revenue,
    ttm_revenue_growth,
    yoy_revenue_growth,
)
from domain.models import (
    USD,
    CompanyMetrics,
    CompanyProfile,
    EligibilityResult,
    EligibilityThresholds,
    EligibilityWarning,
    ExclusionReason,
    FinancialPeriod,
    PriceBar,
    VolumeBasis,
    normalise_ticker,
)
from domain.universe import (
    SUPPORTED_EXCHANGES,
    is_common_stock,
    is_supported_exchange,
    is_supported_listing,
)

__all__ = [
    "DEFAULT_LIQUIDITY_WINDOW",
    "SUPPORTED_EXCHANGES",
    "USD",
    "CompanyMetrics",
    "CompanyProfile",
    "EligibilityResult",
    "EligibilityThresholds",
    "EligibilityWarning",
    "ExclusionReason",
    "FinancialPeriod",
    "PriceBar",
    "VolumeBasis",
    "average_dollar_volume",
    "build_company_metrics",
    "distance_from_52w_high",
    "evaluate_eligibility",
    "fcf_margin",
    "free_cash_flow",
    "gross_margin",
    "gross_profit_growth_yoy",
    "growth_acceleration",
    "high_52w",
    "is_common_stock",
    "is_supported_exchange",
    "is_supported_listing",
    "latest_close",
    "low_52w",
    "net_cash",
    "normalise_ticker",
    "operating_margin",
    "previous_yoy_revenue_growth",
    "resolve_liquidity",
    "return_6m",
    "return_12m",
    "revenue_cagr_3y",
    "share_count_growth_yoy",
    "total_return",
    "trading_days_used",
    "ttm_revenue",
    "ttm_revenue_growth",
    "yoy_revenue_growth",
]

__version__ = "0.1.0"
