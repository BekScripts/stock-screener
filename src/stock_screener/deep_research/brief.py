"""Assemble a `DeepResearchBrief` from stored rows, and touch nothing else.

The zero-network half. Everything here reads the database; nothing here reaches
a provider, a clock-dependent external service or a model. That is the same rule
Phase 3 applies to `assemble_brief`, and it is what makes a brief reproducible:
run the assembler twice over unchanged data and you get the same brief, the same
fingerprints, and therefore a cache hit rather than a second paid report.

The deterministic half is not rebuilt here. It comes from
`research.assemble_deterministic_evidence`, the same function the Phase 3
assembler uses, so a deep brief and a Phase 3 brief describing the same company
on the same score date carry byte-identical score evidence, metrics, periods,
filings and excerpts. Deep research adds three things Phase 3 has no place for —
where the company sits in the ranking, how current the data is, and an external
evidence set that is empty until collection exists — and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from data_access import CompanyRepository, ScoreSnapshotRepository
from deep_research import DataFreshness, DeepResearchBrief, MarketRanking
from domain import CURRENT_SCORE_VERSION
from research import RankingState
from stock_screener.deep_research.preparation import StageState, stored_freshness_dates
from stock_screener.research import (
    DEFAULT_QUARTERS,
    assemble_deterministic_evidence,
    parse_score_breakdown,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

    from data_access import ScoreSnapshot
    from deep_research import ExternalEvidence
    from stock_screener.config import Settings
    from stock_screener.deep_research.preparation import PreparationResult

log = structlog.get_logger(__name__)


def assemble_deep_brief(
    session: Session,
    settings: Settings,
    ticker: str,
    *,
    preparation: PreparationResult | None = None,
    external: Sequence[ExternalEvidence] = (),
    score_version: str = CURRENT_SCORE_VERSION,
    quarters: int = DEFAULT_QUARTERS,
) -> DeepResearchBrief | None:
    """Assemble one company's deep research brief from stored data.

    Performs no network access. Every field comes out of the database, so the
    brief is a function of what is stored plus the preparation record — which is
    what lets the same evidence produce the same fingerprint twice.

    Args:
        session: Open database session.
        settings: Supplies the volume basis the metric engine needs, and the
            provider name recorded on enrichment.
        ticker: The company to describe.
        preparation: What the refresh run did, when one just ran. Supplies the
            refreshed/reused/stale account on `DataFreshness`; without it the
            brief still reports the dates its evidence reaches, and simply does
            not claim anything was refreshed.
        external: Already-collected `W.` evidence. Passed in rather than fetched:
            collection is a network stage and this function performs none, which
            is what keeps a brief reproducible. Empty is the default and a
            perfectly legal brief — deterministic preparation never depends on
            external evidence being available.
        score_version: The formula version whose score the brief explains.
        quarters: Reporting periods to supply.

    Returns:
        The brief, or None when the company is unknown, has never been scored
        under this version, or stored a breakdown that can no longer be read.
    """
    company = CompanyRepository(session).get_by_ticker(ticker)
    if company is None:
        log.warning("deep brief skipped: unknown company", ticker=ticker)
        return None

    snapshots = ScoreSnapshotRepository(session)
    snapshot = snapshots.latest_for_company(company.id, score_version=score_version)
    if snapshot is None:
        log.warning("deep brief skipped: never scored", ticker=ticker, score_version=score_version)
        return None

    score = parse_score_breakdown(snapshot)
    if score is None:
        return None

    evidence = assemble_deterministic_evidence(
        session, settings, company, snapshot, score, quarters=quarters
    )

    return DeepResearchBrief(
        ticker=company.ticker,
        name=company.name,
        sector=company.sector,
        industry=company.industry,
        exchange=company.exchange,
        as_of=evidence.as_of,
        assembled_at=datetime.now(UTC),
        score=evidence.score,
        facts=evidence.facts,
        quarters=evidence.quarters,
        score_history=evidence.score_history,
        filings=evidence.filings,
        excerpts=evidence.excerpts,
        enrichment=evidence.enrichment,
        external=tuple(external),
        ranking=_ranking(session, snapshot, score_version),
        freshness=_freshness(session, company.id, preparation),
    )


def _ranking(session: Session, snapshot: ScoreSnapshot, score_version: str) -> MarketRanking | None:
    """Return where the company stands against the market as most recently known.

    Ranked against every company's newest scored snapshot rather than against
    one `score_date`. Preparing a single stock writes a snapshot on today's date,
    and that date holds only the company just prepared — ranking within it would
    report "1 of 1" for every company ever prepared on its own, which is true and
    worthless. Comparing against each company's latest known score answers the
    question a person actually asked.

    The comparison uses this company's freshly scored value, so a company that
    just improved moves immediately rather than waiting for a market-wide run.

    Args:
        session: Open database session.
        snapshot: The score snapshot the brief is bounded by.
        score_version: The formula version to rank within. Never crossed.

    Returns:
        The position, or None when nothing has been scored under this version.
        A company with no score of its own keeps `rank=None` while still
        reporting how many companies it could not be placed among.
    """
    population = ScoreSnapshotRepository(session).latest_scored_population(
        score_version=score_version
    )
    if not population:
        return None

    universe = len(population)
    state = _enum_ranking_state(snapshot.ranking_state)
    mine = snapshot.final_score

    if mine is None:
        return MarketRanking(rank=None, universe_size=universe, ranking_state=state)

    # Ties take the better position, matching how a ranking numbers equal scores.
    better = sum(
        1 for company_id, score in population if score > mine and company_id != snapshot.company_id
    )
    position = better + 1

    return MarketRanking(
        rank=position,
        universe_size=universe,
        percentile=1 - (position - 1) / universe,
        ranking_state=state,
    )


def _enum_ranking_state(stored: str | None) -> RankingState:
    """Read the stored ranking state, defaulting to the cautious answer.

    An unreadable or absent value becomes `PRELIMINARY` rather than `FINAL`:
    claiming a vendor verified the inputs when the column does not say so would
    overstate the evidence in the one direction that matters.
    """
    if stored is None:
        return RankingState.PRELIMINARY
    try:
        return RankingState(stored)
    except ValueError:
        return RankingState.PRELIMINARY


def _freshness(
    session: Session, company_id: int, preparation: PreparationResult | None
) -> DataFreshness:
    """Describe how current the stored evidence is, and how it got that way.

    The dates come from the database rather than from the preparation report, so
    a stage that degraded leaves the previous date standing and the brief says
    what is actually there. The refreshed/reused/stale account comes from the
    run, because only the run knows which of them was true.
    """
    price_as_of, fundamentals_through, filings_through, excerpts_through = stored_freshness_dates(
        session, company_id
    )

    if preparation is None:
        return DataFreshness(
            price_as_of=price_as_of,
            fundamentals_through=fundamentals_through,
            filings_through=filings_through,
            excerpts_through=excerpts_through,
        )

    return DataFreshness(
        price_as_of=price_as_of,
        fundamentals_through=fundamentals_through,
        filings_through=filings_through,
        excerpts_through=excerpts_through,
        refreshed_at=preparation.finished_at,
        refreshed=preparation.states(StageState.REFRESHED),
        reused=preparation.states(StageState.REUSED, StageState.SKIPPED),
        stale=tuple(outcome.line() for outcome in preparation.degraded),
    )
