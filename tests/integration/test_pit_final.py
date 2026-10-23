"""What A3 resolved, measured: the frozen assembly over a post-A3 index against pre-A3 rows.

One issuer, eight universe months (one never listed), one baseline filing
accepted in February, one candidate the plan authorized and the expansion
acquired, accepted in May. Before A3 the May and June month-ends are open
and solvable, January is open with no candidate, July is open and not
actionable. After A3 May and June are covered by the new filing and nothing
else moves. What must hold: the transitions reconcile to the previously
open months; the source is the model's own nearest observation and is the
A3 filing; participation is counted separately; no realized edge points
forward; two materializations fingerprint identically; a pre-A3 universe
that is not the post-A3 one is refused; the package round-trips and mirrors.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from datetime import date
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from data_access import (
    CompanyRepository,
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)
from data_access.models import ResearchUniverseMonth
from domain import CompanyProfile
from stock_screener.sec_evidence import (
    AUDIT_COMPLETE_MARKER,
    COMPANY,
    FORENSIC_STATUS,
    PIT_COMPLETE_MARKER,
    PIT_FILES,
    AuditError,
    AuditResult,
    B2Configuration,
    B2EvidenceStorage,
    ControlMirror,
    Entry,
    Manifest,
    Outcome,
    PitError,
    PitResult,
    ResolvedIdentity,
    assemble_planning_inputs,
    audit_pit,
    filing_key,
    load_outcomes,
    materialize_pit,
    mirror_audit,
    mirror_pit_package,
    open_index,
    write_audit,
    write_pit_package,
)
from stock_screener.sec_evidence.pit_final import PIT_MIRROR_PREFIX, load_pre_coverage
from stock_screener.sec_evidence.targets import ManifestTarget

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.orm import Session, sessionmaker

    from conftest import FakeS3Client

pytestmark = pytest.mark.integration

BASELINE = (100, "0000000100-24-000001", "2024-02-10")
A3 = (100, "0000000100-24-000003", "2024-05-10")
PLAN = [
    ManifestTarget(
        cik=100,
        accession=A3[1],
        form="8-K",
        filing_date=A3[2],
        potential_months=3,
        yield_adjusted_months=2.8,
        estimated_bytes=40_000,
    )
]


def url_for(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/doc.htm"


def month(identity_id: int, first: date, *, venue: str | None) -> ResearchUniverseMonth:
    return ResearchUniverseMonth(
        company_id=identity_id,
        historical_security_id=None,
        symbol="AAA",
        observation_month=first,
        venue=venue,
        state="UNKNOWN",
        security_class="UNKNOWN",
        class_evidence_mode="ABSENT",
        class_age_days=None,
        class_source_accession=None,
        class_evidence_as_of=None,
        reason="fixture",
    )


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[sessionmaker[Session], int]]:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'stage.db'}")
    create_all(engine)
    maker = build_session_factory(engine)
    with session_scope(maker) as session:
        repo = CompanyRepository(session)
        repo.upsert_profile(CompanyProfile(ticker="AAA", name="AAA Inc.", exchange="NASDAQ"))
        session.flush()
        identity_id = int(next(iter(repo.list_all())).id)
        for m in range(1, 9):
            session.add(month(identity_id, date(2024, m, 1), venue=None if m == 7 else "NASDAQ"))
        session.flush()
    try:
        yield maker, identity_id
    finally:
        engine.dispose()


def _insert_filing(db: sqlite3.Connection, cik: int, accession: str, accepted: str) -> None:
    url = url_for(cik, accession)
    db.execute(
        "INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            url,
            filing_key(cik, accession, "doc.htm"),
            "ab" * 32,
            40_000,
            cik,
            accession,
            "8-K",
            accepted,
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
        "INSERT INTO securities (url, cik, accession, evidence_date, raw_title, normalized_title,"
        " raw_symbol, normalized_symbol, raw_exchange, normalized_exchange, member,"
        " no_trading_symbol, security_class, classification_reason, source_path, source_sha256,"
        " parser_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            url,
            cik,
            accession,
            accepted,
            "Common Stock",
            "Common Stock",
            "AAA",
            "AAA",
            None,
            "UNKNOWN",
            None,
            0,
            "COMMON_STOCK",
            "TITLE_ONLY",
            None,
            None,
            "9L.1",
        ),
    )


@pytest.fixture
def indexes(tmp_path: Path) -> tuple[Path, Path]:
    """The pre-A3 index (baseline filing, both catalogue rows) and the post-A3 one."""
    pre = tmp_path / "pre.db"
    with closing(open_index(pre)) as db:
        _insert_filing(db, *BASELINE)
        for cik, accession, filed in (BASELINE, A3):
            db.execute(
                "INSERT INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
                (cik, accession, "8-K", filed, f"{filed}T16:05:00.000Z", None, "doc.htm", "u"),
            )
        db.commit()
    post = tmp_path / "post.db"
    shutil.copyfile(pre, post)
    with closing(sqlite3.connect(post)) as db:
        _insert_filing(db, *A3)
        db.execute("CREATE TABLE post_a3_delta (path TEXT PRIMARY KEY, url TEXT NOT NULL)")
        db.execute(
            "INSERT INTO post_a3_delta VALUES (?, ?)",
            (filing_key(A3[0], A3[1], "doc.htm"), url_for(A3[0], A3[1])),
        )
        db.commit()
    return pre, post


def entry(cik: int, accession: str) -> Entry:
    return Entry(
        url=url_for(cik, accession),
        outcome=Outcome.DOWNLOADED,
        status=200,
        sha256="ab" * 32,
        bytes=40_000,
        retrieved_at="2026-09-19T00:00:00+00:00",
        path=filing_key(cik, accession, "doc.htm"),
        request_kind="FILING",
        cik=cik,
        accession=accession,
        form="8-K",
    )


@pytest.fixture
def manifests(tmp_path: Path) -> tuple[Manifest, Manifest]:
    (tmp_path / "pre").mkdir()
    (tmp_path / "post").mkdir()
    pre = Manifest(tmp_path / "pre" / "evidence.jsonl")
    pre.append(entry(*BASELINE[:2]))
    post = Manifest(tmp_path / "post" / "evidence.jsonl")
    post.append(entry(*BASELINE[:2]))
    post.append(entry(*A3[:2]))
    return pre, post


def resolution(identity_id: int) -> list[ResolvedIdentity]:
    return [ResolvedIdentity((COMPANY, identity_id), "AAA", "LIVE", 100, "SEC_TICKER_MAP", "")]


@pytest.fixture
def pre_rows(
    world: tuple[sessionmaker[Session], int],
    indexes: tuple[Path, Path],
    manifests: tuple[Manifest, Manifest],
) -> list[dict[str, object]]:
    maker, identity_id = world
    with (
        session_scope(maker) as session,
        closing(sqlite3.connect(f"file:{indexes[0]}?mode=ro", uri=True)) as db,
    ):
        out = assemble_planning_inputs(session, db, manifests[0], resolution(identity_id))
    return [json.loads(json.dumps(row.__dict__)) for row in out.coverage]


def materialize(
    world: tuple[sessionmaker[Session], int],
    indexes: tuple[Path, Path],
    manifests: tuple[Manifest, Manifest],
    pre_rows: list[dict[str, object]],
) -> PitResult:
    maker, identity_id = world
    with (
        session_scope(maker) as session,
        closing(sqlite3.connect(f"file:{indexes[1]}?mode=ro", uri=True)) as db,
    ):
        return materialize_pit(session, db, manifests[1], resolution(identity_id), pre_rows, PLAN)


class TestTheOutcome:
    def test_the_new_filing_covers_its_two_reachable_open_months_and_nothing_else(
        self,
        world: tuple[sessionmaker[Session], int],
        indexes: tuple[Path, Path],
        manifests: tuple[Manifest, Manifest],
        pre_rows: list[dict[str, object]],
    ) -> None:
        result = materialize(world, indexes, manifests, pre_rows)

        summary = result.summary
        # Jan and Aug have no candidate, Jul was never listed, May and Jun are solvable.
        assert summary["pre_open"] == 5
        assert summary["newly_resolved"] == 2 and summary["still_open"] == 3
        assert summary["regressed"] == 0
        assert summary["pre_open"] == summary["newly_resolved"] + summary["still_open"]
        assert summary["predicted_solvable"] == 2 and summary["predicted_solvable_resolved"] == 2
        assert summary["predicted_unsolvable"] == 3
        assert summary["predicted_unsolvable_resolved"] == 0
        assert summary["resolved_with_a3_source"] == 2
        assert summary["future_invalid_edges"] == 0 and summary["source_identity_violations"] == 0
        resolved = {o.month_end: o for o in result.outcomes if o.transition == "RESOLVED"}
        assert set(resolved) == {"2024-05-31", "2024-06-30"}
        assert all(o.source_accession == A3[1] and o.source_is_a3 for o in resolved.values())
        assert all(o.participating_a3 == 1 for o in resolved.values())
        assert resolved["2024-05-31"].days_since == 21 and resolved["2024-06-30"].days_since == 51
        assert result.by_gap["G1_SOLVABLE"] == {"open_before": 2, "resolved": 2, "still_open": 0}
        assert result.by_gap["G4_NO_CANDIDATE"]["still_open"] == 2
        assert result.by_year["2024"] == {"open_before": 5, "resolved": 2, "still_open": 3}
        july = next(o for o in result.outcomes if o.month_end == "2024-07-31")
        assert july.transition == "STILL_OPEN" and july.participating_a3 == 1  # unlisted month
        target = result.targets[0]
        assert target["months_as_source"] == 2 and target["months_participating"] == 2
        assert target["common_stock_rows"] == 1
        assert result.by_identity["fully_resolved"] == 0
        assert result.by_identity["partially_resolved"] == 1

    def test_the_gap_class_of_a_still_open_month_is_re_measured(
        self,
        world: tuple[sessionmaker[Session], int],
        indexes: tuple[Path, Path],
        manifests: tuple[Manifest, Manifest],
        pre_rows: list[dict[str, object]],
    ) -> None:
        result = materialize(world, indexes, manifests, pre_rows)

        january = next(o for o in result.outcomes if o.month_end == "2024-01-31")
        assert january.pre_gap_class == "G4_NO_CANDIDATE" == january.post_gap_class
        assert "G4_NO_CANDIDATE -> G4_NO_CANDIDATE" in result.transitions
        assert result.transitions["G1_SOLVABLE -> RESOLVED"] == 2

    def test_two_materializations_fingerprint_identically(
        self,
        world: tuple[sessionmaker[Session], int],
        indexes: tuple[Path, Path],
        manifests: tuple[Manifest, Manifest],
        pre_rows: list[dict[str, object]],
    ) -> None:
        first = materialize(world, indexes, manifests, pre_rows)
        second = materialize(world, indexes, manifests, pre_rows)

        assert first.fingerprints == second.fingerprints
        assert first.summary == second.summary
        assert first.outcomes == second.outcomes

    def test_a_pre_a3_universe_that_is_not_the_post_a3_one_is_refused(
        self,
        world: tuple[sessionmaker[Session], int],
        indexes: tuple[Path, Path],
        manifests: tuple[Manifest, Manifest],
        pre_rows: list[dict[str, object]],
    ) -> None:
        with pytest.raises(PitError, match="universes differ"):
            materialize(world, indexes, manifests, pre_rows[:-1])


class TestThePackage:
    def test_the_package_round_trips_and_mirrors_its_small_files(
        self,
        world: tuple[sessionmaker[Session], int],
        indexes: tuple[Path, Path],
        manifests: tuple[Manifest, Manifest],
        pre_rows: list[dict[str, object]],
        tmp_path: Path,
        fake_s3: type[FakeS3Client],
    ) -> None:
        result = materialize(world, indexes, manifests, pre_rows)
        bindings = {"post_a3_index_sha256": "11" * 32, "plan_sha256": "0e" * 32}

        digests = write_pit_package(tmp_path / "pit", result, bindings=bindings)
        client = fake_s3()
        mirror = ControlMirror(
            B2EvidenceStorage(
                B2Configuration(
                    endpoint_url="https://s3.us-west-004.backblazeb2.com",
                    bucket="pit-bucket",
                    key_id=SecretStr("pit-key-id"),
                    application_key=SecretStr("pit-application-key"),
                ),
                client=client,
            ),
            "SEC_EVIDENCE_GENERATION_V2",
        )
        records = mirror_pit_package(mirror, tmp_path / "pit", generation_id="G")

        assert set(digests) == set(PIT_FILES)
        for name, sha in digests.items():
            assert hashlib.sha256((tmp_path / "pit" / name).read_bytes()).hexdigest() == sha
        marker = json.loads((tmp_path / "pit" / PIT_COMPLETE_MARKER).read_text())
        assert marker["stage"] == "PIT_FINAL_COMPLETE" and marker["newly_resolved"] == 2
        assert marker["plan_sha256"] == "0e" * 32
        assert marker["files"]["pit_month_outcomes.jsonl"] == digests["pit_month_outcomes.jsonl"]
        rows = (tmp_path / "pit" / "pit_month_outcomes.jsonl").read_text().splitlines()
        assert len(rows) == 5
        assert len(records) == 6
        assert all(
            k.startswith(f"control/SEC_EVIDENCE_GENERATION_V2/{PIT_MIRROR_PREFIX}/")
            for k in client.objects
        )
        assert not any(k.startswith("filings/") for k in client.objects)

    def test_pre_coverage_is_loaded_only_when_it_hashes_as_the_package_says(
        self, tmp_path: Path
    ) -> None:
        package = tmp_path / "pkg"
        package.mkdir()
        (package / "pit_coverage_v2.jsonl").write_text('{"kind":"COMPANY"}\n')
        good = hashlib.sha256(b'{"kind":"COMPANY"}\n').hexdigest()
        (package / "planning_inputs_manifest.json").write_text(
            json.dumps({"components": {"pit_coverage_v2.jsonl": good}})
        )

        rows, digest = load_pre_coverage(package)
        (package / "planning_inputs_manifest.json").write_text(
            json.dumps({"components": {"pit_coverage_v2.jsonl": "00" * 32}})
        )

        assert rows == [{"kind": "COMPANY"}] and digest == good
        with pytest.raises(PitError, match="hashes to"):
            load_pre_coverage(package)


# ----------------------------------------------------------------------------
# B2.17: the residual audit over the same world
# ----------------------------------------------------------------------------

HELD_NO_COVER = (100, "0000000100-24-000000", "2024-01-15")
UNHELD_LATER = (100, "0000000100-24-000004", "2024-08-20")


def _extend_world(indexes: tuple[Path, Path], manifests: tuple[Manifest, Manifest]) -> None:
    """A held filing without cover rows reaching January; an unheld candidate reaching August."""
    for path in indexes:
        with closing(sqlite3.connect(path)) as db:
            for cik, accession, filed in (HELD_NO_COVER, UNHELD_LATER):
                db.execute(
                    "INSERT INTO catalogue VALUES (?,?,?,?,?,?,?,?)",
                    (cik, accession, "8-K", filed, f"{filed}T16:05:00.000Z", None, "doc.htm", "u"),
                )
            cik, accession, accepted = HELD_NO_COVER
            db.execute(
                "INSERT INTO filings VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    url_for(cik, accession),
                    filing_key(cik, accession, "doc.htm"),
                    "cd" * 32,
                    30_000,
                    cik,
                    accession,
                    "8-K",
                    accepted,
                    f"{accepted}T16:05:00.000Z",
                    None,
                    accepted,
                    "ACCEPTED",
                    None,
                    0,
                    0,
                    0,
                    1,
                    None,
                    "NONE",
                    "9L.1",
                ),
            )
            db.commit()
    for manifest in manifests:
        manifest.append(entry(*HELD_NO_COVER[:2]))


@pytest.fixture
def audited(
    world: tuple[sessionmaker[Session], int],
    indexes: tuple[Path, Path],
    manifests: tuple[Manifest, Manifest],
    tmp_path: Path,
) -> tuple[AuditResult, AuditResult, Path]:
    _extend_world(indexes, manifests)
    maker, identity_id = world
    with (
        session_scope(maker) as session,
        closing(sqlite3.connect(f"file:{indexes[0]}?mode=ro", uri=True)) as pre_db,
    ):
        pre = assemble_planning_inputs(session, pre_db, manifests[0], resolution(identity_id))
    pre_rows = [json.loads(json.dumps(row.__dict__)) for row in pre.coverage]
    result = materialize(world, indexes, manifests, pre_rows)
    pit_dir = tmp_path / "pit"
    write_pit_package(pit_dir, result, bindings={"plan_sha256": "0e" * 32})
    plan_bytes = {(A3[0], A3[1]): 40_000}
    audits = []
    with (
        session_scope(maker) as session,
        closing(sqlite3.connect(f"file:{indexes[1]}?mode=ro", uri=True)) as post_db,
    ):
        for _ in range(2):
            audits.append(
                audit_pit(
                    session,
                    post_db,
                    manifests[1],
                    resolution(identity_id),
                    pit_dir,
                    PLAN,
                    plan_bytes,
                    expected_solved=2.8,
                )
            )
    return audits[0], audits[1], pit_dir


class TestTheAudit:
    def test_the_materialization_is_re_proven_and_reconciles(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        audit, _, _ = audited

        repro = audit.reproduction
        assert repro["rows"] == 5 and repro["duplicate_identity_months"] == 0
        assert repro["newly_resolved"] == 2 and repro["still_open"] == 3
        assert repro["regressed"] == 0 and repro["future_invalid"] == 0
        assert repro["reconciles"] is True

    def test_the_planning_matrix_reconciles_to_every_open_month(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        audit, _, _ = audited

        planning = audit.planning
        # January (no candidate), July (never listed) were unsolvable; May, June, August solvable.
        assert planning["predicted_solvable"] == 3 and planning["predicted_unsolvable"] == 2
        assert planning["solvable->resolved"] == 2 and planning["solvable->unresolved"] == 1
        assert planning["unsolvable->resolved"] == 0 and planning["unsolvable->unresolved"] == 2
        assert planning["total"] == 5
        assert planning["actual_solved"] == 2 and planning["actual_minus_expected"] == -0.8
        assert audit.transitions["G1_SOLVABLE"] == {"G1_SOLVABLE": 1, "RESOLVED": 2}

    def test_every_residual_month_gets_one_class_from_the_assembly_reasons(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        audit, _, _ = audited

        by_month = {str(r["month_end"]): r for r in audit.residual_rows}
        assert set(by_month) == {"2024-01-31", "2024-07-31", "2024-08-31"}
        # January: a held filing in reach carries no cover rows — more of the same will not help.
        assert (
            by_month["2024-01-31"]["audit_class"] == "R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE"
        )
        assert by_month["2024-01-31"]["held_in_reach"] == 1
        assert "HELD_FILINGS_WITHOUT_COVER_ROWS" in by_month["2024-01-31"]["flags"]  # type: ignore[operator]
        # July: the listing record never observed the month.
        assert by_month["2024-07-31"]["audit_class"] == "R6_EXCHANGE_EVIDENCE_MISSING"
        # August: an unheld candidate filed 2024-08-20 reaches it.
        assert (
            by_month["2024-08-31"]["audit_class"]
            == "R9_POTENTIALLY_RESOLVABLE_WITH_EXISTING_UNHELD_SEC_FILINGS"
        )
        assert sum(audit.classes.values()) == 3 == sum(audit.sufficiency.values())
        assert audit.sufficiency["SEC_MORE_MAY_HELP"] == 1
        assert audit.sufficiency["SEC_MORE_UNLIKELY_TO_HELP"] == 1
        assert audit.sufficiency["STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL"] == 1
        assert audit.residual_reasons == {"G1_SOLVABLE": 1, "G4_NO_CANDIDATE": 1, "G5_OTHER": 1}

    def test_the_opportunity_is_measured_by_the_unchanged_planner_and_never_authorized(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        audit, _, _ = audited

        opportunity = audit.opportunity
        assert opportunity["candidates_reaching_residual_months"] == 1
        assert opportunity["residual_months_reachable"] == 1
        assert opportunity["future_invalid_candidate_edges"] == 0
        plan = opportunity["forensic_plan"]
        assert isinstance(plan, dict)
        assert plan["targets"] == 1 and plan["residual_months_reached"] == 1
        assert plan["remaining_after_theoretical_acquisition"] == 2
        assert plan["document_availability"] == "UNKNOWN_UNTIL_EXECUTION"
        assert audit.forensic_rows[0]["accession"] == UNHELD_LATER[1]
        assert audit.forensic_rows[0]["status"] == [
            "FORENSIC_ONLY",
            "NOT_AUTHORIZED_FOR_ACQUISITION",
        ]
        assert audit.decision["sec_reachable_months"] == 1
        assert audit.decision["remaining_after_theoretical_extra_sec"] == 2

    def test_the_curve_is_a_prefix_union_and_the_utility_a_participation_count(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        audit, _, _ = audited

        prefixes = audit.curve["prefixes"]
        assert isinstance(prefixes, list)
        assert prefixes[-1]["prefix"] == 1.0 and prefixes[-1]["resolved_months_union"] == 2
        assert prefixes[-1]["bytes"] == 40_000
        assert audit.utility["documents"] == 1 and audit.utility["documents_participating"] == 1
        assert audit.utility["resolved_months"] == 2
        assert audit.identities["by_bucket"]["LIVE"]["still_open"] == 3  # type: ignore[index]
        assert audit.by_year["2024"]["resolved"] == 2 and audit.by_year["2024"]["still_open"] == 3
        assert audit.concentration["top_10_share"] == 1.0

    def test_two_audits_fingerprint_identically(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        first, second, _ = audited

        assert first.fingerprints == second.fingerprints
        assert first.residual_rows == second.residual_rows

    def test_the_package_marks_the_forensic_plan_and_mirrors_only_small_files(
        self,
        audited: tuple[AuditResult, AuditResult, Path],
        tmp_path: Path,
        fake_s3: type[FakeS3Client],
    ) -> None:
        audit, _, _ = audited
        out = tmp_path / "audit"

        digests = write_audit(out, audit, bindings={"plan_sha256": "0e" * 32})
        client = fake_s3()
        mirror = ControlMirror(
            B2EvidenceStorage(
                B2Configuration(
                    endpoint_url="https://s3.us-west-004.backblazeb2.com",
                    bucket="audit-bucket",
                    key_id=SecretStr("audit-key-id"),
                    application_key=SecretStr("audit-application-key"),
                ),
                client=client,
            ),
            "SEC_EVIDENCE_GENERATION_V2",
        )
        records = mirror_audit(mirror, out, generation_id="G")

        status = json.loads((out / "residual_sec_forensic_plan.status.json").read_text())
        assert status["status"] == list(FORENSIC_STATUS)
        rows = (out / "residual_sec_forensic_plan.jsonl").read_text().splitlines()
        assert all(json.loads(r)["status"] == list(FORENSIC_STATUS) for r in rows)
        marker = json.loads((out / AUDIT_COMPLETE_MARKER).read_text())
        assert marker["stage"] == "PIT_AUDIT_COMPLETE" and marker["residual_months"] == 3
        assert marker["files"]["pit_residual_months.jsonl"] == digests["pit_residual_months.jsonl"]
        assert len(records) == 8
        assert not any("residual_sec_forensic_plan" in k for k in client.objects)
        assert not any("pit_residual_months" in k for k in client.objects)

    def test_outcomes_that_do_not_reproduce_their_fingerprint_are_refused(
        self, audited: tuple[AuditResult, AuditResult, Path]
    ) -> None:
        _, _, pit_dir = audited
        path = pit_dir / "pit_month_outcomes.jsonl"
        path.write_bytes(path.read_bytes().replace(b"RESOLVED", b"RESOLVEX", 1))

        with pytest.raises(AuditError, match="does not reproduce"):
            load_outcomes(pit_dir)
