"""The source policy: who may be cited, and when two links are one article."""

from __future__ import annotations

import pytest

from deep_research import ExternalSourceType, SourceTier
from stock_screener.deep_research import (
    EventClass,
    build_queries,
    canonical_url,
    classify_event,
    is_denied,
    registrable_domain,
    search_anchor,
    source_type_for,
    tier_for,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.reuters.com/x", "reuters.com"),
        ("https://REUTERS.com/x", "reuters.com"),
        ("https://investor.acme.com/news", "investor.acme.com"),
        ("https://reuters.com:443/x", "reuters.com"),
        ("not a url", ""),
    ],
)
def test_registrable_domain_normalises_the_host(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://www.reddit.com/r/stocks/comments/abc",
        "https://old.reddit.com/r/stocks/comments/abc",
        "https://x.com/someone/status/1",
        "https://twitter.com/someone/status/1",
        "https://stocktwits.com/symbol/MU",
        "https://seekingalpha.com/article/1-mu",
        "https://www.fool.com/investing/mu",
        "https://finance.yahoo.com/news/mu",
        "https://www.nasdaq.com/articles/mu",
        "https://someone.substack.com/p/mu",
    ],
)
def test_social_forum_and_aggregator_sources_are_denied(url: str) -> None:
    """V1 cites none of these, and the refusal is explicit rather than incidental."""
    assert is_denied(url) is True
    assert tier_for(url) is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.sec.gov/Archives/edgar/data/1/x.htm", SourceTier.TIER_1_PRIMARY),
        ("https://www.businesswire.com/news/home/1", SourceTier.TIER_1_PRIMARY),
        ("https://www.globenewswire.com/news-release/1", SourceTier.TIER_1_PRIMARY),
        ("https://www.reuters.com/technology/x", SourceTier.TIER_2_REPUTABLE),
        ("https://www.wsj.com/articles/x", SourceTier.TIER_2_REPUTABLE),
        ("https://www.cnbc.com/2026/08/18/x.html", SourceTier.TIER_2_REPUTABLE),
        ("https://www.tomshardware.com/news/x", SourceTier.TIER_3_SUPPORTING),
        ("https://www.digitimes.com/news/x", SourceTier.TIER_3_SUPPORTING),
    ],
)
def test_recognised_domains_get_their_tier(url: str, expected: SourceTier) -> None:
    assert tier_for(url) is expected


@pytest.mark.unit
def test_a_subdomain_inherits_its_parents_tier() -> None:
    assert tier_for("https://www.uk.reuters.com/x") is SourceTier.TIER_2_REPUTABLE


@pytest.mark.unit
@pytest.mark.parametrize(
    "url",
    [
        "https://stocknewsdaily.example/mu-soars",
        "https://randomblog.example/why-i-bought-mu",
        "https://content-farm.example/10-stocks",
    ],
)
def test_an_unrecognised_domain_is_refused_rather_than_demoted(url: str) -> None:
    """An allowlist that admitted the unknown would not be an allowlist."""
    assert tier_for(url) is None
    assert is_denied(url) is False


@pytest.mark.unit
def test_a_companys_own_host_is_primary() -> None:
    tier = tier_for(
        "https://investor.acme.com/press/q3", company_domains=frozenset({"investor.acme.com"})
    )

    assert tier is SourceTier.TIER_1_PRIMARY


@pytest.mark.unit
def test_a_company_domain_cannot_rescue_a_denied_source() -> None:
    """The denylist wins, or a company blog on Medium would become primary."""
    tier = tier_for(
        "https://acme.substack.com/p/update", company_domains=frozenset({"acme.substack.com"})
    )

    assert tier is None


@pytest.mark.unit
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.sec.gov/Archives/x.htm", ExternalSourceType.SEC),
        ("https://www.businesswire.com/news/1", ExternalSourceType.COMPANY),
        ("https://www.reuters.com/x", ExternalSourceType.NEWS),
        ("https://www.tomshardware.com/x", ExternalSourceType.INDUSTRY),
    ],
)
def test_source_type_follows_who_is_speaking(url: str, expected: ExternalSourceType) -> None:
    assert source_type_for(url) is expected


@pytest.mark.unit
@pytest.mark.parametrize(
    ("one", "other"),
    [
        ("https://www.reuters.com/a/b", "https://reuters.com/a/b"),
        ("https://www.reuters.com/a/b", "http://www.reuters.com/a/b/"),
        ("https://www.reuters.com/a/b", "https://www.reuters.com/a/b?utm_source=x"),
        ("https://www.reuters.com/a/b", "https://www.reuters.com/a/b#section"),
        ("https://www.reuters.com/a/b", "https://www.reuters.com/a/b/amp"),
        ("https://www.reuters.com/a/b?x=1&y=2", "https://www.reuters.com/a/b?y=2&x=1"),
    ],
)
def test_two_links_to_one_article_canonicalise_together(one: str, other: str) -> None:
    assert canonical_url(one) == canonical_url(other)


@pytest.mark.unit
def test_two_different_articles_stay_apart() -> None:
    assert canonical_url("https://reuters.com/a") != canonical_url("https://reuters.com/b")


@pytest.mark.unit
def test_a_meaningful_query_parameter_survives_canonicalisation() -> None:
    """Some sites identify an article by query string; stripping it would merge them."""
    assert canonical_url("https://example.com/n?id=42") == "https://example.com/n?id=42"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("name", "ticker", "expected"),
    [
        ("Acacia Research Corporation", "ACTG", '"Acacia Research" (ACTG)'),
        ("Micron Technology, Inc.", "MU", '"Micron Technology" (MU)'),
        ("Diamondback Energy, Inc.", "FANG", '"Diamondback Energy" (FANG)'),
        ("Apple Inc.", "AAPL", '"Apple" (AAPL)'),
    ],
)
def test_the_anchor_is_the_company_name_without_its_legal_wrapper(
    name: str, ticker: str, expected: str
) -> None:
    assert search_anchor(name, ticker) == expected


@pytest.mark.unit
def test_a_name_that_is_only_a_legal_wrapper_still_carries_the_ticker() -> None:
    assert "XYZ" in search_anchor("The Company", "XYZ")


@pytest.mark.unit
def test_every_query_is_anchored_on_the_company_name_not_the_ticker() -> None:
    """The ACTG failure: the symbol is also a DNA base sequence."""
    queries = build_queries("ACTG", "Acacia Research Corporation")

    assert all(query.startswith('"Acacia Research"') for query in queries)


@pytest.mark.unit
def test_the_ticker_is_parenthesised_context_never_the_leading_term() -> None:
    """A bare trailing ticker dragged live results toward finance coverage at large."""
    for query in build_queries("ACTG", "Acacia Research Corporation"):
        assert query.startswith('"Acacia Research" (ACTG)')
        assert " ACTG " not in query


@pytest.mark.unit
def test_the_queries_are_distinct_and_deterministic() -> None:
    first = build_queries("MU", "Micron Technology, Inc.")
    second = build_queries("MU", "Micron Technology, Inc.")

    assert first == second
    assert len(set(first)) == len(first)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # Real headlines from the live MU collection.
        (
            "Micron and Anthropic Announce Strategic Agreement to Scale AI Infrastructure",
            EventClass.PRIMARY_DEVELOPMENT,
        ),
        (
            "Micron boosts U.S. investment plan again, commits $250 billion through 2035",
            EventClass.COMPANY_ACTION,
        ),
        (
            "Micron forecasts strong quarterly results on soaring memory chip demand",
            EventClass.EARNINGS,
        ),
        (
            "Micron reported strong earnings. Street sees more momentum on deals",
            EventClass.EARNINGS,
        ),
        (
            "Micron Earnings: Shares Spike 13% On Record $41.5 Billion Quarter",
            EventClass.MARKET_REACTION,
        ),
        (
            "Micron stock jumps over 16% in premarket trading after blockbuster earnings",
            EventClass.MARKET_REACTION,
        ),
        (
            "Micron falls 5% in premarket, paring earlier gains amid tech rout",
            EventClass.MARKET_REACTION,
        ),
        ("Here's What Can End Micron's Stock Pain", EventClass.MARKET_REACTION),
        ("How Micron Stock Can Bust-Out of Its Slump", EventClass.MARKET_REACTION),
        ("Micron Stock Gets Big Price-Target Hike, Hits Record High", EventClass.MARKET_REACTION),
        ("Memory-Chip Stocks Micron, Sandisk Get Their Wings Clipped", EventClass.MARKET_REACTION),
        (
            "Nervous investors await Micron earnings as chip sector whipsaws",
            EventClass.MARKET_REACTION,
        ),
        ("Acme acquires Bolt Systems for $300 million", EventClass.PRIMARY_DEVELOPMENT),
        ("Acme names Jane Roe chief financial officer", EventClass.PRIMARY_DEVELOPMENT),
        ("Acme announces a share repurchase programme", EventClass.PRIMARY_DEVELOPMENT),
        ("Memory shortage set to persist through 2027, analysts say", EventClass.MARKET_REACTION),
    ],
)
def test_headlines_are_classified_by_what_they_report(title: str, expected: EventClass) -> None:
    assert classify_event(title) is expected


@pytest.mark.unit
def test_an_unrecognised_headline_is_never_silently_capped() -> None:
    """The default must not be the capped class, or odd stories would vanish."""
    assert classify_event("Acme opens a facility in Ohio") is not EventClass.MARKET_REACTION
