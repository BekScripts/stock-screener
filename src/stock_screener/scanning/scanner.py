"""The scanner: stored data in, screened companies out.

`scan_market` reads what ingestion stored, hands it to the metric engine, applies
the eligibility screen, and returns the result. It performs no I/O of its own
beyond the database reads, and it deliberately computes **no score** — that
happens in `stock_screener.scoring`, which consumes these rows. Mixing the two
would make the eligibility gate untestable in isolation.

Rows come back sorted by ticker. Alphabetical is the honest default for a screen:
sorting by, say, revenue growth would imply a ranking, and the ranking is a
different step with its own rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

import structlog

from data_access import (
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    to_company_profile,
    to_financial_period,
    to_price_bar,
)
from domain import USD, VolumeBasis, build_company_metrics, evaluate_eligibility

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from domain import CompanyMetrics, CompanyProfile, EligibilityResult, EligibilityThresholds
    from stock_screener.fx import FxRateResolver

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScanRow:
    """One company's scan result.

    Attributes:
        profile: Identity and classification.
        metrics: Every derived figure, with None where data was unavailable.
        eligibility: Whether it passed, and every reason it did not.
    """

    profile: CompanyProfile
    metrics: CompanyMetrics
    eligibility: EligibilityResult

    @property
    def ticker(self) -> str:
        """The company's symbol."""
        return self.profile.ticker

    @property
    def eligible(self) -> bool:
        """Whether the company passed every screen."""
        return self.eligibility.eligible


@dataclass(frozen=True, slots=True)
class ScanResult:
    """The outcome of scanning the stored universe.

    Attributes:
        rows: Every company examined, in ticker order.
    """

    rows: tuple[ScanRow, ...]

    @property
    def eligible(self) -> tuple[ScanRow, ...]:
        """Only the rows that passed every screen."""
        return tuple(row for row in self.rows if row.eligible)

    @property
    def processed(self) -> int:
        """How many companies were examined."""
        return len(self.rows)

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        return f"processed={self.processed} eligible={len(self.eligible)}"


def scan_market(
    session: Session,
    thresholds: EligibilityThresholds,
    *,
    tickers: Sequence[str] | None = None,
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
    fx: FxRateResolver | None = None,
    as_of: date | None = None,
) -> ScanResult:
    """Calculate metrics and eligibility for every stored company.

    Args:
        session: Open database session.
        thresholds: The eligibility minimums to apply.
        fx: Supplies the rate that converts a foreign company's market
            capitalisation into the currency it files in. None leaves every
            currency-sensitive ratio unavailable for those companies, which is
            what an offline scan should produce; a domestic company needs no
            rate and is unaffected either way.
        as_of: The date rates are wanted for. Defaults to today.
        tickers: Restrict the scan to these symbols. Defaults to everything
            stored.
        bar_volume_basis: What the stored bars' volume represents. Decides
            whether the liquidity threshold can be applied to a figure derived
            from them.

    Returns:
        A `ScanResult` holding one row per company examined, in ticker order.
        Ineligible companies are included with their reasons — a scan that
        dropped them would make it impossible to see why the universe shrank.
    """
    companies = CompanyRepository(session)
    snapshots = FinancialSnapshotRepository(session)
    prices = PriceHistoryRepository(session)

    wanted = {ticker.upper() for ticker in tickers} if tickers else None
    rate_date = as_of or datetime.now(UTC).date()
    rows: list[ScanRow] = []

    for company in companies.list_all():
        if wanted is not None and company.ticker not in wanted:
            continue

        profile = to_company_profile(company)
        periods = [to_financial_period(row) for row in snapshots.list_for_company(company.id)]
        bars = [to_price_bar(row) for row in prices.list_for_company(company.id)]

        # The reporting currency comes from the newest period rather than the
        # profile, for the same reason the metric engine prefers it: a vendor's
        # profile currency for an ADR is the currency the share trades in.
        reporting_currency = next(
            (period.reported_currency for period in reversed(periods) if period.reported_currency),
            profile.reporting_currency,
        )
        conversion = (
            fx.rate_for(profile.quote_currency or USD, reporting_currency or USD, rate_date)
            if fx is not None
            else None
        )
        metrics = build_company_metrics(
            profile, periods, bars, bar_volume_basis=bar_volume_basis, fx=conversion
        )
        rows.append(
            ScanRow(
                profile=profile,
                metrics=metrics,
                eligibility=evaluate_eligibility(profile, metrics, thresholds),
            )
        )

    result = ScanResult(rows=tuple(rows))
    log.info("scan complete", summary=result.summary())
    return result
