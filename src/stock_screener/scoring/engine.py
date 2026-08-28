"""Running CompounderScore over the stored market and saving the result.

The formula itself is in `domain.scoring` and knows nothing about databases.
This module is the part that has to: it reads what ingestion stored, hands each
company to the formula, and writes one snapshot per company per day.

Two things are deliberately not done here. Nothing is re-fetched — scoring runs
entirely on stored data, so it can be re-run as often as the formula changes
without touching a provider. And nothing is filtered out: an ineligible company,
a bank and a company with two quarters of history all get a row carrying the
status that explains the absent score, which is what makes "why is this not in
the ranking?" answerable without re-running anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from data_access import (
    MARKET_COVERAGE,
    BenchmarkPriceRepository,
    CompanyRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    to_price_bar,
)
from domain import (
    BenchmarkReturns,
    CompanyScore,
    EligibilityWarning,
    Freshness,
    ScoringError,
    ScoringStatus,
    return_6m,
    return_12m,
    score_company,
)
from stock_screener.fx import FxRateResolver, build_fx_provider, pairs_needed
from stock_screener.scanning import scan_market

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from domain import CompanyMetrics, CompanyProfile
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ScoredCompany:
    """One company's score together with what it was calculated from.

    Attributes:
        company_id: The stored company's primary key.
        profile: Identity and classification.
        metrics: Every derived figure the score was based on.
        score: The score, including the status explaining an absent number.
        exclusion_reasons: Every eligibility check the security failed. Empty
            for one that passed. Carried so the stored snapshot can say *why* a
            company has no score rather than only that it has none.
        freshness: Whether the fundamentals behind the score were current. V1.2
            keeps a stale score out of current rankings and nowhere else — it
            remains on the stock page, in research and in history.
    """

    company_id: int
    profile: CompanyProfile
    metrics: CompanyMetrics
    score: CompanyScore
    exclusion_reasons: tuple[str, ...] = ()
    freshness: Freshness = Freshness.CURRENT

    @property
    def ticker(self) -> str:
        """The company's symbol."""
        return self.profile.ticker


@dataclass(frozen=True, slots=True)
class ScoringRun:
    """The outcome of scoring the stored market once.

    Attributes:
        score_date: The day the scores describe.
        benchmark: The market returns relative strength was measured against.
        rows: One entry per company examined, in ticker order.
        persisted: How many snapshots were written. Zero for a dry run.
    """

    score_date: date
    benchmark: BenchmarkReturns
    rows: tuple[ScoredCompany, ...]
    persisted: int = 0

    @property
    def scored(self) -> tuple[ScoredCompany, ...]:
        """Only the companies that received a number."""
        return tuple(row for row in self.rows if row.score.status is ScoringStatus.SCORED)

    @property
    def status_counts(self) -> dict[str, int]:
        """How many companies ended in each scoring status."""
        return dict(Counter(row.score.status.value for row in self.rows))

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        counts = " ".join(
            f"{status.lower()}={count}" for status, count in self.status_counts.items()
        )
        return f"processed={len(self.rows)} {counts} persisted={self.persisted}"


def load_benchmark_returns(session: Session, symbol: str) -> BenchmarkReturns:
    """Return the benchmark's six- and twelve-month returns from stored bars.

    Args:
        session: Open database session.
        symbol: The benchmark's ticker.

    Returns:
        The returns, each None when the stored history does not reach back far
        enough. A benchmark that was never ingested yields both as None, which
        leaves every company's market confirmation unscoreable rather than
        silently scoring relative strength against zero.
    """
    bars = [to_price_bar(row) for row in BenchmarkPriceRepository(session).list_bars(symbol)]
    return BenchmarkReturns(
        symbol=symbol,
        return_6m=return_6m(bars),
        return_12m=return_12m(bars),
    )


def build_scores(
    session: Session,
    settings: Settings,
    benchmark: BenchmarkReturns,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    fx: FxRateResolver | None = None,
    as_of: date | None = None,
) -> list[ScoredCompany]:
    """Score companies from stored data without persisting anything.

    The scoring half of `score_market`, separated so the enrichment pass can
    re-run the **same** formula over the same code path after replacing a
    candidate's inputs. Two scoring implementations would be two things to keep
    in agreement, and the one that drifted would be the one nobody was watching.

    Args:
        session: Open database session.
        settings: Supplies the eligibility thresholds and the volume basis.
        benchmark: Market returns for relative strength.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Score at most this many companies, in ticker order.
        fx: Resolves the rate each foreign company needs. None leaves their
            currency-sensitive metrics unavailable and touches nothing else.
        as_of: The date rates are wanted for.

    Returns:
        One entry per company examined, in ticker order.
    """
    scan = scan_market(
        session,
        settings.eligibility_thresholds,
        tickers=tickers,
        bar_volume_basis=settings.bar_volume_basis,
        fx=fx,
        as_of=as_of,
    )
    identifiers = {company.ticker: company.id for company in CompanyRepository(session).list_all()}

    selected = scan.rows[:limit] if limit is not None else scan.rows
    rows: list[ScoredCompany] = []
    for scan_row in selected:
        company_id = identifiers.get(scan_row.ticker)
        if company_id is None:  # pragma: no cover — the scan reads the same table
            continue
        rows.append(
            ScoredCompany(
                company_id=company_id,
                profile=scan_row.profile,
                metrics=scan_row.metrics,
                score=_score_one(scan_row.profile, scan_row.metrics, benchmark, scan_row.eligible),
                exclusion_reasons=tuple(reason.value for reason in scan_row.eligibility.reasons),
                # Taken from the screen that already decided it, rather than
                # recomputed here. The staleness bound scales with reporting
                # cadence, and two places deciding it separately is two places to
                # drift apart.
                freshness=(
                    Freshness.STALE
                    if EligibilityWarning.STALE_FUNDAMENTALS in scan_row.eligibility.warnings
                    else Freshness.CURRENT
                ),
            )
        )
    return rows


def score_market(
    session: Session,
    settings: Settings,
    *,
    tickers: Sequence[str] | None = None,
    limit: int | None = None,
    score_date: date | None = None,
    persist: bool = True,
    coverage: str = MARKET_COVERAGE,
) -> ScoringRun:
    """Score every stored company and, by default, save the day's snapshots.

    Args:
        session: Open database session. The caller commits.
        settings: Supplies the eligibility thresholds and the benchmark symbol.
        tickers: Restrict to these symbols. Defaults to every stored company.
        limit: Score at most this many companies, in ticker order.
        score_date: The day the scores describe. Defaults to today in UTC.
        persist: Write the snapshots. False computes and returns without
            touching the table, which is what makes it safe to inspect a
            formula change before it enters the history.
        coverage: Whether this run covers the market or one company. A
            single-stock run must pass `SINGLE_COVERAGE`, or its snapshot
            becomes the newest ranking and every ranking view shows one row.

    Returns:
        The run, holding one entry per company examined.

    Raises:
        ScoringError: If the formula's component weights do not add up. Fatal
            rather than counted: it would be wrong for every company, and a
            market-wide ranking computed against the wrong denominator is worse
            than no ranking.
    """
    as_of = score_date or datetime.now(UTC).date()
    benchmark = load_benchmark_returns(session, settings.benchmark_symbol)
    if benchmark.return_6m is None and benchmark.return_12m is None:
        log.warning(
            "no benchmark history: market confirmation cannot be scored, so every "
            "company will be INSUFFICIENT_DATA — run update-benchmark first",
            symbol=settings.benchmark_symbol,
        )

    fx = FxRateResolver(
        session,
        build_fx_provider(settings),
        max_age_days=settings.fx_max_rate_age_days,
    )
    # Resolved before the loop rather than inside it. Forty companies reporting
    # in euros need one USD/EUR rate between them, and doing this up front also
    # means a rate failure is logged once, here, rather than five thousand times
    # in the middle of a scan.
    needed = pairs_needed(
        (company.quote_currency, company.reporting_currency)
        for company in CompanyRepository(session).list_all()
    )
    if needed:
        found = fx.warm(needed, as_of)
        log.info("fx rates resolved", pairs=len(needed), resolved=found, as_of=str(as_of))

    rows = build_scores(
        session, settings, benchmark, tickers=tickers, limit=limit, fx=fx, as_of=as_of
    )

    persisted = 0
    if persist and rows:
        persisted = ScoreSnapshotRepository(session).upsert_scores(
            [
                ScoreRecord(
                    row.company_id,
                    row.score,
                    row.metrics,
                    exclusion_reasons=row.exclusion_reasons,
                    freshness=row.freshness,
                )
                for row in rows
            ],
            as_of,
            coverage=coverage,
        )

    run = ScoringRun(score_date=as_of, benchmark=benchmark, rows=tuple(rows), persisted=persisted)
    log.info("scoring complete", score_date=str(as_of), summary=run.summary())
    return run


def _score_one(
    profile: CompanyProfile,
    metrics: CompanyMetrics,
    benchmark: BenchmarkReturns,
    eligible: bool,
) -> CompanyScore:
    """Score one company, turning an unexpected failure into a recorded status.

    One company that breaks the formula must not end a market-wide run, and it
    must not vanish either: it gets an `ERROR` row, which is visible in the
    status counts and in the table.

    Raises:
        ScoringError: Re-raised rather than recorded. It means the formula
            itself is misconfigured, which is true for every company.
    """
    try:
        return score_company(profile, metrics, benchmark, eligible=eligible)
    except ScoringError:
        raise
    except Exception:
        log.exception("scoring failed", ticker=profile.ticker)
        return CompanyScore(ticker=profile.ticker, status=ScoringStatus.ERROR)
