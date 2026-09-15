"""Turning a security into the SEC documents that are actually evidence.

Phase 9K.14 fetched 25 submissions indexes — catalogues of filings, not the
filings themselves — and dropped three quarters of its sample for want of a CIK.
These tests cover the layer that closes both gaps: resolution must never invent
an identifier, and expansion must be bounded before a single request is issued.

No test reaches the network. Submissions payloads are fixtures in the SEC's own
column-wise shape.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    RequestKind,
    filing_targets,
    open_evidence_root,
    resolve_ciks,
    submissions_targets,
)
from stock_screener.sec_evidence.sample import Selection

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration


class FakeResolver:
    """The SEC ticker file, without the SEC."""

    def __init__(self, mapping: dict[str, int] | None = None) -> None:
        self.mapping = mapping or {"AAPL": 320193, "MSFT": 789019}
        self.asked: list[str] = []

    def cik_for(self, ticker: str) -> int | None:
        self.asked.append(ticker)
        return self.mapping.get(ticker.strip().upper())


def live(symbol: str) -> Selection:
    """A sampled live company, which carries no CIK of its own."""
    return Selection(bucket="LIVE_REMAINDER", symbol=symbol, name=symbol, cik=None, company_id=1)


def delisted(symbol: str, cik: int | None) -> Selection:
    """A sampled delisted security."""
    return Selection(
        bucket="DELISTED_WITH_CIK", symbol=symbol, name=symbol, cik=cik, historical_security_id=1
    )


def submissions(*rows: tuple[str, str, str, str]) -> Mapping[str, object]:
    """A submissions document in the SEC's column-wise shape."""
    return {
        "cik": "0000320193",
        "filings": {
            "recent": {
                "accessionNumber": [r[0] for r in rows],
                "form": [r[1] for r in rows],
                "filingDate": [r[2] for r in rows],
                "reportDate": ["" for _ in rows],
                "primaryDocument": [r[3] for r in rows],
            }
        },
    }


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    """A verified evidence root."""
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def store(root: EvidenceRoot, cik: int, payload: Mapping[str, object]) -> None:
    """Put a submissions document where expansion will look for it."""
    path = root.submissions_path(cik)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestNoIdentifierIsInvented:
    def test_a_live_ticker_the_sec_maps_resolves(self) -> None:
        resolutions = resolve_ciks([live("AAPL")], FakeResolver())

        assert resolutions[0].cik == 320193
        assert resolutions[0].source == "EDGAR"

    def test_a_live_ticker_the_sec_does_not_map_stays_unresolved(self) -> None:
        resolutions = resolve_ciks([live("NOSUCH")], FakeResolver())

        assert resolutions[0].cik is None
        assert resolutions[0].source == "UNRESOLVED"

    def test_a_stored_cik_is_preserved_and_never_looked_up(self) -> None:
        """A security's own CIK beats today's holder of its symbol — the BBBY rule."""
        resolver = FakeResolver({"BBBY": 999999})

        resolutions = resolve_ciks([delisted("BBBY", 886158)], resolver)

        assert resolutions[0].cik == 886158
        assert resolutions[0].source == "STORED"
        assert resolver.asked == []

    def test_a_delisted_security_without_a_cik_stays_unresolved(self) -> None:
        resolutions = resolve_ciks([delisted("ZZZZ", None)], FakeResolver())

        assert resolutions[0].cik is None

    def test_without_a_resolver_only_stored_ciks_survive(self) -> None:
        """A dry run must not reach for the ticker file."""
        resolutions = resolve_ciks([live("AAPL"), delisted("BBBY", 886158)], None)

        assert [r.cik for r in resolutions] == [None, 886158]

    @pytest.mark.parametrize(
        "symbol", ["STRR", "PRTYQ", "BC.PRC", "NEE.PRU", "CCIIU", "LPAAU", "OXLCZ"]
    )
    def test_the_samples_awkward_symbols_do_not_crash(self, symbol: str) -> None:
        """Dotted class tickers do not match the SEC's dashed form, and must not."""
        resolutions = resolve_ciks([live(symbol)], FakeResolver())

        assert resolutions[0].cik is None
        assert len(resolutions) == 1

    def test_a_dotted_class_ticker_is_never_translated_to_the_dashed_form(self) -> None:
        """`BC.PRC` and `NEE.PRU` stay unresolved, deliberately.

        The SEC writes class and preferred shares with a dash — `BC-PRC` — and
        `normalise_ticker` only strips and upper-cases. No rule in this
        repository maps `.` to `-`, and inventing one would be a guess about
        which security is meant: the dotted form is a vendor convention, and the
        same base symbol carries several classes. An unresolved ticker costs
        coverage; a wrong CIK would attach one issuer's filings to another.
        """
        resolver = FakeResolver({"BC-PRC": 111111, "NEE-PRU": 222222})

        resolutions = resolve_ciks([live("BC.PRC"), live("NEE.PRU")], resolver)

        assert [r.cik for r in resolutions] == [None, None]
        assert [r.source for r in resolutions] == ["UNRESOLVED", "UNRESOLVED"]

    def test_resolution_is_deterministic(self) -> None:
        batch = [live("AAPL"), live("NOSUCH"), delisted("BBBY", 886158)]

        first = [(r.cik, r.source) for r in resolve_ciks(batch, FakeResolver())]
        second = [(r.cik, r.source) for r in resolve_ciks(batch, FakeResolver())]

        assert first == second


class TestSubmissionsBecomeFilingDocuments:
    def test_a_stored_index_produces_document_targets(self, root: EvidenceRoot) -> None:
        store(root, 320193, submissions(("0000320193-24-000001", "10-K", "2024-11-01", "aapl.htm")))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        targets = filing_targets(root, resolutions, filings_per_security=5)

        assert len(targets) == 1
        assert targets[0].kind is RequestKind.FILING
        assert targets[0].url.endswith("/Archives/edgar/data/320193/000032019324000001/aapl.htm")

    def test_provenance_metadata_survives_expansion(self, root: EvidenceRoot) -> None:
        store(root, 320193, submissions(("0000320193-24-000001", "10-K", "2024-11-01", "aapl.htm")))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        target = filing_targets(root, resolutions, filings_per_security=5)[0]

        assert target.cik == 320193
        assert target.accession == "0000320193-24-000001"
        assert target.form == "10-K"
        assert target.filing_date == "2024-11-01"
        assert target.document == "aapl.htm"

    def test_the_cap_bounds_the_expansion(self, root: EvidenceRoot) -> None:
        rows = tuple(
            (f"0000320193-24-00000{i}", "8-K", f"2024-0{i}-01", f"doc{i}.htm") for i in range(1, 6)
        )
        store(root, 320193, submissions(*rows))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        assert len(filing_targets(root, resolutions, filings_per_security=2)) == 2

    def test_a_cap_of_zero_expands_nothing(self, root: EvidenceRoot) -> None:
        store(root, 320193, submissions(("0000320193-24-000001", "10-K", "2024-11-01", "a.htm")))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        assert filing_targets(root, resolutions, filings_per_security=0) == []

    def test_expansion_is_deterministic(self, root: EvidenceRoot) -> None:
        rows = tuple(
            (f"0000320193-24-00000{i}", "8-K", f"2024-0{i}-01", f"doc{i}.htm") for i in range(1, 6)
        )
        store(root, 320193, submissions(*rows))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        first = [t.url for t in filing_targets(root, resolutions, filings_per_security=3)]
        second = [t.url for t in filing_targets(root, resolutions, filings_per_security=3)]

        assert first == second

    def test_a_filing_with_no_primary_document_is_skipped(self, root: EvidenceRoot) -> None:
        """Its URL would be the filing's index page, which is a listing not a document."""
        store(root, 320193, submissions(("0000320193-24-000001", "10-K", "2024-11-01", "")))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        assert filing_targets(root, resolutions, filings_per_security=5) == []

    def test_an_unharvested_security_expands_to_nothing(self, root: EvidenceRoot) -> None:
        """Expansion reads disk and never fetches as a side effect."""
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        assert filing_targets(root, resolutions, filings_per_security=5) == []

    def test_a_malformed_stored_index_fails_safely(self, root: EvidenceRoot) -> None:
        path = root.submissions_path(320193)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        assert filing_targets(root, resolutions, filings_per_security=5) == []

    def test_ragged_index_columns_do_not_pair_the_wrong_fields(self, root: EvidenceRoot) -> None:
        """A short column bounds the read rather than zipping against nothing."""
        payload = {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-24-000001", "0000320193-24-000002"],
                    "form": ["10-K"],
                    "filingDate": ["2024-11-01"],
                    "primaryDocument": ["a.htm", "b.htm"],
                }
            }
        }
        store(root, 320193, payload)
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)

        targets = filing_targets(root, resolutions, filings_per_security=5)

        assert len(targets) == 1
        assert targets[0].accession == "0000320193-24-000001"

    def test_an_unresolved_security_contributes_no_target(self, root: EvidenceRoot) -> None:
        resolutions = resolve_ciks([delisted("ZZZZ", None)], None)

        assert filing_targets(root, resolutions, filings_per_security=5) == []
        assert submissions_targets(resolutions) == []


class TestExpandedTargetsStayInsideTheRoot:
    def test_a_document_target_resolves_beneath_the_evidence_root(self, root: EvidenceRoot) -> None:
        store(root, 320193, submissions(("0000320193-24-000001", "10-K", "2024-11-01", "a.htm")))
        resolutions = resolve_ciks([delisted("AAPL", 320193)], None)
        target = filing_targets(root, resolutions, filings_per_security=1)[0]

        path = root.filing_path(target.cik or 0, target.accession or "", target.document or "")

        assert path.is_relative_to(root.path)

    @pytest.mark.parametrize("document", ["../../etc/passwd", "/etc/passwd", "..", "a/b.htm"])
    def test_a_document_name_that_would_escape_is_refused(
        self, root: EvidenceRoot, document: str
    ) -> None:
        with pytest.raises(ValueError, match="usable SEC document name"):
            root.filing_path(320193, "0000320193-24-000001", document)
