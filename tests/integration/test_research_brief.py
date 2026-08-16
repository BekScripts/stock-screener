"""Assembling a research brief from stored rows, and only from stored rows.

`integration`, because the whole point of the assembler is that it reads a real
database and nothing else. Mocking the session would leave the two claims these
tests exist to check — that the evidence comes from storage, and that no provider
is touched — unverified.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import TYPE_CHECKING

import httpx
import pytest

from data_access import FINAL, CompanyRepository, ScoreSnapshotRepository
from domain import (
    COMPOUNDER_V1,
    CURRENT_SCORE_VERSION,
    MarketCapSource,
    ScoringStatus,
    ValuationBasis,
    VolumeBasis,
)
from research import RankingState, SelectionReason
from stock_screener.research import DEFAULT_QUARTERS, assemble_brief, assemble_briefs

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from stock_screener.config import Settings

SCORE_DATE = date(2026, 6, 30)


@pytest.mark.integration
def test_assembles_a_brief_from_stored_rows(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert brief.ticker == "XYZ"
    assert brief.as_of == SCORE_DATE
    assert brief.facts
    assert brief.score.items


@pytest.mark.integration
def test_makes_no_provider_call_while_assembling(
    session: Session, settings: Settings, scored_company: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("assembling a brief must not reach a provider")

    monkeypatch.setattr(httpx.Client, "request", explode)
    monkeypatch.setattr(httpx.Client, "send", explode)

    assert assemble_brief(session, settings, scored_company) is not None


@pytest.mark.integration
def test_carries_the_score_version_the_brief_explains(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert brief.score.score_version == CURRENT_SCORE_VERSION


@pytest.mark.integration
def test_reads_the_requested_version_not_the_latest(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "OLD")
    seed.score(session, company_id, "OLD", final=60.0, score_version=COMPOUNDER_V1)
    seed.score(session, company_id, "OLD", final=80.0, score_version=CURRENT_SCORE_VERSION)

    brief = assemble_brief(session, settings, "OLD", score_version=COMPOUNDER_V1)

    assert brief is not None
    assert brief.score.score_version == COMPOUNDER_V1
    assert brief.score.final_score == 60.0


@pytest.mark.integration
def test_quarters_run_oldest_to_newest(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    ends = [period.period_end for period in brief.quarters]
    assert ends == sorted(ends)


@pytest.mark.integration
def test_supplies_at_most_the_requested_number_of_quarters(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "LONG", quarters=16)
    seed.score(session, company_id, "LONG")

    brief = assemble_brief(session, settings, "LONG")

    assert brief is not None
    assert len(brief.quarters) == DEFAULT_QUARTERS


@pytest.mark.integration
def test_score_history_holds_only_earlier_days_of_the_same_version(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "HIST")
    earlier = SCORE_DATE - timedelta(days=30)
    seed.score(session, company_id, "HIST", final=64.0, score_date=earlier)
    seed.score(
        session, company_id, "HIST", final=99.0, score_date=earlier, score_version=COMPOUNDER_V1
    )
    seed.score(session, company_id, "HIST", final=80.0)

    brief = assemble_brief(session, settings, "HIST")

    assert brief is not None
    assert [point.final_score for point in brief.score_history] == [64.0]
    assert [point.score_date for point in brief.score_history] == [earlier]


@pytest.mark.integration
def test_unknown_metrics_and_subscores_are_listed(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert "S.growth.growth_secondary" in brief.unknowns
    assert any(unknown.startswith("M.") for unknown in brief.unknowns)


@pytest.mark.integration
def test_an_unavailable_subscore_is_still_citable(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert "S.growth.growth_secondary" in brief.evidence_ids


@pytest.mark.integration
def test_carries_the_provenance_behind_the_score(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert brief.score.ranking_state is RankingState.PRELIMINARY
    assert brief.score.valuation_basis.value == "EV_TO_REVENUE"


@pytest.mark.integration
def test_a_final_ranking_state_survives_into_the_brief(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "ENR")
    seed.score(session, company_id, "ENR", ranking_state=FINAL)

    brief = assemble_brief(session, settings, "ENR")

    assert brief is not None
    assert brief.score.ranking_state is RankingState.FINAL


@pytest.mark.integration
def test_records_the_selection_reason(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company, selection=SelectionReason.HIDDEN_GEM)

    assert brief is not None
    assert brief.selection is SelectionReason.HIDDEN_GEM


# --- fingerprint -----------------------------------------------------------


@pytest.mark.integration
def test_the_same_stored_data_fingerprints_the_same(
    session: Session, settings: Settings, scored_company: str
) -> None:
    first = assemble_brief(session, settings, scored_company)
    second = assemble_brief(session, settings, scored_company)

    assert first is not None
    assert second is not None
    assert first.fingerprint() == second.fingerprint()


@pytest.mark.integration
def test_a_changed_score_changes_the_fingerprint(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "MOVE")
    seed.score(session, company_id, "MOVE", final=70.0)
    before = assemble_brief(session, settings, "MOVE")

    seed.score(session, company_id, "MOVE", final=71.0)
    after = assemble_brief(session, settings, "MOVE")

    assert before is not None
    assert after is not None
    assert before.fingerprint() != after.fingerprint()


@pytest.mark.integration
def test_a_restated_quarter_changes_the_fingerprint(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "REST")
    seed.score(session, company_id, "REST")
    before = assemble_brief(session, settings, "REST")

    seed.company(session, make, "REST", quarters=8, revenue=250_000_000.0)
    after = assemble_brief(session, settings, "REST")

    assert before is not None
    assert after is not None
    assert before.fingerprint() != after.fingerprint()


@pytest.mark.integration
def test_the_fingerprint_is_a_hex_digest(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert len(brief.fingerprint()) == 64
    assert set(brief.fingerprint()) <= set("0123456789abcdef")


# --- what cannot be assembled ----------------------------------------------


@pytest.mark.integration
def test_returns_none_for_an_unknown_company(session: Session, settings: Settings) -> None:
    assert assemble_brief(session, settings, "NOPE") is None


@pytest.mark.integration
def test_returns_none_for_a_company_that_was_never_scored(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    seed.company(session, make, "UNSCORED")

    assert assemble_brief(session, settings, "UNSCORED") is None


@pytest.mark.integration
def test_skips_a_company_whose_breakdown_cannot_be_read(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "BAD")
    seed.score(session, company_id, "BAD")
    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company_id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    snapshot.breakdown = {"not": "a score"}
    session.flush()

    assert assemble_brief(session, settings, "BAD") is None


@pytest.mark.integration
def test_one_unreadable_company_does_not_stop_the_others(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    good_id = seed.company(session, make, "GOOD")
    seed.score(session, good_id, "GOOD")
    bad_id = seed.company(session, make, "BAD")
    seed.score(session, bad_id, "BAD")
    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        bad_id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    snapshot.breakdown = None
    session.flush()

    briefs = assemble_briefs(
        session,
        settings,
        [("BAD", SelectionReason.TOP_RANKED), ("GOOD", SelectionReason.TOP_RANKED)],
    )

    assert [brief.ticker for brief in briefs] == ["GOOD"]


@pytest.mark.integration
def test_assembling_nothing_returns_nothing(session: Session, settings: Settings) -> None:
    assert assemble_briefs(session, settings, []) == []


@pytest.mark.integration
def test_assembles_several_companies_in_one_pass(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    for ticker in ("AAA", "BBB", "CCC"):
        company_id = seed.company(session, make, ticker)
        seed.score(session, company_id, ticker)

    briefs = assemble_briefs(
        session,
        settings,
        [(ticker, SelectionReason.TOP_RANKED) for ticker in ("AAA", "BBB", "CCC")],
    )

    assert [brief.ticker for brief in briefs] == ["AAA", "BBB", "CCC"]
    assert len({brief.fingerprint() for brief in briefs}) == 3


@pytest.mark.integration
def test_an_unscored_status_still_produces_a_brief(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # A company with no number still has a snapshot explaining why. The
    # assembler does not second-guess that; the candidate selector is what keeps
    # such a company out of a research run.
    company_id = seed.company(session, make, "BANK")
    seed.score(session, company_id, "BANK", status=ScoringStatus.UNSUPPORTED_SECTOR)

    brief = assemble_brief(session, settings, "BANK")

    assert brief is not None
    assert brief.score.scoring_status is ScoringStatus.UNSUPPORTED_SECTOR
    assert brief.score.final_score is None


@pytest.mark.integration
def test_the_stored_company_name_and_ticker_are_used(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "NAME", name="Named Holdings Inc.")
    seed.score(session, company_id, "NAME")

    brief = assemble_brief(session, settings, "name")

    assert brief is not None
    assert brief.ticker == "NAME"
    assert brief.name == "Named Holdings Inc."
    assert CompanyRepository(session).get_by_ticker("NAME") is not None


@pytest.mark.integration
def test_filing_references_are_empty_until_something_stores_them(
    session: Session, settings: Settings, scored_company: str
) -> None:
    # Nothing ingests filing metadata yet. The brief says so by carrying none,
    # rather than by reaching for a provider at assembly time.
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert brief.filings == ()


@pytest.mark.integration
def test_no_enrichment_facts_without_a_vendor_figure(
    session: Session, settings: Settings, scored_company: str
) -> None:
    brief = assemble_brief(session, settings, scored_company)

    assert brief is not None
    assert brief.enrichment is None
    assert brief.score.market_cap_source is MarketCapSource.CALCULATED


@pytest.mark.integration
def test_an_absent_provenance_column_reads_back_as_unknown(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # Rows written before the provenance columns existed leave them NULL.
    company_id = seed.company(session, make, "NULLP")
    seed.score(session, company_id, "NULLP")
    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company_id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    snapshot.valuation_basis = None
    snapshot.volume_basis = None
    session.flush()

    brief = assemble_brief(session, settings, "NULLP")

    assert brief is not None
    assert brief.score.valuation_basis is ValuationBasis.NOT_AVAILABLE
    assert brief.score.liquidity_basis is VolumeBasis.UNKNOWN


@pytest.mark.integration
def test_an_unrecognised_provenance_value_falls_back_to_unknown(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    # A value written by a future revision must not stop a brief being built.
    # It reads back as the enum's own "unknown" member, which keeps an absent
    # provenance visibly absent instead of guessed at.
    company_id = seed.company(session, make, "ODD")
    seed.score(session, company_id, "ODD")
    snapshot = ScoreSnapshotRepository(session).latest_for_company(
        company_id, score_version=CURRENT_SCORE_VERSION
    )
    assert snapshot is not None
    snapshot.market_cap_source = "SOMETHING_NEW"
    session.flush()

    brief = assemble_brief(session, settings, "ODD")

    assert brief is not None
    assert brief.score.market_cap_source is MarketCapSource.UNKNOWN


@pytest.mark.integration
def test_a_vendor_supplied_market_cap_becomes_enrichment(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> None:
    company_id = seed.company(session, make, "VEND")
    seed.score(
        session,
        company_id,
        "VEND",
        ranking_state=FINAL,
        market_cap_source=MarketCapSource.PROVIDER,
        liquidity_basis=VolumeBasis.CONSOLIDATED,
    )

    brief = assemble_brief(session, settings, "VEND")

    assert brief is not None
    assert brief.enrichment is not None
    assert {fact.id for fact in brief.enrichment.facts} == {
        "E.market_cap",
        "E.average_dollar_volume_20d",
    }
    assert "E.market_cap" in brief.evidence_ids
