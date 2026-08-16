"""Which companies a research run spends its budget on.

`integration`: the selector is three queries over the ranking views and one over
the fundamentals table, and the interesting behaviour — deduplication across
views, and disqualification on evidence rather than on quality — only exists once
those views return real rows.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import pytest

from data_access import FINAL
from domain import ScoringStatus
from research import SelectionReason
from stock_screener.research import (
    MAX_CANDIDATES,
    MIN_QUARTERS,
    Candidate,
    select_candidates,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed

SCORE_DATE = date(2026, 6, 30)


def _tickers(candidates: list[Candidate]) -> list[str]:
    return [candidate.ticker for candidate in candidates]


@pytest.mark.integration
def test_selects_nothing_when_nothing_has_been_scored(session: Session) -> None:
    assert select_candidates(session) == []


@pytest.mark.integration
def test_selects_the_top_of_the_ranking(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index, ticker in enumerate(("AAA", "BBB", "CCC")):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=80.0 - index)

    candidates = select_candidates(session)

    assert _tickers(candidates) == ["AAA", "BBB", "CCC"]
    assert all(c.selection is SelectionReason.TOP_RANKED for c in candidates)


@pytest.mark.integration
def test_takes_at_most_fifteen_from_the_top(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index in range(20):
        ticker = f"T{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=90.0 - index)

    candidates = select_candidates(session)
    top = [c for c in candidates if c.selection is SelectionReason.TOP_RANKED]

    assert len(top) == 15


@pytest.mark.integration
def test_never_exceeds_the_run_cap(session: Session, make: type[Make], seed: type[Seed]) -> None:
    for index in range(40):
        ticker = f"C{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=90.0 - index * 0.5)

    candidates = select_candidates(session)

    assert len(candidates) <= MAX_CANDIDATES


@pytest.mark.integration
def test_respects_a_lower_explicit_limit(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index in range(10):
        ticker = f"L{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=90.0 - index)

    assert len(select_candidates(session, limit=3)) == 3


@pytest.mark.integration
def test_a_company_is_selected_once_under_its_first_reason(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    # A small, fast-growing, high-scoring company qualifies as both a top
    # opportunity and a hidden gem. It should be researched once.
    company_id = seed.company(session, make, "GEM")
    seed.score(session, company_id, "GEM", final=88.0, market_cap=900_000_000.0)

    candidates = select_candidates(session)

    assert _tickers(candidates) == ["GEM"]
    assert candidates[0].selection is SelectionReason.TOP_RANKED


@pytest.mark.integration
def test_fills_the_gem_slots_with_companies_the_top_did_not_claim(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index in range(16):
        ticker = f"B{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=95.0 - index, market_cap=20_000_000_000.0)

    company_id = seed.company(session, make, "SMALL")
    seed.score(session, company_id, "SMALL", final=72.0, market_cap=900_000_000.0)

    candidates = select_candidates(session)
    gems = [c for c in candidates if c.selection is SelectionReason.HIDDEN_GEM]

    assert _tickers(gems) == ["SMALL"]


# --- qualification ---------------------------------------------------------


@pytest.mark.integration
def test_excludes_a_company_below_the_coverage_floor(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    thin_id = seed.company(session, make, "THIN")
    seed.score(session, thin_id, "THIN", final=95.0, coverage=0.55)
    full_id = seed.company(session, make, "FULL")
    seed.score(session, full_id, "FULL", final=70.0, coverage=1.0)

    assert _tickers(select_candidates(session)) == ["FULL"]


@pytest.mark.integration
def test_includes_a_company_exactly_on_the_coverage_floor(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "EDGE")
    seed.score(session, company_id, "EDGE", coverage=0.60)

    assert _tickers(select_candidates(session)) == ["EDGE"]


@pytest.mark.integration
def test_excludes_a_company_without_enough_reporting_history(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    short_id = seed.company(session, make, "SHORT", quarters=MIN_QUARTERS - 1)
    seed.score(session, short_id, "SHORT", final=95.0)
    long_id = seed.company(session, make, "LONG", quarters=MIN_QUARTERS)
    seed.score(session, long_id, "LONG", final=70.0)

    assert _tickers(select_candidates(session)) == ["LONG"]


@pytest.mark.integration
def test_excludes_a_company_that_was_not_scored(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    bank_id = seed.company(session, make, "BANK")
    seed.score(session, bank_id, "BANK", status=ScoringStatus.UNSUPPORTED_SECTOR)

    assert select_candidates(session) == []


@pytest.mark.integration
def test_a_preliminary_company_still_qualifies(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "PREL")
    seed.score(session, company_id, "PREL")

    candidates = select_candidates(session)

    assert _tickers(candidates) == ["PREL"]
    assert candidates[0].ranking_state == "PRELIMINARY"


@pytest.mark.integration
def test_an_enriched_company_qualifies_the_same_way(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "ENR")
    seed.score(session, company_id, "ENR", ranking_state=FINAL)

    assert select_candidates(session)[0].ranking_state == FINAL


# --- movers ----------------------------------------------------------------


@pytest.mark.integration
def test_selects_a_company_whose_score_rose_materially(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index in range(15):
        ticker = f"H{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=95.0 - index * 0.1)

    mover_id = seed.company(session, make, "MOVE")
    seed.score(session, mover_id, "MOVE", final=66.0, score_date=SCORE_DATE - timedelta(days=30))
    seed.score(session, mover_id, "MOVE", final=80.0)

    candidates = select_candidates(session)
    movers = [c for c in candidates if c.selection is SelectionReason.SCORE_MOVER]

    assert _tickers(movers) == ["MOVE"]
    assert movers[0].score_change_30d == 14.0


@pytest.mark.integration
def test_ignores_a_small_improvement(session: Session, make: type[Make], seed: type[Seed]) -> None:
    for index in range(15):
        ticker = f"H{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=95.0 - index * 0.1)

    company_id = seed.company(session, make, "CREEP")
    seed.score(session, company_id, "CREEP", final=74.0, score_date=SCORE_DATE - timedelta(days=30))
    seed.score(session, company_id, "CREEP", final=80.0)

    candidates = select_candidates(session)

    assert not [c for c in candidates if c.selection is SelectionReason.SCORE_MOVER]


@pytest.mark.integration
def test_ignores_a_large_improvement_from_a_low_base(
    session: Session, make: type[Make], seed: type[Seed]
) -> None:
    for index in range(15):
        ticker = f"H{index:02d}"
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker, final=95.0 - index * 0.1)

    company_id = seed.company(session, make, "LOW")
    seed.score(session, company_id, "LOW", final=20.0, score_date=SCORE_DATE - timedelta(days=30))
    seed.score(session, company_id, "LOW", final=40.0)

    candidates = select_candidates(session)

    assert not [c for c in candidates if c.selection is SelectionReason.SCORE_MOVER]


@pytest.mark.integration
def test_no_history_means_no_movers(session: Session, make: type[Make], seed: type[Seed]) -> None:
    # A database with a single scoring day cannot know what moved. The slots go
    # unfilled rather than being padded from another view.
    company_id = seed.company(session, make, "ONLY")
    seed.score(session, company_id, "ONLY")

    candidates = select_candidates(session)

    assert not [c for c in candidates if c.selection is SelectionReason.SCORE_MOVER]
    assert candidates[0].score_change_30d is None
