"""Choosing the few companies worth spending a model call on.

The scanner exists to reduce thousands of companies to a short list. This module
reduces that short list again, because AI research is the one part of the
pipeline with a per-company cost and the one whose output nobody will read if
there is too much of it.

Three sources, in precedence order: the top of the ranking, the companies whose
score moved most, and the hidden gems. They are three questions about the same
snapshot — what is best, what is improving, what is small and good — and a
company that answers more than one is researched once, under the first reason
that claimed it.

Qualification is about evidence, not quality. A company with thin coverage or
two years of history is not a bad company; it is one nothing useful can be
written about, and asking anyway produces confident prose resting on nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from data_access import CompanyRepository, FinancialSnapshotRepository
from domain import CURRENT_SCORE_VERSION, ScoringStatus
from research import SelectionReason
from stock_screener.scoring.rankings import hidden_gems, improving_fast, top_opportunities

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from stock_screener.scoring.rankings import RankingRow

log = structlog.get_logger(__name__)

MAX_CANDIDATES = 25
"""Companies researched per run, across every source.

A ceiling on cost and on reading. Twenty-five reports is about as much as one
person will actually get through in a sitting, and a queue nobody finishes is
indistinguishable from one that was never generated.
"""

TOP_RANKED_LIMIT = 15
SCORE_MOVER_LIMIT = 5
HIDDEN_GEM_LIMIT = 5

MIN_DATA_COVERAGE = 0.60
"""Coverage a company needs before a report about it is worth writing.

Below this the score itself rests on well under two thirds of its metrics, and a
narrative built on that is mostly the model's imagination with citations
attached.
"""

MIN_QUARTERS = 6
"""Reporting periods required. Fewer cannot show a year-over-year trend and its
predecessor, which is most of what a research report is about."""

MIN_SCORE_CHANGE_30D = 10.0
"""Points a score must have gained to count as a mover."""

MIN_MOVER_SCORE = 65.0
"""A mover must also be good. A company improving from 20 to 32 has moved further
than one going from 70 to 81, and is still not worth researching yet."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One company selected for AI research.

    Attributes:
        ticker: The company's symbol.
        name: Registered company name.
        selection: Which source claimed it, and therefore why it is being read.
        final_score: The risk-adjusted score that put it here.
        data_coverage: Share of scoring metrics available, 0-1.
        ranking_state: `PRELIMINARY` or `FINAL`. Preliminary companies qualify —
            a metered provider's quota must not decide what gets researched —
            but the state caps how confident the resulting report may be.
        quarters: Stored reporting periods behind it.
        score_change_30d: Score movement over the long window, when there is
            enough history to have one.
    """

    ticker: str
    name: str
    selection: SelectionReason
    final_score: float | None
    data_coverage: float | None
    ranking_state: str
    quarters: int
    score_change_30d: float | None = None


def select_candidates(
    session: Session,
    *,
    score_version: str = CURRENT_SCORE_VERSION,
    score_date: date | None = None,
    limit: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Return the companies worth researching from the latest ranking.

    Args:
        session: Open database session.
        score_version: The formula version to read.
        score_date: The day to select from. Defaults to the most recent stored.
        limit: Hard ceiling across every source.

    Returns:
        Candidates in precedence order: top-ranked, then movers, then hidden
        gems. Empty when nothing has been scored, and short of `limit` whenever
        the sources genuinely have less to offer — padding it from a fourth
        source would defeat the point of having three.
    """
    pools = (
        (
            SelectionReason.TOP_RANKED,
            top_opportunities(
                session, score_version=score_version, score_date=score_date, limit=None
            ),
            TOP_RANKED_LIMIT,
        ),
        (
            SelectionReason.SCORE_MOVER,
            _movers(session, score_version=score_version, score_date=score_date),
            SCORE_MOVER_LIMIT,
        ),
        (
            SelectionReason.HIDDEN_GEM,
            hidden_gems(session, score_version=score_version, score_date=score_date, limit=None),
            HIDDEN_GEM_LIMIT,
        ),
    )

    every_row = [row for _, rows, _ in pools for row in rows]
    quarters = _quarter_counts(session, every_row)

    chosen: list[Candidate] = []
    claimed: set[str] = set()

    for reason, rows, slots in pools:
        taken = 0
        for row in rows:
            if taken >= slots or len(chosen) >= limit:
                break
            if row.ticker in claimed or not _qualifies(row, quarters.get(row.ticker, 0)):
                continue
            claimed.add(row.ticker)
            chosen.append(_candidate(row, reason, quarters.get(row.ticker, 0)))
            taken += 1

    log.info(
        "research candidates selected",
        total=len(chosen),
        counts={
            reason.value: sum(1 for candidate in chosen if candidate.selection is reason)
            for reason, _, _ in pools
        },
    )
    return chosen


def _movers(session: Session, *, score_version: str, score_date: date | None) -> list[RankingRow]:
    """Return companies whose score rose materially and which are already good."""
    rows = improving_fast(
        session,
        score_version=score_version,
        score_date=score_date,
        min_change=MIN_SCORE_CHANGE_30D,
        limit=None,
    )
    return [
        row for row in rows if row.final_score is not None and row.final_score >= MIN_MOVER_SCORE
    ]


def _qualifies(row: RankingRow, quarters: int) -> bool:
    """Whether enough is known about a company to write about it.

    `ranking_state` is deliberately not consulted. A `PRELIMINARY` company is one
    a metered provider has not verified yet, which is a statement about a quota
    rather than about the company, and letting it decide the research queue would
    hand a vendor's rate limit control over what gets read.
    """
    return (
        row.scoring_status == ScoringStatus.SCORED.value
        and row.data_coverage is not None
        and row.data_coverage >= MIN_DATA_COVERAGE
        and quarters >= MIN_QUARTERS
    )


def _candidate(row: RankingRow, reason: SelectionReason, quarters: int) -> Candidate:
    """Flatten one ranking row into a selected candidate."""
    return Candidate(
        ticker=row.ticker,
        name=row.name,
        selection=reason,
        final_score=row.final_score,
        data_coverage=row.data_coverage,
        ranking_state=row.ranking_state,
        quarters=quarters,
        score_change_30d=row.score_change_30d,
    )


def _quarter_counts(session: Session, rows: Sequence[RankingRow]) -> dict[str, int]:
    """Return how many stored reporting periods each ranked company has."""
    if not rows:
        return {}

    wanted = {row.ticker for row in rows}
    identifiers = {
        company.ticker: company.id
        for company in CompanyRepository(session).list_all()
        if company.ticker in wanted
    }
    counts = FinancialSnapshotRepository(session).period_counts(list(identifiers.values()))
    return {ticker: counts.get(company_id, 0) for ticker, company_id in identifiers.items()}
