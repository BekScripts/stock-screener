"""Regenerating Plan A3 from a frozen package: loaded, never reassembled; compared, never forced.

A small package is frozen from hand-built inputs and the planner is run over
it with the database, the network and the B2.13 assembly all disabled. What
must hold: the plan is a plan over exactly that package; two runs agree byte
for byte; a mutated component is refused before the planner runs; an injected
plan that steps outside the package fails validation; the historical writer's
bytes are pinned so the surviving digest can be read against them.
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

from stock_screener.cli import app
from stock_screener.config import get_settings
from stock_screener.sec_evidence import (
    A3_AUTHORITATIVE_RECORD,
    A3_EXPECTED_SHA256,
    A3_EXPECTED_TARGETS,
    A3_INPUTS_DIRECTORY,
    A3_PLAN_COMPLETE_MARKER,
    A3_PLAN_FILE,
    A3_STRATEGY,
    ARTIFACT_PREFIXES,
    B2Configuration,
    B2EvidenceStorage,
    Candidate,
    ControlMirror,
    Gap,
    GapClass,
    InputsBindings,
    PackageError,
    Plan,
    PlannedTarget,
    PlanningInputs,
    PlanValidationError,
    mirror_plan,
    month_end,
    plan_coverage_first,
    plan_fingerprints,
    regenerate_a3,
    serialize_plan,
    supported_months,
    validate_plan,
    write_a3,
    write_authorization,
    write_package,
)
from stock_screener.sec_evidence import inputs as inputs_module
from stock_screener.sec_evidence.inputs import SUPERSEDED_SIDECAR, AssembledInputs
from stock_screener.sec_evidence.regenerate import A3_PLAN_FILES

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import FakeS3Client

pytestmark = pytest.mark.integration

A = ("COMPANY", 1)
B = ("COMPANY", 2)
C = ("HISTORICAL", 3)


def gap(identity: tuple[str, int], end: date, klass: GapClass = GapClass.SOLVABLE) -> Gap:
    return Gap(
        identity=identity,
        cik=100 if identity is A else 200 if identity is B else None,
        symbol="AAA" if identity is A else "BBB" if identity is B else "GONE",
        month_end=end,
        previous_observation=None,
        days_since=None,
        next_observation=None,
        identity_state="EXACT",
        universe_state="UNKNOWN",
        gap_class=klass,
    )


def candidate(
    cik: int,
    accession: str,
    filed: date,
    identity: tuple[str, int],
    solvable: set[tuple[tuple[str, int], date]],
    *,
    form: str = "8-K",
) -> Candidate:
    covers = frozenset((identity, m) for m in supported_months(filed) if (identity, m) in solvable)
    return Candidate(
        cik=cik,
        accession=accession,
        form=form,
        filing_date=filed,
        estimated_bytes=40_000 if form == "8-K" else 2_000_000,
        yield_rate=0.94 if form == "8-K" else 0.88,
        covers=covers,
    )


def frozen_inputs() -> PlanningInputs:
    """Two issuers with open months, one unresolved security, one held filing."""
    ends = [month_end(date(2024, m, 1)) for m in range(1, 7)]
    gaps = [gap(A, e) for e in ends] + [gap(B, e) for e in ends[:3]]
    gaps.append(gap(C, ends[0], GapClass.NO_CIK))
    solvable = {(g.identity, g.month_end) for g in gaps if g.gap_class is GapClass.SOLVABLE}
    candidates = [
        candidate(100, "0000000100-24-000001", date(2024, 1, 5), A, solvable),
        candidate(100, "0000000100-24-000002", date(2024, 3, 20), A, solvable, form="10-K"),
        candidate(100, "0000000100-24-000003", date(2024, 5, 2), A, solvable),
        candidate(100, "0000000100-24-000004", date(2024, 5, 3), A, solvable),  # redundant
        candidate(200, "0000000200-24-000001", date(2024, 1, 10), B, solvable),
        candidate(200, "0000000200-24-000009", date(2024, 9, 1), B, solvable),  # covers nothing
    ]
    return PlanningInputs(
        gaps=tuple(gaps),
        candidates=tuple(candidates),
        form_yield={"8-K": 0.94, "10-K": 0.88},
        form_bytes={
            "8-K": {"mean": 40_000.0, "count": 5},
            "10-K": {"mean": 2_000_000.0, "count": 1},
        },
        held=frozenset({(100, "0000000100-24-000000"), (300, "0000000300-24-000001")}),
    )


def bindings() -> InputsBindings:
    return InputsBindings(
        generation_id="SEC_EVIDENCE_GENERATION_V2",
        database_path="stage.db",
        database_sha256="db" * 32,
        universe_count=3,
        universe_sha256="un" * 32,
        resolution_sha256="re" * 32,
        manifest_sha256="ma" * 32,
        index_sha256="ix" * 32,
        fingerprints={"catalogue": "ca" * 32, "filings": "fi" * 32},
        repo_commit="abc",
    )


@pytest.fixture
def package(tmp_path: Path) -> Path:
    inputs = frozen_inputs()
    assembled = AssembledInputs(
        inputs=inputs,
        coverage=[],
        reach=[],
        observations=[],
        form_bytes={k: dict(v) for k, v in inputs.form_bytes.items()},
        conflicts={},
        summary={},
        timings={},
    )
    write_package(tmp_path / "a3_inputs", assembled, bindings())
    return tmp_path / "a3_inputs"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No database, no network, no reassembly: the package must be enough."""

    def refuse(*_: object, **__: object) -> object:
        raise AssertionError("B2.14 must not touch this")

    monkeypatch.setattr(sqlite3, "connect", refuse)
    monkeypatch.setattr("data_access.create_engine_from_url", refuse)
    monkeypatch.setattr(inputs_module, "assemble_planning_inputs", refuse)
    monkeypatch.setattr("socket.socket", refuse)


class TestTheRunIsFromTheFrozenPackageOnly:
    def test_the_planner_runs_with_database_network_and_assembly_disabled(
        self, package: Path, offline: None
    ) -> None:
        result = regenerate_a3(package, expect=bindings())

        assert result.validation.passed
        assert result.plan.strategy == A3_STRATEGY
        # Ties on marginal months break on yield: the 8-K filed in May beats the
        # 10-K filed in March once January's 8-K has taken March.
        assert [t.accession for t in result.plan.targets] == [
            "0000000100-24-000001",
            "0000000200-24-000001",
            "0000000100-24-000003",
            "0000000100-24-000002",
        ]
        assert result.metrics["safe_potential_months"] == 9
        assert result.plan.yield_adjusted_months == pytest.approx(8.4)
        assert result.metrics["raw_unknown_floor"] == 1
        assert result.metrics["expected_remaining"] == pytest.approx(
            10 - result.plan.yield_adjusted_months
        )

    def test_two_runs_are_byte_identical(self, package: Path, offline: None) -> None:
        first = regenerate_a3(package, expect=bindings(), runs=3)
        second = regenerate_a3(package, expect=bindings())

        assert first.determinism["identical"] and first.determinism["runs"] == 3
        assert first.serialized == second.serialized
        assert first.sha256 == second.sha256
        assert first.metrics == second.metrics
        assert first.fingerprints == second.fingerprints

    def test_a_mutated_component_is_refused_before_the_planner_runs(
        self, package: Path, offline: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = package / "candidates_v2.jsonl"
        target.write_bytes(target.read_bytes() + b"\n")

        def never(*_: object, **__: object) -> object:
            raise AssertionError("the planner ran on a mutated package")

        monkeypatch.setattr("stock_screener.sec_evidence.regenerate.plan_coverage_first", never)

        with pytest.raises(PackageError, match="changed since they were frozen"):
            regenerate_a3(package, expect=bindings())

    def test_a_package_bound_to_other_sources_is_refused(
        self, package: Path, offline: None
    ) -> None:
        with pytest.raises(PackageError, match=r"other sources: \['index_sha256'\]"):
            regenerate_a3(package, expect=replace(bindings(), index_sha256="00" * 32))

    def test_the_comparison_never_claims_a_digest_mismatch_is_an_algorithm_mismatch(
        self, package: Path, offline: None
    ) -> None:
        result = regenerate_a3(package, expect=bindings())

        assert result.comparison["historical_rows"] == A3_EXPECTED_TARGETS
        assert result.comparison["historical_sha256"] == A3_EXPECTED_SHA256
        assert result.comparison["byte_comparison"] == "BYTE_DIGEST_COMPARISON_INCONCLUSIVE"
        assert result.comparison["serializer_survives"] is True
        assert result.comparison["serializer_at_a3_time_provable"] is False
        assert not result.exact


class TestValidationRefusesAPlanOutsideItsPackage:
    def injected(self, targets: list[PlannedTarget]) -> Plan:
        return Plan(
            strategy=A3_STRATEGY,
            targets=tuple(targets),
            covered=frozenset(),
            potential_months=0,
            yield_adjusted_months=0.0,
            estimated_bytes=0,
        )

    def target(
        self, cik: int, accession: str, form: str = "8-K", size: int = 40_000
    ) -> PlannedTarget:
        return PlannedTarget(
            plan_version="v1",
            rank=1,
            cik=cik,
            accession=accession,
            form=form,
            filing_date="2024-01-05",
            catalogue_source=f"submissions/CIK{cik:010d}.json",
            already_downloaded=False,
            potential_months=1,
            yield_adjusted_months=0.94,
            estimated_bytes=size,
            first_target_month="2024-01-31",
            last_target_month="2024-01-31",
            reason="injected",
            selection_strategy=A3_STRATEGY,
        )

    def test_a_target_outside_the_candidate_universe_fails(self) -> None:
        inputs = frozen_inputs()

        verdict = validate_plan(self.injected([self.target(999, "0000000999-24-000001")]), inputs)

        assert verdict.outside_candidates == 1
        assert not verdict.passed

    def test_a_target_already_held_fails(self) -> None:
        inputs = frozen_inputs()

        verdict = validate_plan(self.injected([self.target(100, "0000000100-24-000000")]), inputs)

        assert verdict.already_held == 1
        assert not verdict.passed

    def test_a_duplicate_identity_fails(self) -> None:
        inputs = frozen_inputs()
        twice = [
            self.target(100, "0000000100-24-000001"),
            self.target(100, "0000000100-24-000001"),
        ]

        verdict = validate_plan(self.injected(twice), inputs)

        assert verdict.duplicates == 1
        assert not verdict.passed

    def test_a_target_covering_no_solvable_gap_fails(self) -> None:
        inputs = frozen_inputs()

        verdict = validate_plan(self.injected([self.target(200, "0000000200-24-000009")]), inputs)

        assert verdict.without_cover == 1

    def test_a_target_with_a_foreign_form_or_estimate_fails(self) -> None:
        inputs = frozen_inputs()
        bad = self.target(100, "0000000100-24-000001", form="6-K", size=1)

        verdict = validate_plan(self.injected([bad]), inputs)

        assert verdict.invalid_forms == 1 and verdict.wrong_bytes == 1

    def test_the_real_plan_over_the_package_passes(self) -> None:
        inputs = frozen_inputs()

        verdict = validate_plan(plan_coverage_first(inputs.candidates), inputs)

        assert verdict.passed

    def test_regeneration_stops_on_an_invalid_plan(
        self, package: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "stock_screener.sec_evidence.regenerate.plan_coverage_first",
            lambda candidates: self.injected([self.target(999, "x")]),
        )

        with pytest.raises(PlanValidationError, match="does not validate"):
            regenerate_a3(package, expect=bindings())


class TestTheHistoricalSerializationIsPinned:
    """The bytes `write_plan` produces, fixed, so 53e3f0ca… can be read against them."""

    #: `write_plan`'s bytes for one target, fixed. If this changes, the
    #: historical digest can no longer be read against the current writer.
    ONE_TARGET = (
        b'{"accession": "0000000100-24-000001", "already_downloaded": false, '
        b'"catalogue_source": "submissions/CIK0000000100.json", "cik": 100, '
        b'"estimated_bytes": 40000, "filing_date": "2024-01-05", '
        b'"first_target_month": "2024-01-31", "form": "8-K", '
        b'"last_target_month": "2024-03-31", "plan_version": "v1", '
        b'"potential_months": 3, "rank": 1, '
        b'"reason": "covers 3 otherwise-unsupported month-end(s); 8-K observed cover yield 94.0%", '
        b'"selection_strategy": "COVERAGE_FIRST", "yield_adjusted_months": 2.82}\n'
    )
    ONE_TARGET_SHA256 = "3210e0971f27e23ad9a3e2bc146d9eb50c8066724f545c34a6c79bf28b1b5635"

    def test_one_target_serialises_to_exactly_these_bytes(self, tmp_path: Path) -> None:
        inputs = frozen_inputs()
        plan = plan_coverage_first(inputs.candidates[:1])

        assert serialize_plan(plan) == self.ONE_TARGET
        assert hashlib.sha256(self.ONE_TARGET).hexdigest() == self.ONE_TARGET_SHA256
        assert plan_fingerprints(plan)["ordered"] == self.ONE_TARGET_SHA256

    def test_the_historical_writer_writes_those_same_bytes(self, tmp_path: Path) -> None:
        from stock_screener.sec_evidence import write_plan

        plan = plan_coverage_first(frozen_inputs().candidates[:1])

        _, jsonl_path, sha = write_plan(tmp_path, plan)

        assert jsonl_path.read_bytes() == self.ONE_TARGET
        assert sha == self.ONE_TARGET_SHA256

    def test_the_writer_and_the_in_memory_bytes_agree(self, package: Path, tmp_path: Path) -> None:
        result = regenerate_a3(package, expect=bindings())

        digests = write_a3(
            tmp_path / "a3",
            result,
            generation_id="SEC_EVIDENCE_GENERATION_V2",
            algorithm={"plan.py": "00" * 32},
            repo_commit="abc",
        )

        written = (tmp_path / "a3" / A3_PLAN_FILE).read_bytes()
        assert written == result.serialized
        assert written == (tmp_path / "a3" / "expansion_plan_v1.jsonl").read_bytes()
        assert digests[A3_PLAN_FILE] == hashlib.sha256(written).hexdigest() == result.sha256
        assert (tmp_path / "a3" / "expansion_plan_v1.sha256").read_text().startswith(result.sha256)
        marker = json.loads((tmp_path / "a3" / A3_PLAN_COMPLETE_MARKER).read_text())
        assert marker["stage"] == "PLAN_A3_V2_COMPLETE"
        assert marker["exact_match"] is False
        assert marker["rows"] == 4
        assert all((tmp_path / "a3" / name).is_file() for name in A3_PLAN_FILES)

    def test_fingerprints_separate_order_from_membership(self) -> None:
        inputs = frozen_inputs()
        plan = plan_coverage_first(inputs.candidates)
        reversed_plan = replace(plan, targets=tuple(reversed(plan.targets)))

        forward, backward = plan_fingerprints(plan), plan_fingerprints(reversed_plan)

        assert forward["unordered_targets"] == backward["unordered_targets"]
        assert forward["per_form"] == backward["per_form"]
        assert forward["ordered_identities"] != backward["ordered_identities"]
        assert forward["ordered"] != backward["ordered"]


B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="a3-test-bucket",
    key_id=SecretStr("a3-key-id"),
    application_key=SecretStr("a3-application-key"),
)
GENERATION = "SEC_EVIDENCE_GENERATION_V2"


class TestTheMirror:
    def test_every_plan_file_is_mirrored_under_a3_and_read_back(
        self, package: Path, tmp_path: Path, fake_s3: type[FakeS3Client]
    ) -> None:
        result = regenerate_a3(package, expect=bindings())
        write_a3(tmp_path / "a3", result, generation_id=GENERATION, algorithm={}, repo_commit=None)
        client = fake_s3()
        mirror = ControlMirror(B2EvidenceStorage(B2_CONFIG, client=client), GENERATION)

        records = mirror_plan(mirror, tmp_path / "a3", generation_id=GENERATION)

        assert len(records) == len(A3_PLAN_FILES)
        assert all(k.startswith(f"control/{GENERATION}/a3/") for k in client.objects)
        assert not any(k.split("/")[0] in ARTIFACT_PREFIXES for k in client.objects)
        for record in records:
            assert hashlib.sha256(client.objects[record.key][0]).hexdigest() == record.sha256

    def test_a_prefix_keeps_a_corrected_plan_apart_from_the_superseded_one(
        self, package: Path, tmp_path: Path, fake_s3: type[FakeS3Client]
    ) -> None:
        result = regenerate_a3(package, expect=bindings())
        write_a3(
            tmp_path / "a3_r1", result, generation_id=GENERATION, algorithm={}, repo_commit=None
        )
        write_authorization(tmp_path / "a3_r1", result, generation_id=GENERATION, chain={})
        client = fake_s3()
        mirror = ControlMirror(B2EvidenceStorage(B2_CONFIG, client=client), GENERATION)

        records = mirror_plan(mirror, tmp_path / "a3_r1", generation_id=GENERATION, prefix="a3-r1")

        assert len(records) == len(A3_PLAN_FILES) + 1
        assert all(k.startswith(f"control/{GENERATION}/a3-r1/") for k in client.objects)
        assert not any(k.startswith(f"control/{GENERATION}/a3/") for k in client.objects)
        assert any("/a3-r1/authoritative_a3_plan-json/" in k for k in client.objects)


class TestTheForensicGate:
    def test_a_matching_expectation_is_an_exact_reproduction(
        self, package: Path, offline: None
    ) -> None:
        plain = regenerate_a3(package, expect=bindings())

        gated = regenerate_a3(package, expect=bindings(), forensic=(4, plain.sha256))

        assert gated.comparison["forensic_classification"] == "EXACT_FORENSIC_REPRODUCTION"
        assert gated.sha256 == plain.sha256
        assert gated.comparison["forensic_rows"] == 4

    @pytest.mark.parametrize(
        ("rows", "sha_of"),
        [(3, "same"), (4, "other")],
        ids=["row-count-differs", "digest-differs"],
    )
    def test_a_mismatch_is_named_and_never_forced(
        self, package: Path, offline: None, rows: int, sha_of: str
    ) -> None:
        plain = regenerate_a3(package, expect=bindings())
        sha = plain.sha256 if sha_of == "same" else "00" * 32

        gated = regenerate_a3(package, expect=bindings(), forensic=(rows, sha))

        assert gated.comparison["forensic_classification"] == "FORENSIC_PRODUCTION_MISMATCH"
        assert gated.sha256 == plain.sha256
        assert gated.serialized == plain.serialized

    def test_no_expectation_records_no_classification(self, package: Path, offline: None) -> None:
        result = regenerate_a3(package, expect=bindings())

        assert "forensic_classification" not in result.comparison


class TestAuthorization:
    def test_the_record_is_deterministic_and_names_the_chain(
        self, package: Path, tmp_path: Path
    ) -> None:
        result = regenerate_a3(package, expect=bindings())
        write_a3(tmp_path / "p", result, generation_id=GENERATION, algorithm={}, repo_commit=None)
        chain = {"superseded_plan_sha256": "aa" * 32, "forensic_commit": "2a7d3b8"}

        first = write_authorization(tmp_path / "p", result, generation_id=GENERATION, chain=chain)
        first_bytes = (tmp_path / "p" / A3_AUTHORITATIVE_RECORD).read_bytes()
        second = write_authorization(tmp_path / "p", result, generation_id=GENERATION, chain=chain)

        record = json.loads(first_bytes)
        assert first == second == hashlib.sha256(first_bytes).hexdigest()
        assert (tmp_path / "p" / A3_AUTHORITATIVE_RECORD).read_bytes() == first_bytes
        assert record["status"] == ["AUTHORITATIVE_V2_A3_PLAN", "AUTHORIZED_FOR_EXPANSION"]
        assert record["plan_sha256"] == result.sha256 and record["plan_rows"] == 4
        assert record["chain"] == chain
        assert record["historical"]["row_delta"] == 4 - A3_EXPECTED_TARGETS
        assert record["validation"]["passed"] is True

    def test_a_forensic_mismatch_is_refused(self, package: Path, tmp_path: Path) -> None:
        result = regenerate_a3(package, expect=bindings(), forensic=(4, "00" * 32))

        with pytest.raises(PlanValidationError, match="forensic expectation"):
            write_authorization(tmp_path / "p", result, generation_id=GENERATION, chain={})
        assert not (tmp_path / "p" / A3_AUTHORITATIVE_RECORD).exists()

    def test_a_plan_that_did_not_reproduce_is_refused(self, package: Path, tmp_path: Path) -> None:
        result = regenerate_a3(package, expect=bindings())
        broken = replace(result, determinism={**result.determinism, "identical": False})

        with pytest.raises(PlanValidationError, match="cannot be authorized"):
            write_authorization(tmp_path / "p", broken, generation_id=GENERATION, chain={})


class TestSupersededArtifactsAreNeverLoadedOrOverwritten:
    def test_a_superseded_package_is_refused_before_it_is_read(
        self, package: Path, offline: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (package / SUPERSEDED_SIDECAR).write_text("{}", encoding="utf-8")

        def never(*_: object, **__: object) -> object:
            raise AssertionError("a superseded package was loaded")

        monkeypatch.setattr("stock_screener.sec_evidence.regenerate.load_planning_inputs", never)

        with pytest.raises(PackageError, match="marked SUPERSEDED"):
            regenerate_a3(package, expect=bindings())

    def test_a_superseded_plan_directory_is_never_written_over(
        self, package: Path, tmp_path: Path
    ) -> None:
        result = regenerate_a3(package, expect=bindings())
        old = tmp_path / "a3"
        old.mkdir()
        (old / SUPERSEDED_SIDECAR).write_text("{}", encoding="utf-8")
        (old / A3_PLAN_FILE).write_bytes(b"the old plan\n")

        with pytest.raises(PackageError, match="never overwritten"):
            write_a3(old, result, generation_id=GENERATION, algorithm={}, repo_commit=None)
        assert (old / A3_PLAN_FILE).read_bytes() == b"the old plan\n"


class TestTheCommand:
    @pytest.fixture
    def environment(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        fake_s3: type[FakeS3Client],
    ) -> tuple[Path, FakeS3Client]:
        """A control root: manifest, index, resolution, and a package bound to them."""
        from stock_screener.sec_evidence import open_index, semantic_fingerprints

        control = tmp_path / "control"
        (control / "manifests").mkdir(parents=True)
        (control / "plans" / "generation_v2").mkdir(parents=True)
        (control / "derived").mkdir()
        (control / "manifests" / "evidence.jsonl").write_text("", encoding="utf-8")
        index_path = control / "derived" / "sec_evidence_index_v2.db"
        open_index(index_path).close()
        with closing(sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)) as db:
            prints = {k: v.sha256 for k, v in semantic_fingerprints(db).items()}
        resolution = control / "plans" / "generation_v2" / "filer_resolution_v1.jsonl"
        resolution.write_text(
            json.dumps(
                {
                    "symbol": "AAA",
                    "bucket": "LIVE",
                    "company_id": 1,
                    "cik": 100,
                    "source": "STORED",
                    "reason": "",
                }
            )
            + "\n"
        )
        resolution.with_suffix(".sha256").write_text(
            hashlib.sha256(resolution.read_bytes()).hexdigest() + "  filer_resolution_v1.jsonl\n"
        )
        bound = replace(
            bindings(),
            manifest_sha256=hashlib.sha256(b"").hexdigest(),
            index_sha256=hashlib.sha256(index_path.read_bytes()).hexdigest(),
            fingerprints=prints,
            resolution_sha256=hashlib.sha256(resolution.read_bytes()).hexdigest(),
        )
        inputs = frozen_inputs()
        assembled = AssembledInputs(
            inputs=inputs,
            coverage=[],
            reach=[],
            observations=[],
            form_bytes={k: dict(v) for k, v in inputs.form_bytes.items()},
            conflicts={},
            summary={},
            timings={},
        )
        write_package(control / "plans" / "generation_v2" / A3_INPUTS_DIRECTORY, assembled, bound)
        client = fake_s3()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "a3-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "a3-application-key")
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        get_settings.cache_clear()
        return control, client

    def test_the_command_plans_from_the_package_and_mirrors_without_evidence_access(
        self, environment: tuple[Path, FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        control, client = environment

        def refuse(*_: object, **__: object) -> object:
            raise AssertionError("B2.14 must not reassemble")

        monkeypatch.setattr(inputs_module, "assemble_planning_inputs", refuse)

        result = CliRunner().invoke(
            app, ["evidence", "regenerate-a3", "--expect-universe-sha", "un" * 32]
        )
        get_settings.cache_clear()

        assert result.exit_code == 0, result.stdout
        assert "PLAN A3 V2 COMPLETE — 4 targets" in result.stdout
        assert "BYTE_DIGEST_COMPARISON_INCONCLUSIVE" in result.stdout
        plan_dir = control / "plans" / "generation_v2" / "a3_r1"
        assert (plan_dir / A3_PLAN_FILE).is_file() and (
            plan_dir / A3_PLAN_COMPLETE_MARKER
        ).is_file()
        assert not any(key.split("/")[0] in ARTIFACT_PREFIXES for _op, key in client.calls)
        assert not any(op.startswith("delete") for op, _ in client.calls)
        assert any(k.startswith(f"control/{GENERATION}/a3-r1/") for k in client.objects)
        assert not any(k.startswith(f"control/{GENERATION}/a3/") for k in client.objects)
        assert not (plan_dir / A3_AUTHORITATIVE_RECORD).exists()
        assert "a3-application-key" not in result.stdout

    def test_the_command_authorizes_only_an_exact_forensic_reproduction(
        self, environment: tuple[Path, FakeS3Client]
    ) -> None:
        control, client = environment
        plan_dir = control / "plans" / "generation_v2" / "a3_r1"
        first = CliRunner().invoke(app, ["evidence", "regenerate-a3", "--no-mirror"])
        get_settings.cache_clear()
        assert first.exit_code == 0, first.stdout
        sha = (plan_dir / "plan_a3_v2.sha256").read_text().split()[0]

        mismatch = CliRunner().invoke(
            app,
            [
                "evidence",
                "regenerate-a3",
                "--expect-forensic-rows",
                "3",
                "--expect-forensic-sha",
                sha,
                "--authorize",
            ],
        )
        get_settings.cache_clear()
        calls_after_mismatch = list(client.calls)
        authorized_after_mismatch = (plan_dir / A3_AUTHORITATIVE_RECORD).exists()
        match = CliRunner().invoke(
            app,
            [
                "evidence",
                "regenerate-a3",
                "--expect-forensic-rows",
                "4",
                "--expect-forensic-sha",
                sha,
                "--authorize",
                "--supersedes-plan-sha",
                "aa" * 32,
                "--forensic-commit",
                "2a7d3b8",
            ],
        )
        get_settings.cache_clear()

        assert mismatch.exit_code == 1
        assert "STOPPED: FORENSIC_PRODUCTION_MISMATCH" in mismatch.stdout
        assert not any(op == "put_object" for op, _ in calls_after_mismatch)
        assert not authorized_after_mismatch
        assert match.exit_code == 0, match.stdout
        assert "EXACT_FORENSIC_REPRODUCTION" in match.stdout
        record = json.loads((plan_dir / A3_AUTHORITATIVE_RECORD).read_text())
        assert record["status"] == ["AUTHORITATIVE_V2_A3_PLAN", "AUTHORIZED_FOR_EXPANSION"]
        assert record["chain"]["superseded_plan_sha256"] == "aa" * 32
        assert record["chain"]["forensic_commit"] == "2a7d3b8"
        assert any("/a3-r1/authoritative_a3_plan-json/" in k for k in client.objects)

    def test_the_command_wants_both_halves_of_a_forensic_expectation(
        self, environment: tuple[Path, FakeS3Client]
    ) -> None:
        control, client = environment

        result = CliRunner().invoke(
            app, ["evidence", "regenerate-a3", "--expect-forensic-rows", "4"]
        )
        get_settings.cache_clear()

        assert result.exit_code == 1
        assert "go together" in result.stdout
        assert not (control / "plans" / "generation_v2" / "a3_r1").exists()
        assert not client.calls

    def test_the_command_refuses_a_package_whose_component_changed(
        self, environment: tuple[Path, FakeS3Client]
    ) -> None:
        control, client = environment
        target = control / "plans" / "generation_v2" / A3_INPUTS_DIRECTORY / "held_v2.jsonl"
        target.write_bytes(target.read_bytes() + b'{"cik":1,"accession":"x"}\n')

        result = CliRunner().invoke(app, ["evidence", "regenerate-a3"])
        get_settings.cache_clear()

        assert result.exit_code == 1
        assert "changed since they were frozen" in result.stdout
        assert not (control / "plans" / "generation_v2" / "a3_r1").exists()
        assert not any(op == "put_object" for op, _ in client.calls)

    def test_the_command_refuses_an_index_that_moved(
        self, environment: tuple[Path, FakeS3Client]
    ) -> None:
        control, _ = environment
        with closing(sqlite3.connect(control / "derived" / "sec_evidence_index_v2.db")) as db:
            db.execute("INSERT INTO meta VALUES ('moved', 'yes')")
            db.commit()

        result = CliRunner().invoke(app, ["evidence", "regenerate-a3"])
        get_settings.cache_clear()

        assert result.exit_code == 1
        assert "other sources" in result.stdout


class TestGreedySeparabilityAndCascade:
    """Ties decide rank and a change cascades within one issuer: why a count cannot name rows."""

    def test_same_day_filings_of_different_issuers_tie_and_are_ordered_by_identity(self) -> None:
        ends = [month_end(date(2024, m, 1)) for m in range(1, 4)]
        solvable = {(A, e) for e in ends} | {(B, e) for e in ends}
        same_day = [
            candidate(200, "0000000200-24-000001", date(2024, 1, 5), B, solvable),
            candidate(100, "0000000100-24-000001", date(2024, 1, 5), A, solvable),
        ]

        forward = plan_coverage_first(same_day)
        backward = plan_coverage_first(list(reversed(same_day)))

        assert [t.cik for t in forward.targets] == [100, 200], "the lower CIK breaks the tie"
        assert forward.targets == backward.targets, "input order never decides"

    def test_adding_a_candidate_for_one_issuer_changes_only_that_issuer(self) -> None:
        inputs = frozen_inputs()
        solvable = {
            (g.identity, g.month_end) for g in inputs.gaps if g.gap_class is GapClass.SOLVABLE
        }
        # Filed three days earlier than A's January 8-K, covering the same three
        # months: it ties on every scored component and wins on filing date, so
        # the January 8-K becomes redundant. Issuer B must not notice.
        earlier = candidate(100, "0000000100-24-000000", date(2024, 1, 2), A, solvable)

        before = plan_coverage_first(inputs.candidates)
        after = plan_coverage_first([*inputs.candidates, earlier])

        keys_before = {(t.cik, t.accession) for t in before.targets}
        keys_after = {(t.cik, t.accession) for t in after.targets}
        assert keys_before ^ keys_after == {
            (100, "0000000100-24-000000"),
            (100, "0000000100-24-000001"),
        }
        assert {k for k in keys_before if k[0] == 200} == {k for k in keys_after if k[0] == 200}
        assert after.potential_months == before.potential_months
        assert plan_fingerprints(before)["ordered"] != plan_fingerprints(after)["ordered"]
        assert plan_fingerprints(before)["per_form"] == plan_fingerprints(after)["per_form"]
