from collections.abc import Callable
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from deep_research import (
    DataFreshness,
    DeepResearchBrief,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    external_evidence_id,
)
from research import MetricFact, ScoreEvidence


@pytest.mark.unit
def test_canonicalises_its_ticker(
    score: ScoreEvidence, score_date: date, assembled_at: datetime
) -> None:
    brief = DeepResearchBrief(
        ticker="  acme ",
        name="Acme Compounders Inc.",
        as_of=score_date,
        assembled_at=assembled_at,
        score=score,
    )

    assert brief.ticker == "ACME"


@pytest.mark.unit
def test_rejects_a_naive_assembly_timestamp(score: ScoreEvidence, score_date: date) -> None:
    with pytest.raises(ValidationError, match="assembled_at must be timezone-aware"):
        DeepResearchBrief(
            ticker="ACME",
            name="Acme Compounders Inc.",
            as_of=score_date,
            assembled_at=datetime(2026, 8, 14, 18, 30),  # noqa: DTZ001 — the case under test
            score=score,
        )


@pytest.mark.unit
def test_rejects_duplicate_external_ids(
    score: ScoreEvidence,
    score_date: date,
    assembled_at: datetime,
    make_external: Callable[..., ExternalEvidence],
) -> None:
    same = make_external("https://example.test/one")

    with pytest.raises(ValidationError, match="duplicate external evidence id"):
        DeepResearchBrief(
            ticker="ACME",
            name="Acme Compounders Inc.",
            as_of=score_date,
            assembled_at=assembled_at,
            score=score,
            external=(same, same),
        )


@pytest.mark.unit
def test_rejects_an_external_item_outside_the_w_namespace(assembled_at: datetime) -> None:
    """The model rejects it, so the brief cannot smuggle one past its own check."""
    with pytest.raises(ValidationError, match=r"must start with 'W\.'"):
        ExternalEvidence(
            evidence_id="D.0000320193-26-000073",
            source_type=ExternalSourceType.SEC,
            tier=SourceTier.TIER_1_PRIMARY,
            title="A filing index",
            publisher="SEC",
            url="https://sec.gov/x",
            excerpt="Something.",
            retrieved_at=assembled_at,
        )


@pytest.mark.unit
def test_evidence_ids_span_both_namespaces(researched: DeepResearchBrief, accession: str) -> None:
    ids = researched.evidence_ids

    assert "S.growth" in ids
    assert "M.fcf_margin" in ids
    assert "F.2026-03-31" in ids
    assert f"D.{accession}" in ids
    assert f"X.{accession}.mda" in ids
    assert "E.market_cap" in ids
    assert researched.external_ids <= ids


@pytest.mark.unit
def test_deterministic_and_external_id_sets_do_not_overlap(
    researched: DeepResearchBrief,
) -> None:
    assert not (researched.deterministic_ids & researched.external_ids)


@pytest.mark.unit
def test_extractable_ids_exclude_filing_metadata(
    researched: DeepResearchBrief, accession: str
) -> None:
    """`D.` proves a filing exists; only `X.` quotes what it says."""
    assert researched.extractable_ids == frozenset({f"X.{accession}.mda"})


@pytest.mark.unit
def test_unknowns_name_the_missing_metric_and_the_unscored_line(
    brief: DeepResearchBrief,
) -> None:
    assert "M.revenue_cagr_3y" in brief.unknowns
    assert "S.growth.cagr_3y" in brief.unknowns


@pytest.mark.unit
def test_unknowns_exclude_metrics_that_have_a_value(brief: DeepResearchBrief) -> None:
    assert "M.fcf_margin" not in brief.unknowns


@pytest.mark.unit
def test_a_brief_with_no_external_evidence_is_legal(brief: DeepResearchBrief) -> None:
    """Collection has not been built, and a quiet company is a real case."""
    assert brief.external == ()
    assert brief.external_ids == frozenset()
    assert brief.external_fingerprint()


@pytest.mark.unit
def test_adding_external_evidence_moves_the_external_fingerprint(
    brief: DeepResearchBrief, researched: DeepResearchBrief
) -> None:
    assert brief.external_fingerprint() != researched.external_fingerprint()


@pytest.mark.unit
def test_adding_external_evidence_moves_the_evidence_fingerprint(
    brief: DeepResearchBrief, researched: DeepResearchBrief
) -> None:
    assert brief.evidence_fingerprint() != researched.evidence_fingerprint()


@pytest.mark.unit
def test_adding_external_evidence_leaves_the_deterministic_fingerprint_alone(
    brief: DeepResearchBrief, researched: DeepResearchBrief
) -> None:
    """The whole reason there are two hashes.

    News moving and fundamentals moving are different reasons to regenerate, and
    a single fingerprint could not tell a caller which had happened.
    """
    assert brief.deterministic_fingerprint() == researched.deterministic_fingerprint()


@pytest.mark.unit
def test_changing_a_metric_moves_the_deterministic_fingerprint(
    brief: DeepResearchBrief,
) -> None:
    moved = brief.model_copy(
        update={"facts": (MetricFact(id="M.fcf_margin", label="FCF margin", value=0.144),)}
    )

    assert moved.deterministic_fingerprint() != brief.deterministic_fingerprint()


@pytest.mark.unit
def test_changing_a_metric_moves_the_evidence_fingerprint(brief: DeepResearchBrief) -> None:
    moved = brief.model_copy(
        update={"facts": (MetricFact(id="M.fcf_margin", label="FCF margin", value=0.144),)}
    )

    assert moved.evidence_fingerprint() != brief.evidence_fingerprint()


@pytest.mark.unit
def test_changing_the_score_moves_the_deterministic_fingerprint(
    brief: DeepResearchBrief,
) -> None:
    rescored = brief.model_copy(
        update={"score": brief.score.model_copy(update={"final_score": 79.1})}
    )

    assert rescored.deterministic_fingerprint() != brief.deterministic_fingerprint()


@pytest.mark.unit
def test_reassembling_the_same_evidence_leaves_every_fingerprint_unchanged(
    researched: DeepResearchBrief,
) -> None:
    """The cache hit. Assembling twice is not new evidence."""
    again = researched.model_copy(update={"assembled_at": datetime(2026, 9, 1, 9, 0, tzinfo=UTC)})

    assert again.deterministic_fingerprint() == researched.deterministic_fingerprint()
    assert again.evidence_fingerprint() == researched.evidence_fingerprint()


@pytest.mark.unit
def test_refetching_the_same_article_leaves_the_external_fingerprint_unchanged(
    brief: DeepResearchBrief, make_external: Callable[..., ExternalEvidence]
) -> None:
    """`retrieved_at` records when we looked, not what the source says."""
    monday = brief.model_copy(
        update={"external": (make_external(retrieved_at=datetime(2026, 8, 14, 17, 0, tzinfo=UTC)),)}
    )
    tuesday = brief.model_copy(
        update={"external": (make_external(retrieved_at=datetime(2026, 8, 15, 9, 0, tzinfo=UTC)),)}
    )

    assert monday.external_fingerprint() == tuesday.external_fingerprint()


@pytest.mark.unit
def test_a_refresh_that_found_nothing_leaves_the_deterministic_fingerprint_unchanged(
    brief: DeepResearchBrief,
) -> None:
    later = brief.model_copy(
        update={
            "freshness": brief.freshness.model_copy(
                update={"refreshed_at": datetime(2026, 8, 15, 6, 0, tzinfo=UTC)}
            )
        }
    )

    assert later.deterministic_fingerprint() == brief.deterministic_fingerprint()


@pytest.mark.unit
def test_a_failed_optional_provider_does_not_move_the_deterministic_fingerprint(
    brief: DeepResearchBrief,
) -> None:
    """A provider having a bad afternoon is not a change in the company's evidence.

    Hashing `stale` would hand the same unchanged company a new cache key every
    time an optional provider timed out, so the cache would buy nothing exactly
    when it matters.
    """
    degraded = brief.model_copy(
        update={
            "freshness": brief.freshness.model_copy(
                update={"stale": ("enrichment: provider returned 429",)}
            )
        }
    )

    assert degraded.deterministic_fingerprint() == brief.deterministic_fingerprint()


@pytest.mark.unit
def test_which_stages_were_refreshed_does_not_move_the_deterministic_fingerprint(
    brief: DeepResearchBrief,
) -> None:
    """Fetching a figure and already having it are the same evidence."""
    fetched = brief.model_copy(
        update={"freshness": brief.freshness.model_copy(update={"refreshed": ("market_data",)})}
    )
    reused = brief.model_copy(
        update={"freshness": brief.freshness.model_copy(update={"reused": ("market_data",)})}
    )

    assert fetched.deterministic_fingerprint() == reused.deterministic_fingerprint()


@pytest.mark.unit
def test_a_newer_price_date_does_move_the_deterministic_fingerprint(
    brief: DeepResearchBrief,
) -> None:
    """A freshness *date* is a property of the evidence, not of the run."""
    moved = brief.model_copy(
        update={"freshness": brief.freshness.model_copy(update={"price_as_of": date(2026, 8, 15)})}
    )

    assert moved.deterministic_fingerprint() != brief.deterministic_fingerprint()


@pytest.mark.unit
def test_collection_order_is_not_evidence(
    brief: DeepResearchBrief, make_external: Callable[..., ExternalEvidence]
) -> None:
    """Which request finished first must not read as a change in the sources."""
    one = make_external("https://example.test/one")
    other = make_external("https://example.test/two")

    forwards = brief.model_copy(update={"external": (one, other)})
    backwards = brief.model_copy(update={"external": (other, one)})

    assert forwards.external_fingerprint() == backwards.external_fingerprint()


@pytest.mark.unit
def test_a_different_article_moves_the_external_fingerprint(
    brief: DeepResearchBrief, make_external: Callable[..., ExternalEvidence]
) -> None:
    one = brief.model_copy(update={"external": (make_external("https://example.test/one"),)})
    other = brief.model_copy(update={"external": (make_external("https://example.test/two"),)})

    assert one.external_fingerprint() != other.external_fingerprint()


@pytest.mark.unit
def test_the_evidence_fingerprint_composes_both_halves(researched: DeepResearchBrief) -> None:
    assert researched.evidence_fingerprint() not in {
        researched.deterministic_fingerprint(),
        researched.external_fingerprint(),
    }


@pytest.mark.unit
def test_a_brief_carries_no_selection_reason() -> None:
    """Deep research is on-demand. There is no screener reason to record."""
    assert "selection" not in DeepResearchBrief.model_fields


@pytest.mark.unit
def test_freshness_separates_what_was_refreshed_reused_and_left_stale(
    brief: DeepResearchBrief,
) -> None:
    """The three questions a reader asks of a brief that claims to be current."""
    freshness = DataFreshness(
        price_as_of=date(2026, 8, 14),
        refreshed=("market_data", "score"),
        reused=("fundamentals",),
        stale=("enrichment: provider returned 429",),
    )

    assert freshness.refreshed == ("market_data", "score")
    assert freshness.reused == ("fundamentals",)
    assert freshness.stale == ("enrichment: provider returned 429",)


@pytest.mark.unit
def test_an_external_id_built_for_one_source_type_reads_back_as_that_type() -> None:
    identifier = external_evidence_id(ExternalSourceType.COMPANY, "https://acme.test/ir")

    assert identifier.startswith("W.company.")


@pytest.mark.unit
def test_an_external_item_smuggled_past_its_own_check_is_still_rejected(
    score: ScoreEvidence,
    score_date: date,
    assembled_at: datetime,
    make_external: Callable[..., ExternalEvidence],
) -> None:
    """`model_copy` skips validation; assembling the brief revalidates the item."""
    smuggled = make_external().model_copy(update={"evidence_id": "M.revenue_growth_yoy"})

    with pytest.raises(ValidationError, match=r"must start with 'W\.'"):
        DeepResearchBrief(
            ticker="ACME",
            name="Acme Compounders Inc.",
            as_of=score_date,
            assembled_at=assembled_at,
            score=score,
            external=(smuggled,),
        )
