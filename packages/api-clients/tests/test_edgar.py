"""SEC EDGAR adapter tests.

The payloads here are hand-built but shaped exactly like real company-facts
documents, because every bug this adapter had came from a property of the real
format rather than from the code being obviously wrong: cumulative cash-flow
ladders, weighted averages that cannot be differenced, retired tags that still
carry a decade-old fact, and the same quarter filed four times.

Each of those is pinned below. All requests go through `httpx.MockTransport`;
nothing here touches the SEC.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from api_clients import ProviderDataError, SecEdgarFundamentals

USER_AGENT = "Compounder Radar test suite@example.com"

TICKER_MAP: dict[str, Any] = {
    "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    "1": {"cik_str": 723603, "ticker": "CULP", "title": "Culp, Inc."},
}


def duration(start: str, end: str, val: float, filed: str = "2026-01-01", **extra: Any) -> dict:
    """Build one duration fact, as an income or cash-flow concept is filed."""
    return {"start": start, "end": end, "val": val, "filed": filed, **extra}


def instant(end: str, val: float, filed: str = "2026-01-01") -> dict:
    """Build one instant fact, as a balance-sheet concept is filed."""
    return {"end": end, "val": val, "filed": filed}


def facts_document(**concepts: dict[str, list[dict]]) -> dict[str, Any]:
    """Assemble a company-facts document from `tag={unit: [facts]}` pairs."""
    return {
        "cik": 320193,
        "entityName": "Apple Inc.",
        "facts": {"us-gaap": {tag: {"units": units} for tag, units in concepts.items()}},
    }


def adapter(document: dict[str, Any], *, ticker_map: dict[str, Any] | None = None) -> Any:
    """Build an adapter serving one canned company-facts document."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=ticker_map if ticker_map is not None else TICKER_MAP)
        return httpx.Response(200, json=document)

    return SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def by_end(periods: list[Any]) -> dict[date, Any]:
    """Index returned periods by their end date."""
    return {period.period_end: period for period in periods}


# -- credentials and identity -----------------------------------------------


@pytest.mark.unit
def test_a_blank_user_agent_is_rejected() -> None:
    # The SEC blocks requests that do not identify the caller, so this fails at
    # construction rather than as a wall of 403s mid-scan.
    with pytest.raises(ValueError, match="User-Agent"):
        SecEdgarFundamentals("   ")


@pytest.mark.unit
def test_the_user_agent_is_sent_on_every_request() -> None:
    # Checked on the client the adapter builds for itself, which is the one that
    # reaches the SEC in production.
    provider = SecEdgarFundamentals(USER_AGENT)
    try:
        assert provider._client.headers["user-agent"] == USER_AGENT
    finally:
        provider.close()


@pytest.mark.unit
def test_a_ticker_the_sec_does_not_list_returns_nothing() -> None:
    provider = adapter(facts_document())

    assert provider.get_company_profile("NOPE") is None
    assert provider.get_financial_statements("NOPE") == []


@pytest.mark.unit
def test_the_profile_carries_identity_but_no_market_data() -> None:
    # EDGAR is a filings archive. Leaving market cap None is what makes the
    # eligibility screen exclude the company rather than score it on a guess.
    profile = adapter(facts_document()).get_company_profile("aapl")

    assert profile is not None
    assert profile.ticker == "AAPL"
    assert profile.name == "Apple Inc."
    assert profile.market_cap is None
    assert profile.sector is None


@pytest.mark.unit
def test_the_ticker_map_is_fetched_once_and_reused() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        return httpx.Response(200, json=facts_document())

    provider = SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.get_company_profile("AAPL")
    provider.get_company_profile("CULP")

    assert sum("company_tickers" in path for path in calls) == 1


@pytest.mark.unit
def test_a_malformed_ticker_map_raises() -> None:
    provider = adapter(facts_document(), ticker_map={})

    with pytest.raises(ProviderDataError, match="no usable entries"):
        provider.get_company_profile("AAPL")


# -- discrete quarters -------------------------------------------------------


@pytest.mark.unit
def test_discrete_quarterly_facts_are_read_directly() -> None:
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                duration("2025-04-01", "2025-06-30", 110.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].revenue == pytest.approx(100.0)
    assert periods[date(2025, 6, 30)].revenue == pytest.approx(110.0)


@pytest.mark.unit
def test_six_and_nine_month_cumulative_facts_are_not_mistaken_for_quarters() -> None:
    # A filer reports Q1 and then year-to-date. Taking the cumulative figure at
    # face value would report a six-month total as one quarter's revenue.
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                duration("2025-01-01", "2025-06-30", 210.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 6, 30)].revenue == pytest.approx(110.0)


# -- the cumulative ladder ---------------------------------------------------


@pytest.mark.unit
def test_a_cumulative_cash_flow_ladder_is_differenced_into_quarters() -> None:
    # The exact shape Apple files: one fiscal year, four rungs sharing a start,
    # of which only the first is a standalone quarter. Without differencing,
    # three quarters in four would have no cash flow at all.
    document = facts_document(
        NetCashProvidedByUsedInOperatingActivities={
            "USD": [
                duration("2025-01-01", "2025-03-31", 50.0),
                duration("2025-01-01", "2025-06-30", 120.0),
                duration("2025-01-01", "2025-09-30", 200.0),
                duration("2025-01-01", "2025-12-31", 260.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].operating_cash_flow == pytest.approx(50.0)
    assert periods[date(2025, 6, 30)].operating_cash_flow == pytest.approx(70.0)
    assert periods[date(2025, 9, 30)].operating_cash_flow == pytest.approx(80.0)
    assert periods[date(2025, 12, 31)].operating_cash_flow == pytest.approx(60.0)


@pytest.mark.unit
def test_the_first_rung_is_both_a_quarter_and_part_of_its_ladder() -> None:
    # Routing the three-month fact only to "discrete quarters" left the second
    # quarter with nothing to subtract from, so every Q2 cash flow went missing.
    document = facts_document(
        NetCashProvidedByUsedInOperatingActivities={
            "USD": [
                duration("2025-01-01", "2025-03-31", 50.0),
                duration("2025-01-01", "2025-06-30", 120.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].operating_cash_flow == pytest.approx(50.0)
    assert periods[date(2025, 6, 30)].operating_cash_flow == pytest.approx(70.0)


@pytest.mark.unit
def test_a_fourth_quarter_is_derived_from_the_annual_figure() -> None:
    # No filer reports Q4 as a quarter; it exists only inside the 10-K's year.
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                duration("2025-04-01", "2025-06-30", 110.0),
                duration("2025-07-01", "2025-09-30", 120.0),
                duration("2025-01-01", "2025-12-31", 460.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert date(2025, 12, 31) in periods
    assert periods[date(2025, 12, 31)].revenue == pytest.approx(130.0)


@pytest.mark.unit
def test_a_directly_reported_quarter_beats_one_reconstructed_by_subtraction() -> None:
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                # A cumulative figure that disagrees; the discrete fact wins.
                duration("2025-01-01", "2025-06-30", 999.0),
                duration("2025-04-01", "2025-06-30", 110.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 6, 30)].revenue == pytest.approx(110.0)


@pytest.mark.unit
def test_a_gap_in_the_ladder_does_not_produce_a_bogus_quarter() -> None:
    # Six months between rungs is not a quarter, so nothing is invented for it.
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                duration("2025-01-01", "2025-09-30", 330.0),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert date(2025, 9, 30) not in periods


# -- concepts that must not be differenced -----------------------------------


@pytest.mark.unit
def test_share_counts_are_never_differenced_into_negatives() -> None:
    # A weighted average is not a sum. Subtracting the quarters from the year
    # produced share counts like -30 billion, which is how this was caught.
    document = facts_document(
        WeightedAverageNumberOfDilutedSharesOutstanding={
            "shares": [
                duration("2025-01-01", "2025-03-31", 1_000.0),
                duration("2025-04-01", "2025-06-30", 1_010.0),
                duration("2025-07-01", "2025-09-30", 1_020.0),
                duration("2025-01-01", "2025-12-31", 1_015.0),
            ]
        }
    )

    periods = adapter(document).get_financial_statements("AAPL")
    counts = [p.shares_outstanding for p in periods if p.shares_outstanding is not None]

    assert counts, "expected the discrete quarterly share counts"
    assert all(count > 0 for count in counts)
    # The fiscal Q4 stays absent rather than being invented by subtraction.
    assert date(2025, 12, 31) not in by_end(periods)


# -- duplicates and restatements ---------------------------------------------


@pytest.mark.unit
def test_the_most_recently_filed_value_wins_for_a_repeated_period() -> None:
    # One quarter appears in its own 10-Q, in the next year's comparatives, and
    # in each 10-K. A restatement arrives as a later filing of the same period.
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0, filed="2025-04-30"),
                duration("2025-01-01", "2025-03-31", 125.0, filed="2026-01-30"),
                duration("2025-01-01", "2025-03-31", 110.0, filed="2025-10-31"),
            ]
        }
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].revenue == pytest.approx(125.0)


# -- tag chains --------------------------------------------------------------


@pytest.mark.unit
def test_a_retired_tag_does_not_hide_the_current_one() -> None:
    # Apple still carries five 2010-era facts under a retired short-term
    # investments tag. Stopping at the first tag with any data returned a
    # decade-old number and nothing recent.
    document = facts_document(
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 40.0)]},
        AvailableForSaleSecuritiesDebtSecuritiesCurrent={"USD": [instant("2010-12-31", 16.0)]},
        MarketableSecuritiesCurrent={"USD": [instant("2025-03-31", 22.0)]},
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].cash == pytest.approx(62.0)


@pytest.mark.unit
def test_a_more_specific_tag_overrides_a_general_one_for_the_same_period() -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 90.0)]},
        RevenueFromContractWithCustomerExcludingAssessedTax={
            "USD": [duration("2025-01-01", "2025-03-31", 100.0)]
        },
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].revenue == pytest.approx(100.0)


@pytest.mark.unit
def test_a_filer_that_switched_tags_gets_one_continuous_series() -> None:
    document = facts_document(
        SalesRevenueNet={"USD": [duration("2024-01-01", "2024-03-31", 80.0)]},
        RevenueFromContractWithCustomerExcludingAssessedTax={
            "USD": [duration("2025-01-01", "2025-03-31", 100.0)]
        },
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2024, 3, 31)].revenue == pytest.approx(80.0)
    assert periods[date(2025, 3, 31)].revenue == pytest.approx(100.0)


@pytest.mark.unit
def test_gross_profit_is_derived_when_only_the_cost_of_revenue_is_tagged() -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CostOfRevenue={"USD": [duration("2025-01-01", "2025-03-31", 60.0)]},
    )

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].gross_profit == pytest.approx(40.0)


@pytest.mark.unit
def test_gross_profit_stays_missing_when_neither_concept_is_tagged() -> None:
    document = facts_document(Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]})

    periods = by_end(adapter(document).get_financial_statements("AAPL"))

    assert periods[date(2025, 3, 31)].gross_profit is None


# -- balance sheet -----------------------------------------------------------


@pytest.mark.unit
def test_instant_facts_are_joined_to_the_quarter_they_close() -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 500.0)]},
        LongTermDebtNoncurrent={"USD": [instant("2025-03-31", 200.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.cash == pytest.approx(500.0)
    assert period.total_debt == pytest.approx(200.0)


@pytest.mark.unit
def test_a_balance_sheet_dated_a_day_out_still_matches_its_quarter() -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-30", 500.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.cash == pytest.approx(500.0)


@pytest.mark.unit
def test_short_and_long_term_borrowings_are_summed() -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 500.0)]},
        LongTermDebtNoncurrent={"USD": [instant("2025-03-31", 200.0)]},
        DebtCurrent={"USD": [instant("2025-03-31", 50.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.total_debt == pytest.approx(250.0)


@pytest.mark.unit
def test_a_balance_sheet_with_no_borrowings_leaves_debt_unknown() -> None:
    # A filer that repaid its debt and one whose borrowing tag this adapter does
    # not recognise look identical. Reading either as zero would let a leveraged
    # company pass as debt-free, which is a far worse error than a debt-free one
    # losing its net-cash figure.
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 500.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.total_debt is None


@pytest.mark.unit
def test_a_filing_that_states_zero_debt_is_believed() -> None:
    # An explicit zero is a reported value, not an absence.
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 500.0)]},
        LongTermDebtNoncurrent={"USD": [instant("2025-03-31", 0.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.total_debt == 0.0


@pytest.mark.unit
def test_debt_stays_unknown_when_no_balance_sheet_was_found() -> None:
    # Nothing to infer from: the quarter has an income statement only.
    document = facts_document(Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]})

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.total_debt is None


@pytest.mark.unit
def test_operating_lease_schedules_are_not_treated_as_debt() -> None:
    # Future minimum lease payments are neither borrowings nor a balance-sheet
    # amount; counting them would overstate leverage for every lessee. With a
    # real borrowing alongside, only the borrowing is counted.
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        CashAndCashEquivalentsAtCarryingValue={"USD": [instant("2025-03-31", 500.0)]},
        LongTermDebtNoncurrent={"USD": [instant("2025-03-31", 200.0)]},
        LesseeOperatingLeaseLiabilityPaymentsDue={"USD": [instant("2025-03-31", 900.0)]},
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.total_debt == pytest.approx(200.0)


# -- signs and units ---------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("reported", [25.0, -25.0])
def test_capex_is_stored_as_a_positive_outflow(reported: float) -> None:
    document = facts_document(
        Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]},
        PaymentsToAcquirePropertyPlantAndEquipment={
            "USD": [duration("2025-01-01", "2025-03-31", reported)]
        },
    )

    period = by_end(adapter(document).get_financial_statements("AAPL"))[date(2025, 3, 31)]

    assert period.capital_expenditure == pytest.approx(25.0)


@pytest.mark.unit
def test_periods_are_returned_oldest_first_and_capped_at_the_limit() -> None:
    document = facts_document(
        Revenues={
            "USD": [
                duration("2025-01-01", "2025-03-31", 100.0),
                duration("2025-04-01", "2025-06-30", 110.0),
                duration("2025-07-01", "2025-09-30", 120.0),
            ]
        }
    )

    periods = adapter(document).get_financial_statements("AAPL", limit=2)

    assert [p.period_end for p in periods] == [date(2025, 6, 30), date(2025, 9, 30)]


@pytest.mark.unit
def test_every_period_is_tagged_with_its_source_and_currency() -> None:
    document = facts_document(Revenues={"USD": [duration("2025-01-01", "2025-03-31", 100.0)]})

    period = adapter(document).get_financial_statements("AAPL")[0]

    assert period.source == "sec-edgar"
    assert period.reported_currency == "USD"


@pytest.mark.unit
def test_a_filer_with_no_us_gaap_facts_returns_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in request.url.path:
            return httpx.Response(200, json=TICKER_MAP)
        return httpx.Response(200, json={"entityName": "Empty Co", "facts": {}})

    provider = SecEdgarFundamentals(
        USER_AGENT, client=httpx.Client(transport=httpx.MockTransport(handler))
    )

    assert provider.get_financial_statements("AAPL") == []
