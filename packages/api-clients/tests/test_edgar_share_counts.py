"""Cover-page share counts and SIC classification from EDGAR.

Both exist so the broad scan can run without a commercial provider: the share
count is what turns a price into a market capitalisation, and the SIC
description is what keeps banks out of a ranking whose economics do not fit
them. Nothing here touches the SEC — every request goes through
`httpx.MockTransport`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_clients import SecEdgarFundamentals

USER_AGENT = "Compounder Radar test suite@example.com"
TICKER_MAP: dict[str, Any] = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def instant(end: str, val: float, filed: str = "2026-01-01") -> dict[str, Any]:
    """Build one instant fact, as a point-in-time concept is filed."""
    return {"end": end, "val": val, "filed": filed}


def duration(start: str, end: str, val: float, filed: str = "2026-01-01") -> dict[str, Any]:
    """Build one duration fact, as an income-statement concept is filed."""
    return {"start": start, "end": end, "val": val, "filed": filed}


def document(
    *,
    usd: dict[str, list[dict[str, Any]]] | None = None,
    shares: dict[str, list[dict[str, Any]]] | None = None,
    dei: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Assemble a company-facts document from `tag=[facts]` pairs.

    Units matter: the adapter reads money under `USD` and share counts under
    `shares`, and a fact filed under the wrong one is invisible to it — which is
    exactly what a real filer's typo looks like.
    """
    gaap: dict[str, Any] = {tag: {"units": {"USD": facts}} for tag, facts in (usd or {}).items()}
    gaap.update({tag: {"units": {"shares": facts}} for tag, facts in (shares or {}).items()})
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {
            "us-gaap": gaap,
            "dei": {tag: {"units": {"shares": facts}} for tag, facts in (dei or {}).items()},
        },
    }


def adapter(
    facts: dict[str, Any], *, submissions: dict[str, Any] | None = None
) -> SecEdgarFundamentals:
    """Build an adapter serving canned facts and submissions documents."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        if "submissions" in request.url.path:
            return httpx.Response(200, json=submissions if submissions is not None else {})
        return httpx.Response(200, json=facts)

    return SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


REVENUE = {
    "Revenues": [
        duration("2026-01-01", "2026-03-31", 100.0),
        duration("2026-04-01", "2026-06-30", 120.0),
    ]
}


@pytest.mark.unit
def test_the_cover_page_share_count_is_read_from_the_dei_taxonomy() -> None:
    # Dated three weeks after the quarter it was filed with, which is how a
    # cover page works and why a same-day match would find nothing.
    facts = document(
        usd=REVENUE,
        dei={"EntityCommonStockSharesOutstanding": [instant("2026-07-21", 50_000_000.0)]},
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert periods[-1].common_shares_outstanding == 50_000_000.0


@pytest.mark.unit
def test_the_balance_sheet_count_is_the_fallback() -> None:
    facts = document(
        usd=REVENUE,
        shares={"CommonStockSharesOutstanding": [instant("2026-06-30", 44_000_000.0)]},
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert periods[-1].common_shares_outstanding == 44_000_000.0


@pytest.mark.unit
def test_the_cover_page_count_beats_the_balance_sheet_count() -> None:
    facts = document(
        usd=REVENUE,
        shares={"CommonStockSharesOutstanding": [instant("2026-06-30", 44_000_000.0)]},
        dei={"EntityCommonStockSharesOutstanding": [instant("2026-06-30", 50_000_000.0)]},
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert periods[-1].common_shares_outstanding == 50_000_000.0


@pytest.mark.unit
def test_a_count_filed_far_after_a_quarter_is_not_attributed_to_it() -> None:
    # A count dated six months after the quarter belongs to a later filing. Left
    # attached, an old quarter would carry a share base it never had.
    facts = document(
        usd=REVENUE,
        dei={"EntityCommonStockSharesOutstanding": [instant("2026-12-15", 90_000_000.0)]},
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert all(period.common_shares_outstanding is None for period in periods)


@pytest.mark.unit
def test_the_newest_count_in_the_window_wins() -> None:
    facts = document(
        usd=REVENUE,
        dei={
            "EntityCommonStockSharesOutstanding": [
                instant("2026-07-05", 48_000_000.0),
                instant("2026-07-24", 51_000_000.0),
            ]
        },
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert periods[-1].common_shares_outstanding == 51_000_000.0


@pytest.mark.unit
def test_the_weighted_average_count_stays_in_its_own_field() -> None:
    # The two concepts must never be conflated: one prices the company, the
    # other measures dilution.
    facts = document(
        usd=REVENUE,
        shares={
            "WeightedAverageNumberOfDilutedSharesOutstanding": [
                duration("2026-04-01", "2026-06-30", 47_500_000.0)
            ]
        },
        dei={"EntityCommonStockSharesOutstanding": [instant("2026-07-21", 50_000_000.0)]},
    )

    latest = adapter(facts).get_financial_statements("AAPL")[-1]

    assert latest.shares_outstanding == 47_500_000.0
    assert latest.common_shares_outstanding == 50_000_000.0


@pytest.mark.unit
def test_the_sic_description_becomes_the_industry() -> None:
    profile = adapter(
        document(usd=REVENUE),
        submissions={"sicDescription": "State Commercial Banks", "sic": "6022"},
    ).get_company_profile("AAPL")

    assert profile is not None
    assert profile.industry == "State Commercial Banks"


@pytest.mark.unit
def test_a_missing_sic_description_leaves_the_industry_unknown() -> None:
    profile = adapter(document(usd=REVENUE), submissions={}).get_company_profile("AAPL")

    assert profile is not None
    assert profile.industry is None


@pytest.mark.unit
def test_a_failing_submissions_lookup_does_not_lose_the_company() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        if "submissions" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, json=document(usd=REVENUE))

    provider = SecEdgarFundamentals(
        USER_AGENT,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        retry=None,
    )
    profile = provider.get_company_profile("AAPL")

    assert profile is not None
    assert profile.name == "Apple Inc."
    assert profile.industry is None
