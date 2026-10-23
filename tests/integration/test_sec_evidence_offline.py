"""Reading the harvested corpus offline, and refusing to read the wrong one.

Two failure modes drive almost every test here.

**The wrong database looks exactly like the right one.** Phase 9K.25 ran a chunk
against the production database instead of the staging one and every gate passed
— converged, reconciled, checkpoint written — over a population 3,150 identities
smaller. `TestSourceBindingIsFailClosed` is the guarantee that cannot happen
again silently.

**A parser can invent history.** A filing states what was true on one day.
Everything that turns those points into monthly coverage is inference, and the
tests below pin the boundaries of that inference: carry-forward never reaches
backwards, an evidence date never comes from when we downloaded something, and a
class the evidence cannot settle stays `UNKNOWN` rather than becoming common
stock because a ticker happened to be present.
"""

from __future__ import annotations

import json
import socket
from datetime import date
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import (
    Company,
    FtdIdentityObservation,
    HistoricalSecurity,
    SecurityIdentifier,
)
from domain import CompanyProfile
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    BindingError,
    Entry,
    Exchange,
    JoinCache,
    JoinState,
    Manifest,
    Outcome,
    bind_source,
    build_index,
    derived_root,
    evidence_date_for,
    extract_catalogue,
    extract_filing,
    fingerprint_corpus,
    join_symbol,
    load_fingerprint,
    normalise_exchange,
    open_evidence_root,
    open_index,
    reconcile,
    save_fingerprint,
    store_atomically,
)
from stock_screener.sec_evidence.coverage import (
    carry_forward_coverage,
    catalogue_opportunity,
    class_breakdown,
    span_statistics,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration


def cover(*securities: str, contexts: str = "") -> str:
    """An inline-XBRL cover page carrying the given fact blocks."""
    return f"<html><body>{contexts}{''.join(securities)}</body></html>"


def fact(concept: str, value: str, context: str = "c1") -> str:
    """One `ix:nonNumeric` cover fact."""
    return f'<ix:nonNumeric contextRef="{context}" name="dei:{concept}">{value}</ix:nonNumeric>'


def security(title: str, symbol: str, exchange: str, context: str = "c1") -> str:
    """The three cover facts describing one registered class."""
    return (
        fact("Security12bTitle", title, context)
        + fact("TradingSymbol", symbol, context)
        + fact("SecurityExchangeName", exchange, context)
    )


def entry(url: str = "https://sec.gov/f.htm", **overrides: object) -> Entry:
    """A trusted FILING manifest record."""
    fields: dict[str, object] = {
        "url": url,
        "outcome": Outcome.DOWNLOADED,
        "status": 200,
        "sha256": "a" * 64,
        "bytes": 100,
        "path": "filings/0000000001/x/f.htm",
        "request_kind": "FILING",
        "cik": 1,
        "accession": "0000000001-24-000001",
        "form": "8-K",
        "filing_date": "2024-03-15",
    }
    fields.update(overrides)
    return Entry(**fields)  # type: ignore[arg-type]


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


@pytest.fixture
def factory(tmp_path: Path) -> Iterator[sessionmaker[Session]]:
    """A small production universe: 3 live companies, 2 delisted securities."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'stage.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    with session_scope(maker) as session:
        for i, ticker in enumerate(("AAA", "BBB", "CCC")):
            CompanyRepository(session).upsert_profile(
                CompanyProfile(ticker=ticker, name=f"Co {i}", exchange="NASDAQ")
            )
        for i, ticker in enumerate(("DDD", "EEE")):
            session.add(
                HistoricalSecurity(
                    primary_ticker=ticker, name=f"Gone {i}", cik=900 + i, status="UNKNOWN"
                )
            )
        session.flush()
    try:
        yield maker
    finally:
        engine.dispose()


@pytest.fixture
def bbby(tmp_path: Path) -> Iterator[tuple[sessionmaker[Session], int, int]]:
    """The real `BBBY` record: Bed Bath through 2023-05, a different company from 2025-08.

    The same shape `test_pit_attribution` pins, rebuilt here because this phase
    asks a different question of it — not "which month" but "which date" — and
    the answer must come out the same.
    """
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'bbby.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    with session_scope(maker) as session:
        security = HistoricalSecurity(
            primary_ticker="BBBY",
            name="Bed Bath & Beyond Inc.",
            cik=886158,
            last_trading_date=date(2023, 5, 2),
            status="UNKNOWN",
        )
        session.add(security)
        session.flush()
        security_id = int(security.id)
        CompanyRepository(session).upsert_profile(
            CompanyProfile(ticker="BBBY", name="Neighborhood Intelligence, Inc.", exchange="NYSE")
        )
        live_id = int(
            session.execute(select(Company.id).where(Company.ticker == "BBBY")).scalar_one()
        )
        for cusip, name, first, last in (
            ("075896100", "BED BATH & BEYOND INC", date(2004, 3, 24), date(2023, 5, 3)),
            ("690370101", "BED BATH & BEYOND, INC", date(2025, 9, 2), date(2026, 8, 12)),
        ):
            session.add(
                FtdIdentityObservation(
                    cusip=cusip,
                    issuer_base=cusip[:6],
                    symbol="BBBY",
                    issuer_name=name,
                    first_settlement_date=first,
                    last_settlement_date=last,
                    observation_count=100,
                    cusip_valid=True,
                    first_source_file="cnsfails.zip",
                    last_source_file="cnsfails.zip",
                    evidence_hash=f"{cusip}{first}",
                )
            )
        session.add(
            SecurityIdentifier(
                historical_security_id=security_id,
                cusip="075896100",
                ticker="BBBY",
                valid_from=date(2004, 3, 24),
                valid_to=date(2023, 5, 3),
                source="SEC_FTD",
                confidence="OBSERVED",
                lineage="INITIAL",
                issuer_name="BBBY",
            )
        )
        session.flush()
    try:
        yield maker, security_id, live_id
    finally:
        engine.dispose()


class TestSourceBindingIsFailClosed:
    """The 9K.25 near-miss, made impossible to repeat quietly."""

    def test_the_named_database_binds_when_the_universe_matches(
        self, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        with session_scope(factory) as session:
            binding = bind_source(session, tmp_path / "stage.db", expect_count=5)

        assert binding.universe_count == 5
        assert len(binding.universe_digest) == 64

    def test_a_database_with_the_wrong_population_is_refused(
        self, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """The exact 9K.25 shape: a real database, a plausible run, wrong data."""
        with session_scope(factory) as session, pytest.raises(BindingError, match="expected 8919"):
            bind_source(session, tmp_path / "stage.db", expect_count=8919)

    def test_a_changed_population_is_refused_even_at_the_right_count(
        self, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        """Count alone cannot detect a swap that preserves size."""
        with session_scope(factory) as session, pytest.raises(BindingError, match="digest"):
            bind_source(session, tmp_path / "stage.db", expect_count=5, expect_digest="b" * 64)

    def test_a_missing_database_is_refused(
        self, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        with session_scope(factory) as session, pytest.raises(BindingError, match="no database"):
            bind_source(session, tmp_path / "absent.db", expect_count=5)

    def test_the_binding_records_what_it_read(
        self, factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        with session_scope(factory) as session:
            binding = bind_source(
                session, tmp_path / "stage.db", expect_count=5, verify_digest=True
            )

        assert binding.sha256 and len(binding.sha256) == 64
        assert binding.size > 0
        assert "stage.db" in binding.path


class TestExtractionNeedsNoNetwork:
    """Offline is a property to prove, not an intention to state."""

    def test_extraction_succeeds_with_sockets_disabled(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        payload = cover(security("Common Stock", "ABC", "The Nasdaq Stock Market LLC"))
        store_atomically(root.path / "filings/x/f.htm", payload.encode(), root=root.path)
        Manifest(root.manifests / EVIDENCE_MANIFEST).append(entry(path="filings/x/f.htm"))

        def refuse(*args: object, **kwargs: object) -> None:
            raise OSError("network access is not permitted in this phase")

        monkeypatch.setattr(socket, "socket", refuse)
        monkeypatch.setattr(socket, "create_connection", refuse)

        report = build_index(
            root,
            Manifest(root.manifests / EVIDENCE_MANIFEST),
            derived_root(root) / "index.db",
        )

        assert report.filings_parsed == 1
        assert report.security_rows == 1


class TestTheCorpusFingerprintDetectsChange:
    def test_a_fingerprint_round_trips(self, root: EvidenceRoot) -> None:
        Manifest(root.manifests / EVIDENCE_MANIFEST).append(entry())
        first = fingerprint_corpus(root)
        save_fingerprint(root, first)

        assert load_fingerprint(root) is not None
        assert first.matches(load_fingerprint(root))  # type: ignore[arg-type]

    def test_an_added_artifact_changes_both_digests(self, root: EvidenceRoot) -> None:
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(entry())
        before = fingerprint_corpus(root)
        manifest.append(entry(url="https://sec.gov/g.htm", sha256="b" * 64))

        after = fingerprint_corpus(root)

        assert not before.matches(after)
        assert before.urls != after.urls
        assert before.contents != after.contents

    def test_changed_bytes_change_only_the_contents_digest(self, root: EvidenceRoot) -> None:
        """A digest that moved under an unchanged URL set is tampering, not growth."""
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        manifest.append(entry())
        before = fingerprint_corpus(root)
        manifest.append(entry(sha256="c" * 64))

        after = fingerprint_corpus(root)

        assert before.urls == after.urls
        assert before.contents != after.contents


class TestDerivedOutputIsNotEvidence:
    def test_the_derived_subtree_is_not_counted_as_an_orphan(self, root: EvidenceRoot) -> None:
        """The 9K.23 checkpoint lesson, applied to this phase's own output."""
        payload = b'{"ok":1}'
        store_atomically(root.submissions_path(320193), payload, root=root.path)
        import hashlib

        Manifest(root.manifests / EVIDENCE_MANIFEST).append(
            Entry(
                url="https://data.sec.gov/submissions/CIK0000320193.json",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256=hashlib.sha256(payload).hexdigest(),
                bytes=len(payload),
                path="submissions/CIK0000320193.json",
                request_kind="SUBMISSIONS",
                cik=320193,
            )
        )
        (derived_root(root) / "sec_evidence_index.db").write_bytes(b"derived, not evidence")

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_files == 0
        assert state.files_on_disk == 1
        assert state.clean


class TestExchangeNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("The Nasdaq Stock Market LLC", Exchange.NASDAQ),
            ("NASDAQ Global Select Market", Exchange.NASDAQ),
            ("New York Stock Exchange", Exchange.NYSE),
            ("NYSE", Exchange.NYSE),
            ("NYSE American", Exchange.NYSE_AMERICAN),
            ("NYSE American LLC", Exchange.NYSE_AMERICAN),
            ("NYSE MKT", Exchange.NYSE_AMERICAN),
            ("NYSE Arca", Exchange.OTHER),
            ("Cboe BZX Exchange", Exchange.OTHER),
            ("", Exchange.UNKNOWN),
            (None, Exchange.UNKNOWN),
        ],
    )
    def test_venues_normalise_conservatively(self, raw: str | None, expected: Exchange) -> None:
        assert normalise_exchange(raw) is expected

    def test_nyse_american_is_never_folded_into_nyse(self) -> None:
        """Every NYSE American spelling contains "NYSE"; order decides correctness."""
        for spelling in ("NYSE American", "NYSE American LLC", "NYSE MKT", "NYSE Amex"):
            assert normalise_exchange(spelling) is Exchange.NYSE_AMERICAN

    def test_an_unrecognised_venue_is_other_not_unknown(self) -> None:
        """ "We did not ask" and "not one of ours" are different answers."""
        assert normalise_exchange("Toronto Stock Exchange") is Exchange.OTHER
        assert normalise_exchange(None) is Exchange.UNKNOWN


class TestSecurityClassExtraction:
    def test_common_stock_is_recognised(self) -> None:
        page = cover(security("Common Stock, $0.01 par value", "ABC", "NYSE"))

        _, rows = extract_filing(entry(), page)

        assert len(rows) == 1
        assert rows[0].security_class == "COMMON_STOCK"
        assert rows[0].normalized_symbol == "ABC"
        assert rows[0].normalized_exchange == "NYSE"

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("7.00% Series A Preferred Stock", "PREFERRED_STOCK"),
            ("Warrants, each exercisable for one share", "WARRANT"),
            ("Units, each consisting of one share", "UNIT"),
            ("American Depositary Shares", "ADR_OR_ADS"),
        ],
    )
    def test_non_common_classes_are_not_admitted(self, title: str, expected: str) -> None:
        """A ticker is not evidence of common stock — the title is."""
        _, rows = extract_filing(entry(), cover(security(title, "ABCW", "NASDAQ")))

        assert rows[0].security_class == expected
        assert rows[0].security_class != "COMMON_STOCK"

    def test_class_a_and_class_b_are_both_common(self) -> None:
        for title in ("Class A Common Stock", "Class B Common Stock", "Ordinary Shares"):
            _, rows = extract_filing(entry(), cover(security(title, "X", "NYSE")))
            assert rows[0].security_class == "COMMON_STOCK", title

    def test_a_multi_security_filing_keeps_every_class_apart(self) -> None:
        """Two registered classes, separated by their dimension members."""
        contexts = (
            '<context id="ca"><entity><segment><explicitMember '
            'dimension="us-gaap:StatementClassOfStockAxis">us-gaap:CommonStockMember'
            "</explicitMember></segment></entity></context>"
            '<context id="cw"><entity><segment><explicitMember '
            'dimension="us-gaap:StatementClassOfStockAxis">custom:WarrantMember'
            "</explicitMember></segment></entity></context>"
        )
        page = cover(
            security("Common Stock", "ABC", "NYSE", "ca"),
            security("Warrants", "ABC.WS", "NYSE", "cw"),
            contexts=contexts,
        )

        filing, rows = extract_filing(entry(), page)

        assert filing.cover_securities == 2
        assert {r.normalized_symbol for r in rows} == {"ABC", "ABC.WS"}
        assert {r.security_class for r in rows} == {"COMMON_STOCK", "WARRANT"}

    def test_a_filing_with_no_cover_facts_yields_nothing_and_is_not_an_error(self) -> None:
        """Anything before inline XBRL carries no cover facts. That is expected."""
        filing, rows = extract_filing(entry(), "<html><body>No XBRL here.</body></html>")

        assert rows == []
        assert filing.parsed
        assert filing.symbol_provenance == "MISSING"

    def test_the_raw_text_survives_normalisation(self) -> None:
        _, rows = extract_filing(
            entry(), cover(security("  Common   Stock  ", " abc ", "  NYSE  "))
        )

        assert rows[0].raw_title == "Common Stock"
        assert rows[0].normalized_symbol == "ABC"


class TestPointInTimeSemantics:
    def test_the_acceptance_timestamp_is_preferred(self) -> None:
        assert evidence_date_for("2024-03-15T18:04:11.000Z", "2024-03-16") == (
            "2024-03-15",
            "ACCEPTED",
        )

    def test_the_filing_date_is_the_fallback(self) -> None:
        assert evidence_date_for(None, "2024-03-16") == ("2024-03-16", "FILED")

    def test_neither_source_yields_no_date_rather_than_today(self) -> None:
        assert evidence_date_for(None, None) == (None, "NONE")

    def test_the_download_date_never_becomes_the_evidence_date(self) -> None:
        """`retrieved_at` describes us, not the filing. Using it would be circular."""
        record = entry(retrieved_at="2026-09-07T00:00:00+00:00", filing_date="2019-05-02")

        filing, _ = extract_filing(record, cover(security("Common Stock", "A", "NYSE")))

        assert filing.evidence_date == "2019-05-02"
        assert "2026" not in (filing.evidence_date or "")

    def test_carry_forward_never_reaches_backwards(self, root: EvidenceRoot) -> None:
        """A March filing may speak for April. It may never speak for February."""
        db = open_index(derived_root(root) / "index.db")
        db.execute(
            "INSERT INTO securities (normalized_symbol, evidence_date, security_class)"
            " VALUES ('ABC', '2024-03-15', 'COMMON_STOCK')"
        )
        db.commit()

        covered = {row.horizon_days: row for row in carry_forward_coverage(db, horizons=(0, 90))}
        db.close()

        assert covered[0].carried == 0, "exact-date only carries nothing"
        assert covered[90].carried > 0
        assert covered[90].covered > covered[0].covered

    def test_a_wider_horizon_never_reduces_coverage(self, root: EvidenceRoot) -> None:
        db = open_index(derived_root(root) / "index.db")
        for month in ("2024-01-15", "2024-06-15"):
            db.execute(
                "INSERT INTO securities (normalized_symbol, evidence_date, security_class)"
                " VALUES ('ABC', ?, 'COMMON_STOCK')",
                (month,),
            )
        db.commit()

        rows = carry_forward_coverage(db, horizons=(0, 30, 60, 90, 120, 180))
        db.close()

        assert [r.covered for r in rows] == sorted(r.covered for r in rows)


class TestCatalogueAudit:
    def test_a_catalogue_yields_qualifying_filings_and_acceptance_times(self) -> None:
        payload = json.dumps(
            {
                "cik": 320193,
                "filings": {
                    "recent": {
                        "accessionNumber": ["0000320193-24-000001", "0000320193-24-000002"],
                        "form": ["10-K", "4"],
                        "filingDate": ["2024-11-01", "2024-11-02"],
                        "reportDate": ["2024-09-28", ""],
                        "primaryDocument": ["aapl-20240928.htm", "x.xml"],
                        "acceptanceDateTime": [
                            "2024-11-01T18:04:11.000Z",
                            "2024-11-02T10:00:00.000Z",
                        ],
                    },
                    "files": [{"name": "CIK0000320193-submissions-001.json"}],
                },
            }
        ).encode()

        rows, recent, older = extract_catalogue(
            entry(request_kind="SUBMISSIONS", cik=320193), payload
        )

        assert [r.form for r in rows] == ["10-K"], "Form 4 is not a qualifying form"
        assert rows[0].accepted_at == "2024-11-01T18:04:11.000Z"
        assert recent == 2
        assert older == 1, "history we never downloaded must be counted, not ignored"

    def test_a_truncated_catalogue_is_reported_not_hidden(self, root: EvidenceRoot) -> None:
        """Metadata we do not hold is a known unknown, and must be visible."""
        db = open_index(derived_root(root) / "index.db")
        db.execute("INSERT INTO catalogue_stats VALUES (1, 1000, 3, 400)")
        db.execute("INSERT INTO catalogue VALUES (1,'a','10-K','2020-01-01',NULL,NULL,'d.htm','u')")
        db.commit()

        opportunity = catalogue_opportunity(db)
        db.close()

        assert opportunity.truncated_ciks == 1
        assert opportunity.additional_history_files == 3
        assert opportunity.not_downloaded == 1

    def test_unparseable_catalogue_bytes_yield_nothing_rather_than_raising(self) -> None:
        rows, recent, older = extract_catalogue(entry(request_kind="SUBMISSIONS"), b"not json")

        assert (rows, recent, older) == ([], 0, 0)


class TestOfflineRebuildIsDeterministic:
    def test_two_builds_of_one_corpus_agree(self, root: EvidenceRoot) -> None:
        for i, (title, symbol) in enumerate(
            [("Common Stock", "AAA"), ("Class A Common Stock", "BBB"), ("Warrants", "CCC.WS")]
        ):
            path = f"filings/x/f{i}.htm"
            store_atomically(
                root.path / path,
                cover(security(title, symbol, "NYSE")).encode(),
                root=root.path,
            )
            Manifest(root.manifests / EVIDENCE_MANIFEST).append(
                entry(url=f"https://sec.gov/f{i}.htm", path=path, accession=f"acc-{i}")
            )

        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        first = build_index(root, manifest, derived_root(root) / "a.db")
        second = build_index(root, manifest, derived_root(root) / "b.db")

        assert first.security_rows == second.security_rows == 3
        assert first.filings_parsed == second.filings_parsed == 3

        def rows(name: str) -> list[tuple[str, ...]]:
            db = open_index(derived_root(root) / name)
            out = list(
                db.execute(
                    "SELECT url, normalized_symbol, security_class, normalized_exchange"
                    " FROM securities ORDER BY url"
                )
            )
            db.close()
            return out

        assert rows("a.db") == rows("b.db")

    def test_a_missing_file_is_counted_not_fatal(self, root: EvidenceRoot) -> None:
        Manifest(root.manifests / EVIDENCE_MANIFEST).append(entry(path="filings/gone.htm"))

        report = build_index(
            root, Manifest(root.manifests / EVIDENCE_MANIFEST), derived_root(root) / "i.db"
        )

        assert report.missing_files == 1
        assert report.parse_failures == 0


class TestSpanStatistics:
    def test_a_single_filing_spans_nothing(self, root: EvidenceRoot) -> None:
        """Six filings is a count; what matters is the history they pin down."""
        db = open_index(derived_root(root) / "index.db")
        db.execute("INSERT INTO filings (url, cik, evidence_date) VALUES ('u', 1, '2024-01-01')")
        db.commit()

        stats = span_statistics(db)
        db.close()

        assert stats[0].filings == 1
        assert stats[0].span_days == 0
        assert stats[0].max_gap == 0

    def test_gaps_are_measured_between_consecutive_observations(self, root: EvidenceRoot) -> None:
        db = open_index(derived_root(root) / "index.db")
        for i, when in enumerate(("2024-01-01", "2024-02-01", "2024-12-01")):
            db.execute(
                "INSERT INTO filings (url, cik, evidence_date) VALUES (?, 1, ?)",
                (f"u{i}", when),
            )
        db.commit()

        stats = span_statistics(db)
        db.close()

        assert stats[0].filings == 3
        assert stats[0].span_days == 335
        assert stats[0].max_gap == 304, "a ten-month hole is the number that matters"

    def test_class_breakdown_separates_uncertain_from_non_common(self, root: EvidenceRoot) -> None:
        db = open_index(derived_root(root) / "index.db")
        for klass in ("COMMON_STOCK", "WARRANT", "UNKNOWN", "UNKNOWN"):
            db.execute(
                "INSERT INTO securities (security_class, classification_reason,"
                " normalized_exchange) VALUES (?, 'TITLE_ONLY', 'NYSE')",
                (klass,),
            )
        db.commit()

        breakdown = class_breakdown(db)
        db.close()

        assert breakdown.common == 1
        assert breakdown.non_common == 1
        assert breakdown.uncertain == 2


class TestIdentityJoinRespectsOccupancy:
    """Sections 11 and 12: a cover row attaches to the occupant of its own date, or to nothing.

    These reuse the real `BBBY` shape from `test_pit_attribution`: Bed Bath &
    Beyond held the letters until May 2023, and a different company holds them
    now. A cover page filed in 2022 is evidence about the first, and a join that
    handed it to the second would reintroduce the exact defect Phase 9K.11 was
    opened to fix — this time silently, inside a coverage statistic.
    """

    def test_a_2022_cover_row_joins_the_delisted_occupant_not_the_live_company(
        self, bbby: tuple[sessionmaker[Session], int, int]
    ) -> None:
        maker, security_id, live_id = bbby
        with session_scope(maker) as session:
            cache = JoinCache.load(session)

            result = join_symbol(session, "BBBY", date(2022, 6, 15), cache)

        assert result.historical_security_id == security_id
        assert result.company_id is None, "the live company has no claim on 2022"
        assert result.company_id != live_id

    def test_a_2026_cover_row_joins_the_live_company(
        self, bbby: tuple[sessionmaker[Session], int, int]
    ) -> None:
        maker, _security_id, live_id = bbby
        with session_scope(maker) as session:
            cache = JoinCache.load(session)

            result = join_symbol(session, "BBBY", date(2026, 7, 1), cache)

        assert result.company_id == live_id
        assert result.historical_security_id is None

    def test_a_date_before_any_occupant_is_unresolved_rather_than_guessed(
        self, bbby: tuple[sessionmaker[Session], int, int]
    ) -> None:
        maker, _security_id, _live_id = bbby
        with session_scope(maker) as session:
            cache = JoinCache.load(session)

            result = join_symbol(session, "BBBY", date(1999, 1, 1), cache)

        assert result.state is JoinState.UNRESOLVED
        assert result.company_id is None
        assert result.historical_security_id is None

    def test_an_unknown_symbol_is_unresolved_not_invented(
        self, bbby: tuple[sessionmaker[Session], int, int]
    ) -> None:
        maker, _security_id, _live_id = bbby
        with session_scope(maker) as session:
            cache = JoinCache.load(session)

            result = join_symbol(session, "ZZZZ", date(2024, 1, 1), cache)

        assert result.state is JoinState.UNRESOLVED

    def test_two_stored_securities_sharing_a_symbol_stay_ambiguous(
        self, factory: sessionmaker[Session]
    ) -> None:
        """SGEN-style continuity: recorded as unsettled, never merged."""
        with session_scope(factory) as session:
            for name in ("Seagen Inc.", "Seattle Genetics, Inc."):
                session.add(HistoricalSecurity(primary_ticker="SGEN", name=name, status="UNKNOWN"))
            session.flush()
            cache = JoinCache.load(session)

            result = join_symbol(session, "SGEN", date(2022, 6, 1), cache)

        assert result.state is JoinState.AMBIGUOUS
        assert result.historical_security_id is None, "no merge, and no arbitrary winner"

    def test_two_classes_of_one_filer_stay_separate_identities(
        self, factory: sessionmaker[Session]
    ) -> None:
        """§12: AIZ and AIZN are one filer's two registered classes, never one security.

        Note what the schema forces here. `historical_securities` carries
        `UniqueConstraint("cik")`, so a filer's *second* registered class cannot
        store the same CIK — it is kept with `cik=None`. The identities stay
        separate, which is the guarantee that matters, but the preferred class
        loses its filer handle, and that is a real limitation of the stored
        model rather than a property of the evidence.
        """
        with session_scope(factory) as session:
            session.add(
                HistoricalSecurity(
                    primary_ticker="AIZ", name="Assurant, Inc.", cik=1267238, status="UNKNOWN"
                )
            )
            session.add(
                HistoricalSecurity(
                    primary_ticker="AIZN", name="Assurant, Inc.", cik=None, status="UNKNOWN"
                )
            )
            session.flush()
            cache = JoinCache.load(session)

            common = join_symbol(session, "AIZ", date(2021, 6, 1), cache)
            preferred = join_symbol(session, "AIZN", date(2021, 6, 1), cache)

        assert common.historical_security_id != preferred.historical_security_id
        assert common.state is JoinState.EXACT
        assert preferred.state is JoinState.EXACT

    def test_the_schema_refuses_a_second_security_under_one_cik(
        self, factory: sessionmaker[Session]
    ) -> None:
        """Documenting the constraint above as behaviour, so it cannot silently change."""
        from sqlalchemy.exc import IntegrityError

        with pytest.raises(IntegrityError), session_scope(factory) as session:
            for ticker in ("AIZ", "AIZN"):
                session.add(
                    HistoricalSecurity(
                        primary_ticker=ticker,
                        name="Assurant, Inc.",
                        cik=1267238,
                        status="UNKNOWN",
                    )
                )
            session.flush()
