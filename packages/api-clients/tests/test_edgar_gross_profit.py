"""Cost-of-revenue concepts, and the provenance that keeps them apart.

The payloads mirror what the live filings actually look like: HF Sinclair
stopped tagging `CostOfGoodsAndServicesSold` in 2024 while still reporting
revenue, Expedia dropped `CostOfRevenue` in 2019, and Expand Energy last tagged
`GrossProfit` in 2011. Widening the chain is what recovers those margins — and
the concepts are not interchangeable, which is what `gross_profit_basis` records.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_clients import SecEdgarFundamentals

USER_AGENT = "Compounder Radar test suite@example.com"
TICKER_MAP: dict[str, Any] = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def duration(start: str, end: str, val: float, filed: str = "2026-08-01") -> dict[str, Any]:
    """Build one duration fact, as an income-statement concept is filed."""
    return {"start": start, "end": end, "val": val, "filed": filed}


QUARTERS = (("2025-04-01", "2025-06-30"), ("2026-04-01", "2026-06-30"))


def series(*values: float) -> list[dict[str, Any]]:
    """Build one discrete quarterly fact per value, oldest first."""
    return [
        duration(start, end, value) for (start, end), value in zip(QUARTERS, values, strict=True)
    ]


def document(**tags: list[dict[str, Any]]) -> dict[str, Any]:
    """Assemble a company-facts document from `tag=[facts]` pairs."""
    return {
        "cik": 320193,
        "entityName": "Example Corp",
        "facts": {"us-gaap": {tag: {"units": {"USD": facts}} for tag, facts in tags.items()}},
    }


def adapter(facts: dict[str, Any]) -> SecEdgarFundamentals:
    """Build an adapter serving one canned company-facts document."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        if "submissions" in request.url.path:
            return httpx.Response(200, json={})
        return httpx.Response(200, json=facts)

    return SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


REVENUE = series(800.0, 1000.0)


def latest(facts: dict[str, Any]):
    """Return the newest period the adapter produced."""
    return adapter(facts).get_financial_statements("AAPL")[-1]


@pytest.mark.unit
@pytest.mark.parametrize(
    "concept",
    [
        "CostOfGoodsAndServicesSold",
        "CostOfRevenue",
        "CostOfGoodsSold",
        "CostOfServices",
        "DirectOperatingCosts",
        "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization",
        "CostOfGoodsSoldExcludingDepreciationDepletionAndAmortization",
        "CostOfRevenueExcludingDepreciationDepletionAndAmortization",
    ],
)
def test_each_cost_concept_yields_a_gross_profit(concept: str) -> None:
    period = latest(document(Revenues=REVENUE, **{concept: series(500.0, 600.0)}))

    assert period.revenue == 1000.0
    assert period.gross_profit == 400.0
    assert period.gross_profit_basis == concept


@pytest.mark.unit
def test_total_costs_are_never_read_as_cost_of_revenue() -> None:
    # `CostsAndExpenses` is every operating cost, including SG&A. Subtracting it
    # from revenue gives something close to operating income, and calling that a
    # gross profit would be wrong by the whole operating expense base.
    period = latest(document(Revenues=REVENUE, CostsAndExpenses=series(500.0, 600.0)))

    assert period.gross_profit is None
    assert period.gross_profit_basis is None


@pytest.mark.unit
def test_a_reported_gross_profit_is_never_overwritten_by_a_derived_one() -> None:
    facts = document(
        Revenues=REVENUE,
        GrossProfit=series(300.0, 350.0),
        CostOfServices=series(500.0, 600.0),
    )

    period = latest(facts)

    assert period.gross_profit == 350.0
    assert period.gross_profit_basis == "GrossProfit"


@pytest.mark.unit
def test_a_cost_including_depreciation_beats_one_excluding_it() -> None:
    # Both are present for the same quarter. The GAAP-basis concept wins, and
    # the basis says so — the excluding-D&A figure would overstate gross profit.
    facts = document(
        Revenues=REVENUE,
        CostOfRevenue=series(500.0, 600.0),
        CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization=series(400.0, 450.0),
    )

    period = latest(facts)

    assert period.gross_profit == 400.0
    assert period.gross_profit_basis == "CostOfRevenue"


@pytest.mark.unit
def test_a_filer_that_migrated_concepts_keeps_one_continuous_series() -> None:
    # The HF Sinclair shape: the old concept stops, a new one continues.
    facts = document(
        Revenues=REVENUE,
        CostOfGoodsAndServicesSold=[duration("2025-04-01", "2025-06-30", 500.0)],
        CostOfServices=[duration("2026-04-01", "2026-06-30", 600.0)],
    )

    periods = adapter(facts).get_financial_statements("AAPL")

    assert [p.gross_profit for p in periods] == [300.0, 400.0]
    assert [p.gross_profit_basis for p in periods] == [
        "CostOfGoodsAndServicesSold",
        "CostOfServices",
    ]


@pytest.mark.unit
def test_a_stale_concept_does_not_supply_a_recent_quarter() -> None:
    # The Expand Energy shape: `GrossProfit` was abandoned years ago. The old
    # fact stays attached to its own quarter and must not leak into a later one.
    facts = document(
        Revenues=REVENUE,
        GrossProfit=[duration("2011-04-01", "2011-06-30", 90.0, filed="2011-08-01")],
        CostOfServices=series(500.0, 600.0),
    )

    period = latest(facts)

    assert period.gross_profit == 400.0
    assert period.gross_profit_basis == "CostOfServices"


@pytest.mark.unit
def test_no_cost_concept_leaves_gross_profit_unknown() -> None:
    period = latest(document(Revenues=REVENUE))

    assert period.gross_profit is None
    assert period.gross_profit_basis is None


@pytest.mark.unit
def test_revenue_without_a_cost_is_not_a_full_margin() -> None:
    # Guard against the obvious wrong fix: absent cost must not read as zero.
    period = latest(document(Revenues=REVENUE, CostOfServices=[]))

    assert period.gross_profit is None
