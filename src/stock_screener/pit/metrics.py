"""Fundamental metrics as they stood on a past date.

The operational path builds metrics from `financial_snapshots`, which holds one
row per period and is overwritten when a filing restates it. That is correct for
a screener — the newest figure is the best estimate of the truth — and it makes
the table useless for reconstructing a past decision, because the figure a
decision saw has been replaced by the figure that came later.

This module builds the same metrics from the append-only observation record
instead, bounded to what had actually been filed by a given date.

## What is and is not new here

**No metric is redefined.** Gross margin is still gross profit over revenue.
Every calculation is `domain.build_company_metrics`, unchanged and uncalled-into
— the arithmetic layer is a pure function over injected periods and has always
been indifferent to where they came from. Only the retrieval changes.

**No normalisation is reimplemented.** Turning concepts into periods —
the taxonomy detection, the concept chains, the capital-expenditure and debt
assembly, the cadence and currency rules — is the provider's, and it is reused
byte for byte through `api_clients.periods_from_observations`. A second
implementation would drift, and drift here would look like history.

So the layering is:

    observations filed by as_of      <- this module chooses the bound
        -> provider normalisation    <- frozen, shared with the live path
        -> FinancialPeriod
        -> domain.build_company_metrics   <- frozen arithmetic
        -> CompanyMetrics

The temporal semantics live entirely in the first step.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from api_clients import periods_from_observations
from data_access import (
    CompanyRepository,
    CorporateActionRepository,
    FundamentalObservationRepository,
    PriceHistoryRepository,
    to_company_profile,
    to_corporate_action,
    to_filing_observation,
    to_price_bar,
)
from domain import (
    DEFAULT_LIQUIDITY_WINDOW,
    VolumeBasis,
    as_traded_price,
    build_company_metrics,
    latest_close,
)
from stock_screener.pit.decision_time import DecisionPoint, decision_instant

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from data_access.models import FundamentalObservation
    from domain import CompanyMetrics, FinancialPeriod, FxConversion

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class PointInTimeMetrics:
    """Metrics for one company as of one date, with what produced them.

    Attributes:
        as_of: The decision date the metrics describe. Nothing filed after it
            contributed.
        metrics: The metrics themselves, or None when no observation had been
            filed by `as_of` and there was nothing to build from.
        periods: The periods the metrics were built from, oldest first. Exposed
            because a reader checking a historical figure needs to see which
            filings it rests on.
        observations_available: How many observations had been filed by
            `as_of`.
        observations_excluded_undated: How many were skipped for carrying no
            filing date. Reported rather than hidden: it is the size of the gap
            between what is stored and what is usable point-in-time.
    """

    as_of: date
    metrics: CompanyMetrics | None
    periods: tuple[FinancialPeriod, ...] = ()
    observations_available: int = 0
    observations_excluded_undated: int = 0


def periods_as_of(
    session: Session,
    company_id: int,
    as_of: date,
    *,
    decision: DecisionPoint = DecisionPoint.CLOSE,
) -> list[FinancialPeriod]:
    """Return the reported periods that were public on a date.

    Reads the append-only observation record, keeps what had been filed by
    `as_of`, and normalises it with the provider's own logic.

    A restatement filed after the cutoff is invisible; the figure that stood at
    the time is what comes back. An observation with no filing date never
    appears, because its availability is unknown and `period_end` is not a
    proxy for it.

    Availability is judged against an *instant*, not a date — the close of
    `as_of` by default. See `stock_screener.pit.decision_time` for why a date
    comparison cannot answer the question.

    Args:
        session: Open database session.
        company_id: The owning company's primary key.
        as_of: The decision date.
        decision: Where in the day the decision sits. Defaults to the close.

    Returns:
        Periods as they would have been assembled at that point, oldest first.
    """
    rows = FundamentalObservationRepository(session).available_as_of(
        company_id, as_of, accepted_by=decision_instant(as_of, decision)
    )
    return periods_from_observations(to_filing_observation(row) for row in rows)


def build_metrics_as_of(
    session: Session,
    company_id: int,
    as_of: date,
    *,
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW,
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
    fx: FxConversion | None = None,
    decision: DecisionPoint = DecisionPoint.CLOSE,
) -> PointInTimeMetrics:
    """Build one company's metrics from information available on a date.

    Both halves are bounded. Fundamentals come from observations filed on or
    before `as_of`; price bars are read with the same cutoff, so a metric
    cannot be computed against a session that had not happened.

    The market capitalisation is point-in-time on both sides. The profile's
    figure is a current quote and is suppressed outright — using it would value
    a 2019 snapshot at today's price, which is the largest look-ahead available
    in this codebase. What replaces it is the filed share count multiplied by
    the price the security actually traded at, recovered from the stored close
    with the split factors since. See `domain.pricing` for what that recovery
    does and does not undo.

    Args:
        session: Open database session.
        company_id: The owning company's primary key.
        as_of: The decision date.
        liquidity_window: Sessions to average dollar volume over.
        bar_volume_basis: What the bars' volume represents.
        fx: Rate converting quote to reporting currency, resolved by the caller.
            The caller is responsible for supplying one dated no later than
            `as_of`; this module performs no I/O and cannot check it.
        decision: Where in the day the decision sits. Defaults to the close;
            see `stock_screener.pit.decision_time` for the contract.

    Returns:
        The metrics and the evidence behind them. `metrics` is None when
        nothing had been filed by `as_of`.
    """
    company = CompanyRepository(session).get(company_id)
    if company is None:
        log.warning("no such company for point-in-time metrics", company_id=company_id)
        return PointInTimeMetrics(as_of=as_of, metrics=None)

    observations = FundamentalObservationRepository(session).available_as_of(company_id, as_of)
    periods = periods_from_observations(to_filing_observation(row) for row in observations)
    if not periods:
        log.info(
            "nothing had been filed by this date",
            ticker=company.ticker,
            as_of=str(as_of),
        )
        return PointInTimeMetrics(
            as_of=as_of,
            metrics=None,
            observations_available=len(observations),
        )

    bars = [
        to_price_bar(row)
        for row in PriceHistoryRepository(session).list_for_company(company_id, until=as_of)
    ]

    # Every action, not only those on or before `as_of`. The ones *after* it are
    # precisely the ones that need undoing: a stored close for 2020-08-28 is on
    # today's basis because of a split that happened three days later, and
    # bounding this read to the decision date would leave the very factor that
    # makes the close wrong out of the correction. Nothing here is used as
    # knowledge available on the date — a split is applied to a price, not read
    # as a fact about the company.
    actions = [
        to_corporate_action(row)
        for row in CorporateActionRepository(session).for_company(company_id)
    ]
    # Anchored on the date the share count describes, not on `as_of`. AAPL
    # split on 2020-08-31 and did not file again until 2020-10-30; for those two
    # months the newest available count is pre-split and the price is not.
    # Anchoring on `as_of` applies no factor across that window and values the
    # company at 557bn instead of 2.2tn. See `domain.pricing.market_cap_as_of`.
    share_basis_close = as_traded_price(
        latest_close(bars), actions, _share_count_date(observations) or as_of
    )

    # The stored market capitalisation is a current quote against a current
    # share count. On a past date it is not merely stale, it is the answer to a
    # different question, so it is withheld rather than aged.
    profile = to_company_profile(company).model_copy(update={"market_cap": None})

    metrics = build_company_metrics(
        profile,
        periods,
        bars,
        liquidity_window=liquidity_window,
        bar_volume_basis=bar_volume_basis,
        fx=fx,
        share_basis_close=share_basis_close,
    )
    return PointInTimeMetrics(
        as_of=as_of,
        metrics=metrics,
        periods=tuple(periods),
        observations_available=len(observations),
    )


#: The cover-page count's XBRL concept. `dei:EntityCommonStockSharesOutstanding`
#: is what `api_clients.edgar` reads for `common_shares_outstanding`, so it is
#: what dates that figure.
_COVER_PAGE_SHARES = "EntityCommonStockSharesOutstanding"


def _share_count_date(observations: Sequence[FundamentalObservation]) -> date | None:
    """Return the instant the newest available cover-page share count describes.

    Its own `period_end`, not the filing's date: a count stated as of 17 July
    and filed on 31 July is a statement about 17 July, and a split between the
    two would not be in it.

    Args:
        observations: The observations available on the decision date, as the
            repository returned them.

    Returns:
        The date, or None when no cover-page count is among them.
    """
    dated = [
        row.period_end
        for row in observations
        if row.concept.endswith(_COVER_PAGE_SHARES) and row.period_end is not None
    ]
    return max(dated) if dated else None
