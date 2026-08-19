"""Collecting current external evidence, with a fake search provider.

`integration`: the collector reads a prepared brief out of a database, so these
exercise the real join between deterministic state and `W.` evidence. No network
call is made — `MockExternalResearch` serves fixtures — and there is no research
provider anywhere in this path.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from api_clients import MockExternalResearch
from data_access import FilingRepository
from deep_research import ExternalSourceType, SourceTier
from domain import ExternalSearchResult, Filing
from stock_screener.deep_research import (
    MAX_MARKET_REACTION_ITEMS,
    EventClass,
    Rejection,
    assemble_deep_brief,
    build_queries,
    classify_event,
    collect_external_evidence,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from conftest import Make, Seed
    from deep_research import DeepResearchBrief
    from stock_screener.config import Settings

TICKER = "ACME"
NOW = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
ACCESSION = "0000320193-26-000073"

BODY = (
    "Acme Corporation reported third-quarter revenue of $412 million, up 38% "
    "year over year, and raised its full-year guidance on continued platform demand."
)

RECENT = TODAY - timedelta(days=3)
"""Default publication date, a distinct sentinel so `published=None` can mean
"the vendor supplied no date" — a case the collector must handle honestly."""


def _result(
    url: str,
    title: str,
    *,
    published: date | None = RECENT,
    snippet: str = BODY,
    publisher: str = "",
) -> ExternalSearchResult:
    """One search hit, defaulting to a well-formed recent article."""
    return ExternalSearchResult(
        title=title,
        url=url,
        snippet=snippet,
        published_at=published,
        publisher=publisher,
    )


@pytest.fixture
def prepared(
    session: Session, settings: Settings, make: type[Make], seed: type[Seed]
) -> DeepResearchBrief:
    """A scored company with one stored filing, ready for external collection."""
    company_id = seed.company(session, make, TICKER)
    seed.score(session, company_id, TICKER)
    FilingRepository(session).upsert_filings(
        company_id,
        [
            Filing(
                accession=ACCESSION,
                form="10-Q",
                filed=date(2026, 5, 2),
                period_end=date(2026, 3, 31),
                url="https://www.sec.gov/Archives/edgar/data/1/x.htm",
            )
        ],
    )
    session.flush()
    brief = assemble_deep_brief(session, settings, TICKER)
    assert brief is not None
    return brief


def _collect(
    settings: Settings, brief: DeepResearchBrief, results: list[ExternalSearchResult], **kwargs: Any
) -> Any:
    """Run collection over a fixed result set served for every query."""
    provider = MockExternalResearch({"": results}, **kwargs)
    return collect_external_evidence(provider, settings, brief, now=NOW)


@pytest.mark.integration
def test_accepts_a_well_formed_reputable_source(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")]
    )

    assert len(report.evidence) == 1
    item = report.evidence[0]
    assert item.tier is SourceTier.TIER_2_REPUTABLE
    assert item.source_type is ExternalSourceType.NEWS
    assert item.ticker == TICKER
    assert item.evidence_id.startswith("W.news.")


@pytest.mark.integration
def test_the_evidence_id_is_stable_across_collections(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The same article tomorrow must not read as a different source."""
    results = [_result("https://reuters.com/a", "Acme raises guidance")]

    first = _collect(settings, prepared, results)
    later = collect_external_evidence(
        MockExternalResearch({"": results}),
        settings,
        prepared,
        now=NOW + timedelta(days=1),
    )

    assert first.evidence[0].evidence_id == later.evidence[0].evidence_id


@pytest.mark.integration
def test_the_id_ignores_tracking_parameters_and_www(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    plain = _collect(settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")])
    tracked = _collect(
        settings,
        prepared,
        [_result("https://www.reuters.com/a?utm_source=news", "Acme raises guidance")],
    )

    assert plain.evidence[0].evidence_id == tracked.evidence[0].evidence_id


@pytest.mark.integration
def test_a_denied_source_is_rejected_and_named(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings, prepared, [_result("https://reddit.com/r/stocks/x", "ACME to the moon")]
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.DENIED_SOURCE


@pytest.mark.integration
def test_an_unrecognised_domain_is_rejected(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(settings, prepared, [_result("https://seo-farm.example/x", "Acme news")])

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.UNTRUSTED_SOURCE


@pytest.mark.integration
def test_material_older_than_the_window_is_rejected(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    stale = TODAY - timedelta(days=settings.external_research_window_days + 30)

    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme in 2024", published=stale)]
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.OUT_OF_WINDOW


@pytest.mark.integration
def test_an_undated_source_is_kept_rather_than_guessed_at(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """A missing date is recorded as missing. Inventing one would be worse."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/business/acme-wins-contract",
                "Acme wins contract",
                published=None,
            )
        ],
    )

    assert len(report.evidence) == 1
    assert report.evidence[0].published_at is None


@pytest.mark.integration
def test_a_web_copy_of_a_stored_filing_is_rejected(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """`X.` is authoritative for filings; a duplicate web copy adds nothing."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                f"https://www.sec.gov/Archives/edgar/data/1/{ACCESSION.replace('-', '')}/x.htm",
                "Acme 10-Q filing",
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.SEC_DUPLICATE


@pytest.mark.integration
def test_commentary_about_a_filing_is_not_treated_as_the_filing(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Reporting on a filing is a different thing from the filing, and often useful."""
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "What Acme's 10-Q reveals")]
    )

    assert len(report.evidence) == 1


@pytest.mark.integration
def test_the_same_article_under_two_links_is_kept_once(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises guidance"),
            _result("https://www.reuters.com/a/?utm_campaign=x", "Acme raises guidance"),
        ],
    )

    assert len(report.evidence) == 1
    assert report.unique == 1
    assert report.duplicates == report.raw - 1


@pytest.mark.integration
def test_one_event_reported_by_five_outlets_is_kept_once(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The duplicate-event flooding the caps exist to prevent."""
    headline = "Acme Corporation raises full-year guidance after strong third quarter"
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", headline),
            _result("https://cnbc.com/b", headline),
            _result("https://wsj.com/c", headline),
            _result("https://barrons.com/d", headline),
            _result("https://forbes.com/e", headline),
        ],
    )

    assert len(report.evidence) == 1
    assert sum(1 for item in report.rejected if item.reason is Rejection.DUPLICATE_EVENT) == 4


@pytest.mark.integration
def test_different_events_all_survive(settings: Settings, prepared: DeepResearchBrief) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises full-year guidance"),
            _result("https://cnbc.com/b", "Acme names Jane Roe chief financial officer"),
            _result("https://wsj.com/c", "Acme acquires Bolt Systems for $300 million"),
            _result("https://barrons.com/d", "Regulator opens inquiry into Acme billing"),
        ],
    )

    assert len(report.evidence) == 4


@pytest.mark.integration
def test_one_publisher_cannot_flood_the_set(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises full-year guidance"),
            _result("https://reuters.com/b", "Acme names Jane Roe chief financial officer"),
            _result("https://reuters.com/c", "Acme acquires Bolt Systems"),
            _result("https://reuters.com/d", "Regulator opens inquiry into Acme billing"),
            _result("https://reuters.com/e", "Acme opens a plant in Ohio"),
        ],
    )

    assert len(report.evidence) == settings.external_research_max_per_domain
    assert any(item.reason is Rejection.DOMAIN_CAP for item in report.rejected)


@pytest.mark.integration
def test_the_item_cap_is_enforced(settings: Settings, prepared: DeepResearchBrief) -> None:
    # Each headline is a genuinely different event, or the duplicate-event
    # filter would collapse them and this would be testing that instead.
    events = [
        "Acme raises full-year guidance",
        "Acme names Jane Roe chief financial officer",
        "Acme acquires Bolt Systems",
        "Regulator opens inquiry into Acme billing",
        "Acme opens a manufacturing plant in Ohio",
        "Acme wins a defence supply contract",
        "Acme prices a convertible note offering",
        "Acme settles patent litigation with Northwind",
        "Acme discontinues its legacy hardware line",
        "Acme partners with Vertex on cloud migration",
        "Acme announces a share repurchase programme",
        "Acme reports a security breach affecting customers",
        "Acme expands into the Japanese market",
        "Acme appoints two new independent directors",
        "Acme cuts two hundred roles in restructuring",
        "Acme signs a distribution deal with Harbour Group",
        "Acme receives regulatory clearance for its sensor",
        "Acme divests its consumer analytics unit",
    ]
    hosts = [
        "reuters.com",
        "cnbc.com",
        "wsj.com",
        "barrons.com",
        "forbes.com",
        "fortune.com",
        "ft.com",
        "bloomberg.com",
        "apnews.com",
        "axios.com",
        "npr.org",
        "cnn.com",
        "nbcnews.com",
        "cbsnews.com",
        "usatoday.com",
        "latimes.com",
        "politico.com",
        "economist.com",
    ]
    results = [
        _result(f"https://{host}/x", title) for host, title in zip(hosts, events, strict=True)
    ]

    report = _collect(settings, prepared, results)

    assert len(report.evidence) == settings.external_research_max_items
    assert any(item.reason is Rejection.ITEM_CAP for item in report.rejected)


@pytest.mark.integration
def test_a_headline_with_no_quotable_text_is_rejected(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme up", snippet="Acme up 2%.")]
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.THIN_EXCERPT


@pytest.mark.integration
def test_the_excerpt_is_the_sources_words_and_is_bounded(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The collector stores evidence. Rewriting it into a claim happens nowhere here."""
    long_body = BODY + " " + ("Additional reporting follows. " * 200)

    report = _collect(
        settings,
        prepared,
        [_result("https://reuters.com/a", "Acme raises guidance", snippet=long_body)],
    )

    item = report.evidence[0]
    assert item.excerpt.startswith("Acme Corporation reported third-quarter revenue")
    assert len(item.excerpt) <= 2000


@pytest.mark.integration
def test_best_tier_wins_when_the_set_is_full(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Ordering is by tier then recency, so a full set is the best available."""
    results = [
        _result("https://tomshardware.com/x", "Acme trade coverage item one"),
        _result("https://businesswire.com/y", "Acme Corporation announces quarterly dividend"),
    ]

    report = _collect(settings, prepared, results)

    assert report.evidence[0].tier is SourceTier.TIER_1_PRIMARY


@pytest.mark.integration
def test_provider_ordering_does_not_change_the_accepted_set(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """A vendor reshuffling its ranking is not new evidence."""
    results = [
        _result("https://reuters.com/a", "Acme raises full-year guidance"),
        _result("https://cnbc.com/b", "Acme names Jane Roe chief financial officer"),
        _result("https://wsj.com/c", "Acme acquires Bolt Systems"),
    ]

    forwards = _collect(settings, prepared, list(results))
    backwards = _collect(settings, prepared, list(reversed(results)))

    assert [item.evidence_id for item in forwards.evidence] == [
        item.evidence_id for item in backwards.evidence
    ]


@pytest.mark.integration
def test_a_search_failure_degrades_rather_than_raising(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """External evidence is optional; a vendor outage must not cost a brief."""
    report = _collect(settings, prepared, [], failing="provider unavailable")

    assert report.evidence == ()
    assert report.failures
    assert len(report.failures) == len(report.queries)


@pytest.mark.integration
def test_every_query_is_issued(settings: Settings, prepared: DeepResearchBrief) -> None:
    provider = MockExternalResearch({"": []})

    collect_external_evidence(provider, settings, prepared, now=NOW)

    assert provider.queries == list(build_queries(TICKER, prepared.name))


@pytest.mark.integration
def test_queries_name_the_company_and_ticker(prepared: DeepResearchBrief) -> None:
    queries = build_queries(TICKER, prepared.name)

    assert all(TICKER in query for query in queries)
    assert len(set(queries)) == len(queries)


@pytest.mark.integration
def test_external_evidence_is_optional_and_absent_by_default(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Deterministic preparation must never depend on a search vendor."""
    brief = assemble_deep_brief(session, settings, TICKER)

    assert brief is not None
    assert brief.external == ()
    assert brief.external_fingerprint() == prepared.external_fingerprint()


@pytest.mark.integration
def test_attaching_evidence_moves_the_external_and_combined_fingerprints(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")]
    )

    withevidence = assemble_deep_brief(session, settings, TICKER, external=report.evidence)

    assert withevidence is not None
    assert withevidence.external_fingerprint() != prepared.external_fingerprint()
    assert withevidence.evidence_fingerprint() != prepared.evidence_fingerprint()


@pytest.mark.integration
def test_attaching_evidence_leaves_the_deterministic_fingerprint_alone(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Web evidence never touches the deterministic layer, fingerprint included."""
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")]
    )

    withevidence = assemble_deep_brief(session, settings, TICKER, external=report.evidence)

    assert withevidence is not None
    assert withevidence.deterministic_fingerprint() == prepared.deterministic_fingerprint()


@pytest.mark.integration
def test_recollecting_the_same_sources_leaves_the_external_fingerprint_unchanged(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Only `retrieved_at` differs between the two runs, and it is not hashed."""
    results = [_result("https://reuters.com/a", "Acme raises guidance")]
    monday = _collect(settings, prepared, results)
    tuesday = collect_external_evidence(
        MockExternalResearch({"": results}), settings, prepared, now=NOW + timedelta(days=1)
    )

    first = assemble_deep_brief(session, settings, TICKER, external=monday.evidence)
    second = assemble_deep_brief(session, settings, TICKER, external=tuesday.evidence)

    assert first is not None
    assert second is not None
    assert first.external_fingerprint() == second.external_fingerprint()
    assert first.evidence_fingerprint() == second.evidence_fingerprint()


@pytest.mark.integration
def test_a_changed_excerpt_moves_the_external_fingerprint(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """What a source says is evidence; a material change to it is a new brief."""
    original = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")]
    )
    revised = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/a",
                "Acme raises guidance",
                snippet=BODY + " The company later corrected the figure to $410 million.",
            )
        ],
    )

    first = assemble_deep_brief(session, settings, TICKER, external=original.evidence)
    second = assemble_deep_brief(session, settings, TICKER, external=revised.evidence)

    assert first is not None
    assert second is not None
    assert first.external_fingerprint() != second.external_fingerprint()


@pytest.mark.integration
def test_provider_ordering_does_not_move_the_external_fingerprint(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    results = [
        _result("https://reuters.com/a", "Acme raises full-year guidance"),
        _result("https://cnbc.com/b", "Acme names Jane Roe chief financial officer"),
    ]
    forwards = _collect(settings, prepared, list(results))
    backwards = _collect(settings, prepared, list(reversed(results)))

    first = assemble_deep_brief(session, settings, TICKER, external=forwards.evidence)
    second = assemble_deep_brief(session, settings, TICKER, external=backwards.evidence)

    assert first is not None
    assert second is not None
    assert first.external_fingerprint() == second.external_fingerprint()


@pytest.mark.integration
def test_brief_assembly_makes_no_search_call(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Collection is a network stage; assembly is a pure read, and stays one."""
    provider = MockExternalResearch(
        {"": [_result("https://reuters.com/a", "Acme raises guidance")]}
    )
    report = collect_external_evidence(provider, settings, prepared, now=NOW)
    issued = len(provider.queries)

    assemble_deep_brief(session, settings, TICKER, external=report.evidence)

    assert len(provider.queries) == issued


@pytest.mark.integration
def test_the_collected_evidence_resolves_inside_the_brief(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Every W. id must be citable, or a later claim would dangle."""
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises full-year guidance"),
            _result("https://businesswire.com/b", "Acme declares quarterly dividend"),
        ],
    )

    brief = assemble_deep_brief(session, settings, TICKER, external=report.evidence)

    assert brief is not None
    assert brief.external_ids == {item.evidence_id for item in report.evidence}
    assert brief.external_ids <= brief.evidence_ids
    assert not (brief.external_ids & brief.deterministic_ids)


@pytest.mark.integration
def test_no_research_model_is_reachable_from_the_collection_path() -> None:
    """Phase 6C collects evidence. It must not be able to call a model."""
    import stock_screener.deep_research.collection as module

    assert not hasattr(module, "build_research_provider")
    assert not hasattr(module, "research_company")
    assert not hasattr(module, "ResearchProvider")
    assert "anthropic" not in (module.__doc__ or "").lower()


@pytest.mark.integration
def test_a_company_home_page_is_not_evidence(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Generic company context is what the deterministic and SEC layers already give."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://acme.com/",
                "Acme Corporation -- building industrial software",
                published=None,
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.LANDING_PAGE


@pytest.mark.integration
def test_an_investor_relations_archive_page_is_not_evidence(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/quarterly-results",
                "Acme quarterly results",
                published=None,
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.LANDING_PAGE


@pytest.mark.integration
def test_a_dated_story_at_a_shallow_path_is_still_evidence(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The rule needs both signals, or real stories on short URLs would be lost."""
    report = _collect(
        settings, prepared, [_result("https://reuters.com/a", "Acme raises full-year guidance")]
    )

    assert len(report.evidence) == 1


@pytest.mark.integration
def test_an_undated_sec_result_is_refused_rather_than_treated_as_current(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Undated must never read as recent. The filing is already `D.`/`X.` anyway."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://www.sec.gov/Archives/edgar/data/723125/000110465999075940/ex99.htm",
                "MICRON TECHNOLOGY INC - Form 8-K",
                published=None,
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.OUT_OF_WINDOW
    assert "accession year 1999" in report.rejected[0].detail


@pytest.mark.integration
def test_an_undated_sec_result_with_no_accession_is_refused_as_undated(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://www.sec.gov/news/statement/some-statement", "SEC statement", published=None
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.MISSING_DATE


@pytest.mark.integration
def test_a_dated_sec_result_inside_the_window_is_still_accepted(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The rule refuses undated material, not SEC material."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://www.sec.gov/Archives/edgar/data/999/000099900026000001/ex99.htm",
                "Acme announces a new credit facility",
                published=TODAY - timedelta(days=10),
            )
        ],
    )

    assert len(report.evidence) == 1
    assert report.evidence[0].source_type is ExternalSourceType.SEC


@pytest.mark.integration
def test_an_undated_non_sec_source_is_unaffected_by_the_sec_date_rule(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Only regulator material is held to the stricter standard."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/business/acme-wins-contract",
                "Acme wins contract",
                published=None,
            )
        ],
    )

    assert len(report.evidence) == 1


@pytest.mark.integration
def test_one_bad_url_returned_by_four_queries_is_rejected_once(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The accounting defect: four copies of one aggregator read as four rejections."""
    denied = _result("https://seekingalpha.com/article/1-acme", "Acme deep dive")
    provider = MockExternalResearch({"": [denied]})

    report = collect_external_evidence(provider, settings, prepared, now=NOW)

    assert report.raw == 4
    assert report.unique == 1
    assert report.duplicates == 3
    assert len(report.rejected) == 1
    assert report.rejected[0].reason is Rejection.DENIED_SOURCE


@pytest.mark.integration
def test_rejection_counts_are_over_unique_documents(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result("https://seo-farm.example/a", "Acme one"),
            _result("https://seo-farm.example/a?utm_source=x", "Acme one"),
            _result("https://www.seo-farm.example/a/", "Acme one"),
        ],
    )

    assert report.unique == 1
    assert report.counts() == {Rejection.UNTRUSTED_SOURCE.value: 1}


@pytest.mark.integration
def test_duplicate_raw_hits_do_not_change_the_external_fingerprint(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    """A provider repeating itself is not new evidence."""
    once = _collect(settings, prepared, [_result("https://reuters.com/a", "Acme raises guidance")])
    thrice = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises guidance"),
            _result("https://www.reuters.com/a/", "Acme raises guidance"),
            _result("https://reuters.com/a?utm_campaign=z", "Acme raises guidance"),
        ],
    )

    first = assemble_deep_brief(session, settings, TICKER, external=once.evidence)
    second = assemble_deep_brief(session, settings, TICKER, external=thrice.evidence)

    assert first is not None
    assert second is not None
    assert first.external_fingerprint() == second.external_fingerprint()


@pytest.mark.integration
def test_removing_an_evidence_item_moves_the_external_fingerprint(
    session: Session, settings: Settings, prepared: DeepResearchBrief
) -> None:
    both = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme raises full-year guidance"),
            _result("https://cnbc.com/b", "Acme names Jane Roe chief financial officer"),
        ],
    )

    full = assemble_deep_brief(session, settings, TICKER, external=both.evidence)
    trimmed = assemble_deep_brief(session, settings, TICKER, external=both.evidence[:1])

    assert full is not None
    assert trimmed is not None
    assert full.external_fingerprint() != trimmed.external_fingerprint()
    assert full.evidence_fingerprint() != trimmed.evidence_fingerprint()
    assert full.deterministic_fingerprint() == trimmed.deterministic_fingerprint()


@pytest.mark.integration
def test_a_daily_market_roundup_is_not_company_evidence(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Names the company in a list of tickers, says nothing about the business."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://investors.com/market-trend/stock-market-today/x",
                "Stock Market Today: Dow Rises As GM Surges; Acme, Sandisk Rally",
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.MARKET_ROUNDUP


@pytest.mark.integration
def test_a_story_naming_two_companies_is_still_evidence(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The rule targets market columns, not stories that happen to involve a partner."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/business/acme-vertex-deal",
                "Acme and Vertex sign a multi-year cloud agreement",
            )
        ],
    )

    assert len(report.evidence) == 1


@pytest.mark.integration
def test_an_article_about_another_company_is_rejected(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Impeccable source, wrong subject — the failure a tier check cannot catch."""
    report = _collect(
        settings,
        prepared,
        [
            _result(
                "https://reuters.com/legal/ftc-probes-epic-systems",
                "FTC probes health records giant Epic Systems, sources say",
            )
        ],
    )

    assert report.evidence == ()
    assert report.rejected[0].reason is Rejection.OFF_TOPIC


def _reaction(host: str, slug: str, title: str, *, days: int) -> ExternalSearchResult:
    """A market-reaction story from a given publisher and age."""
    return _result(f"https://{host}/{slug}", title, published=TODAY - timedelta(days=days))


@pytest.mark.integration
def test_market_reaction_evidence_is_capped(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Seven accounts of one price move are seven citations pointing at one fact."""
    report = _collect(
        settings,
        prepared,
        [
            _reaction("reuters.com", "a", "Acme shares jump 12% after results", days=1),
            _reaction("cnbc.com", "b", "Acme falls 5% in premarket trading", days=2),
            _reaction("barrons.com", "c", "Here's What Can End Acme's Stock Pain", days=3),
            _reaction("forbes.com", "d", "Acme Stock Gets Big Price-Target Hike", days=4),
            _reaction("cnn.com", "e", "Acme stock dives as sector sell-off deepens", days=5),
        ],
    )

    assert len(report.evidence) == MAX_MARKET_REACTION_ITEMS
    assert sum(1 for item in report.rejected if item.reason is Rejection.REACTION_CAP) == 3


@pytest.mark.integration
def test_the_cap_keeps_the_best_sourced_and_newest_reaction_pieces(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _reaction("tomshardware.com", "a", "Acme shares slip on trade coverage", days=1),
            _reaction("reuters.com", "b", "Acme shares jump 12% after results", days=9),
            _reaction("cnbc.com", "c", "Acme stock dives amid sector rout", days=2),
        ],
    )

    publishers = {item.publisher for item in report.evidence}
    assert publishers == {"reuters.com", "cnbc.com"}


@pytest.mark.integration
def test_business_developments_are_unaffected_by_the_reaction_cap(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The cap must not touch the evidence the set exists to carry."""
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme acquires Bolt Systems for $300 million"),
            _result("https://cnbc.com/b", "Acme signs a multi-year supply agreement with Vertex"),
            _result("https://wsj.com/c", "Acme commits $2 billion to a new plant"),
            _result("https://ft.com/d", "Acme names Jane Roe chief financial officer"),
            _result("https://barrons.com/e", "Regulator clears Acme's sensor for sale"),
        ],
    )

    assert len(report.evidence) == 5
    assert not any(item.reason is Rejection.REACTION_CAP for item in report.rejected)


@pytest.mark.integration
def test_earnings_and_guidance_coverage_is_unaffected_by_the_reaction_cap(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/a", "Acme forecasts strong quarterly results"),
            _result("https://cnbc.com/b", "Acme reported record revenue for the quarter"),
            _result("https://wsj.com/c", "Acme raises full-year guidance"),
        ],
    )

    assert len(report.evidence) == 3


@pytest.mark.integration
def test_a_mixed_set_keeps_the_developments_and_trims_the_reactions(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """The MU shape: real events plus a pile of coverage about one price move."""
    report = _collect(
        settings,
        prepared,
        [
            _result("https://reuters.com/deal", "Acme signs a multi-year supply agreement"),
            _result("https://wsj.com/capex", "Acme commits $2 billion to a new plant"),
            _result("https://ft.com/results", "Acme forecasts strong quarterly results"),
            _reaction("cnbc.com", "a", "Acme shares jump 12% after results", days=1),
            _reaction("barrons.com", "b", "Acme stock dives amid sector rout", days=2),
            _reaction("cnn.com", "c", "Here's What Can End Acme's Stock Pain", days=3),
            _reaction("forbes.com", "d", "Acme Stock Gets Big Price-Target Hike", days=4),
        ],
    )

    classes = [classify_event(item.title) for item in report.evidence]
    assert classes.count(EventClass.MARKET_REACTION) == MAX_MARKET_REACTION_ITEMS
    assert len(report.evidence) == 5


@pytest.mark.integration
def test_collection_is_deterministic_across_runs(
    settings: Settings, prepared: DeepResearchBrief
) -> None:
    """Same input, same accepted set, in the same order."""
    results = [
        _result("https://reuters.com/a", "Acme signs a multi-year supply agreement"),
        _reaction("cnbc.com", "b", "Acme shares jump 12% after results", days=1),
        _reaction("barrons.com", "c", "Acme stock dives amid sector rout", days=2),
        _reaction("cnn.com", "d", "Here's What Can End Acme's Stock Pain", days=3),
    ]

    first = _collect(settings, prepared, list(results))
    second = _collect(settings, prepared, list(reversed(results)))

    assert [item.evidence_id for item in first.evidence] == [
        item.evidence_id for item in second.evidence
    ]
