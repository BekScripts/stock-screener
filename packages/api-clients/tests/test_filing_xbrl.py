"""Reading numbers out of a filing's own XBRL when company-facts is behind.

`unit`: no SEC request is made. The instances here are small but shaped exactly
like a real one, because every risk in this parser is a property of the format
rather than of the code being obviously wrong — dimensional contexts that look
like consolidated ones, instants that look like durations, and comparatives that
look like new history.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from api_clients import ProviderDataError, SecEdgarFundamentals
from api_clients.edgar import FILING_INSTANCE_SOURCE
from api_clients.filing_xbrl import merge_instance_facts, parse_filing_instance
from domain import PeriodCadence

MILLION = 1_000_000.0
ACCESSION = "0001628280-26-025362"


def instance(body: str) -> str:
    """Wrap fact and context XML in the namespaces a real instance declares."""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<xbrl xmlns="http://www.xbrl.org/2003/instance" '
        'xmlns:ifrs-full="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" '
        'xmlns:dei="http://xbrl.sec.gov/dei/2025" '
        'xmlns:tsm="http://www.tsmc.com/20251231" '
        'xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        f"{body}</xbrl>"
    )


def duration_context(identifier: str, start: str, end: str, *, member: str | None = None) -> str:
    """One duration context, optionally scoped to a dimensional member."""
    segment = (
        f'<segment><xbrldi:explicitMember dimension="tsm:Axis">{member}'
        "</xbrldi:explicitMember></segment>"
        if member
        else ""
    )
    return (
        f'<context id="{identifier}"><entity>'
        '<identifier scheme="http://www.sec.gov/CIK">0001046179</identifier>'
        f"{segment}</entity>"
        f"<period><startDate>{start}</startDate><endDate>{end}</endDate></period></context>"
    )


def instant_context(identifier: str, moment: str) -> str:
    return (
        f'<context id="{identifier}"><entity>'
        '<identifier scheme="http://www.sec.gov/CIK">0001046179</identifier></entity>'
        f"<period><instant>{moment}</instant></period></context>"
    )


UNITS = (
    '<unit id="twd"><measure>iso4217:TWD</measure></unit>'
    '<unit id="usd"><measure>iso4217:USD</measure></unit>'
    '<unit id="shares"><measure>xbrli:shares</measure></unit>'
    '<unit id="eps"><divide><unitNumerator><measure>iso4217:TWD</measure></unitNumerator>'
    "<unitDenominator><measure>xbrli:shares</measure></unitDenominator></divide></unit>"
)


def parse(body: str) -> dict[str, Any]:
    return parse_filing_instance(
        instance(body), accession=ACCESSION, form="20-F", filed="2026-04-16"
    )


# -- contexts and units -----------------------------------------------------


@pytest.mark.unit
def test_a_duration_fact_keeps_both_of_its_dates() -> None:
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="twd">3809054300000</ifrs-full:Revenue>'
    )

    fact = facts["ifrs-full"]["Revenue"]["units"]["TWD"][0]
    assert fact["start"] == "2025-01-01"
    assert fact["end"] == "2025-12-31"
    assert fact["val"] == pytest.approx(3_809_054.3 * MILLION)


@pytest.mark.unit
def test_an_instant_fact_has_an_end_and_no_start() -> None:
    # The same shape company-facts uses for a balance sheet, which is what lets
    # the existing instant/duration split work on these facts unchanged.
    facts = parse(
        UNITS
        + instant_context("i-1", "2025-12-31")
        + '<ifrs-full:CashAndCashEquivalents contextRef="i-1" unitRef="twd">2767856400000'
        "</ifrs-full:CashAndCashEquivalents>"
    )

    fact = facts["ifrs-full"]["CashAndCashEquivalents"]["units"]["TWD"][0]
    assert fact["end"] == "2025-12-31"
    assert "start" not in fact


@pytest.mark.unit
def test_units_are_named_the_way_company_facts_names_them() -> None:
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="twd">1</ifrs-full:Revenue>'
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="usd">2</ifrs-full:Revenue>'
        + '<ifrs-full:WeightedAverageShares contextRef="c-1" unitRef="shares">3'
        "</ifrs-full:WeightedAverageShares>"
    )

    assert set(facts["ifrs-full"]["Revenue"]["units"]) == {"TWD", "USD"}
    assert set(facts["ifrs-full"]["WeightedAverageShares"]["units"]) == {"shares"}


@pytest.mark.unit
def test_a_divided_unit_is_ignored() -> None:
    # Earnings per share is neither money nor a share count, and no normalized
    # field reads one.
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<ifrs-full:BasicEarningsLossPerShare contextRef="c-1" unitRef="eps">59.56'
        "</ifrs-full:BasicEarningsLossPerShare>"
    )

    assert "BasicEarningsLossPerShare" not in facts.get("ifrs-full", {})


# -- dimension safety -------------------------------------------------------


@pytest.mark.unit
def test_a_segmented_fact_is_refused() -> None:
    # The most important property here. A raw instance carries revenue by
    # geography, by product and by subsidiary under the same concept, date and
    # unit as the consolidated figure, distinguishable only by context. Company
    # facts filters them out before anyone sees them; so does this.
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + duration_context("c-2", "2025-01-01", "2025-12-31", member="tsm:NorthAmericaMember")
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="twd">3809054300000</ifrs-full:Revenue>'
        + '<ifrs-full:Revenue contextRef="c-2" unitRef="twd">2700000000000</ifrs-full:Revenue>'
    )

    entries = facts["ifrs-full"]["Revenue"]["units"]["TWD"]
    assert len(entries) == 1
    assert entries[0]["val"] == pytest.approx(3_809_054.3 * MILLION)


@pytest.mark.unit
def test_segments_are_never_summed_into_a_total() -> None:
    # Where only dimensional facts exist the field stays absent. Adding the
    # segments would produce a total the filer never stated, and one that is
    # wrong whenever the segments do not tile the business exactly.
    facts = parse(
        UNITS
        + duration_context("c-2", "2025-01-01", "2025-12-31", member="tsm:NorthAmericaMember")
        + duration_context("c-3", "2025-01-01", "2025-12-31", member="tsm:AsiaMember")
        + '<ifrs-full:Revenue contextRef="c-2" unitRef="twd">2700000000000</ifrs-full:Revenue>'
        + '<ifrs-full:Revenue contextRef="c-3" unitRef="twd">1100000000000</ifrs-full:Revenue>'
    )

    assert facts == {} or "Revenue" not in facts.get("ifrs-full", {})


# -- taxonomy scope ---------------------------------------------------------


@pytest.mark.unit
def test_a_company_extension_concept_is_not_guessed_at() -> None:
    # A chain that has never heard of `tsm:SomethingSpecial` cannot say what it
    # means, and inventing a mapping is how a segment becomes a total.
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<tsm:AdjustedOperatingRevenue contextRef="c-1" unitRef="twd">1'
        "</tsm:AdjustedOperatingRevenue>"
    )

    assert "tsm" not in facts


@pytest.mark.unit
def test_us_gaap_concepts_parse_the_same_way() -> None:
    # The fallback is not IFRS-specific. A domestic filer whose company-facts
    # lagged would be read by exactly this path.
    facts = parse(
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<us-gaap:Revenues contextRef="c-1" unitRef="usd">500000000</us-gaap:Revenues>'
    )

    assert facts["us-gaap"]["Revenues"]["units"]["USD"][0]["val"] == pytest.approx(500.0 * MILLION)


@pytest.mark.unit
def test_a_document_that_is_not_xml_is_rejected() -> None:
    with pytest.raises(ProviderDataError, match="not valid XML"):
        parse_filing_instance("<not xml", accession=ACCESSION, form="20-F", filed="2026-04-16")


# -- merging ----------------------------------------------------------------


@pytest.mark.unit
def test_merging_keeps_facts_from_both_sources() -> None:
    primary = {"ifrs-full": {"Revenue": {"units": {"TWD": [{"end": "2024-12-31", "val": 1.0}]}}}}
    extra = {"ifrs-full": {"Revenue": {"units": {"TWD": [{"end": "2025-12-31", "val": 2.0}]}}}}

    merged = merge_instance_facts(primary, extra)

    assert len(merged["ifrs-full"]["Revenue"]["units"]["TWD"]) == 2
    # Neither input is modified.
    assert len(primary["ifrs-full"]["Revenue"]["units"]["TWD"]) == 1


# -- the trigger, through the adapter ---------------------------------------

TICKER_MAP: dict[str, Any] = {"0": {"cik_str": 1046179, "ticker": "TSM", "title": "TSM"}}


def build_adapter(
    *,
    facts_through_2024: bool = True,
    filing_period: str | None = "2025-12-31",
    instance_body: str | None = None,
    instance_status: int = 200,
    counter: list[str] | None = None,
) -> SecEdgarFundamentals:
    """An adapter whose company-facts stops at 2024 and whose filing may be newer."""
    annual = [
        {"start": f"{y}-01-01", "end": f"{y}-12-31", "val": v, "filed": f"{y + 1}-04-17"}
        for y, v in ((2023, 2_161_735.8 * MILLION), (2024, 2_894_307.7 * MILLION))
    ]
    facts = {
        "cik": 1046179,
        "entityName": "TSM",
        "facts": {
            "ifrs-full": {"Revenue": {"units": {"TWD": annual if facts_through_2024 else []}}}
        },
    }
    submissions = {
        "cik": 1046179,
        "filings": {
            "recent": {
                "accessionNumber": [ACCESSION],
                "form": ["20-F"],
                "filingDate": ["2026-04-16"],
                "reportDate": [filing_period or ""],
                "primaryDocument": ["tsm-20251231.htm"],
            }
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = str(request.url)
        if "company_tickers" in path:
            return httpx.Response(200, json=TICKER_MAP)
        if "submissions" in path:
            return httpx.Response(200, json=submissions)
        if "companyfacts" in path:
            return httpx.Response(200, json=facts)
        if path.endswith("_htm.xml"):
            if counter is not None:
                counter.append(path)
            if instance_status != 200 or instance_body is None:
                return httpx.Response(instance_status, text="not found")
            return httpx.Response(200, text=instance(instance_body))
        return httpx.Response(404)

    return SecEdgarFundamentals(
        "test test@example.com", client=httpx.Client(transport=httpx.MockTransport(handler))
    )


FY2025 = (
    UNITS
    + duration_context("c-1", "2025-01-01", "2025-12-31")
    + duration_context("c-0", "2024-01-01", "2024-12-31")
    + '<ifrs-full:Revenue contextRef="c-1" unitRef="twd">3809054300000</ifrs-full:Revenue>'
    + '<ifrs-full:Revenue contextRef="c-0" unitRef="twd">9999999999999</ifrs-full:Revenue>'
)


@pytest.mark.unit
def test_a_filing_newer_than_company_facts_supplies_the_missing_year() -> None:
    periods = build_adapter(instance_body=FY2025).get_financial_statements("TSM")

    assert periods[-1].period_end == date(2025, 12, 31)
    assert periods[-1].revenue == pytest.approx(3_809_054.3 * MILLION)
    assert periods[-1].source == FILING_INSTANCE_SOURCE
    assert periods[-1].cadence is PeriodCadence.ANNUAL


@pytest.mark.unit
def test_a_comparative_year_in_the_new_filing_does_not_rewrite_history() -> None:
    # The FY2025 filing restates FY2024 — here with an obviously wrong value, so
    # a leak would be unmissable. Company-facts stays authoritative for the
    # periods it covers; the instance only adds what comes after them.
    periods = build_adapter(instance_body=FY2025).get_financial_statements("TSM")

    by_end = {period.period_end: period for period in periods}
    assert len(periods) == 3
    assert by_end[date(2024, 12, 31)].revenue == pytest.approx(2_894_307.7 * MILLION)
    assert by_end[date(2024, 12, 31)].source == "sec-edgar"


@pytest.mark.unit
def test_no_fallback_when_company_facts_already_covers_the_filing() -> None:
    calls: list[str] = []
    adapter = build_adapter(filing_period="2024-12-31", instance_body=FY2025, counter=calls)

    periods = adapter.get_financial_statements("TSM")

    assert calls == []
    assert periods[-1].period_end == date(2024, 12, 31)
    assert all(period.source == "sec-edgar" for period in periods)


@pytest.mark.unit
def test_a_failed_instance_retrieval_keeps_the_existing_history() -> None:
    # An unreachable document costs the newest year and nothing else. Periods are
    # never erased and no number is invented to stand in for the missing one.
    periods = build_adapter(instance_status=404).get_financial_statements("TSM")

    assert [period.period_end for period in periods] == [date(2023, 12, 31), date(2024, 12, 31)]
    assert periods[-1].revenue == pytest.approx(2_894_307.7 * MILLION)


@pytest.mark.unit
def test_an_instance_missing_a_field_leaves_it_none() -> None:
    # Only revenue is tagged in this instance. The rest of the period is absent
    # rather than filled from the prior year or from zero.
    periods = build_adapter(instance_body=FY2025).get_financial_statements("TSM")

    latest = periods[-1]
    assert latest.revenue is not None
    assert latest.operating_cash_flow is None
    assert latest.total_debt is None


@pytest.mark.unit
def test_the_instance_is_downloaded_once_per_accession() -> None:
    calls: list[str] = []
    adapter = build_adapter(instance_body=FY2025, counter=calls)

    adapter.get_financial_statements("TSM")
    adapter.get_financial_statements("TSM")

    assert len(calls) == 1


@pytest.mark.unit
def test_the_reporting_currency_still_wins_over_a_convenience_translation() -> None:
    # The instance carries TSM's USD courtesy column too. Unit selection is the
    # same code as the primary path, so the answer is the same: TWD.
    body = (
        UNITS
        + duration_context("c-1", "2025-01-01", "2025-12-31")
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="twd">3809054300000</ifrs-full:Revenue>'
        + '<ifrs-full:Revenue contextRef="c-1" unitRef="usd">121423500000</ifrs-full:Revenue>'
    )

    periods = build_adapter(instance_body=body).get_financial_statements("TSM")

    assert periods[-1].reported_currency == "TWD"
    assert periods[-1].revenue == pytest.approx(3_809_054.3 * MILLION)


@pytest.mark.unit
def test_a_segmented_only_instance_adds_no_period() -> None:
    body = (
        UNITS
        + duration_context("c-2", "2025-01-01", "2025-12-31", member="tsm:AsiaMember")
        + '<ifrs-full:Revenue contextRef="c-2" unitRef="twd">3809054300000</ifrs-full:Revenue>'
    )

    periods = build_adapter(instance_body=body).get_financial_statements("TSM")

    assert periods[-1].period_end == date(2024, 12, 31)
