"""Reading the SEC submissions index into filing metadata.

The index is published column-wise — one array per field, aligned by position —
which is the detail that makes it worth testing. A misaligned read pairs a form
with the wrong accession number and produces citations that look right and point
at the wrong document.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx
import pytest

from api_clients import SecEdgarFundamentals
from api_clients.edgar import FILING_FORMS

CIK = 320193


def _submissions(**index: Any) -> dict[str, Any]:
    """Wrap an index in the envelope the SEC returns."""
    return {"cik": CIK, "filings": {"recent": index}}


def _client(payload: dict[str, Any], *, tickers: dict[str, Any] | None = None) -> httpx.Client:
    """A transport serving the ticker map and one submissions document."""
    mapping = tickers or {"0": {"cik_str": CIK, "ticker": "AAPL", "title": "Apple Inc."}}

    def handler(request: httpx.Request) -> httpx.Response:
        if "company_tickers" in str(request.url):
            return httpx.Response(200, json=mapping)
        if "submissions" in str(request.url):
            return httpx.Response(200, json=payload)
        return httpx.Response(404)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _provider(payload: dict[str, Any]) -> SecEdgarFundamentals:
    return SecEdgarFundamentals(user_agent="test test@example.com", client=_client(payload))


@pytest.mark.unit
def test_reads_a_filing_from_the_index() -> None:
    provider = _provider(
        _submissions(
            accessionNumber=["0000320193-26-000073"],
            form=["10-Q"],
            filingDate=["2026-05-02"],
            reportDate=["2026-03-31"],
            primaryDocument=["aapl-20260331.htm"],
        )
    )

    filings = provider.get_filings("AAPL")

    assert len(filings) == 1
    filing = filings[0]
    assert filing.accession == "0000320193-26-000073"
    assert filing.form == "10-Q"
    assert filing.filed == date(2026, 5, 2)
    assert filing.period_end == date(2026, 3, 31)
    assert filing.primary_document == "aapl-20260331.htm"
    assert filing.source == "sec-edgar"


@pytest.mark.unit
def test_builds_the_canonical_archive_url() -> None:
    provider = _provider(
        _submissions(
            accessionNumber=["0000320193-26-000073"],
            form=["10-Q"],
            filingDate=["2026-05-02"],
            reportDate=["2026-03-31"],
            primaryDocument=["aapl-20260331.htm"],
        )
    )

    url = provider.get_filings("AAPL")[0].url

    # The path strips the dashes; the printed accession keeps them.
    assert url == (
        "https://www.sec.gov/Archives/edgar/data/320193/000032019326000073/aapl-20260331.htm"
    )


@pytest.mark.unit
def test_falls_back_to_the_filing_index_page_without_a_primary_document() -> None:
    provider = _provider(
        _submissions(
            accessionNumber=["0000320193-26-000073"],
            form=["8-K"],
            filingDate=["2026-05-02"],
            reportDate=[""],
            primaryDocument=[""],
        )
    )

    filing = provider.get_filings("AAPL")[0]

    assert filing.primary_document is None
    assert filing.url.endswith("/0000320193-26-000073-index.htm")


@pytest.mark.unit
def test_keeps_only_the_forms_a_brief_may_cite() -> None:
    # The recent index is mostly ownership reports and registration statements.
    provider = _provider(
        _submissions(
            accessionNumber=["a-1", "a-2", "a-3", "a-4", "a-5"],
            form=["10-K", "4", "10-Q", "S-8", "8-K"],
            filingDate=["2026-02-01", "2026-02-02", "2026-05-02", "2026-05-03", "2026-06-01"],
            reportDate=["2025-12-31", "", "2026-03-31", "", ""],
            primaryDocument=["a.htm", "b.htm", "c.htm", "d.htm", "e.htm"],
        )
    )

    forms = {filing.form for filing in provider.get_filings("AAPL")}

    assert forms == {"10-K", "10-Q", "8-K"}
    assert forms <= FILING_FORMS


@pytest.mark.unit
def test_keeps_the_foreign_private_issuer_annual_reports() -> None:
    # A 20-F filer has no 10-K and no 10-Q, so indexing only the domestic forms
    # left every foreign issuer with no filings at all.
    provider = _provider(
        _submissions(
            accessionNumber=["b-1", "b-2", "b-3", "b-4"],
            form=["20-F", "6-K", "SC 13G", "40-F"],
            filingDate=["2026-04-16", "2026-08-14", "2026-08-15", "2026-03-01"],
            reportDate=["2025-12-31", "2026-06-30", "", "2025-12-31"],
            primaryDocument=["a.htm", "b.htm", "c.htm", "d.htm"],
        )
    )

    forms = {filing.form for filing in provider.get_filings("AAPL")}

    assert forms == {"20-F", "40-F"}


@pytest.mark.unit
def test_a_flood_of_six_ks_does_not_hide_the_annual_report() -> None:
    # TSM files fifty to ninety 6-Ks a year against one 20-F. Indexing them left
    # the eight most recent filings all 6-Ks, so the only filing with text worth
    # quoting was unreachable.
    count = 12
    provider = _provider(
        _submissions(
            accessionNumber=[f"c-{index}" for index in range(count)] + ["annual"],
            form=["6-K"] * count + ["20-F"],
            filingDate=[f"2026-08-{index + 1:02d}" for index in range(count)] + ["2026-04-16"],
            reportDate=[""] * count + ["2025-12-31"],
            primaryDocument=[f"{index}.htm" for index in range(count)] + ["a.htm"],
        )
    )

    filings = provider.get_filings("AAPL")

    assert [filing.form for filing in filings] == ["20-F"]


@pytest.mark.unit
def test_returns_filings_newest_first() -> None:
    provider = _provider(
        _submissions(
            accessionNumber=["old", "new", "middle"],
            form=["8-K", "8-K", "8-K"],
            filingDate=["2026-01-01", "2026-06-01", "2026-03-01"],
            reportDate=["", "", ""],
            primaryDocument=["a.htm", "b.htm", "c.htm"],
        )
    )

    filed = [filing.filed for filing in provider.get_filings("AAPL")]

    assert filed == sorted(filed, reverse=True)


@pytest.mark.unit
def test_caps_the_number_returned() -> None:
    count = 12
    provider = _provider(
        _submissions(
            accessionNumber=[f"a-{index}" for index in range(count)],
            form=["8-K"] * count,
            filingDate=[f"2026-01-{index + 1:02d}" for index in range(count)],
            reportDate=[""] * count,
            primaryDocument=["a.htm"] * count,
        )
    )

    assert len(provider.get_filings("AAPL", limit=3)) == 3


@pytest.mark.unit
def test_ignores_rows_beyond_the_shortest_column() -> None:
    # A filer whose arrays disagree in length must not pair a form with another
    # filing's accession number.
    provider = _provider(
        _submissions(
            accessionNumber=["a-1", "a-2"],
            form=["10-K", "10-Q", "8-K"],
            filingDate=["2026-02-01"],
            reportDate=["2025-12-31"],
            primaryDocument=["a.htm"],
        )
    )

    filings = provider.get_filings("AAPL")

    assert len(filings) == 1
    assert filings[0].accession == "a-1"
    assert filings[0].form == "10-K"


@pytest.mark.unit
def test_a_row_without_a_filing_date_is_dropped() -> None:
    provider = _provider(
        _submissions(
            accessionNumber=["a-1", "a-2"],
            form=["10-K", "10-Q"],
            filingDate=["", "2026-05-02"],
            reportDate=["", "2026-03-31"],
            primaryDocument=["a.htm", "b.htm"],
        )
    )

    assert [filing.accession for filing in provider.get_filings("AAPL")] == ["a-2"]


@pytest.mark.unit
def test_an_unknown_ticker_has_no_filings() -> None:
    provider = _provider(_submissions(accessionNumber=[], form=[], filingDate=[]))

    assert provider.get_filings("NOSUCH") == []


@pytest.mark.unit
def test_a_submissions_document_without_an_index_has_no_filings() -> None:
    provider = _provider({"cik": CIK, "filings": {}})

    assert provider.get_filings("AAPL") == []


@pytest.mark.unit
def test_a_submissions_document_without_filings_at_all_has_no_filings() -> None:
    provider = _provider({"cik": CIK})

    assert provider.get_filings("AAPL") == []
