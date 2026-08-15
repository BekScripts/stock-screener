"""The basic eligibility screen.

This is the gate every security passes through before it is worth calculating
anything else about. It answers one question — is this thing investable at all —
and deliberately says nothing about whether it is a good investment. Scoring is
Phase 2 and lives elsewhere.

The screen reports *every* reason a security failed rather than stopping at the
first. A stock excluded for both price and liquidity is a different animal from
one excluded on price alone, and the difference is only visible if both are
recorded.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.models import (
    EligibilityResult,
    EligibilityThresholds,
    EligibilityWarning,
    ExclusionReason,
    MarketCapSource,
    VolumeBasis,
)
from domain.universe import is_supported_listing

if TYPE_CHECKING:
    from domain.models import CompanyMetrics, CompanyProfile

MARKET_CAP_DISCREPANCY_THRESHOLD = 0.25
"""How far a calculated market cap may sit from a provider's before it is flagged.

A quarter is wide enough to absorb a share count that is a filing out of date
and a price that moved since, and narrow enough to catch a missing share class
or an issuance that changed the size of the company.
"""


def evaluate_eligibility(
    profile: CompanyProfile,
    metrics: CompanyMetrics,
    thresholds: EligibilityThresholds | None = None,
) -> EligibilityResult:
    """Screen one security against the configured minimums.

    Liquidity is checked against a full window of price history as well as the
    dollar-volume threshold: a stock that has traded four sessions has no
    meaningful twenty-day average, so it is excluded as illiquid rather than
    admitted on a figure derived from a handful of bars.

    The dollar-volume threshold is only applied to a **consolidated** figure.
    Where the only volume available covers a single exchange, the check is
    reported as a warning instead: a free feed carries a few percent of real
    volume, so imposing a whole-market threshold on it would exclude most of the
    small companies this screen is meant to find.

    Missing data excludes rather than passes. A company whose market cap never
    arrived is not implicitly small or implicitly large — it is unscreenable, and
    `MISSING_REQUIRED_DATA` says so instead of a threshold silently deciding.

    A company filing in a currency other than USD is excluded outright. Its
    market cap is quoted in dollars while its revenue is not, so every ratio
    built from the two would be wrong by an exchange rate — silently, and by a
    factor that looks like a plausible valuation.

    A market capitalisation calculated from filings and a price is screened on
    exactly like a provider's, and carries a warning saying which it is. The
    alternative — excluding every company no vendor covers — would shrink the
    universe to whatever a metered plan could reach that day.

    Args:
        profile: Identity, exchange and activity status for the security.
        metrics: Its calculated metrics, as produced by `build_company_metrics`.
        thresholds: The minimums to apply. Defaults to the Phase 1 values.

    Returns:
        The verdict, with every failed check listed in `reasons`.
    """
    limits = thresholds or EligibilityThresholds()
    reasons: list[ExclusionReason] = []

    if not profile.is_active:
        reasons.append(ExclusionReason.INACTIVE)

    # A provider that says "this is an ETF" is authoritative; the name
    # heuristics are only a fallback for providers that say nothing.
    if profile.is_fund or not is_supported_listing(profile.ticker, profile.name, profile.exchange):
        reasons.append(ExclusionReason.UNSUPPORTED_SECURITY_TYPE)

    if not profile.reports_in_usd:
        reasons.append(ExclusionReason.UNSUPPORTED_CURRENCY)

    if metrics.price is None or metrics.market_cap is None:
        reasons.append(ExclusionReason.MISSING_REQUIRED_DATA)

    if metrics.price is not None and metrics.price < limits.min_price:
        reasons.append(ExclusionReason.PRICE_BELOW_MINIMUM)

    if metrics.market_cap is not None and metrics.market_cap < limits.min_market_cap:
        reasons.append(ExclusionReason.MARKET_CAP_BELOW_MINIMUM)

    warnings: list[EligibilityWarning] = []
    if metrics.market_cap_source is MarketCapSource.CALCULATED:
        warnings.append(EligibilityWarning.MARKET_CAP_CALCULATED)
    if (
        metrics.market_cap_discrepancy is not None
        and metrics.market_cap_discrepancy > MARKET_CAP_DISCREPANCY_THRESHOLD
    ):
        warnings.append(EligibilityWarning.MARKET_CAP_DISCREPANCY)

    if metrics.trading_days_used < limits.min_trading_days:
        # Too little history to judge liquidity, whatever the feed.
        reasons.append(ExclusionReason.LOW_LIQUIDITY)
    elif metrics.liquidity_basis is VolumeBasis.CONSOLIDATED:
        if (
            metrics.average_dollar_volume_20d is None
            or metrics.average_dollar_volume_20d < limits.min_avg_dollar_volume
        ):
            reasons.append(ExclusionReason.LOW_LIQUIDITY)
    else:
        # The only volume available covers part of the market, so the threshold
        # cannot be applied to it. Warning rather than exclusion: a filter that
        # is silently twenty-five times too strict would remove most of the
        # smaller companies this project exists to surface.
        warnings.append(EligibilityWarning.LIQUIDITY_UNVERIFIED)

    return EligibilityResult(
        ticker=profile.ticker,
        eligible=not reasons,
        reasons=tuple(reasons),
        warnings=tuple(warnings),
    )
