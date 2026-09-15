"""Refusing to advance a chunk that has not earned it.

The orchestrator's whole value is in what it declines to do. A harvest command
exiting zero means one pass ran; it does not mean the chunk is finished, the
corpus still reconciles, the volume is still there, or the 250 identities are
the ones the checkpoint was written for. Each test here removes one of those
guarantees and asserts the sweep stops instead of continuing.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    DIAGNOSTICS,
    EVIDENCE_MANIFEST,
    Checkpoint,
    ChunkStage,
    Entry,
    Manifest,
    Outcome,
    Selection,
    SweepError,
    checkpoint_path,
    free_bytes,
    guard_selection,
    load_checkpoint,
    new_urls_since,
    open_evidence_root,
    preflight,
    prior_sample,
    reconcile,
    reconcile_subset,
    save_checkpoint,
    selection_digest,
    store_atomically,
    verify_digests_of,
)

if TYPE_CHECKING:
    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def selection(symbol: str, company_id: int | None = 1) -> Selection:
    return Selection(bucket="LIVE", symbol=symbol, name=symbol, cik=None, company_id=company_id)


def store(root: EvidenceRoot, cik: int, payload: bytes = b'{"ok":1}') -> Entry:
    """Put one trusted submissions artifact in place, manifest and all."""
    import hashlib

    path = root.submissions_path(cik)
    store_atomically(path, payload, root=root.path)
    entry = Entry(
        url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        outcome=Outcome.DOWNLOADED,
        status=200,
        sha256=hashlib.sha256(payload).hexdigest(),
        bytes=len(payload),
        retrieved_at="2026-09-05T00:00:00+00:00",
        path=f"submissions/CIK{cik:010d}.json",
        request_kind="SUBMISSIONS",
        cik=cik,
    )
    Manifest(root.manifests / EVIDENCE_MANIFEST).append(entry)
    return entry


class TestSelectionIdentityIsTheChunk:
    def test_the_digest_covers_the_whole_identity_not_the_ticker(self) -> None:
        """138 real symbols exist as both a live row and a delisted one."""
        live = Selection(bucket="LIVE", symbol="STRR", name="x", cik=None, company_id=7)
        dead = Selection(
            bucket="DELISTED", symbol="STRR", name="x", cik=None, historical_security_id=7
        )

        assert selection_digest([live]) != selection_digest([dead])

    def test_the_same_identities_digest_the_same(self) -> None:
        rows = [selection("A"), selection("B")]

        assert selection_digest(rows) == selection_digest(list(rows))

    def test_order_is_part_of_the_chunk(self) -> None:
        a, b = selection("A"), selection("B")

        assert selection_digest([a, b]) != selection_digest([b, a])

    def test_a_changed_population_stops_the_chunk(self) -> None:
        stored = Checkpoint(offset=500, limit=250, selection_digest="old", selected=250)

        with pytest.raises(SweepError, match="population changed"):
            guard_selection(stored, "new", 500)

    def test_an_unattempted_chunk_passes_the_guard(self) -> None:
        guard_selection(None, "any", 500)

    def test_a_matching_digest_passes_the_guard(self) -> None:
        stored = Checkpoint(offset=500, limit=250, selection_digest="same", selected=250)

        guard_selection(stored, "same", 500)


class TestPreflightRefusesUnsafeStarts:
    def test_a_missing_root_stops_the_chunk(self, root: EvidenceRoot) -> None:
        for child in sorted(root.path.rglob("*"), reverse=True):
            child.rmdir() if child.is_dir() else child.unlink()
        root.path.rmdir()

        with pytest.raises(SweepError, match="not a directory"):
            preflight(root)

    def test_an_exhausted_reserve_stops_the_chunk(self, root: EvidenceRoot) -> None:
        with pytest.raises(SweepError, match="below the"):
            preflight(root, min_free_bytes=free_bytes(root) + 1)

    def test_an_unreconciled_corpus_stops_the_chunk(self, root: EvidenceRoot) -> None:
        """A chunk starting on a broken corpus cannot tell what it broke."""
        entry = store(root, 320193)
        (root.path / (entry.path or "")).write_bytes(b"tampered")

        with pytest.raises(SweepError, match="does not reconcile"):
            preflight(root)

    def test_a_sound_corpus_passes(self, root: EvidenceRoot) -> None:
        store(root, 320193)

        preflight(root, min_free_bytes=0)


class TestCheckpointsSurviveInterruption:
    def test_a_checkpoint_round_trips(self, root: EvidenceRoot) -> None:
        path = checkpoint_path(root, 500, 250)
        point = Checkpoint(offset=500, limit=250, selection_digest="abc", selected=250)

        save_checkpoint(path, point)

        assert load_checkpoint(path) == point

    def test_an_absent_checkpoint_reads_as_none(self, root: EvidenceRoot) -> None:
        assert load_checkpoint(checkpoint_path(root, 999, 250)) is None

    def test_a_damaged_checkpoint_reads_as_none_rather_than_raising(
        self, root: EvidenceRoot
    ) -> None:
        """The digest is re-derived every run, so a lost checkpoint is recoverable."""
        path = checkpoint_path(root, 500, 250)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")

        assert load_checkpoint(path) is None

    def test_a_save_leaves_no_temporary_file(self, root: EvidenceRoot) -> None:
        path = checkpoint_path(root, 500, 250)

        save_checkpoint(path, Checkpoint(offset=500, limit=250, selection_digest="a", selected=250))

        assert [p.name for p in path.parent.iterdir()] == [path.name]

    def test_resume_from_pass_one_complete(self, root: EvidenceRoot) -> None:
        path = checkpoint_path(root, 500, 250)
        save_checkpoint(
            path,
            Checkpoint(
                offset=500,
                limit=250,
                selection_digest="d",
                selected=250,
                stage=ChunkStage.PASS1_DONE,
            ),
        )

        resumed = load_checkpoint(path)

        assert resumed is not None
        assert resumed.stage == ChunkStage.PASS1_DONE
        assert not resumed.complete

    def test_resume_from_pass_two_without_convergence_is_incomplete(
        self, root: EvidenceRoot
    ) -> None:
        path = checkpoint_path(root, 500, 250)
        save_checkpoint(
            path,
            Checkpoint(
                offset=500,
                limit=250,
                selection_digest="d",
                selected=250,
                stage=ChunkStage.PASS2_DONE,
            ),
        )

        resumed = load_checkpoint(path)

        assert resumed is not None
        assert not resumed.complete

    def test_only_the_complete_stage_counts_as_finished(self) -> None:
        point = Checkpoint(offset=0, limit=250, selection_digest="d", selected=250)
        for stage in (
            ChunkStage.PENDING,
            ChunkStage.PASS1_DONE,
            ChunkStage.PASS2_DONE,
            ChunkStage.CONVERGED,
            ChunkStage.FAILED,
        ):
            point.stage = stage
            assert not point.complete
        point.stage = ChunkStage.COMPLETE
        assert point.complete

    def test_the_checkpoint_lives_beneath_the_evidence_root(self, root: EvidenceRoot) -> None:
        """Never on the boot disk — it belongs with the evidence it describes."""
        assert checkpoint_path(root, 500, 250).is_relative_to(root.path)


class TestReconciliationFindsWhatItShould:
    def test_a_sound_corpus_is_clean(self, root: EvidenceRoot) -> None:
        store(root, 320193)

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.clean
        assert state.trusted == 1 and state.submissions == 1

    def test_a_tampered_artifact_is_a_hash_mismatch(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).write_bytes(b"tampered")

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.hash_mismatches == 1
        assert not state.clean

    def test_a_deleted_artifact_is_an_orphan_record(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).unlink()

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_records == 1

    def test_a_file_nobody_vouches_for_is_an_orphan_file(self, root: EvidenceRoot) -> None:
        store_atomically(root.submissions_path(1), b"stray", root=root.path)

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_files == 1

    def test_a_record_whose_path_contradicts_its_url_is_a_violation(
        self, root: EvidenceRoot
    ) -> None:
        store(root, 320193)
        Manifest(root.manifests / EVIDENCE_MANIFEST).append(
            Entry(
                url="https://data.sec.gov/submissions/CIK0000000042.json",
                outcome=Outcome.DOWNLOADED,
                status=200,
                sha256="a" * 64,
                bytes=5,
                path="submissions/CIK0000320193.json",
                request_kind="SUBMISSIONS",
                cik=42,
            )
        )

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.path_violations >= 1

    def test_problems_names_every_failing_category(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).unlink()

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert "orphan_records" in state.problems
        assert state.summary().startswith("1 trusted")

    def test_a_checkpoint_is_not_mistaken_for_an_orphan_artifact(self, root: EvidenceRoot) -> None:
        """A sweep writes its checkpoint under the evidence root.

        The first real chunk stopped on exactly this: the orchestrator wrote its
        own progress file and the next reconciliation called it evidence nobody
        vouched for. Bookkeeping lives beside the evidence and is not evidence.
        """
        store(root, 320193)
        save_checkpoint(
            checkpoint_path(root, 500, 250),
            Checkpoint(offset=500, limit=250, selection_digest="d", selected=250),
        )

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_files == 0
        assert state.clean

    def test_a_checkpoint_does_not_inflate_the_file_count(self, root: EvidenceRoot) -> None:
        store(root, 320193)
        save_checkpoint(
            checkpoint_path(root, 0, 250),
            Checkpoint(offset=0, limit=250, selection_digest="d", selected=250),
        )

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.files_on_disk == 1

    def test_an_appledouble_sidecar_is_never_touched(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """macOS writes `._` sidecars on exFAT, and one can raise while being written.

        Chunk 3 died on exactly this: `[Errno 70] Stale NFS file handle` from
        `is_file()` on a sidecar macOS was still creating, which aborted a
        multi-hour sweep during reconciliation. A sidecar is not evidence and is
        never inspected, so it must be excluded by name before anything touches
        the filesystem.
        """
        store(root, 320193)
        sidecar = root.submissions / "._CIK0000320193.json"
        sidecar.write_bytes(b"appledouble")
        original = Path.is_file

        def explode(self: Path) -> bool:
            if self.name.startswith("._"):
                message = "Stale NFS file handle"
                raise OSError(70, message)
            return original(self)

        monkeypatch.setattr(Path, "is_file", explode)

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.clean
        assert state.files_on_disk == 1

    def test_a_sidecar_is_not_counted_as_evidence(self, root: EvidenceRoot) -> None:
        store(root, 320193)
        (root.submissions / "._CIK0000320193.json").write_bytes(b"appledouble")

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_files == 0
        assert state.files_on_disk == 1

    def test_an_unreadable_real_artifact_still_raises(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A volume that cannot serve real evidence is exactly when to stop."""
        store(root, 320193)
        original = Path.is_file

        def explode(self: Path) -> bool:
            if self.name == "CIK0000320193.json":
                message = "Stale NFS file handle"
                raise OSError(70, message)
            return original(self)

        monkeypatch.setattr(Path, "is_file", explode)

        with pytest.raises(OSError, match="Stale"):
            reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

    def test_skipping_digests_still_finds_missing_files(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).unlink()

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST), verify_digests=False)

        assert state.orphan_records == 1
        assert not state.digests_verified


class TestTieredVerification:
    """The 9K.25 policy: everything new is hashed, the old corpus is sampled.

    Full rehashing reached 1,523s at 8,963 artifacts and grows with the corpus,
    so verifying everything after every one of 28 chunks would cost more hours
    than the fetching does. The policy that replaces it is only defensible if the
    sample is *reproducible* — a sample drawn from process state proves nothing
    afterwards, because nobody can say which artifacts it covered.
    """

    def test_new_urls_are_exactly_what_the_chunk_appended(self, root: EvidenceRoot) -> None:
        store(root, 1)
        store(root, 2)
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        before = sum(1 for _ in manifest.entries())
        store(root, 3)

        assert new_urls_since(manifest, before) == {
            "https://data.sec.gov/submissions/CIK0000000003.json"
        }

    def test_a_retry_of_an_older_url_counts_as_new(self, root: EvidenceRoot) -> None:
        """Re-storing an artifact rewrites its bytes, so it is re-verified."""
        first = store(root, 1)
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        before = sum(1 for _ in manifest.entries())
        manifest.append(first)

        assert new_urls_since(manifest, before) == {first.url}

    def test_the_sample_is_identical_across_runs(self, root: EvidenceRoot) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 60)]

        first = prior_sample(entries, exclude=set(), minimum=10)
        second = prior_sample(list(reversed(entries)), exclude=set(), minimum=10)

        assert [e.url for e in first] == [e.url for e in second]

    def test_the_sample_ignores_what_was_already_verified(self, root: EvidenceRoot) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 40)]
        exclude = {entries[0].url, entries[1].url}

        chosen = prior_sample(entries, exclude=exclude, minimum=5)

        assert exclude.isdisjoint({e.url for e in chosen})

    def test_the_floor_applies_until_the_percentage_exceeds_it(self, root: EvidenceRoot) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 200)]

        small = prior_sample(entries[:80], exclude=set(), minimum=100, fraction=0.05)
        large = prior_sample(entries, exclude=set(), minimum=5, fraction=0.5)

        assert len(small) == 80, "a corpus smaller than the floor is taken whole"
        assert len(large) == 99, "the percentage wins once it exceeds the floor"

    def test_the_sample_holds_no_duplicates(self, root: EvidenceRoot) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 150)]

        chosen = prior_sample(entries, exclude=set(), minimum=140)

        assert len({e.url for e in chosen}) == len(chosen)

    def test_a_tampered_sampled_artifact_is_caught(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).write_bytes(b"different")

        assert verify_digests_of(root, [entry])["hash_mismatches"] == 1

    def test_a_missing_sampled_artifact_is_caught(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).unlink()

        result = verify_digests_of(root, [entry])

        assert result == {"checked": 0, "hash_mismatches": 0, "byte_mismatches": 0, "missing": 1}

    def test_an_intact_corpus_verifies_clean(self, root: EvidenceRoot) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 20)]

        result = verify_digests_of(root, entries)

        assert result["checked"] == 19
        assert result["hash_mismatches"] == 0
        assert result["missing"] == 0

    def test_an_empty_prior_corpus_samples_nothing(self, root: EvidenceRoot) -> None:
        entry = store(root, 1)

        assert prior_sample([entry], exclude={entry.url}, minimum=500) == []


class TestScopedReconciliationKeepsItsGuarantees:
    """Reconciling a batch's own artifacts without re-walking the whole corpus.

    Phase 9L.6 measured the whole-corpus walk at 108,503 entries and ~46 s, run
    after every one of 61 batches — O(batches x corpus), the same quadratic shape
    9K.17 removed from manifest lookup. Scoping the per-batch pass fixes that.

    The bargain has to be exact: **every artifact named is checked as fully as
    the whole-corpus pass would check it**, and only the scope narrows. These
    tests pin both halves — that nothing in the batch can hide from the scoped
    check, and that what the scoped check cannot see is still caught by the
    scheduled full walk.
    """

    def test_a_tampered_batch_artifact_is_caught(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).write_bytes(b"tampered")

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [entry.url])

        assert state.hash_mismatches == 1
        assert not state.clean

    def test_a_deleted_batch_artifact_is_caught(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).unlink()

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [entry.url])

        assert state.orphan_records == 1
        assert not state.clean

    def test_a_truncated_batch_artifact_is_caught(self, root: EvidenceRoot) -> None:
        entry = store(root, 320193)
        (root.path / (entry.path or "")).write_bytes(b"")

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [entry.url])

        assert state.zero_byte == 1
        assert state.byte_mismatches == 1

    def test_a_stray_file_in_the_batch_directory_is_caught(self, root: EvidenceRoot) -> None:
        """Bytes nobody vouches for, written beside this batch's own artifacts."""
        entry = store(root, 320193)
        (root.path / (entry.path or "")).parent.joinpath("stray.htm").write_bytes(b"junk")

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [entry.url])

        assert state.orphan_files == 1
        assert not state.clean

    def test_an_earlier_batchs_artifact_in_the_same_directory_is_not_an_orphan(
        self, root: EvidenceRoot
    ) -> None:
        """Scoped orphan detection compares against the WHOLE trusted set."""
        earlier = store(root, 320193, payload=b'{"first":1}')
        later = store(root, 320194, payload=b'{"second":1}')
        # Put the later artifact in the earlier one's directory, as a
        # co-registrant or same-accession document would be.
        assert (root.path / (earlier.path or "")).parent == (root.path / (later.path or "")).parent

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [later.url])

        assert state.orphan_files == 0, "an earlier batch's trusted file is claimed, not orphaned"
        assert state.clean

    def test_the_scoped_pass_checks_digests_as_fully_as_the_full_pass(
        self, root: EvidenceRoot
    ) -> None:
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 12)]
        (root.path / (entries[7].path or "")).write_bytes(b"changed")
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)

        scoped = reconcile_subset(root, manifest, [e.url for e in entries])
        full = reconcile(root, manifest, verify_digests=True)

        assert scoped.hash_mismatches == full.hash_mismatches == 1
        assert scoped.trusted == full.trusted == len(entries)

    def test_the_scoped_pass_does_not_see_corruption_outside_its_scope(
        self, root: EvidenceRoot
    ) -> None:
        """The deliberate limit — and precisely why the full walk is retained."""
        mine = store(root, 1, payload=b'{"mine":1}')
        elsewhere = store(root, 999999, payload=b'{"other":1}')
        (root.path / (elsewhere.path or "")).write_bytes(b"tampered elsewhere")
        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)

        scoped = reconcile_subset(root, manifest, [mine.url])
        full = reconcile(root, manifest, verify_digests=True)

        assert scoped.hash_mismatches == 0, "out of scope by design"
        assert full.hash_mismatches == 1, "the scheduled full walk still finds it"

    def test_the_full_walk_still_scans_the_complete_corpus(self, root: EvidenceRoot) -> None:
        """Milestone and terminal passes must remain whole-corpus."""
        entries = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 25)]
        (root.path / (entries[0].path or "")).parent.joinpath("orphan.json").write_bytes(b"x")

        full = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST), verify_digests=True)

        assert full.trusted == len(entries)
        assert full.files_on_disk == len(entries) + 1
        assert full.orphan_files == 1

    def test_an_empty_scope_checks_nothing_and_stays_clean(self, root: EvidenceRoot) -> None:
        store(root, 320193)

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [])

        assert state.trusted == 0
        assert state.clean

    def test_scoped_reconciliation_reports_manifest_wide_accounting(
        self, root: EvidenceRoot
    ) -> None:
        """Record counts stay corpus-wide — they are cheap and must not narrow."""
        first = store(root, 1)
        store(root, 2)
        store(root, 3)

        state = reconcile_subset(root, Manifest(root.manifests / EVIDENCE_MANIFEST), [first.url])

        assert state.manifest_records == 3, "all records counted"
        assert state.trusted == 1, "only the named artifact checked"


class TestRecoveryAfterAStorageFault:
    """What must hold when the volume vanishes mid-write and comes back.

    Phase 9L.6 lost the block device under sustained load while a checkpoint was
    being written. Two things decide whether the corpus can be trusted
    afterwards: that non-evidence subtrees never masquerade as evidence, and
    that **a checkpoint claiming COMPLETE can never stand in for the bytes**.
    The manifest and the disk are authoritative; a checkpoint is bookkeeping.
    """

    def test_the_diagnostics_subtree_is_not_evidence(self, root: EvidenceRoot) -> None:
        """Storage qualification writes real bytes to the real volume, by design."""
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
        bench = root.path / DIAGNOSTICS / "storage-test"
        bench.mkdir(parents=True)
        (bench / "large.bin").write_bytes(b"x" * 4096)
        for index in range(50):
            (bench / f"small-{index}.bin").write_bytes(b"y")

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.orphan_files == 0, "51 diagnostic files must not read as orphans"
        assert state.files_on_disk == 1
        assert state.clean

    def test_a_checkpoint_cannot_vouch_for_missing_evidence(
        self, root: EvidenceRoot, tmp_path: Path
    ) -> None:
        """The rule the recovery audit rests on: trust the manifest, not the status."""
        entry = store(root, 320193)
        marker = checkpoint_path(root, 0, 250)
        save_checkpoint(
            marker,
            Checkpoint(
                offset=0,
                limit=250,
                selection_digest="x" * 64,
                selected=250,
                stage=ChunkStage.COMPLETE,
            ),
        )
        (root.path / (entry.path or "")).unlink()

        recorded = load_checkpoint(marker)
        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert recorded is not None and recorded.complete, "checkpoint still claims COMPLETE"
        assert state.orphan_records == 1, "but the evidence is gone, and reconciliation says so"
        assert not state.clean

    def test_trusted_state_is_derived_from_the_manifest_not_the_checkpoint(
        self, root: EvidenceRoot
    ) -> None:
        """A partially-harvested batch reports exactly what it actually holds."""
        stored = [store(root, cik, payload=f'{{"n":{cik}}}'.encode()) for cik in range(1, 4)]
        planned = {(e.cik, e.accession) for e in stored} | {(999, "never-fetched")}

        manifest = Manifest(root.manifests / EVIDENCE_MANIFEST)
        held = {
            (e.cik, e.accession)
            for e in manifest.latest_by_url().values()
            if e.is_stored and e.cik is not None
        }

        assert len(planned & held) == 3
        assert planned - held == {(999, "never-fetched")}

    def test_an_abandoned_temp_file_is_visible_as_a_temporary(self, root: EvidenceRoot) -> None:
        """A torn atomic write leaves a temp file, never a corrupt artifact."""
        store(root, 320193)
        (root.submissions / "CIK0000320194.json.tmp").write_bytes(b"partial")

        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST))

        assert state.temporary_files == 0, "a .tmp without a leading dot is an orphan, not hidden"
        assert state.orphan_files == 1, "and it is reported rather than silently tolerated"
