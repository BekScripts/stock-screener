"""Assembling the A3 planner's inputs: PIT-safe, class-aware, deterministic, frozen.

The lost harness is rebuilt here against a synthetic world small enough to
reason about by hand: a single-class issuer, an issuer with two genuine common
classes, an issuer whose filings cannot be attributed to any of its securities,
an issuer with one common line and one baby bond, a co-registrant accession, a
reused ticker with two occupants, a delisted security with no CIK, a foreign
filer, and months with no listing observation at all. Every rule the phase must
not regress — no future evidence, no CIK-wide over-credit, no cross-occupant
credit, missing stays missing — is one test below.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import (
    FtdIdentityObservation,
    HistoricalSecurity,
    ResearchUniverseMonth,
    SecurityIdentifier,
)
from domain import CompanyProfile
from stock_screener.cli import app
from stock_screener.config import get_settings
from stock_screener.sec_evidence import (
    A3_INPUTS_COMPLETE_MARKER,
    A3_INPUTS_DIRECTORY,
    A3_INPUTS_MANIFEST,
    A3_SUPERSESSION_RECORD,
    ARTIFACT_PREFIXES,
    CANDIDATE_WINDOW_DEFINITION,
    COMPANY,
    COMPONENTS,
    HISTORICAL,
    SUPERSEDED_SIDECAR,
    WINDOW_END,
    WINDOW_START,
    AssembledInputs,
    B2Configuration,
    B2EvidenceStorage,
    ControlMirror,
    CoverageRow,
    CoverageStatus,
    Entry,
    GapClass,
    InputsBindings,
    Manifest,
    Outcome,
    PackageError,
    Reach,
    ResolvedIdentity,
    assemble_planning_inputs,
    component_digests,
    dashed_accession,
    filing_key,
    in_candidate_window,
    load_planning_inputs,
    load_resolution,
    mirror_package,
    open_index,
    package_files,
    package_sha256,
    semantic_fingerprints,
    write_package,
    write_supersession,
)
from stock_screener.sec_evidence import plan as plan_module

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from conftest import FakeS3Client
    from stock_screener.sec_evidence import PlanningInputs

pytestmark = pytest.mark.integration

M = date  # a short alias for the month-start dates below


def months(first: date, last: date) -> list[date]:
    out: list[date] = []
    cursor = first
    while cursor <= last:
        out.append(cursor)
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    return out


#: Issuers and their securities. Ids are assigned by the database; symbols are
#: what the tests reason in.
LIVE = ("AAA", "LSXMA", "LSXMB", "UNA", "UNB", "AFG", "AFGB", "FRGN", "BBBY")
DELISTED = (("GONE", None), ("BBBY", 886158))
CIK = {
    "AAA": 100,
    "LSXMA": 200,
    "LSXMB": 200,
    "UNA": 300,
    "UNB": 300,
    "AFG": 400,
    "AFGB": 400,
    "FRGN": 600,
    "BBBY": 700,  # the live occupant
}
UNIVERSE_MONTHS = months(M(2024, 1, 1), M(2024, 8, 1))


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sessionmaker[Session], dict[str, tuple[str, int]]]]:
    """The frozen PIT database: identities, their universe months, and the BBBY reuse."""
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'stage.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    ids: dict[str, tuple[str, int]] = {}
    with session_scope(maker) as session:
        repo = CompanyRepository(session)
        for ticker in LIVE:
            repo.upsert_profile(
                CompanyProfile(ticker=ticker, name=f"{ticker} Inc.", exchange="NASDAQ")
            )
        session.flush()
        for company in repo.list_all():
            ids[company.ticker] = (COMPANY, int(company.id))
        for ticker, cik in DELISTED:
            security = HistoricalSecurity(
                primary_ticker=ticker, name=f"{ticker} (delisted)", cik=cik, status="UNKNOWN"
            )
            session.add(security)
            session.flush()
            ids[f"{ticker}:historical"] = (HISTORICAL, int(security.id))
        # The real BBBY shape: Bed Bath held the letters to 2023-05, a new company from 2025-09.
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
                historical_security_id=ids["BBBY:historical"][1],
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
        # Universe months: every live identity UNKNOWN through 2024; the delisted
        # BBBY in 2022; the live BBBY from 2025-09. AAA's July has no listing.
        for ticker in LIVE:
            if ticker == "BBBY":
                continue
            for month in UNIVERSE_MONTHS:
                session.add(
                    _month(
                        ids[ticker],
                        ticker,
                        month,
                        venue=None if (ticker == "AAA" and month == M(2024, 7, 1)) else "NASDAQ",
                    )
                )
        for month in months(M(2022, 1, 1), M(2022, 12, 1)):
            session.add(_month(ids["BBBY:historical"], "BBBY", month, venue="NASDAQ"))
        for month in months(M(2025, 9, 1), M(2026, 2, 1)):
            session.add(_month(ids["BBBY"], "BBBY", month, venue="NYSE"))
        for month in UNIVERSE_MONTHS:
            session.add(_month(ids["GONE:historical"], "GONE", month, venue="NASDAQ"))
        # January 2022, the first universe month: only a filing dated inside the
        # window may be its candidate, whatever December 2021 could reach.
        session.add(_month(ids["AAA"], "AAA", M(2022, 1, 1), venue="NASDAQ"))
        # One decided month, to prove it is never planned for.
        session.add(
            _month(
                ids["AAA"],
                "AAA",
                M(2023, 12, 1),
                venue="NASDAQ",
                state="ELIGIBLE",
                security_class="COMMON_STOCK",
                accession="000000010023000001",
                as_of=date(2023, 11, 15),
            )
        )
        session.flush()
    try:
        yield maker, ids
    finally:
        engine.dispose()


def _month(
    identity: tuple[str, int],
    symbol: str,
    month: date,
    *,
    venue: str | None,
    state: str = "UNKNOWN",
    security_class: str = "UNKNOWN",
    accession: str | None = None,
    as_of: date | None = None,
) -> ResearchUniverseMonth:
    kind, identity_id = identity
    return ResearchUniverseMonth(
        company_id=identity_id if kind == COMPANY else None,
        historical_security_id=identity_id if kind == HISTORICAL else None,
        symbol=symbol,
        observation_month=month,
        venue=venue,
        state=state,
        security_class=security_class,
        class_evidence_mode="ABSENT" if accession is None else "CARRIED_FORWARD",
        class_age_days=None,
        class_source_accession=accession,
        class_evidence_as_of=as_of,
        reason="fixture",
    )


#: Cover rows the V2 index holds: (cik, accession, filing_date, accepted, form,
#: symbol, class, reason). A None symbol is a row the join cannot attach.
COVERS: list[tuple[int, str, str, str, str, str | None, str, str]] = [
    (
        100,
        "0000000100-24-000001",
        "2024-02-12",
        "2024-02-10",
        "8-K",
        "AAA",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
    (
        100,
        "0000000100-24-000002",
        "2024-07-15",
        "2024-07-15",
        "8-K",
        "AAA",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
    (
        200,
        "0000000200-24-000001",
        "2024-03-01",
        "2024-03-01",
        "10-K",
        "LSXMA",
        "COMMON_STOCK",
        "TITLE_AND_MEMBER",
    ),
    (
        200,
        "0000000200-24-000001",
        "2024-03-01",
        "2024-03-01",
        "10-K",
        "LSXMB",
        "COMMON_STOCK",
        "TITLE_AND_MEMBER",
    ),
    (300, "0000000300-24-000001", "2024-03-01", "2024-03-01", "8-K", None, "UNKNOWN", "ABSENT"),
    (
        400,
        "0000000400-24-000001",
        "2024-03-01",
        "2024-03-01",
        "10-Q",
        "AFG",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
    (
        400,
        "0000000400-24-000001",
        "2024-03-01",
        "2024-03-01",
        "10-Q",
        "AFGB",
        "NOTE_OR_DEBT",
        "TITLE_ONLY",
    ),
    (
        600,
        "0000000600-24-000001",
        "2024-04-30",
        "2024-04-30",
        "20-F",
        "FRGN",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
    (
        886158,
        "0000886158-22-000001",
        "2022-06-15",
        "2022-06-15",
        "8-K",
        "BBBY",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
    (
        700,
        "0000000700-26-000001",
        "2026-01-10",
        "2026-01-10",
        "8-K",
        "BBBY",
        "COMMON_STOCK",
        "TITLE_ONLY",
    ),
]

#: Catalogue rows: (cik, accession, form, filing_date). The held ones are the
#: cover rows above; the rest are undownloaded candidates.
CATALOGUE: list[tuple[int, str, str, str]] = [
    *[(c, a, f, d) for c, a, d, _acc, f, _s, _k, _r in COVERS],
    (100, "0000000100-24-000003", "8-K", "2024-05-10"),
    (100, "0000000100-24-000004", "8-K", "2024-08-20"),  # supports Aug and beyond the universe
    (200, "0000000200-24-000002", "8-K", "2024-01-05"),
    (300, "0000000300-24-000002", "8-K", "2024-01-05"),
    (400, "0000000400-24-000002", "8-K", "2024-01-05"),
    (500, "0000000100-24-000003", "8-K", "2024-05-10"),  # the co-registrant copy
    (600, "0000000600-24-000002", "20-F", "2024-01-05"),
    (100, "0000000100-21-000001", "10-K", "2021-06-01"),  # cannot reach any window month
    (100, "0000000100-21-000011", "8-K", "2021-11-15"),  # reaches Jan 2022; outside the window
    (100, "0000000100-21-000012", "8-K", "2021-12-31"),  # reaches Jan-Feb 2022; outside the window
    (100, "0000000100-22-000001", "8-K", "2022-01-01"),  # WINDOW_START itself: inside
    (100, "0000000100-26-000731", "8-K", "2026-07-31"),  # WINDOW_END itself: inside
    (100, "0000000100-26-000801", "8-K", "2026-08-01"),  # after WINDOW_END: outside
]


def url_for(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/doc.htm"


@pytest.fixture
def index(tmp_path: Path) -> Path:
    """The V2 derived index, with the cover rows and the catalogue above."""
    path = tmp_path / "sec_evidence_index_v2.db"
    with closing(open_index(path)) as db:
        seen: set[str] = set()
        for cik, accession, filed, accepted, form, symbol, klass, reason in COVERS:
            url = url_for(cik, accession)
            if url not in seen:
                seen.add(url)
                db.execute(
                    "INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        url,
                        filing_key(cik, accession, "doc.htm"),
                        "ab" * 32,
                        40_000,
                        cik,
                        accession,
                        form,
                        filed,
                        f"{accepted}T16:05:00.000Z",
                        None,
                        accepted,
                        "ACCEPTED",
                        None,
                        1,
                        0,
                        0,
                        1,
                        None,
                        "CORROBORATED",
                        "9L.1",
                    ),
                )
            db.execute(
                "INSERT INTO securities (url, cik, accession, evidence_date, raw_title,"
                " normalized_title, raw_symbol, normalized_symbol, raw_exchange,"
                " normalized_exchange, member, no_trading_symbol, security_class,"
                " classification_reason, source_path,"
                " source_sha256, parser_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url,
                    cik,
                    accession,
                    accepted,
                    "Common Stock",
                    "Common Stock",
                    symbol,
                    symbol,
                    None,
                    "UNKNOWN",
                    None,
                    0,
                    klass,
                    reason,
                    None,
                    None,
                    "9L.1",
                ),
            )
        for cik, accession, form, filed in CATALOGUE:
            db.execute(
                "INSERT OR REPLACE INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
                (cik, accession, form, filed, f"{filed}T16:05:00.000Z", None, "doc.htm", "u"),
            )
        db.commit()
    return path


@pytest.fixture
def manifest(tmp_path: Path) -> Manifest:
    """The V2 manifest vouching for every held filing, the co-registrant copy included."""
    (tmp_path / "manifests").mkdir()
    log = Manifest(tmp_path / "manifests" / "evidence.jsonl")
    held = {(c, a) for c, a, *_ in COVERS} | {(500, "0000000100-24-000003")}
    for cik, accession in sorted(held):
        log.append(
            Entry(
                url=url_for(cik, accession),
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="ab" * 32,
                bytes=40_000,
                retrieved_at="2026-09-18T00:00:00+00:00",
                path=filing_key(cik, accession, "doc.htm"),
                request_kind="FILING",
                cik=cik,
                accession=accession,
                form="8-K",
                filing_date="2024-01-01",
            )
        )
    return log


def resolution_for(ids: dict[str, tuple[str, int]]) -> list[ResolvedIdentity]:
    rows = []
    for ticker in LIVE:
        rows.append(
            ResolvedIdentity(ids[ticker], ticker, "LIVE", CIK[ticker], "SEC_TICKER_MAP", "live")
        )
    rows.append(
        ResolvedIdentity(ids["GONE:historical"], "GONE", "DELISTED", None, "UNRESOLVED", "no cik")
    )
    rows.append(
        ResolvedIdentity(ids["BBBY:historical"], "BBBY", "DELISTED", 886158, "STORED", "stored")
    )
    return rows


@pytest.fixture
def assembled(
    world: tuple[sessionmaker[Session], dict[str, tuple[str, int]]], index: Path, manifest: Manifest
) -> tuple[AssembledInputs, dict[str, tuple[str, int]]]:
    maker, ids = world
    with (
        session_scope(maker) as session,
        closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db,
    ):
        out = assemble_planning_inputs(session, db, manifest, resolution_for(ids))
    return out, ids


def counts(value: object) -> dict[str, int]:
    assert isinstance(value, dict)
    return {str(k): int(v) for k, v in value.items()}


def as_int(value: object) -> int:
    assert isinstance(value, int)
    return value


def coverage_of(out: AssembledInputs, identity: tuple[str, int]) -> dict[date, CoverageRow]:
    rows = out.coverage
    return {date.fromisoformat(r.observation_month): r for r in rows if r.identity == identity}


class TestPitCutoff:
    def test_a_filing_covers_its_month_end_and_ninety_days_forward_never_earlier(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """AAA's 8-K accepted 2024-02-10: Feb, Mar, Apr covered; Jan never; May too old."""
        out, ids = assembled
        by_month = coverage_of(out, ids["AAA"])

        assert by_month[M(2024, 1, 1)].status == CoverageStatus.OPEN
        assert by_month[M(2024, 1, 1)].previous_observation is None
        assert [by_month[M(2024, m, 1)].status for m in (2, 3, 4)] == [CoverageStatus.COVERED] * 3
        assert by_month[M(2024, 4, 1)].days_since == 80
        assert by_month[M(2024, 5, 1)].status == CoverageStatus.OPEN
        assert by_month[M(2024, 5, 1)].days_since == 111

    def test_the_acceptance_date_dates_the_evidence_not_the_filing_date(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled

        assert coverage_of(out, ids["AAA"])[M(2024, 2, 1)].previous_observation == "2024-02-10"

    def test_a_filing_accepted_after_the_cutoff_cannot_cover_the_month(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """AAA's second 8-K is accepted 2024-07-15: June stays open, August is covered."""
        out, ids = assembled
        by_month = coverage_of(out, ids["AAA"])

        assert by_month[M(2024, 6, 1)].status == CoverageStatus.OPEN
        assert by_month[M(2024, 6, 1)].next_observation == "2024-07-15"
        assert by_month[M(2024, 8, 1)].status == CoverageStatus.COVERED

    def test_a_month_with_no_listing_observation_is_not_actionable(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """AAA's July has no venue: an 8-K is accepted that month, yet it cannot be admitted."""
        out, ids = assembled
        row = coverage_of(out, ids["AAA"])[M(2024, 7, 1)]

        assert row.status == CoverageStatus.OPEN
        assert row.gap_class == GapClass.OTHER
        assert "no listing observation" in row.reason

    def test_a_decided_month_is_never_planned_for(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        row = coverage_of(out, ids["AAA"])[M(2023, 12, 1)]

        assert row.status == CoverageStatus.DECIDED
        assert row.gap_class is None
        assert all(g.month_end != date(2023, 12, 31) for g in out.inputs.gaps)

    def test_the_frozen_accession_is_compared_in_the_index_form(self) -> None:
        assert dashed_accession("000000010023000001") == "0000000100-23-000001"
        assert dashed_accession("0000000100-23-000001") == "0000000100-23-000001"


class TestClassAwareCredit:
    def test_a_single_class_issuer_is_safe_single_and_its_gaps_are_solvable(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        by_month = coverage_of(out, ids["AAA"])

        assert by_month[M(2024, 5, 1)].reach == Reach.SAFE_SINGLE
        assert by_month[M(2024, 5, 1)].gap_class == GapClass.SOLVABLE
        assert by_month[M(2024, 1, 1)].gap_class == GapClass.NO_CANDIDATE

    def test_two_genuine_common_classes_are_both_credited(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        reach = {(r.kind, r.identity_id): r for r in out.reach}

        assert reach[ids["LSXMA"]].reach == Reach.SAFE_COMMON_CLASS
        assert reach[ids["LSXMA"]].credited and reach[ids["LSXMB"]].credited

    def test_an_issuer_whose_filings_attribute_to_no_class_credits_nothing(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """UNA/UNB share CIK 300; the only cover row has no symbol. Neither may be credited."""
        out, ids = assembled
        reach = {(r.kind, r.identity_id): r for r in out.reach}
        candidates = {c.key: c for c in out.inputs.candidates}

        assert reach[ids["UNA"]].reach == Reach.MULTI_CLASS_UNASSIGNED
        assert not reach[ids["UNA"]].credited and not reach[ids["UNB"]].credited
        assert coverage_of(out, ids["UNA"])[M(2024, 3, 1)].gap_class == GapClass.OTHER
        assert candidates[(300, "0000000300-24-000002")].covers == frozenset()

    def test_only_the_common_line_is_credited_never_the_baby_bond(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """AFG/AFGB: one 10-Q names both; the bond is excluded and its months are ambiguous."""
        out, ids = assembled
        reach = {(r.kind, r.identity_id): r for r in out.reach}
        candidates = {c.key: c for c in out.inputs.candidates}

        assert reach[ids["AFG"]].credited and not reach[ids["AFGB"]].credited
        assert reach[ids["AFGB"]].identity_class == "NON_COMMON"
        assert coverage_of(out, ids["AFGB"])[M(2024, 6, 1)].gap_class == GapClass.AMBIGUOUS_IDENTITY
        covered_identities = {
            identity for identity, _ in candidates[(400, "0000000400-24-000002")].covers
        }
        assert covered_identities == {ids["AFG"]}

    def test_a_cover_row_without_a_symbol_is_never_an_observation(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled

        assert all(o.symbol for o in out.observations)
        assert not any(o.cik == 300 for o in out.observations)

    def test_a_foreign_filer_is_covered_by_its_20_f(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        by_month = coverage_of(out, ids["FRGN"])
        candidates = {c.key: c for c in out.inputs.candidates}

        assert by_month[M(2024, 5, 1)].status == CoverageStatus.COVERED
        assert candidates[(600, "0000000600-24-000002")].form == "20-F"
        assert candidates[(600, "0000000600-24-000002")].yield_rate == 1.0

    def test_a_security_without_a_cik_has_no_cik_gaps(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        by_month = coverage_of(out, ids["GONE:historical"])

        assert {row.gap_class for row in by_month.values()} == {GapClass.NO_CIK}
        assert {row.reach for row in by_month.values()} == {"NO_CIK"}


class TestTickerReuse:
    def test_each_occupant_is_covered_only_by_its_own_filings(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        old = coverage_of(out, ids["BBBY:historical"])
        new = coverage_of(out, ids["BBBY"])

        assert old[M(2022, 6, 1)].status == CoverageStatus.COVERED
        assert old[M(2022, 6, 1)].source_cik == 886158
        assert new[M(2026, 1, 1)].status == CoverageStatus.COVERED
        assert new[M(2026, 1, 1)].source_cik == 700
        assert new[M(2025, 9, 1)].status == CoverageStatus.OPEN
        assert new[M(2025, 9, 1)].previous_observation is None, (
            "2022 evidence is not this occupant's"
        )

    def test_the_2022_cover_row_joins_the_delisted_occupant(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        joined = {o.accession: o.identity for o in out.observations if o.symbol == "BBBY"}

        assert joined["0000886158-22-000001"] == ids["BBBY:historical"]
        assert joined["0000000700-26-000001"] == ids["BBBY"]


class TestCandidatesAndHeld:
    def test_the_held_set_is_keyed_by_cik_and_accession_and_keeps_co_registrants_apart(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled
        held = out.inputs.held

        assert (100, "0000000100-24-000003") not in held
        assert (500, "0000000100-24-000003") in held
        assert len(held) == len({(c, a) for c, a, *_ in COVERS}) + 1

    def test_held_filings_are_never_candidates_but_the_unheld_co_registrant_copy_is(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled
        keys = {c.key for c in out.inputs.candidates}

        assert (500, "0000000100-24-000003") not in keys
        assert (100, "0000000100-24-000003") in keys
        assert (100, "0000000100-24-000001") not in keys

    def test_a_candidate_covers_only_open_solvable_month_ends_it_can_reach(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, ids = assembled
        candidates = {c.key: c for c in out.inputs.candidates}

        # Filed 2024-05-10: reaches May, June, July; July has no listing.
        assert candidates[(100, "0000000100-24-000003")].covers == {
            (ids["AAA"], date(2024, 5, 31)),
            (ids["AAA"], date(2024, 6, 30)),
        }
        # Filed 2024-08-20: August is already covered; nothing to add.
        assert candidates[(100, "0000000100-24-000004")].covers == frozenset()

    def test_a_filing_that_reaches_no_window_month_is_not_a_candidate(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled

        assert (100, "0000000100-21-000001") not in {c.key for c in out.inputs.candidates}


class TestTheHistoricalCandidateWindow:
    """Acquisition scope, not point-in-time: the 9L window, both ends inclusive."""

    @pytest.mark.parametrize(
        ("filed", "inside"),
        [
            (date(2021, 11, 15), False),
            (date(2021, 12, 31), False),
            (WINDOW_START, True),
            (date(2024, 6, 15), True),
            (WINDOW_END, True),
            (date(2026, 8, 1), False),
        ],
    )
    def test_the_window_is_inclusive_at_both_ends(self, filed: date, inside: bool) -> None:
        assert in_candidate_window(filed) is inside

    def test_the_window_constants_are_the_planners(self) -> None:
        assert (date(2022, 1, 1), date(2026, 7, 31)) == (WINDOW_START, WINDOW_END)
        assert CANDIDATE_WINDOW_DEFINITION == "HISTORICAL_9L_V1"

    def test_november_and_december_2021_filings_never_become_candidates(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        """They reach January 2022 under the planning rule — and are still out of scope."""
        out, ids = assembled
        keys = {c.key for c in out.inputs.candidates}

        assert (100, "0000000100-21-000011") not in keys
        assert (100, "0000000100-21-000012") not in keys
        assert (100, "0000000100-22-000001") in keys
        assert (100, "0000000100-26-000731") in keys
        assert (100, "0000000100-26-000801") not in keys
        january = coverage_of(out, ids["AAA"])[M(2022, 1, 1)]
        assert january.status == CoverageStatus.OPEN
        assert january.gap_class == GapClass.SOLVABLE, "the 2022-01-01 filing reaches it"
        cover = {
            c.key for c in out.inputs.candidates if (ids["AAA"], date(2022, 1, 31)) in c.covers
        }
        assert cover == {(100, "0000000100-22-000001")}, "never the December filing"

    def test_the_summary_accounts_for_every_catalogue_row_outside_the_window(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled
        summary = out.summary

        assert summary["candidate_window_definition"] == CANDIDATE_WINDOW_DEFINITION
        assert summary["candidate_window"] == [WINDOW_START.isoformat(), WINDOW_END.isoformat()]
        assert as_int(summary["catalogue_rows_in_window"]) + as_int(
            summary["catalogue_rows_outside_window"]
        ) == as_int(summary["catalogue_rows"])
        assert summary["catalogue_rows_outside_window_2021_11_12"] == 2

    def test_form_yield_and_sizes_are_measured_from_held_documents(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled
        inputs = out.inputs

        assert inputs.form_yield["8-K"] == 4 / 5, "one of five 8-Ks has no usable common row"
        assert inputs.form_yield["10-Q"] == 1.0
        assert inputs.form_bytes["8-K"]["count"] == 5
        assert {c.estimated_bytes for c in inputs.candidates} == {40_000}

    def test_coverage_statuses_partition_the_universe(
        self, assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]]
    ) -> None:
        out, _ = assembled
        summary = out.summary
        status = counts(summary["status"])

        assert sum(status.values()) == summary["universe_rows"]
        assert sum(counts(summary["gap_classes"]).values()) == status["OPEN"]
        assert (
            as_int(summary["solvable_months"]) + as_int(summary["raw_unknown_floor"])
            == (status["OPEN"])
        )


class TestTheFrozenPackage:
    @pytest.fixture
    def bindings(self, index: Path) -> InputsBindings:
        with closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db:
            prints = {k: v.sha256 for k, v in semantic_fingerprints(db).items()}
        return InputsBindings(
            generation_id="SEC_EVIDENCE_GENERATION_V2",
            database_path="stage.db",
            database_sha256="db" * 32,
            universe_count=11,
            universe_sha256="un" * 32,
            resolution_sha256="re" * 32,
            manifest_sha256="ma" * 32,
            index_sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
            fingerprints=prints,
            repo_commit="abc",
        )

    def test_the_package_round_trips_to_identical_planning_inputs(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        out, _ = assembled
        original: PlanningInputs = out.inputs

        manifest = write_package(tmp_path / "pkg", out, bindings)
        loaded, read = load_planning_inputs(tmp_path / "pkg", expect=bindings)

        assert loaded.gaps == original.gaps
        assert loaded.candidates == original.candidates
        assert loaded.held == original.held
        assert loaded.form_yield == original.form_yield
        assert loaded.form_bytes == original.form_bytes
        assert read["package_sha256"] == manifest["package_sha256"]
        assert (tmp_path / "pkg" / A3_INPUTS_COMPLETE_MARKER).is_file()
        assert all((tmp_path / "pkg" / name).is_file() for name in package_files())

    def test_two_assemblies_produce_byte_identical_components(
        self,
        world: tuple[sessionmaker[Session], dict[str, tuple[str, int]]],
        index: Path,
        manifest: Manifest,
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        maker, ids = world
        digests = []
        for name in ("one", "two"):
            with (
                session_scope(maker) as session,
                closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db,
            ):
                out = assemble_planning_inputs(session, db, manifest, resolution_for(ids))
            write_package(tmp_path / name, out, bindings)
            digests.append(component_digests(tmp_path / name))

        assert digests[0] == digests[1]
        assert package_sha256(digests[0]) == package_sha256(digests[1])
        assert (tmp_path / "one" / "candidates_v2.jsonl").read_bytes() == (
            tmp_path / "two" / "candidates_v2.jsonl"
        ).read_bytes()

    def test_a_package_frozen_against_other_sources_is_refused(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        out, _ = assembled
        write_package(tmp_path / "pkg", out, bindings)
        moved = replace(bindings, index_sha256="00" * 32)

        with pytest.raises(PackageError, match=r"other sources: \['index_sha256'\]"):
            load_planning_inputs(tmp_path / "pkg", expect=moved)

    @pytest.mark.parametrize(
        "field", ["database_sha256", "universe_sha256", "manifest_sha256", "resolution_sha256"]
    )
    def test_every_source_binding_is_enforced(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
        field: str,
    ) -> None:
        out, _ = assembled
        write_package(tmp_path / "pkg", out, bindings)
        moved = InputsBindings(**{**bindings.as_dict(), field: "00" * 32})  # type: ignore[arg-type]  # a loose test rebuild

        with pytest.raises(PackageError, match=field):
            load_planning_inputs(tmp_path / "pkg", expect=moved)

    def test_a_changed_fingerprint_is_a_changed_source(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        out, _ = assembled
        write_package(tmp_path / "pkg", out, bindings)
        prints = {**bindings.fingerprints, "securities": "00" * 32}
        moved = replace(bindings, fingerprints=prints)

        with pytest.raises(PackageError, match="fingerprints"):
            load_planning_inputs(tmp_path / "pkg", expect=moved)

    def test_a_tampered_component_is_refused(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        out, _ = assembled
        write_package(tmp_path / "pkg", out, bindings)
        target = tmp_path / "pkg" / "held_v2.jsonl"
        target.write_text(target.read_text(encoding="utf-8") + '{"cik":1,"accession":"x"}\n')

        with pytest.raises(
            PackageError, match=r"changed since they were frozen: \['held_v2.jsonl'\]"
        ):
            load_planning_inputs(tmp_path / "pkg")

    def test_a_manifest_whose_package_digest_disagrees_is_refused(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        bindings: InputsBindings,
        tmp_path: Path,
    ) -> None:
        out, _ = assembled
        write_package(tmp_path / "pkg", out, bindings)
        manifest_path = tmp_path / "pkg" / A3_INPUTS_MANIFEST
        facts = json.loads(manifest_path.read_text(encoding="utf-8"))
        facts["package_sha256"] = "00" * 32
        manifest_path.write_text(json.dumps(facts), encoding="utf-8")

        with pytest.raises(PackageError, match="does not bind"):
            load_planning_inputs(tmp_path / "pkg")

    def test_the_frozen_resolution_must_hash_to_its_companion(self, tmp_path: Path) -> None:
        path = tmp_path / "filer_resolution_v1.jsonl"
        path.write_text(
            '{"symbol":"AAA","bucket":"LIVE","company_id":1,"cik":1,"source":"STORED"}\n'
        )
        path.with_suffix(".sha256").write_text("00" * 32 + "  filer_resolution_v1.jsonl\n")

        with pytest.raises(PackageError, match="its companion says"):
            load_resolution(path)

    def test_the_component_order_is_the_digest_order(self) -> None:
        assert COMPONENTS[0] == "pit_coverage_v2.jsonl"
        assert COMPONENTS[-1] == "conflict_report_v2.json"


def bindings_for_test(prints: dict[str, str]) -> InputsBindings:
    return InputsBindings(
        generation_id="SEC_EVIDENCE_GENERATION_V2",
        database_path="stage.db",
        database_sha256="db" * 32,
        universe_count=11,
        universe_sha256="un" * 32,
        resolution_sha256="re" * 32,
        manifest_sha256="ma" * 32,
        index_sha256="ix" * 32,
        fingerprints=prints,
        repo_commit=None,
    )


class TestSupersession:
    def test_a_package_built_under_another_window_definition_is_refused(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        tmp_path: Path,
        index: Path,
    ) -> None:
        out, _ = assembled
        with closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db:
            bindings = bindings_for_test(
                {k: v.sha256 for k, v in semantic_fingerprints(db).items()}
            )
        write_package(tmp_path / "pkg", out, bindings)
        widened = replace(bindings, candidate_window_definition="EXTENDED_90_DAY_REACH")

        with pytest.raises(PackageError, match="candidate_window_definition"):
            load_planning_inputs(tmp_path / "pkg", expect=widened)

    def test_the_supersession_record_and_sidecars_never_touch_the_old_artifacts(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        tmp_path: Path,
        index: Path,
    ) -> None:
        out, _ = assembled
        with closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db:
            bindings = bindings_for_test(
                {k: v.sha256 for k, v in semantic_fingerprints(db).items()}
            )
        old_package, old_plan = tmp_path / "a3_inputs", tmp_path / "a3"
        old_package.mkdir()
        old_plan.mkdir()
        (old_package / "candidates_v2.jsonl").write_text("old\n")
        (old_plan / "plan_a3_v2.jsonl").write_text("old plan\n")
        facts = {
            "old_package_sha256": "83" * 32,
            "old_plan_sha256": "a3" * 32,
            "forensic_verdict": "C",
            "forensic_commit": "2a7d3b8",
            "reason": "candidate-window reconstruction discrepancy",
        }
        manifest = write_package(tmp_path / "new", out, bindings, supersedes=facts)

        record = write_supersession(
            tmp_path / "new",
            str(manifest["package_sha256"]),
            old_package=old_package,
            old_plan=old_plan,
            facts=facts,
        )

        assert record["old_package_sha256"] == "83" * 32
        assert record["new_package_sha256"] == manifest["package_sha256"]
        assert A3_SUPERSESSION_RECORD in package_files(tmp_path / "new")
        assert manifest["supersedes"] == facts
        assert manifest["candidate_window_definition"] == CANDIDATE_WINDOW_DEFINITION
        for old in (old_package, old_plan):
            sidecar = json.loads((old / SUPERSEDED_SIDECAR).read_text())
            assert "NOT_AUTHORIZED_FOR_EXPANSION" in sidecar["status"]
        assert (old_package / "candidates_v2.jsonl").read_text() == "old\n"
        assert (old_plan / "plan_a3_v2.jsonl").read_text() == "old plan\n"

    def test_an_existing_sidecar_is_never_overwritten(self, tmp_path: Path) -> None:
        old = tmp_path / "a3_inputs"
        old.mkdir()
        (old / SUPERSEDED_SIDECAR).write_text("first\n")

        write_supersession(
            tmp_path / "new", "00" * 32, old_package=old, old_plan=None, facts={"reason": "x"}
        )

        assert (old / SUPERSEDED_SIDECAR).read_text() == "first\n"


B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="inputs-test-bucket",
    key_id=SecretStr("inputs-key-id"),
    application_key=SecretStr("inputs-application-key"),
)
GENERATION = "SEC_EVIDENCE_GENERATION_V2"


class TestTheMirror:
    def test_every_package_file_is_mirrored_and_read_back(
        self,
        assembled: tuple[AssembledInputs, dict[str, tuple[str, int]]],
        index: Path,
        tmp_path: Path,
        fake_s3: type[FakeS3Client],
    ) -> None:
        out, _ = assembled
        with closing(sqlite3.connect(f"file:{index}?mode=ro", uri=True)) as db:
            prints = {k: v.sha256 for k, v in semantic_fingerprints(db).items()}
        bindings = InputsBindings(
            generation_id=GENERATION,
            database_path="stage.db",
            database_sha256="db" * 32,
            universe_count=11,
            universe_sha256="un" * 32,
            resolution_sha256="re" * 32,
            manifest_sha256="ma" * 32,
            index_sha256="ix" * 32,
            fingerprints=prints,
            repo_commit=None,
        )
        write_package(tmp_path / "pkg", out, bindings)
        client = fake_s3()
        mirror = ControlMirror(B2EvidenceStorage(B2_CONFIG, client=client), GENERATION)

        records = mirror_package(mirror, tmp_path / "pkg", generation_id=GENERATION)

        assert len(records) == len(package_files(tmp_path / "pkg"))
        assert {r.outcome for r in records} == {"MIRRORED"}
        assert all(k.startswith(f"control/{GENERATION}/a3-inputs-r1/") for k in client.objects)
        assert not any(k.split("/")[0] in ARTIFACT_PREFIXES for k in client.objects)
        for record in records:
            assert hashlib.sha256(client.objects[record.key][0]).hexdigest() == record.sha256


class TestTheCommand:
    @pytest.fixture
    def environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        world: tuple[sessionmaker[Session], dict[str, tuple[str, int]]],
        index: Path,
        manifest: Manifest,
        fake_s3: type[FakeS3Client],
    ) -> tuple[Path, FakeS3Client, dict[str, tuple[str, int]]]:
        """A control root carrying the index, its marker, the manifest and the resolution."""
        control = tmp_path / "control"
        (control / "manifests").mkdir(parents=True)
        (control / "plans" / "generation_v2").mkdir(parents=True)
        (control / "derived").mkdir()
        manifest.path.replace(control / "manifests" / "evidence.jsonl")
        index.replace(control / "derived" / "sec_evidence_index_v2.db")
        index_path = control / "derived" / "sec_evidence_index_v2.db"
        with closing(sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)) as db:
            prints = {
                k: {"rows": v.rows, "sha256": v.sha256}
                for k, v in semantic_fingerprints(db).items()
            }
        marker = {
            "stage": "DERIVED_INDEX_COMPLETE",
            "generation_id": GENERATION,
            "manifest_sha256": hashlib.sha256(
                (control / "manifests" / "evidence.jsonl").read_bytes()
            ).hexdigest(),
            "index_sha256": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            "fingerprints": prints,
        }
        marker_bytes = (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode()
        (control / "derived" / "derived_index_complete.json").write_bytes(marker_bytes)
        _maker, ids = world
        resolution = control / "plans" / "generation_v2" / "filer_resolution_v1.jsonl"
        lines = []
        for row in resolution_for(ids):
            kind, identity_id = row.identity
            lines.append(
                json.dumps(
                    {
                        "symbol": row.symbol,
                        "bucket": row.bucket,
                        "company_id": identity_id if kind == COMPANY else None,
                        "historical_security_id": identity_id if kind == HISTORICAL else None,
                        "cik": row.cik,
                        "source": row.source,
                        "reason": row.reason,
                        "position": 0,
                        "chunk": 0,
                    },
                    sort_keys=True,
                )
            )
        resolution.write_text("\n".join(lines) + "\n", encoding="utf-8")
        resolution.with_suffix(".sha256").write_text(
            hashlib.sha256(resolution.read_bytes()).hexdigest() + "  filer_resolution_v1.jsonl\n"
        )
        client = fake_s3()
        # The mirrored derived-index marker, as B2.12 left it.
        digest = hashlib.sha256(marker_bytes).hexdigest()
        prefix = f"control/{GENERATION}/derived/index-complete"
        client.objects[f"{prefix}/derived_index_complete-{digest[:16]}.json"] = (marker_bytes, {})
        descriptor = json.dumps(
            {
                "key": f"derived/index-complete/derived_index_complete-{digest[:16]}.json",
                "sha256": digest,
            }
        ).encode()
        client.objects[f"{prefix}/derived_index_complete-20260918T144746.834346Z.snapshot.json"] = (
            descriptor,
            {},
        )
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "inputs-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "inputs-application-key")
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        get_settings.cache_clear()
        return control, client, ids

    def test_the_command_freezes_mirrors_and_never_plans(
        self,
        environment: tuple[Path, FakeS3Client, dict[str, tuple[str, int]]],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        control, client, _ = environment

        def never(*_: object, **__: object) -> object:
            raise AssertionError("the planner must not run in B2.13")

        monkeypatch.setattr(plan_module, "_greedy", never)
        for name in (
            "plan_coverage_first",
            "plan_minimum_cover",
            "plan_balanced",
            "plan_storage_efficient",
        ):
            monkeypatch.setattr(plan_module, name, never)

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "build-a3-inputs",
                "--database",
                str(tmp_path / "stage.db"),
                "--expect-universe-count",
                "11",
            ],
        )
        get_settings.cache_clear()

        assert result.exit_code == 0, result.stdout
        assert "A3 INPUTS COMPLETE" in result.stdout
        assert "second assembly  : identical" in result.stdout
        package = control / "plans" / "generation_v2" / A3_INPUTS_DIRECTORY
        assert all((package / name).is_file() for name in package_files(package))
        assert not (package.parent / f".{A3_INPUTS_DIRECTORY}.check").exists()
        gets = [key for op, key in client.calls if op == "get_object"]
        assert not any(key.split("/")[0] in ARTIFACT_PREFIXES for key in gets)
        assert not any(
            key.split("/")[0] in ARTIFACT_PREFIXES for op, key in client.calls if op == "put_object"
        )
        assert not any(op.startswith("delete") for op, _ in client.calls)
        assert any(k.startswith(f"control/{GENERATION}/a3-inputs-r1/") for k in client.objects)
        assert "inputs-application-key" not in result.stdout

    def test_the_command_refuses_a_marker_that_does_not_describe_the_index(
        self,
        environment: tuple[Path, FakeS3Client, dict[str, tuple[str, int]]],
        tmp_path: Path,
    ) -> None:
        control, client, _ = environment
        marker = control / "derived" / "derived_index_complete.json"
        facts = json.loads(marker.read_text(encoding="utf-8"))
        facts["index_sha256"] = "00" * 32
        marker.write_text(json.dumps(facts), encoding="utf-8")

        result = CliRunner().invoke(
            app, ["evidence", "build-a3-inputs", "--database", str(tmp_path / "stage.db")]
        )
        get_settings.cache_clear()

        assert result.exit_code == 1
        assert "does not describe the manifest and index on disk" in result.stdout
        assert not (control / "plans" / "generation_v2" / A3_INPUTS_DIRECTORY).exists()
        assert not any(op == "put_object" for op, _ in client.calls)

    def test_the_command_refuses_when_the_mirrored_marker_differs(
        self,
        environment: tuple[Path, FakeS3Client, dict[str, tuple[str, int]]],
        tmp_path: Path,
    ) -> None:
        control, client, _ = environment
        for key in list(client.objects):
            if key.endswith(".snapshot.json"):
                client.objects[key] = (json.dumps({"key": "x", "sha256": "00" * 32}).encode(), {})

        result = CliRunner().invoke(
            app,
            ["evidence", "build-a3-inputs", "--database", str(tmp_path / "stage.db")],
        )
        get_settings.cache_clear()

        assert result.exit_code == 1
        assert "mirrored derived-index marker is not the local one" in result.stdout
        assert not (control / "plans" / "generation_v2" / A3_INPUTS_DIRECTORY).exists()
