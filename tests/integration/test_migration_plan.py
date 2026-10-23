"""The migration planner: one action per trusted key, from the manifest alone.

The planner is the last thing that runs before bytes move, and the only
thing standing between "whatever is on the old volume" and "what the bucket
must hold". These tests pin what it may conclude and from what:

- trust comes from the manifest's replay semantics, never from a file's
  presence, a bucket listing, or a line count;
- every trusted key gets exactly one action, in a fixed order of precedence,
  and the plan file reproduces byte for byte from the same inputs;
- the bucket is only ever listed, stat'd and read — never written — and an
  uncertain bucket is not planned against;
- the old volume is asked only about manifest keys, and a volume that fails
  mid-probe loses nothing already learned and is not asked again.

No network. The bucket is the fake client from `conftest`; the source is a
directory in `tmp_path`; the manifest is written by the tests.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING

import pytest
from botocore.exceptions import EndpointConnectionError
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_screener.cli import app
from stock_screener.config import Settings, get_settings
from stock_screener.sec_evidence import (
    MIGRATION_PLAN_VERSION,
    A3Accounting,
    Action,
    B2Configuration,
    B2EvidenceStorage,
    Entry,
    LocalEvidenceStorage,
    Manifest,
    Outcome,
    RemoteStatus,
    SourceProbe,
    SourceStatus,
    StorageUnavailableError,
    a3_accounting,
    audit_manifest,
    build_plan,
    open_control_root,
    plan_paths,
    trusted_corpus,
    write_migration_plan,
)
from stock_screener.sec_evidence.migration import read_plan_rows

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import FakeS3Client, MemoryEvidenceStorage
    from stock_screener.sec_evidence import EvidenceControlRoot

pytestmark = pytest.mark.integration

B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="planner-test-bucket",
    key_id=SecretStr("planner-key-id"),
    application_key=SecretStr("planner-application-key"),
)


def entry(
    key: str, payload: bytes, *, outcome: Outcome = Outcome.DOWNLOADED, **over: object
) -> Entry:
    """A manifest record for `payload` under `key`, URL derived from the key."""
    segments = key.split("/")
    fields: dict[str, object] = {
        "url": f"https://www.sec.gov/{key}",
        "outcome": outcome,
        "status": 200,
        "sha256": hashlib.sha256(payload).hexdigest(),
        "bytes": len(payload),
        "retrieved_at": "2026-09-15T00:00:00+00:00",
        "path": key,
        "request_kind": {"filings": "FILING", "submissions": "SUBMISSIONS"}.get(
            segments[0], "FULLTEXT"
        ),
    }
    if segments[0] == "filings":
        fields.update(
            url=f"https://www.sec.gov/Archives/edgar/data/{int(segments[1])}/{segments[2]}/{segments[3]}",
            cik=int(segments[1]),
            accession=segments[2],
            form="8-K",
            filing_date="2024-01-15",
        )
    elif segments[0] == "submissions":
        cik = int(segments[1].removeprefix("CIK").removesuffix(".json"))
        fields.update(url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json", cik=cik)
    fields.update(over)
    return Entry(**fields)  # type: ignore[arg-type]


CORPUS = {
    "filings/0000000001/000000000124000001/a.htm": b"filing a",
    "filings/0000000001/000000000124000002/b.htm": b"filing b, longer",
    "filings/0000000002/000000000224000001/c.htm": b"",
    "submissions/CIK0000000001.json": b'{"cik":1}',
    f"fulltext/{'0' * 64}.json": b"[]",
}


@pytest.fixture
def control(tmp_path: Path) -> EvidenceControlRoot:
    root = tmp_path / "control"
    root.mkdir()
    return open_control_root(Settings(sec_control_dir=str(root)))


@pytest.fixture
def manifest(control: EvidenceControlRoot) -> Manifest:
    log = Manifest(control.manifest_path)
    for key, payload in CORPUS.items():
        log.append(entry(key, payload))
    return log


@pytest.fixture
def source_root(tmp_path: Path) -> Path:
    root = tmp_path / "legacy-sec"
    root.mkdir()
    return root


def populate(root: Path, keys: dict[str, bytes]) -> LocalEvidenceStorage:
    storage = LocalEvidenceStorage(root)
    for key, payload in keys.items():
        storage.write_bytes(key, payload)
    return storage


def remote_with(
    fake_s3: type[FakeS3Client], keys: dict[str, bytes]
) -> tuple[B2EvidenceStorage, FakeS3Client]:
    client = fake_s3()
    for key, payload in keys.items():
        client.objects[key] = (payload, {"sha256": hashlib.sha256(payload).hexdigest()})
    return B2EvidenceStorage(B2_CONFIG, client=client), client


class TestTrustComesFromTheManifestReplay:
    def test_one_trusted_entry_per_key(self, manifest: Manifest) -> None:
        corpus = trusted_corpus(manifest)

        assert set(corpus.by_key) == set(CORPUS)
        assert corpus.conflicts == {}
        assert corpus.logical_bytes == sum(len(p) for p in CORPUS.values())
        assert corpus.counts_by_kind() == {"filings": 3, "submissions": 1, "fulltext": 1}
        assert corpus.audit.records == 5
        assert corpus.audit.trusted == 5

    def test_history_folds_to_the_latest_word_per_url(self, control: EvidenceControlRoot) -> None:
        """Failed, then downloaded, then cache hit, then a second URL for the same bytes."""
        log = Manifest(control.manifest_path)
        key = "filings/0000000001/000000000124000001/a.htm"
        log.append(entry(key, b"x", outcome=Outcome.FAILED, status=503, sha256=None, bytes=None))
        log.append(entry(key, b"filing a"))
        log.append(entry(key, b"filing a", outcome=Outcome.CACHE_HIT))
        log.append(entry(key, b"filing a", url="https://www.sec.gov/alternate/a.htm"))

        corpus = trusted_corpus(log)

        assert list(corpus.by_key) == [key]
        assert corpus.by_key[key].sha256 == hashlib.sha256(b"filing a").hexdigest()
        assert corpus.audit.records == 4
        assert corpus.audit.trusted == 2, "two URLs vouch; one key"
        assert corpus.audit.shared_keys == (key,)
        assert corpus.conflicts == {}

    def test_a_url_whose_latest_word_is_failure_is_not_trusted(
        self, control: EvidenceControlRoot
    ) -> None:
        log = Manifest(control.manifest_path)
        key = "filings/0000000001/000000000124000001/a.htm"
        log.append(entry(key, b"filing a"))
        log.append(entry(key, b"", outcome=Outcome.FAILED, status=500, sha256=None, bytes=None))

        corpus = trusted_corpus(log)

        assert corpus.by_key == {}

    def test_two_urls_claiming_one_key_with_different_bytes_is_a_conflict(
        self, control: EvidenceControlRoot
    ) -> None:
        log = Manifest(control.manifest_path)
        key = "filings/0000000001/000000000124000001/a.htm"
        log.append(entry(key, b"first bytes"))
        log.append(entry(key, b"other bytes!", url="https://www.sec.gov/alternate/a.htm"))

        corpus = trusted_corpus(log)
        plan = build_plan(log, remote=None, source=None, probe=SourceProbe.NONE)

        assert corpus.audit.conflicting_keys == (key,)
        assert corpus.audit.ambiguous
        assert plan.counts()[Action.CONFLICT] == 1
        assert plan.complete

    def test_an_unsafe_manifest_key_is_reported_and_excluded(
        self, control: EvidenceControlRoot
    ) -> None:
        log = Manifest(control.manifest_path)
        log.append(entry("filings/0000000001/000000000124000001/a.htm", b"ok"))
        log.append(
            entry("filings/0000000001/000000000124000001/b.htm", b"escape", path="../escape")
        )

        audit, by_key, _ = audit_manifest(log)

        assert audit.invalid_keys == (
            "https://www.sec.gov/Archives/edgar/data/1/000000000124000001/b.htm",
        )
        assert list(by_key) == ["filings/0000000001/000000000124000001/a.htm"]
        assert audit.ambiguous

    def test_an_unreadable_line_is_counted_not_trusted(self, control: EvidenceControlRoot) -> None:
        log = Manifest(control.manifest_path)
        log.append(entry("filings/0000000001/000000000124000001/a.htm", b"ok"))
        with control.manifest_path.open("a") as handle:
            handle.write("{not json\n")

        corpus = trusted_corpus(log)

        assert corpus.audit.unreadable_lines == 1
        assert corpus.audit.records == 1
        assert len(corpus.by_key) == 1


class TestManifestOnlyPlan:
    def test_no_remote_no_source_classifies_everything_refetch_required(
        self, manifest: Manifest
    ) -> None:
        plan = build_plan(manifest, remote=None, source=None, probe=SourceProbe.NONE)

        assert plan.complete
        assert plan.counts() == {
            Action.REMOTE_VERIFIED: 0,
            Action.LOCAL_VERIFIED_COPY_CANDIDATE: 0,
            Action.LOCAL_COPY_CANDIDATE: 0,
            Action.REFETCH_REQUIRED: 5,
            Action.SOURCE_UNRECOVERABLE: 0,
            Action.CONFLICT: 0,
        }
        assert all(row.local_source_status == SourceStatus.UNKNOWN for row in plan.rows)
        assert all(row.remote_status == "NOT_CONSULTED" for row in plan.rows)
        assert [row.key for row in plan.rows] == sorted(CORPUS)

    def test_a_trusted_entry_without_a_digest_is_unrecoverable_and_a_conflict(
        self, control: EvidenceControlRoot
    ) -> None:
        log = Manifest(control.manifest_path)
        log.append(entry("filings/0000000001/000000000124000001/a.htm", b"ok", sha256=None))

        plan = build_plan(log, remote=None, source=None, probe=SourceProbe.NONE)

        assert plan.counts()[Action.CONFLICT] == 1, "no digest means nothing can be verified"
        assert plan.complete

    def test_every_row_carries_only_provenance_and_observations(self, manifest: Manifest) -> None:
        plan = build_plan(manifest, remote=None, source=None, probe=SourceProbe.NONE)

        row = next(
            r for r in plan.rows if r.key.startswith("filings/0000000001/000000000124000001")
        )
        assert row.plan_version == MIGRATION_PLAN_VERSION
        assert row.expected_bytes == len(b"filing a")
        assert row.expected_sha256 == hashlib.sha256(b"filing a").hexdigest()
        assert row.artifact_kind == "filings"
        assert row.cik == 1
        assert row.accession == "000000000124000001"
        assert "planner-application-key" not in row.line()
        assert "/Volumes" not in row.line() and "pytest-of" not in row.line()


class TestLocalSourceObservations:
    def test_metadata_probe_marks_present_missing_and_size_mismatch(
        self, manifest: Manifest, source_root: Path
    ) -> None:
        present = dict(list(CORPUS.items())[:2])
        source = populate(source_root, present)
        source.write_bytes("filings/0000000002/000000000224000001/c.htm", b"not empty any more")

        plan = build_plan(manifest, remote=None, source=source, probe=SourceProbe.METADATA)

        by_key = {row.key: row for row in plan.rows}
        assert (
            by_key["filings/0000000001/000000000124000001/a.htm"].action
            == Action.LOCAL_COPY_CANDIDATE
        )
        assert (
            by_key["filings/0000000001/000000000124000001/a.htm"].local_source_status
            == SourceStatus.PRESENT
        )
        assert by_key["filings/0000000002/000000000224000001/c.htm"].action == Action.CONFLICT
        assert (
            by_key["filings/0000000002/000000000224000001/c.htm"].local_source_status
            == SourceStatus.SIZE_MISMATCH
        )
        assert by_key["submissions/CIK0000000001.json"].action == Action.REFETCH_REQUIRED
        assert by_key["submissions/CIK0000000001.json"].local_source_status == SourceStatus.MISSING
        assert plan.source_asked == 5
        assert plan.source_fault is None
        assert plan.complete

    def test_verify_probe_hashes_and_catches_same_length_corruption(
        self, manifest: Manifest, source_root: Path
    ) -> None:
        source = populate(source_root, CORPUS)
        source.write_bytes("filings/0000000001/000000000124000001/a.htm", b"filing X")

        plan = build_plan(manifest, remote=None, source=source, probe=SourceProbe.VERIFY)

        by_key = {row.key: row for row in plan.rows}
        assert by_key["filings/0000000001/000000000124000001/a.htm"].action == Action.CONFLICT
        assert (
            by_key["filings/0000000001/000000000124000001/a.htm"].local_source_status
            == SourceStatus.SHA_MISMATCH
        )
        assert (
            by_key["filings/0000000001/000000000124000002/b.htm"].action
            == Action.LOCAL_VERIFIED_COPY_CANDIDATE
        )
        assert plan.counts()[Action.LOCAL_VERIFIED_COPY_CANDIDATE] == 4

    def test_a_source_that_is_not_mounted_yields_unavailable_after_one_ask(
        self, manifest: Manifest, tmp_path: Path
    ) -> None:
        source = LocalEvidenceStorage(tmp_path / "Volumes" / "GONE" / "sec")

        plan = build_plan(manifest, remote=None, source=source, probe=SourceProbe.METADATA)

        assert plan.source_asked == 1, "one ask, then no more hammering"
        assert plan.source_fault is not None
        assert plan.source_counts()[SourceStatus.UNAVAILABLE] == 5
        assert plan.counts()[Action.REFETCH_REQUIRED] == 5
        assert plan.complete

    def test_a_source_failing_mid_probe_keeps_what_it_learned(
        self, manifest: Manifest, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        """Two keys answered, then the volume goes: those two keep their answers."""
        source = memory_storage()
        keys = sorted(CORPUS)
        for key in keys[:2]:
            source.write_bytes(key, CORPUS[key])
        asked: list[str] = []
        original = source.stat

        def failing_after_two(key: str):  # type: ignore[no-untyped-def]  # a test hook
            asked.append(key)
            if len(asked) > 2:
                raise StorageUnavailableError("Errno 5: the volume went away")
            return original(key)

        source.stat = failing_after_two  # type: ignore[method-assign]

        plan = build_plan(manifest, remote=None, source=source, probe=SourceProbe.METADATA)

        statuses = [row.local_source_status for row in plan.rows]
        assert statuses[:2] == [SourceStatus.PRESENT, SourceStatus.PRESENT]
        assert statuses[2:] == [SourceStatus.UNAVAILABLE] * 3
        assert asked == keys[:3], "the failing ask is the last ask"
        assert plan.source_asked == 3
        assert "Errno 5" in (plan.source_fault or "")
        assert plan.complete

    def test_the_source_is_never_walked(
        self, manifest: Manifest, memory_storage: type[MemoryEvidenceStorage]
    ) -> None:
        source = memory_storage()

        build_plan(manifest, remote=None, source=source, probe=SourceProbe.VERIFY)

        assert source.listed == [], "manifest keys are asked about; the tree is not enumerated"


class TestRemoteObservations:
    def test_an_empty_bucket_costs_three_lists_and_nothing_else(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        remote, client = remote_with(fake_s3, {})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        assert plan.remote is not None and plan.remote.empty
        assert [op for op, _ in client.calls] == ["list_objects_v2"] * 3
        assert all(row.remote_status == RemoteStatus.ABSENT for row in plan.rows)
        assert plan.counts()[Action.REFETCH_REQUIRED] == 5

    def test_a_remote_object_is_verified_by_download_not_metadata(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        key = "filings/0000000001/000000000124000001/a.htm"
        remote, client = remote_with(fake_s3, {key: CORPUS[key]})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        by_key = {row.key: row for row in plan.rows}
        assert by_key[key].action == Action.REMOTE_VERIFIED
        assert by_key[key].remote_status == RemoteStatus.VERIFIED
        assert ("get_object", key) in client.calls, "the digest came from a GET"
        assert plan.remote is not None and plan.remote.digests_read == 1

    def test_a_remote_object_of_the_wrong_size_is_a_conflict(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        key = "filings/0000000001/000000000124000002/b.htm"
        remote, client = remote_with(fake_s3, {key: b"short"})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        by_key = {row.key: row for row in plan.rows}
        assert by_key[key].action == Action.CONFLICT
        assert by_key[key].remote_status == RemoteStatus.SIZE_MISMATCH
        assert ("get_object", key) not in client.calls, (
            "no download for a length that already disagrees"
        )

    def test_a_remote_object_of_the_right_size_and_wrong_bytes_is_a_conflict(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        key = "filings/0000000001/000000000124000001/a.htm"
        remote, client = remote_with(fake_s3, {key: b"filing X"})
        client.objects[key] = (b"filing X", {"sha256": hashlib.sha256(CORPUS[key]).hexdigest()})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        by_key = {row.key: row for row in plan.rows}
        assert by_key[key].action == Action.CONFLICT
        assert by_key[key].remote_status == RemoteStatus.SHA_MISMATCH, (
            "a lying metadata claim did not help"
        )

    def test_a_remote_object_nobody_vouches_for_is_an_orphan(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        remote, _ = remote_with(fake_s3, {"filings/0000000009/x/stray.htm": b"?"})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        assert plan.remote_orphans == ("filings/0000000009/x/stray.htm",)
        assert plan.complete, "orphans block execution; they do not break the plan"

    def test_a_canary_object_is_never_production_evidence(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        remote, client = remote_with(fake_s3, {})
        client.objects["_canary/b28-x/filings/0000000001/000000000124000001/a.htm"] = (b"?", {})

        plan = build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

        assert plan.remote is not None and plan.remote.empty
        assert plan.remote_orphans == ()

    def test_a_listing_failure_stops_the_planner_rather_than_assuming(
        self, manifest: Manifest, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3(
            failing={"list_objects_v2": EndpointConnectionError(endpoint_url="https://x")}
        )
        remote = B2EvidenceStorage(B2_CONFIG, client=client)

        with pytest.raises(StorageUnavailableError):
            build_plan(manifest, remote=remote, source=None, probe=SourceProbe.NONE)

    def test_the_planner_never_writes_or_deletes(
        self, manifest: Manifest, fake_s3: type[FakeS3Client], source_root: Path
    ) -> None:
        key = "filings/0000000001/000000000124000001/a.htm"
        remote, client = remote_with(fake_s3, {key: CORPUS[key]})
        source = populate(source_root, CORPUS)

        build_plan(manifest, remote=remote, source=source, probe=SourceProbe.VERIFY)

        operations = {op for op, _ in client.calls}
        assert operations <= {"list_objects_v2", "head_object", "get_object"}
        assert "put_object" not in operations
        assert "delete_object" not in operations
        assert "delete_objects" not in operations


class TestPrecedenceAndCompleteness:
    def test_remote_verified_wins_over_a_local_copy(
        self, manifest: Manifest, fake_s3: type[FakeS3Client], source_root: Path
    ) -> None:
        key = "filings/0000000001/000000000124000001/a.htm"
        remote, _ = remote_with(fake_s3, {key: CORPUS[key]})
        source = populate(source_root, CORPUS)

        plan = build_plan(manifest, remote=remote, source=source, probe=SourceProbe.VERIFY)

        by_key = {row.key: row for row in plan.rows}
        assert by_key[key].action == Action.REMOTE_VERIFIED
        assert plan.counts()[Action.LOCAL_VERIFIED_COPY_CANDIDATE] == 4

    def test_every_trusted_key_appears_exactly_once_and_the_counts_sum(
        self, manifest: Manifest, fake_s3: type[FakeS3Client], source_root: Path
    ) -> None:
        remote, _ = remote_with(
            fake_s3, {"filings/0000000001/000000000124000001/a.htm": b"filing a"}
        )
        source = populate(
            source_root, {"filings/0000000001/000000000124000002/b.htm": b"filing b, longer"}
        )

        plan = build_plan(manifest, remote=remote, source=source, probe=SourceProbe.METADATA)

        assert plan.complete
        assert sum(plan.counts().values()) == len(plan.corpus.by_key) == 5
        assert len({row.key for row in plan.rows}) == 5


class TestThePlanFile:
    def test_written_deterministically_with_digest_batches_and_summary(
        self, manifest: Manifest, control: EvidenceControlRoot, fake_s3: type[FakeS3Client]
    ) -> None:
        remote, _ = remote_with(fake_s3, {})

        first = build_plan(
            manifest, remote=remote, source=None, probe=SourceProbe.NONE, batch_size=2
        )
        paths, sha_one = write_migration_plan(first, control)
        bytes_one = paths["plan"].read_bytes()
        second = build_plan(
            manifest, remote=remote, source=None, probe=SourceProbe.NONE, batch_size=2
        )
        _, sha_two = write_migration_plan(second, control)

        assert paths["plan"].read_bytes() == bytes_one
        assert sha_one == sha_two == hashlib.sha256(bytes_one).hexdigest()
        assert paths["sha256"].read_text() == f"{sha_one}  sec_b2_migration_plan_v1.jsonl\n"
        assert [b.number for b in first.batches] == [0, 1, 2]
        assert [b.targets for b in first.batches] == [2, 2, 1]
        assert [b.sha256 for b in first.batches] == [b.sha256 for b in second.batches]
        assert sum(b.logical_bytes for b in first.batches) == first.corpus.logical_bytes
        assert paths["plan"].is_relative_to(control.migration)
        summary = json.loads(paths["summary"].read_text())
        assert summary["plan_sha256"] == sha_one
        assert summary["rows"] == 5
        assert summary["complete"] is True
        assert summary["remote_orphans"] == []
        assert summary["manifest"]["trusted_keys"] == 5
        assert list(read_plan_rows(paths["plan"])) == first.rows

    def test_rows_are_in_key_order(self, manifest: Manifest, control: EvidenceControlRoot) -> None:
        plan = build_plan(manifest, remote=None, source=None, probe=SourceProbe.NONE)
        paths, _ = write_migration_plan(plan, control)

        keys = [json.loads(line)["key"] for line in paths["plan"].read_text().splitlines()]

        assert keys == sorted(keys)

    def test_the_plan_digest_changes_only_with_its_inputs(
        self, manifest: Manifest, control: EvidenceControlRoot, source_root: Path
    ) -> None:
        without = build_plan(manifest, remote=None, source=None, probe=SourceProbe.NONE)
        source = populate(source_root, CORPUS)
        with_source = build_plan(manifest, remote=None, source=source, probe=SourceProbe.METADATA)

        assert without.sha256() != with_source.sha256(), "source observations are part of the plan"
        assert (
            build_plan(manifest, remote=None, source=None, probe=SourceProbe.NONE).sha256()
            == without.sha256()
        )

    def test_the_plan_lives_under_the_control_root_not_the_artifacts(
        self, control: EvidenceControlRoot
    ) -> None:
        paths = plan_paths(control)

        assert all(p.is_relative_to(control.path / "migration") for p in paths.values())


class TestA3Accounting:
    def test_frozen_targets_partition_into_trusted_and_remaining(
        self, manifest: Manifest, tmp_path: Path
    ) -> None:
        plan = tmp_path / "a3.jsonl"
        rows = [
            {
                "cik": 1,
                "accession": "000000000124000001",
                "form": "8-K",
                "filing_date": "2024-01-15",
            },
            {
                "cik": 1,
                "accession": "000000000124000002",
                "form": "8-K",
                "filing_date": "2024-01-16",
            },
            {
                "cik": 3,
                "accession": "000000000324000001",
                "form": "10-K",
                "filing_date": "2024-02-01",
            },
        ]
        plan.write_text("".join(json.dumps(r) + "\n" for r in rows))

        accounting = a3_accounting(plan, manifest)

        assert isinstance(accounting, A3Accounting)
        assert len(accounting.frozen) == 3
        assert accounting.trusted == {(1, "000000000124000001"), (1, "000000000124000002")}
        assert accounting.remaining == {(3, "000000000324000001")}
        assert accounting.partitioned
        assert accounting.plan_sha256 == hashlib.sha256(plan.read_bytes()).hexdigest()

    def test_a_trusted_filing_outside_the_plan_does_not_count(
        self, manifest: Manifest, tmp_path: Path
    ) -> None:
        plan = tmp_path / "a3.jsonl"
        plan.write_text(
            json.dumps({"cik": 9, "accession": "x", "form": "8-K", "filing_date": "2024-01-01"})
            + "\n"
        )

        accounting = a3_accounting(plan, manifest)

        assert accounting.trusted == frozenset()
        assert accounting.remaining == {(9, "x")}
        assert accounting.partitioned


class TestTheCommand:
    @pytest.fixture
    def environment(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]  # yields
        control = tmp_path / "control"
        control.mkdir()
        monkeypatch.setenv("ENVIRONMENT", "test")
        monkeypatch.setenv("SEC_STORAGE_BACKEND", "b2")
        monkeypatch.setenv("SEC_CONTROL_DIR", str(control))
        monkeypatch.setenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
        monkeypatch.setenv("B2_BUCKET", "planner-test-bucket")
        monkeypatch.setenv("B2_KEY_ID", "planner-key-id")
        monkeypatch.setenv("B2_APPLICATION_KEY", "planner-application-key")
        get_settings.cache_clear()
        yield control
        get_settings.cache_clear()

    def test_a_missing_manifest_is_blocked_not_reconstructed(
        self, environment: Path, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )

        result = CliRunner().invoke(app, ["evidence", "migration-plan"])

        assert result.exit_code == 1
        assert "BLOCKED — AUTHORITATIVE MANIFEST UNAVAILABLE" in result.stdout
        assert "read-only listing" in result.stdout
        assert not (environment / "migration").exists()
        assert {op for op, _ in client.calls} == {"list_objects_v2"}

    def test_a_present_manifest_produces_a_plan_under_the_control_root(
        self,
        environment: Path,
        fake_s3: type[FakeS3Client],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        log = Manifest(environment / "manifests" / "evidence.jsonl")
        (environment / "manifests").mkdir()
        for key, payload in CORPUS.items():
            log.append(entry(key, payload))
        client = fake_s3()
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )
        source = tmp_path / "legacy"
        source.mkdir()
        populate(source, dict(list(CORPUS.items())[:1]))

        result = CliRunner().invoke(
            app, ["evidence", "migration-plan", "--source-root", str(source), "--batch-size", "2"]
        )

        assert result.exit_code == 0, result.stdout
        assert "PLAN WRITTEN — nothing was moved" in result.stdout
        assert "trusted artifacts: 5" in result.stdout
        assert "'LOCAL_COPY_CANDIDATE': 1" in result.stdout
        assert "'REFETCH_REQUIRED': 4" in result.stdout
        assert (environment / "migration" / "sec_b2_migration_plan_v1.jsonl").is_file()
        assert (environment / "migration" / "sec_b2_migration_plan_v1.sha256").is_file()
        assert "planner-application-key" not in result.stdout
        assert "put_object" not in {op for op, _ in client.calls}

    def test_remote_orphans_block(
        self, environment: Path, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        log = Manifest(environment / "manifests" / "evidence.jsonl")
        (environment / "manifests").mkdir()
        log.append(entry("filings/0000000001/000000000124000001/a.htm", b"filing a"))
        client = fake_s3()
        client.objects["filings/0000000009/x/stray.htm"] = (b"?", {})
        monkeypatch.setattr(
            "stock_screener.sec_evidence.wiring.B2EvidenceStorage",
            lambda config: B2EvidenceStorage(config, client=client),
        )

        result = CliRunner().invoke(app, ["evidence", "migration-plan", "--probe", "none"])

        assert result.exit_code == 1
        assert "BLOCKED — REMOTE ORPHANS" in result.stdout
        assert (environment / "migration" / "sec_b2_migration_plan_v1.jsonl").is_file(), (
            "the plan is still written"
        )
