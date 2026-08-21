"""FMP adapter tests.

The payloads below are trimmed copies of **real** `/stable` responses, captured
from a live AAPL request on 2026-08-13. That matters: the first version of this
adapter was written from documentation and got three field names wrong, each of
which produced a silent `None` rather than an error. Keeping the fixtures
faithful to the wire format is what makes these tests able to catch that.

Re-run `scripts/verify_fundamentals.py` after any provider change; if it reports
a field the adapter no longer finds, update these fixtures too.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from api_clients import FmpFundamentals

INCOME = [
    {
        "date": "2026-06-30",
        "revenue": 100_000_000,
        "grossProfit": 38_200_000,
        "operatingIncome": 9_000_000,
        "weightedAverageShsOutDil": 41_000_000,
    },
    {"date": "2026-03-31", "revenue": 92_000_000, "grossProfit": 34_000_000},
]
BALANCE = [
    {
        "date": "2026-06-30",
        "cashAndShortTermInvestments": 410_000_000,
        "totalDebt": 95_000_000,
    }
]
CASH_FLOW = [
    {
        "date": "2026-06-30",
        "operatingCashFlow": 10_500_000,
        # FMP reports capital expenditure as a negative number.
        "capitalExpenditure": -4_000_000,
    }
]


def _adapter(routes: dict[str, Any]) -> FmpFundamentals:
    """Build an adapter serving canned payloads keyed by URL fragment."""

    def handler(request: httpx.Request) -> httpx.Response:
        for fragment, payload in routes.items():
            if fragment in request.url.path:
                return httpx.Response(200, json=payload)
        return httpx.Response(200, json=[])

    return FmpFundamentals(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fmp.test"),
    )


def _statements_adapter() -> FmpFundamentals:
    """Build an adapter serving the three statement endpoints above."""
    return _adapter(
        {
            "income-statement": INCOME,
            "balance-sheet-statement": BALANCE,
            "cash-flow-statement": CASH_FLOW,
        }
    )


@pytest.mark.unit
def test_the_three_statements_merge_into_one_period_per_date() -> None:
    periods = _statements_adapter().get_financial_statements("XYZ")

    assert [p.period_end for p in periods] == [date(2026, 3, 31), date(2026, 6, 30)]

    latest = periods[-1]
    assert latest.revenue == pytest.approx(100_000_000)
    assert latest.cash == pytest.approx(410_000_000)
    assert latest.operating_cash_flow == pytest.approx(10_500_000)


@pytest.mark.unit
def test_capex_is_normalised_to_a_positive_outflow() -> None:
    # FMP sends -4,000,000. Stored as +4,000,000 so free cash flow is
    # 10.5m - 4m = 6.5m, not 14.5m.
    latest = _statements_adapter().get_financial_statements("XYZ")[-1]

    assert latest.capital_expenditure == pytest.approx(4_000_000)


@pytest.mark.unit
def test_a_date_missing_from_one_statement_leaves_those_fields_none() -> None:
    # Q1 has an income statement but no balance sheet. Dropping the period
    # entirely would lose a quarter of revenue history; zeroing the balance
    # sheet would invent a debt-free company.
    first = _statements_adapter().get_financial_statements("XYZ")[0]

    assert first.revenue == pytest.approx(92_000_000)
    assert first.cash is None
    assert first.total_debt is None


@pytest.mark.unit
def test_total_debt_falls_back_to_summing_the_two_maturities() -> None:
    adapter = _adapter(
        {
            "income-statement": [{"date": "2026-06-30", "revenue": 10}],
            "balance-sheet-statement": [
                {"date": "2026-06-30", "shortTermDebt": 30, "longTermDebt": 70}
            ],
        }
    )

    assert adapter.get_financial_statements("XYZ")[-1].total_debt == pytest.approx(100)


@pytest.mark.unit
def test_a_missing_number_stays_none_rather_than_becoming_zero() -> None:
    adapter = _adapter({"income-statement": [{"date": "2026-06-30", "revenue": None}]})

    assert adapter.get_financial_statements("XYZ")[-1].revenue is None


@pytest.mark.unit
def test_an_unparseable_number_stays_none() -> None:
    adapter = _adapter({"income-statement": [{"date": "2026-06-30", "revenue": "n/a"}]})

    assert adapter.get_financial_statements("XYZ")[-1].revenue is None


@pytest.mark.unit
def test_a_numeric_string_is_still_read_as_a_number() -> None:
    adapter = _adapter({"income-statement": [{"date": "2026-06-30", "revenue": "12345.5"}]})

    assert adapter.get_financial_statements("XYZ")[-1].revenue == pytest.approx(12_345.5)


@pytest.mark.unit
def test_a_row_with_a_malformed_date_is_skipped() -> None:
    adapter = _adapter(
        {
            "income-statement": [
                {"date": "not-a-date", "revenue": 1},
                {"date": "2026-06-30", "revenue": 2},
            ]
        }
    )

    periods = adapter.get_financial_statements("XYZ")

    assert [p.period_end for p in periods] == [date(2026, 6, 30)]


@pytest.mark.unit
def test_the_profile_carries_sector_industry_and_market_cap() -> None:
    adapter = _adapter(
        {
            "profile": [
                {
                    "symbol": "XYZ",
                    "companyName": "Example Corp",
                    "exchange": "NASDAQ",
                    "sector": "Technology",
                    "industry": "Software",
                    "marketCap": 1_240_000_000,
                    "currency": "USD",
                    "isActivelyTrading": 1,
                    "isEtf": 0,
                    "isFund": 0,
                }
            ]
        }
    )

    profile = adapter.get_company_profile("xyz")

    assert profile is not None
    assert profile.ticker == "XYZ"
    assert profile.exchange == "NASDAQ"
    assert profile.sector == "Technology"
    assert profile.market_cap == pytest.approx(1_240_000_000)
    assert profile.is_active is True
    assert profile.is_fund is False


@pytest.mark.unit
def test_an_uncovered_symbol_returns_none_rather_than_raising() -> None:
    # A vendor that does not cover a symbol is a coverage gap, not a failure —
    # raising here would abort a scan over one obscure listing.
    assert _adapter({"profile": []}).get_company_profile("NOPE") is None


@pytest.mark.unit
def test_a_delisted_company_is_marked_inactive() -> None:
    adapter = _adapter(
        {"profile": [{"symbol": "GONE", "companyName": "Gone Inc", "isActivelyTrading": 0}]}
    )

    profile = adapter.get_company_profile("GONE")

    assert profile is not None
    assert profile.is_active is False


@pytest.mark.unit
def test_the_api_key_is_sent_as_a_query_parameter() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    adapter = FmpFundamentals(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fmp.test"),
    )
    adapter.get_financial_statements("XYZ", limit=8)

    assert seen["apikey"] == "test-key"
    assert seen["period"] == "quarter"
    assert seen["limit"] == "8"


# -- audit regressions -------------------------------------------------------


@pytest.mark.unit
def test_debt_is_none_when_only_one_maturity_is_reported() -> None:
    # Summing whichever bucket happened to arrive would understate total debt.
    # A company with $2bn of omitted long-term borrowing would then look like it
    # holds net cash, and Phase 2 would *reduce* its risk penalty for it.
    adapter = _adapter(
        {
            "income-statement": [{"date": "2026-06-30", "revenue": 10}],
            "balance-sheet-statement": [{"date": "2026-06-30", "shortTermDebt": 30}],
        }
    )

    assert adapter.get_financial_statements("XYZ")[-1].total_debt is None


@pytest.mark.unit
def test_the_quote_currency_is_captured_from_the_profile() -> None:
    adapter = _adapter(
        {"profile": [{"symbol": "XYZ", "companyName": "Example", "currency": "EUR"}]}
    )

    profile = adapter.get_company_profile("XYZ")

    assert profile is not None
    # FMP's profile currency is what the *share* trades in, never what the
    # company files in. Putting it in `reporting_currency` was how an ADR's USD
    # quote came to stand in for a TWD balance sheet.
    assert profile.quote_currency == "EUR"
    # And it decides nothing about the statements. `reports_in_usd` reads the
    # reporting currency alone, so a vendor's quote currency can no longer stand
    # in for one — which is how an ADR's dollar quote came to be read as a
    # dollar balance sheet.
    assert profile.reporting_currency is None
    assert profile.reports_in_usd is True


@pytest.mark.unit
def test_the_reporting_currency_is_captured_from_the_statements() -> None:
    adapter = _adapter(
        {"income-statement": [{"date": "2026-06-30", "revenue": 10, "reportedCurrency": "CAD"}]}
    )

    assert adapter.get_financial_statements("XYZ")[-1].reported_currency == "CAD"


@pytest.mark.unit
def test_an_etf_is_flagged_from_the_provider_not_from_its_name() -> None:
    # A provider that classifies the instrument beats guessing from the name,
    # which is how a fund with an ordinary-sounding name slips into a ranking.
    adapter = _adapter({"profile": [{"symbol": "SPY", "companyName": "SPDR Trust", "isEtf": 1}]})

    profile = adapter.get_company_profile("SPY")

    assert profile is not None
    assert profile.is_fund is True


@pytest.mark.unit
def test_the_symbol_travels_as_a_query_parameter() -> None:
    # The stable API takes ?symbol=X; the retired v3 API took it as a path
    # segment. Getting this wrong is a 403, not a wrong number, but it is the
    # difference between the adapter working and not.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen.update(dict(request.url.params))
        return httpx.Response(200, json=[])

    adapter = FmpFundamentals(
        api_key="test-key",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="https://fmp.test"),
    )
    adapter.get_company_profile("aapl")

    assert seen["path"] == "/stable/profile"
    assert seen["symbol"] == "AAPL"


@pytest.mark.unit
def test_the_consolidated_average_volume_is_captured() -> None:
    # This is the liquidity input the eligibility threshold is calibrated for;
    # a single-exchange feed's volume is not comparable with it.
    adapter = _adapter(
        {"profile": [{"symbol": "XYZ", "companyName": "Example", "averageVolume": 53_498_387}]}
    )

    profile = adapter.get_company_profile("XYZ")

    assert profile is not None
    assert profile.average_volume == pytest.approx(53_498_387)
