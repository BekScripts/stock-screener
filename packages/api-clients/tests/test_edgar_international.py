"""EDGAR adapter tests for foreign private issuers and the IFRS taxonomy.

Every payload here is hand-built but carries **figures taken from real filings**,
because the mistakes this code can make are all arithmetic that looks plausible.
The debt cases in particular are pinned to balance sheets that were read to
confirm them: TSM's FY2024 total borrowings of NT$1,018,286.8m, SAP's FY2025
`Borrowings` of EUR 6,150.0m and NVO's of DKK 130,958.0m. Each of those numbers
is reachable by two or three wrong routes that differ by a factor of thirty.

Nothing here touches the SEC; requests go through `httpx.MockTransport`.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from api_clients import SecEdgarFundamentals

USER_AGENT = "Compounder Radar test suite@example.com"

TICKER_MAP: dict[str, Any] = {
    "0": {"cik_str": 1046179, "ticker": "TSM", "title": "Taiwan Semiconductor"},
    "1": {"cik_str": 937966, "ticker": "ASML", "title": "ASML Holding NV"},
}

MILLION = 1_000_000.0


def duration(start: str, end: str, val: float, filed: str = "2026-04-01", **extra: Any) -> dict:
    """Build one duration fact, as an income or cash-flow concept is filed."""
    return {"start": start, "end": end, "val": val, "filed": filed, **extra}


def instant(end: str, val: float, filed: str = "2026-04-01", **extra: Any) -> dict:
    """Build one instant fact, as a balance-sheet concept is filed."""
    return {"end": end, "val": val, "filed": filed, **extra}


def document(namespace: str, concepts: dict[str, dict], *, dei: dict | None = None) -> dict:
    """Assemble a company-facts document under one taxonomy."""
    facts: dict[str, Any] = {namespace: {tag: {"units": units} for tag, units in concepts.items()}}
    if dei is not None:
        facts["dei"] = {tag: {"units": units} for tag, units in dei.items()}
    return {"cik": 1046179, "entityName": "Taiwan Semiconductor", "facts": facts}


def adapter(payload: dict[str, Any]) -> SecEdgarFundamentals:
    """Build an adapter serving one canned company-facts document."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        return httpx.Response(200, json=payload)

    return SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def quarterly_revenue(unit: str, tag: str = "Revenue") -> dict[str, dict]:
    """Two adjacent quarters of revenue, enough to build a period."""
    return {
        tag: {
            unit: [
                duration("2024-07-01", "2024-09-30", 100.0 * MILLION),
                duration("2024-10-01", "2024-12-31", 120.0 * MILLION),
            ]
        }
    }


# -- taxonomy detection -----------------------------------------------------


@pytest.mark.unit
def test_ifrs_statements_are_read_rather_than_skipped() -> None:
    # Before this, the adapter read `facts["us-gaap"]` and returned [] when it
    # was absent, so every IFRS filer had no fundamentals at all.
    provider = adapter(document("ifrs-full", quarterly_revenue("TWD")))

    periods = provider.get_financial_statements("TSM")

    assert [period.revenue for period in periods] == [100.0 * MILLION, 120.0 * MILLION]


@pytest.mark.unit
def test_a_vestigial_taxonomy_block_does_not_win_over_the_real_one() -> None:
    # Telus carries one us-gaap concept with four facts, newest from 2018,
    # beside 269 ifrs-full concepts. Taking the first namespace that exists read
    # the dead one and returned nothing.
    payload = document("ifrs-full", quarterly_revenue("CAD"))
    payload["facts"]["us-gaap"] = {
        "LineOfCreditFacilityMaximumBorrowingCapacity": {
            "CAD": [instant("2018-06-30", 5.0 * MILLION)]
        }
    }

    periods = adapter(payload).get_financial_statements("TSM")

    assert len(periods) == 2
    assert periods[0].reported_currency == "CAD"


@pytest.mark.unit
def test_us_gaap_wins_when_a_filer_tags_both_equally() -> None:
    # A filer mid-transition tagging the same revenue under both taxonomies
    # resolves to us-gaap, so a domestic filer's reading never changes.
    payload = document("us-gaap", quarterly_revenue("USD", "Revenues"))
    payload["facts"]["ifrs-full"] = {
        "Revenue": {"USD": [duration("2024-10-01", "2024-12-31", 999.0 * MILLION)]}
    }

    periods = adapter(payload).get_financial_statements("TSM")

    assert periods[-1].revenue == 120.0 * MILLION


# -- currency ---------------------------------------------------------------


@pytest.mark.unit
def test_the_reporting_currency_is_read_from_the_filing() -> None:
    # Hardcoding USD told the eligibility screen that TSM's TWD statements were
    # comparable with its dollar market capitalisation. They are not.
    provider = adapter(document("ifrs-full", quarterly_revenue("TWD")))

    periods = provider.get_financial_statements("TSM")
    profile = provider.get_company_profile("TSM")

    assert periods[-1].reported_currency == "TWD"
    assert profile is not None
    assert profile.reporting_currency == "TWD"


@pytest.mark.unit
def test_a_convenience_translation_does_not_become_the_reporting_currency() -> None:
    # TSM restates its statements in USD at one year-end spot rate as a
    # courtesy. Reading that column would mix two exchange rates into every
    # growth rate; the currency the company reports in wins on fact count.
    concepts = {
        "Revenue": {
            "TWD": [
                duration("2024-07-01", "2024-09-30", 100.0 * MILLION),
                duration("2024-10-01", "2024-12-31", 120.0 * MILLION),
            ],
            "USD": [duration("2024-10-01", "2024-12-31", 3.7 * MILLION)],
        }
    }

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].reported_currency == "TWD"
    assert periods[-1].revenue == 120.0 * MILLION


@pytest.mark.unit
def test_us_gaap_concepts_filed_in_euros_are_read() -> None:
    # ASML tags every concept the us-gaap chains already know, in EUR. Reading
    # the USD unit key found nothing, so it produced no periods despite needing
    # no new mapping at all.
    concepts = quarterly_revenue("EUR", "RevenueFromContractWithCustomerExcludingAssessedTax")
    concepts["CashAndCashEquivalentsAtCarryingValue"] = {
        "EUR": [instant("2024-12-31", 7.0 * MILLION)]
    }

    periods = adapter(document("us-gaap", concepts)).get_financial_statements("ASML")

    assert periods[-1].revenue == 120.0 * MILLION
    assert periods[-1].cash == 7.0 * MILLION
    assert periods[-1].reported_currency == "EUR"


# -- borrowings -------------------------------------------------------------


@pytest.mark.unit
def test_ifrs_bonds_are_added_to_borrowings_when_no_total_is_stated() -> None:
    # TSM's FY2024 balance sheet: NT$926,604.5m of non-current bonds, NT$31,824.4m
    # of long-term bank loans and NT$59,857.9m of current maturities. Reading
    # `LongtermBorrowings` alone reports 3% of the debt and a nearly debt-free
    # company.
    concepts = quarterly_revenue("TWD")
    concepts.update(
        {
            "LongtermBorrowings": {"TWD": [instant("2024-12-31", 31_824.4 * MILLION)]},
            "NoncurrentPortionOfNoncurrentBondsIssued": {
                "TWD": [instant("2024-12-31", 926_604.5 * MILLION)]
            },
            "CurrentPortionOfLongtermBorrowings": {
                "TWD": [instant("2024-12-31", 59_857.9 * MILLION)]
            },
        }
    )

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].total_debt == pytest.approx(1_018_286.8 * MILLION)


@pytest.mark.unit
def test_current_bonds_are_not_added_to_the_current_maturities_they_sit_inside() -> None:
    # TSM tags NT$57,148.0m of current bonds, but its balance sheet shows one
    # current line of NT$59,857.9m that already contains them. Summing the two
    # overstates total debt by the whole bond figure.
    concepts = quarterly_revenue("TWD")
    concepts.update(
        {
            "CurrentPortionOfLongtermBorrowings": {
                "TWD": [instant("2024-12-31", 59_857.9 * MILLION)]
            },
            "CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued": {
                "TWD": [instant("2024-12-31", 57_148.0 * MILLION)]
            },
        }
    )

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].total_debt == pytest.approx(59_857.9 * MILLION)


@pytest.mark.unit
def test_current_bonds_are_read_when_nothing_else_states_current_debt() -> None:
    # The fallback exists so a filer whose only current-debt tag is the bond one
    # is not reported as having none.
    concepts = quarterly_revenue("TWD")
    concepts["CurrentBondsIssuedAndCurrentPortionOfNoncurrentBondsIssued"] = {
        "TWD": [instant("2024-12-31", 57_148.0 * MILLION)]
    }

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].total_debt == pytest.approx(57_148.0 * MILLION)


@pytest.mark.unit
def test_a_stated_entity_wide_total_wins_over_its_own_breakdown() -> None:
    # SAP's FY2025 `Borrowings` of EUR 6,150.0m is exactly its EUR 4,550.0m
    # non-current plus EUR 1,600.0m current. It also tags EUR 5,294.0m of bonds,
    # which are part of that total — adding them would report EUR 11,444.0m.
    concepts = quarterly_revenue("EUR")
    concepts.update(
        {
            "Borrowings": {"EUR": [instant("2024-12-31", 6_150.0 * MILLION)]},
            "LongtermBorrowings": {"EUR": [instant("2024-12-31", 4_550.0 * MILLION)]},
            "BondsIssued": {"EUR": [instant("2024-12-31", 5_294.0 * MILLION)]},
            "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings": {
                "EUR": [instant("2024-12-31", 1_600.0 * MILLION)]
            },
        }
    )

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].total_debt == pytest.approx(6_150.0 * MILLION)


@pytest.mark.unit
def test_a_borrowing_the_filer_never_tagged_is_none_rather_than_zero() -> None:
    # Absence is not a debt-free company. A missing figure that read as zero
    # would flatter exactly the balance sheets the quality score exists to judge.
    periods = adapter(document("ifrs-full", quarterly_revenue("TWD"))).get_financial_statements(
        "TSM"
    )

    assert periods[-1].total_debt is None


# -- capital expenditure ----------------------------------------------------


@pytest.mark.unit
def test_ifrs_capex_sums_property_and_intangibles_when_stated_separately() -> None:
    # TSM and NVO tag the two lines separately; a capital programme reported
    # without its intangibles understates reinvestment.
    concepts = quarterly_revenue("TWD")
    concepts.update(
        {
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {
                "TWD": [duration("2024-10-01", "2024-12-31", 40.0 * MILLION)]
            },
            "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities": {
                "TWD": [duration("2024-10-01", "2024-12-31", 5.0 * MILLION)]
            },
        }
    )

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].capital_expenditure == pytest.approx(45.0 * MILLION)


@pytest.mark.unit
def test_a_combined_capex_concept_is_not_added_to_its_own_components() -> None:
    # SAP states one concept already covering property and intangibles. Summing
    # it with them would report capital spending twice.
    concepts = quarterly_revenue("EUR")
    combined = (
        "PurchaseOfPropertyPlantAndEquipmentIntangibleAssetsOtherThanGoodwill"
        "InvestmentPropertyAndOtherNoncurrentAssets"
    )
    concepts.update(
        {
            combined: {"EUR": [duration("2024-10-01", "2024-12-31", 45.0 * MILLION)]},
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": {
                "EUR": [duration("2024-10-01", "2024-12-31", 40.0 * MILLION)]
            },
        }
    )

    periods = adapter(document("ifrs-full", concepts)).get_financial_statements("TSM")

    assert periods[-1].capital_expenditure == pytest.approx(45.0 * MILLION)


# -- share counts and market capitalisation ---------------------------------


@pytest.mark.unit
def test_a_cover_page_share_count_from_a_20_f_is_not_read() -> None:
    # The count is in ordinary shares while the U.S. security is an ADS. TSM's
    # ratio is five, so multiplying its 25.9bn shares by the ADS price values
    # the company at $11tn against a real $2.2tn. Nothing in the XBRL says the
    # ratio, so the count is not usable and is dropped.
    concepts = quarterly_revenue("TWD")
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "shares": [instant("2024-12-31", 25_932_524_521.0, form="20-F")]
        }
    }

    periods = adapter(document("ifrs-full", concepts, dei=dei)).get_financial_statements("TSM")

    assert periods[-1].common_shares_outstanding is None


@pytest.mark.unit
def test_a_cover_page_share_count_from_a_10_k_is_still_read() -> None:
    # The domestic path is untouched: a 10-K cover page states the count for the
    # security that actually trades.
    concepts = quarterly_revenue("USD", "Revenues")
    dei = {
        "EntityCommonStockSharesOutstanding": {
            "shares": [instant("2025-01-15", 500.0 * MILLION, form="10-K")]
        }
    }

    periods = adapter(document("us-gaap", concepts, dei=dei)).get_financial_statements("TSM")

    assert periods[-1].common_shares_outstanding == 500.0 * MILLION


@pytest.mark.unit
def test_an_annual_only_filer_yields_no_quarters() -> None:
    # A 20-F filer tags twelve-month durations and nothing else. Four of them
    # are not a trailing year and must not become one; the honest answer is no
    # quarterly periods rather than invented ones.
    concepts = {
        "Revenue": {
            "TWD": [
                duration("2023-01-01", "2023-12-31", 2_161_735.8 * MILLION),
                duration("2024-01-01", "2024-12-31", 2_894_307.7 * MILLION),
            ]
        }
    }

    assert adapter(document("ifrs-full", concepts)).get_financial_statements("TSM") == []


@pytest.mark.unit
def test_a_filer_with_no_recognised_taxonomy_returns_nothing() -> None:
    payload = {"cik": 1, "entityName": "Nothing Ltd", "facts": {"dei": {}}}

    assert adapter(payload).get_financial_statements("TSM") == []
