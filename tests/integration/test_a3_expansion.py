"""Executing the authorized Plan A3 against fakes: authority, batches, the chain, resume.

A small authorized plan — five targets, two of them a co-registrant pair —
is frozen with its record, its mirror snapshots and a completed baseline,
and executed against the fake bucket and a dict of SEC URLs. What must
hold: only the authorized plan is accepted; batches are cut once and never
re-cut; a target is trusted only after its remote body read back; an object
nobody recorded is adopted only when it is the SEC's bytes; a permanent
source failure or a transport fault stops the run without a marker; a
rerun skips completed batches and fetches nothing for trusted targets; and
COMPLETE means the marker, the final snapshot and a bucket-only recovery.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING, cast

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from api_clients.errors import ProviderAuthError, ProviderRequestError
from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    ARTIFACT_PREFIXES,
    EXPANSION_MARKER,
    EXPANSION_MIRROR_PREFIX,
    AuthorizedPlan,
    B2Configuration,
    B2EvidenceStorage,
    Budget,
    ControlMirror,
    Entry,
    EvidenceWriteError,
    ExpansionRefusedError,
    ExpansionReport,
    Manifest,
    Outcome,
    StoredArtifact,
    freeze_batches,
    load_authorized_plan,
    open_control_root,
    open_index,
    run_expansion,
)
from stock_screener.sec_evidence.baseline import BASELINE_MARKER
from stock_screener.sec_evidence.expansion import (
    EXPANSION_CHECKPOINT,
    EXPANSION_DIR,
    ExpansionStage,
    load_expansion_checkpoint,
)
from stock_screener.sec_evidence.harvest import Target
from stock_screener.sec_evidence.inputs import SUPERSEDED_SIDECAR
from stock_screener.sec_evidence.regenerate import A3_AUTHORITATIVE_RECORD, A3_PLAN_FILE
from stock_screener.sec_evidence.storage import RequestKind

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import FakeS3Client, FakeSec
    from stock_screener.sec_evidence import EvidenceControlRoot, EvidenceStorage

pytestmark = pytest.mark.integration

B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="expansion-test-bucket",
    key_id=SecretStr("expansion-key-id"),
    application_key=SecretStr("expansion-application-key"),
)
GENERATION = "SEC_EVIDENCE_GENERATION_V2"
PACKAGE_SHA = "79" * 32
SUPERSEDED_PLAN_SHA = "a3" * 32

#: (cik, accession, form, filing_date, document). Rows 3 and 4 are one
#: co-registrant filing under two CIKs.
PLAN_ROWS = [
    (100, "0000000100-24-000001", "8-K", "2024-01-05", "a-1.htm"),
    (100, "0000000100-24-000002", "10-K", "2024-03-20", "a-2.htm"),
    (200, "0000000200-24-000001", "8-K", "2024-01-10", "b-1.htm"),
    (300, "0000000300-24-000007", "8-K", "2024-02-01", "shared.htm"),
    (301, "0000000300-24-000007", "8-K", "2024-02-01", "shared.htm"),
]


def url_for(cik: int, accession: str, document: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"


def key_for_row(cik: int, accession: str, document: str) -> str:
    return f"filings/{cik:010d}/{accession.replace('-', '')}/{document}"


class Environment:
    """A control root with a complete baseline, an authorized plan and its mirror."""

    def __init__(self, tmp_path: Path, client: FakeS3Client, sec: FakeSec) -> None:
        self.client = client
        self.sec = sec
        self.storage = B2EvidenceStorage(B2_CONFIG, client=client)
        self.control_path = tmp_path / "control"
        self.control_path.mkdir()
        self.control: EvidenceControlRoot = open_control_root(
            Settings(sec_control_dir=str(self.control_path))
        )
        self.mirror = ControlMirror(self.storage, GENERATION)
        generation = self.control.plans / "generation_v2"
        generation.mkdir(parents=True, exist_ok=True)
        self.plan_dir = generation / "a3_r1"
        self.plan_dir.mkdir()
        (self.control_path / "derived").mkdir(parents=True, exist_ok=True)
        self.index_path = self.control_path / "derived" / "sec_evidence_index_v2.db"
        # --- the baseline: one trusted submissions catalogue, its marker mirrored
        self.manifest = Manifest(self.control.manifest_path)
        body = b'{"cik": 100}'
        self.storage.write_bytes("submissions/CIK0000000100.json", body)
        self.manifest.append(
            Entry(
                url="https://data.sec.gov/submissions/CIK0000000100.json",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256=hashlib.sha256(body).hexdigest(),
                bytes=len(body),
                retrieved_at="2026-09-16T00:00:00+00:00",
                path="submissions/CIK0000000100.json",
                request_kind="SUBMISSIONS",
                cik=100,
            )
        )
        self.baseline_manifest_sha = sha256_of(self.control.manifest_path)
        marker = generation / BASELINE_MARKER
        marker.write_text(
            json.dumps(
                {
                    "generation_id": GENERATION,
                    "stage": "BASELINE_COMPLETE",
                    "chunks": 1,
                    "manifest_records": 1,
                    "trusted": 1,
                    "logical_bytes": len(body),
                    "manifest_sha256": self.baseline_manifest_sha,
                    "terminal_verified": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        self.marker_sha = sha256_of(marker)
        self.mirror.mirror(marker, BASELINE_MARKER)
        # --- the catalogue every target resolves through
        with closing(open_index(self.index_path)) as db:
            for cik, accession, form, filed, document in PLAN_ROWS:
                db.execute(
                    "INSERT INTO catalogue VALUES (?, ?, ?, ?, NULL, NULL, ?, ?)",
                    (cik, accession, form, filed, document, url_for(cik, accession, document)),
                )
            db.commit()
        # --- the plan, its companion and its authorization record
        lines = []
        for rank, (cik, accession, form, filed, _) in enumerate(PLAN_ROWS, start=1):
            lines.append(
                json.dumps(
                    {
                        "accession": accession,
                        "cik": cik,
                        "estimated_bytes": 40_000,
                        "filing_date": filed,
                        "form": form,
                        "plan_version": "v1",
                        "potential_months": 3,
                        "rank": rank,
                        "selection_strategy": "COVERAGE_FIRST",
                        "yield_adjusted_months": 2.8,
                    },
                    sort_keys=True,
                )
            )
        plan_bytes = ("\n".join(lines) + "\n").encode()
        (self.plan_dir / A3_PLAN_FILE).write_bytes(plan_bytes)
        self.plan_sha = hashlib.sha256(plan_bytes).hexdigest()
        (self.plan_dir / "plan_a3_v2.sha256").write_text(f"{self.plan_sha}  {A3_PLAN_FILE}\n")
        self.write_record(self.plan_sha)
        # --- what recovery reads back: the package manifest, the old plan's sidecar
        package_manifest = generation / "planning_inputs_manifest.json"
        package_manifest.write_text(json.dumps({"package_sha256": PACKAGE_SHA}) + "\n")
        self.mirror.mirror_snapshot(package_manifest, "a3-inputs-r1/planning_inputs_manifest-json")
        package_manifest.unlink()
        old = generation / "a3"
        old.mkdir()
        (old / SUPERSEDED_SIDECAR).write_text(
            json.dumps({"status": ["SUPERSEDED", "NOT_AUTHORIZED_FOR_EXPANSION"]}) + "\n"
        )
        self.mirror.mirror_snapshot(old / SUPERSEDED_SIDECAR, "a3/superseded")
        # --- the SEC
        for cik, accession, _, _, document in PLAN_ROWS:
            sec.responses[url_for(cik, accession, document)] = (
                f"<html>{cik} {accession}</html>".encode()
            )
        self.calls_before = len(client.calls)

    def write_record(self, plan_sha: str, *, status: list[str] | None = None) -> str:
        record = {
            "generation_id": GENERATION,
            "stage": "AUTHORITATIVE_V2_A3_PLAN",
            "status": status
            if status is not None
            else ["AUTHORITATIVE_V2_A3_PLAN", "AUTHORIZED_FOR_EXPANSION"],
            "plan_sha256": plan_sha,
            "plan_rows": len(PLAN_ROWS),
            "package_sha256": PACKAGE_SHA,
            "chain": {"superseded_plan_sha256": SUPERSEDED_PLAN_SHA},
        }
        path = self.plan_dir / A3_AUTHORITATIVE_RECORD
        path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        self.record_sha = sha256_of(path)
        self.mirror.mirror_snapshot(path, f"a3-r1/{A3_AUTHORITATIVE_RECORD.replace('.', '-')}")
        return self.record_sha

    def authorized(self, *, expect_plan_sha256: str | None = None) -> AuthorizedPlan:
        return load_authorized_plan(
            self.plan_dir,
            expect_plan_sha256=expect_plan_sha256 or self.plan_sha,
            expect_rows=len(PLAN_ROWS),
            expect_package_sha256=PACKAGE_SHA,
        )

    def run(
        self,
        storage: EvidenceStorage | None = None,
        *,
        batch_size: int = 2,
        stop_after_batches: int | None = None,
    ) -> ExpansionReport:
        with closing(sqlite3.connect(f"file:{self.index_path}?mode=ro", uri=True)) as catalogue:
            return run_expansion(
                storage or self.storage,
                self.control,
                lambda _budget: self.sec,
                self.authorized(),
                catalogue,
                expect_baseline_marker_sha256=self.marker_sha,
                expect_baseline_manifest_sha256=self.baseline_manifest_sha,
                expect_baseline_trusted=1,
                budget_factory=lambda: Budget(),
                batch_size=batch_size,
                workers=2,
                stop_after_batches=stop_after_batches,
            )

    def sec_calls(self) -> list[str]:
        return list(self.sec.calls)

    def evidence_calls(self, operation: str) -> list[str]:
        return [
            key
            for op, key in self.client.calls[self.calls_before :]
            if op == operation and key.split("/", 1)[0] in ARTIFACT_PREFIXES
        ]


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path: Path, fake_s3: type[FakeS3Client], fake_sec: type[FakeSec]) -> Environment:
    return Environment(tmp_path, fake_s3(), fake_sec())


class TestAuthority:
    def test_the_authorized_plan_is_accepted_with_its_bindings(self, env: Environment) -> None:
        plan = env.authorized()

        assert len(plan.targets) == 5
        assert plan.plan_sha256 == env.plan_sha
        assert plan.record_sha256 == env.record_sha
        assert plan.package_sha256 == PACKAGE_SHA
        assert plan.forms == {"10-K": 1, "8-K": 4}

    def test_a_superseded_directory_is_refused(self, env: Environment) -> None:
        (env.plan_dir / SUPERSEDED_SIDECAR).write_text("{}")

        with pytest.raises(ExpansionRefusedError, match="SUPERSEDED"):
            env.authorized()

    def test_a_plan_without_a_record_is_refused(self, env: Environment) -> None:
        (env.plan_dir / A3_AUTHORITATIVE_RECORD).unlink()

        with pytest.raises(ExpansionRefusedError, match=r"no authoritative_a3_plan\.json"):
            env.authorized()

    def test_a_record_that_does_not_authorize_expansion_is_refused(self, env: Environment) -> None:
        env.write_record(env.plan_sha, status=["AUTHORITATIVE_V2_A3_PLAN"])

        with pytest.raises(ExpansionRefusedError, match="AUTHORIZED_FOR_EXPANSION"):
            env.authorized()

    def test_a_plan_sha_mismatch_is_refused_even_with_a_valid_record(
        self, env: Environment
    ) -> None:
        with pytest.raises(ExpansionRefusedError, match="authorizes plan"):
            env.authorized(expect_plan_sha256=SUPERSEDED_PLAN_SHA)

    def test_a_record_naming_another_plan_is_refused(self, env: Environment) -> None:
        env.write_record(SUPERSEDED_PLAN_SHA)

        with pytest.raises(ExpansionRefusedError, match="authorizes plan"):
            env.authorized()

    def test_a_record_digest_mismatch_is_refused(self, env: Environment) -> None:
        with pytest.raises(ExpansionRefusedError, match="record hashes to"):
            load_authorized_plan(
                env.plan_dir, expect_plan_sha256=env.plan_sha, expect_record_sha256="00" * 32
            )

    def test_a_plan_whose_bytes_moved_is_refused(self, env: Environment) -> None:
        (env.plan_dir / A3_PLAN_FILE).write_bytes(b"")

        with pytest.raises(ExpansionRefusedError, match="not the authorized one"):
            env.authorized()


class TestBatches:
    def targets(self) -> list[Target]:
        return [
            Target(
                url=url_for(cik, accession, document),
                kind=RequestKind.FILING,
                cik=cik,
                accession=accession,
                document=document,
                form=form,
                filing_date=filed,
            )
            for cik, accession, form, filed, document in PLAN_ROWS
        ]

    def test_batches_are_cut_in_plan_order_and_digested_deterministically(
        self, env: Environment, tmp_path: Path
    ) -> None:
        plan = env.authorized()

        first, set_sha = freeze_batches(plan, self.targets(), tmp_path / "x", batch_size=2)
        second, set_again = freeze_batches(plan, self.targets(), tmp_path / "y", batch_size=2)

        assert [(r.batch, r.start, r.end, r.count) for r in first] == [
            (0, 0, 2, 2),
            (1, 2, 4, 2),
            (2, 4, 5, 1),
        ]
        assert first == second and set_sha == set_again
        lines = (tmp_path / "x" / "batches" / "batch_001.jsonl").read_text().splitlines()
        assert [json.loads(line)["rank"] for line in lines] == [3, 4]
        batch_set = json.loads((tmp_path / "x" / "batch_set.json").read_text())
        assert batch_set["plan_sha256"] == env.plan_sha
        assert batch_set["batch_set_sha256"] == set_sha

    def test_frozen_membership_is_never_regenerated(self, env: Environment, tmp_path: Path) -> None:
        plan = env.authorized()
        freeze_batches(plan, self.targets(), tmp_path / "x", batch_size=2)
        (tmp_path / "x" / "batches" / "batch_000.jsonl").write_text("{}\n")

        with pytest.raises(ExpansionRefusedError, match="never regenerated"):
            freeze_batches(plan, self.targets(), tmp_path / "x", batch_size=2)


class TestTheChain:
    def test_end_to_end_completes_with_marker_snapshot_and_recovery(self, env: Environment) -> None:
        report = env.run()

        assert report.failure is None, report.failure
        assert report.passed and report.stage == ExpansionStage.COMPLETE
        assert report.completed_batches == 3 and report.batches == 3
        marker_path = env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER
        marker = json.loads(marker_path.read_text())
        assert marker["trusted_expansion_count"] == 5 and marker["final_trusted_count"] == 6
        assert marker["baseline_manifest_sha256"] == env.baseline_manifest_sha
        assert marker["plan_sha256"] == env.plan_sha
        assert marker["superseded_plan_sha256"] == SUPERSEDED_PLAN_SHA
        assert marker["terminal_expansion_verified"] == 5
        assert marker["final_manifest_sha256"] == sha256_of(env.control.manifest_path)
        assert report.marker_sha256 == sha256_of(marker_path)
        assert f"control/{GENERATION}/{EXPANSION_MIRROR_PREFIX}/{EXPANSION_MARKER}" in (
            env.client.objects
        )
        assert report.recovery["A3_EXPANSION_RECOVERY_SUFFICIENT"] == "YES"
        assert all(v == "YES" for k, v in report.recovery.items())
        # per new target: one PUT, HEADs, one verification GET; then the terminal GET
        assert len(env.evidence_calls("put_object")) == 5
        assert len(env.evidence_calls("get_object")) == 5 + 5
        assert env.sec_calls().count(url_for(100, "0000000100-24-000001", "a-1.htm")) == 1
        assert not any(op.startswith("delete") for op, _ in env.client.calls)

    def test_co_registrant_copies_stay_distinct(self, env: Environment) -> None:
        env.run()

        keys = {
            key_for_row(300, "0000000300-24-000007", "shared.htm"),
            key_for_row(301, "0000000300-24-000007", "shared.htm"),
        }
        assert keys <= set(env.client.objects)
        trusted = [e for e in env.manifest.latest_by_url().values() if e.is_stored]
        assert {(e.cik, e.accession) for e in trusted if e.accession == "0000000300-24-000007"} == {
            (300, "0000000300-24-000007"),
            (301, "0000000300-24-000007"),
        }

    def test_nothing_reaches_the_manifest_before_the_remote_body_verified(
        self, env: Environment
    ) -> None:
        class ReadsBackWrong:
            def __init__(self, inner: B2EvidenceStorage) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

            def digest(self, key: str) -> str:
                real = self._inner.digest(key)
                return "00" * 32 if key.startswith("filings/") else real

        report = env.run(storage=cast("EvidenceStorage", ReadsBackWrong(env.storage)))

        assert report.verdict == "D" and "read back as" in str(report.failure)
        assert not any(e.request_kind == "FILING" for e in env.manifest.latest_by_url().values())
        assert not (env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER).exists()

    def test_an_unrecorded_object_holding_the_sec_bytes_is_adopted_without_a_put(
        self, env: Environment
    ) -> None:
        url = url_for(100, "0000000100-24-000001", "a-1.htm")
        key = key_for_row(100, "0000000100-24-000001", "a-1.htm")
        env.storage.write_bytes(key, env.sec.responses[url])
        puts_before = len(env.evidence_calls("put_object"))

        report = env.run()

        assert report.passed, report.failure
        assert len(env.evidence_calls("put_object")) - puts_before == 4
        entry = env.manifest.latest_by_url()[url]
        assert entry.is_stored and entry.error is not None and "adopted" in entry.error
        summary = json.loads(
            (env.plan_dir / EXPANSION_DIR / "summaries" / "batch_000.json").read_text()
        )
        assert summary["adopted_after_interrupted_put"] == 1

    def test_a_transport_fault_after_the_put_is_recovered_by_reading_the_object_back(
        self, env: Environment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stock_screener.sec_evidence.expansion.WRITE_PAUSE_S", 0.0)
        target = key_for_row(100, "0000000100-24-000002", "a-2.htm")

        class HeadTimesOutOnce:
            def __init__(self, inner: B2EvidenceStorage) -> None:
                self._inner = inner
                self.faulted = False

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

            def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
                stored = self._inner.write_bytes(key, data)
                if key == target and not self.faulted:
                    self.faulted = True
                    raise EvidenceWriteError(f"B2 upload of {key!r} could not be verified")
                return stored

        report = env.run(storage=cast("EvidenceStorage", HeadTimesOutOnce(env.storage)))

        assert report.passed, report.failure
        assert env.evidence_calls("put_object").count(target) == 1
        entry = env.manifest.latest_by_url()[url_for(100, "0000000100-24-000002", "a-2.htm")]
        assert entry.is_stored and entry.error is not None and "adopted" in entry.error

    def test_a_persistent_transport_fault_stops_as_b_without_a_manifest_row(
        self, env: Environment, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("stock_screener.sec_evidence.expansion.WRITE_PAUSE_S", 0.0)
        target = key_for_row(100, "0000000100-24-000002", "a-2.htm")

        class PutNeverLands:
            def __init__(self, inner: B2EvidenceStorage) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> object:
                return getattr(self._inner, name)

            def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
                if key == target:
                    raise EvidenceWriteError(f"B2 upload of {key!r} failed")
                return self._inner.write_bytes(key, data)

        report = env.run(storage=cast("EvidenceStorage", PutNeverLands(env.storage)))

        assert report.verdict == "B" and "upload of" in str(report.failure)
        assert url_for(100, "0000000100-24-000002", "a-2.htm") not in {
            e.url for e in env.manifest.latest_by_url().values() if e.is_stored
        }
        assert not (env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER).exists()

    def test_an_unrecorded_object_holding_other_bytes_stops_and_is_not_overwritten(
        self, env: Environment
    ) -> None:
        key = key_for_row(100, "0000000100-24-000001", "a-1.htm")
        env.storage.write_bytes(key, b"something else entirely")

        report = env.run()

        assert report.verdict == "D" and "not overwritten" in str(report.failure)
        assert env.client.objects[key][0] == b"something else entirely"
        assert not (env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER).exists()


class TestStopsWithoutAMarker:
    def test_a_permanent_source_absence_is_verdict_c_naming_the_target(
        self, env: Environment
    ) -> None:
        class NotFoundError(ProviderRequestError):
            def __init__(self, _message: str) -> None:
                super().__init__("sec request failed with HTTP 404")

        url = url_for(200, "0000000200-24-000001", "b-1.htm")
        env.sec.fail_always[url] = NotFoundError

        report = env.run()

        assert report.verdict == "C" and report.failed_targets == (url,)
        assert env.sec_calls().count(url) == 2  # two passes, no substitution
        assert not (env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER).exists()
        checkpoint = load_expansion_checkpoint(env.plan_dir / EXPANSION_DIR / EXPANSION_CHECKPOINT)
        assert checkpoint is not None and checkpoint.completed_batches == [0]

    def test_a_transient_failure_is_retried_within_the_batch(self, env: Environment) -> None:
        url = url_for(200, "0000000200-24-000001", "b-1.htm")
        env.sec.fail_once[url] = ProviderRequestError

        report = env.run()

        assert report.passed, report.failure
        assert env.sec_calls().count(url) == 2

    def test_a_refusal_stops_before_the_next_request_as_verdict_b(self, env: Environment) -> None:
        url = url_for(100, "0000000100-24-000002", "a-2.htm")
        env.sec.fail_always[url] = ProviderAuthError

        report = env.run()

        assert report.verdict == "B" and "refused the caller" in str(report.failure)
        assert env.sec_calls()[-1] == url
        assert not (env.plan_dir / EXPANSION_DIR / EXPANSION_MARKER).exists()


class TestResume:
    def test_completed_batches_are_skipped_and_trusted_targets_are_not_refetched(
        self, env: Environment
    ) -> None:
        first = env.run(stop_after_batches=1)
        calls_after_first = list(env.sec.calls)
        checkpoint = load_expansion_checkpoint(env.plan_dir / EXPANSION_DIR / EXPANSION_CHECKPOINT)

        second = env.run()

        assert not first.passed and first.stage == ExpansionStage.HARVESTING
        assert checkpoint is not None and checkpoint.completed_batches == [0]
        assert second.passed, second.failure
        new_calls = env.sec.calls[len(calls_after_first) :]
        assert url_for(100, "0000000100-24-000001", "a-1.htm") not in new_calls
        assert len(new_calls) == 3

    def test_a_target_trusted_mid_batch_costs_one_head_and_no_sec_request(
        self, env: Environment
    ) -> None:
        url = url_for(100, "0000000100-24-000001", "a-1.htm")
        key = key_for_row(100, "0000000100-24-000001", "a-1.htm")
        body = env.sec.responses[url]
        env.storage.write_bytes(key, body)
        env.manifest.append(
            Entry(
                url=url,
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256=hashlib.sha256(body).hexdigest(),
                bytes=len(body),
                retrieved_at="2026-09-19T00:00:00+00:00",
                path=key,
                request_kind="FILING",
                cik=100,
                accession="0000000100-24-000001",
            )
        )
        env.baseline_manifest_sha = sha256_of(env.control.manifest_path)
        env.storage.write_bytes  # noqa: B018 — the marker below re-binds to the new manifest
        marker = env.control.plans / "generation_v2" / BASELINE_MARKER
        facts = json.loads(marker.read_text())
        facts["manifest_sha256"] = env.baseline_manifest_sha
        facts["trusted"] = facts["terminal_verified"] = 2
        marker.write_text(json.dumps(facts, indent=2, sort_keys=True) + "\n")
        env.marker_sha = sha256_of(marker)
        env.mirror.mirror(marker, BASELINE_MARKER + ".v2")
        env.client.objects[f"control/{GENERATION}/{BASELINE_MARKER}"] = env.client.objects[
            f"control/{GENERATION}/{BASELINE_MARKER}.v2"
        ]

        with closing(sqlite3.connect(f"file:{env.index_path}?mode=ro", uri=True)) as catalogue:
            report = run_expansion(
                env.storage,
                env.control,
                lambda _budget: env.sec,
                env.authorized(),
                catalogue,
                expect_baseline_marker_sha256=env.marker_sha,
                expect_baseline_manifest_sha256=env.baseline_manifest_sha,
                expect_baseline_trusted=2,
                budget_factory=lambda: Budget(),
                batch_size=2,
            )

        assert report.passed, report.failure
        assert url not in env.sec.calls
        summary = json.loads(
            (env.plan_dir / EXPANSION_DIR / "summaries" / "batch_000.json").read_text()
        )
        assert summary["cache_hits"] == 1 and summary["new_downloads"] == 1

    def test_a_checkpoint_bound_to_other_inputs_is_refused(self, env: Environment) -> None:
        env.run(stop_after_batches=1)
        path = env.plan_dir / EXPANSION_DIR / EXPANSION_CHECKPOINT
        checkpoint = load_expansion_checkpoint(path)
        assert checkpoint is not None
        checkpoint.plan_sha256 = SUPERSEDED_PLAN_SHA
        path.write_text(json.dumps(checkpoint.__dict__, indent=2, sort_keys=True))

        report = env.run()

        assert report.verdict == "FAIL" and "binds other inputs" in str(report.failure)


class TestTheCommand:
    @pytest.fixture
    def wired(self, env: Environment, monkeypatch: pytest.MonkeyPatch) -> Environment:
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(env.control_path))
        monkeypatch.setenv("SEC_USER_AGENT", "Compounder Radar tests test@example.com")
        monkeypatch.setenv("B2_ENDPOINT_URL", B2_CONFIG.endpoint_url)
        monkeypatch.setenv("B2_BUCKET", B2_CONFIG.bucket)
        monkeypatch.setenv("B2_KEY_ID", "expansion-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "expansion-application-key")
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=env.client),
        )
        monkeypatch.setattr("stock_screener.cli._sec_documents", lambda _s, _b: env.sec)
        get_settings.cache_clear()
        return env

    def test_refuses_without_live_and_without_the_digests(self, wired: Environment) -> None:
        without_live = CliRunner().invoke(app, ["evidence", "expand-a3"])
        without_digests = CliRunner().invoke(app, ["evidence", "expand-a3", "--live"])
        get_settings.cache_clear()

        assert without_live.exit_code == 1 and "refusing without --live" in without_live.stdout
        assert without_digests.exit_code == 1 and "--expect-plan-sha" in without_digests.stdout
        assert not wired.sec.calls

    def test_the_command_executes_the_authorized_plan_to_complete(self, wired: Environment) -> None:
        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "expand-a3",
                "--live",
                "--expect-plan-sha",
                wired.plan_sha,
                "--expect-plan-rows",
                "5",
                "--expect-package-sha",
                PACKAGE_SHA,
                "--expect-record-sha",
                wired.record_sha,
                "--expect-baseline-marker-sha",
                wired.marker_sha,
                "--expect-baseline-manifest-sha",
                wired.baseline_manifest_sha,
                "--expect-baseline-trusted",
                "1",
                "--batch-size",
                "2",
            ],
        )
        get_settings.cache_clear()

        assert result.exit_code == 0, result.stdout
        assert "A3 EXPANSION COMPLETE — 5 targets trusted" in result.stdout
        assert "expansion-application-key" not in result.stdout

    def test_the_command_refuses_a_superseded_plan_directory(self, wired: Environment) -> None:
        (wired.plan_dir / SUPERSEDED_SIDECAR).write_text("{}")

        result = CliRunner().invoke(
            app,
            [
                "evidence",
                "expand-a3",
                "--live",
                "--expect-plan-sha",
                wired.plan_sha,
                "--expect-baseline-marker-sha",
                wired.marker_sha,
                "--expect-baseline-manifest-sha",
                wired.baseline_manifest_sha,
            ],
        )
        get_settings.cache_clear()

        assert result.exit_code == 1 and "SUPERSEDED" in result.stdout
        assert not wired.sec.calls
