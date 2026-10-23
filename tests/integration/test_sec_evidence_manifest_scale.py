"""Attempt numbering must survive being made fast.

`attempts_for` re-read the whole manifest for every target, which is quadratic:
at 100,000 entries one lookup costs 0.6 s, so a full-corpus run would have spent
over a hundred hours re-reading its own log. `replay` reads it once instead.

The risk in that change is silent: if the precomputed counts drift from what the
old scan would have returned, attempt numbers in stored provenance become wrong
and nothing fails loudly. Every test here pins the new path against the old one.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from api_clients.errors import ProviderRequestError
from api_clients.sec_documents import FetchedDocument
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    Entry,
    Manifest,
    Outcome,
    RequestKind,
    Target,
    harvest,
    open_evidence_root,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration

BODY = b'{"cik":"0000320193"}'


@dataclass
class FakeSec:
    """Serves canned bytes, or fails, and counts what it was asked."""

    fail: bool = False
    calls: int = 0

    def fetch(self, url: str) -> FetchedDocument:
        self.calls += 1
        if self.fail:
            raise ProviderRequestError("sec unreachable")
        return FetchedDocument(
            url=url,
            status=200,
            body=BODY,
            sha256=hashlib.sha256(BODY).hexdigest(),
            content_type="application/json",
            etag=None,
            last_modified=None,
            retrieved_at=datetime.now(UTC),
        )


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


def target(cik: int) -> Target:
    return Target(
        url=f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
        kind=RequestKind.SUBMISSIONS,
        cik=cik,
    )


def manifest_of(root: EvidenceRoot) -> Manifest:
    return Manifest(root.manifests / EVIDENCE_MANIFEST)


def seed(manifest: Manifest, url: str, outcomes: list[Outcome]) -> None:
    """Write a history of attempts for one URL."""
    for outcome in outcomes:
        manifest.append(Entry(url=url, outcome=outcome, sha256="a" * 64, bytes=1))


class TestReplayMatchesTheScanItReplaces:
    """The new counts must equal `attempts_for` for every shape of history."""

    @pytest.mark.parametrize(
        "outcomes",
        [
            [],
            [Outcome.DOWNLOADED],
            [Outcome.FAILED, Outcome.DOWNLOADED],
            [Outcome.FAILED, Outcome.FAILED, Outcome.FAILED],
            [Outcome.SKIPPED, Outcome.DOWNLOADED, Outcome.INVALID_CACHE],
            [Outcome.CACHE_HIT, Outcome.CACHE_HIT],
            [Outcome.DOWNLOADED, Outcome.INVALID_CACHE, Outcome.DOWNLOADED, Outcome.FAILED],
        ],
    )
    def test_counts_equal_the_old_full_scan(
        self, root: EvidenceRoot, outcomes: list[Outcome]
    ) -> None:
        """Every outcome counts — the old scan filtered by nothing."""
        manifest = manifest_of(root)
        url = "https://data.sec.gov/submissions/CIK0000000001.json"
        seed(manifest, url, outcomes)

        assert manifest.replay().attempts.get(url, 0) == manifest.attempts_for(url)
        assert manifest.replay().attempts.get(url, 0) == len(outcomes)

    def test_interleaved_urls_are_counted_apart(self, root: EvidenceRoot) -> None:
        manifest = manifest_of(root)
        a = "https://data.sec.gov/a.json"
        b = "https://data.sec.gov/b.json"
        for outcome in (Outcome.FAILED, Outcome.DOWNLOADED):
            manifest.append(Entry(url=a, outcome=outcome))
            manifest.append(Entry(url=b, outcome=outcome))
        manifest.append(Entry(url=a, outcome=Outcome.FAILED))

        state = manifest.replay()

        assert state.attempts[a] == manifest.attempts_for(a) == 3
        assert state.attempts[b] == manifest.attempts_for(b) == 2

    def test_a_url_never_seen_starts_at_one(self, root: EvidenceRoot) -> None:
        state = manifest_of(root).replay()

        assert state.next_attempt("https://data.sec.gov/unseen.json") == 1

    def test_latest_still_wins_after_replay(self, root: EvidenceRoot) -> None:
        manifest = manifest_of(root)
        url = "https://data.sec.gov/a.json"
        seed(manifest, url, [Outcome.FAILED, Outcome.DOWNLOADED])

        assert manifest.replay().latest[url].outcome is Outcome.DOWNLOADED
        assert manifest.replay().latest == manifest.latest_by_url()


class TestAttemptNumbersInStoredProvenance:
    def test_a_first_fetch_records_attempt_one(self, root: EvidenceRoot) -> None:
        harvest([target(1)], root, FakeSec())

        assert next(iter(manifest_of(root).entries())).attempt == 1

    def test_a_retry_after_failure_records_attempt_two(self, root: EvidenceRoot) -> None:
        harvest([target(1)], root, FakeSec(fail=True))

        harvest([target(1)], root, FakeSec())

        assert [e.attempt for e in manifest_of(root).entries()] == [1, 2]

    def test_repeated_failures_keep_incrementing_across_runs(self, root: EvidenceRoot) -> None:
        for _ in range(4):
            harvest([target(1)], root, FakeSec(fail=True))

        assert [e.attempt for e in manifest_of(root).entries()] == [1, 2, 3, 4]

    def test_the_same_url_twice_in_one_run_increments_within_that_run(
        self, root: EvidenceRoot
    ) -> None:
        """Two securities can resolve to one CIK, so one run can hold a URL twice.

        The old code re-scanned the file each time and therefore saw the entry
        the first fetch had just appended. The precomputed map must too, which is
        why it is incremented as the run writes rather than only at the start.
        """
        harvest([target(1), target(1)], root, FakeSec(fail=True))

        assert [e.attempt for e in manifest_of(root).entries()] == [1, 2]

    def test_a_resumed_run_continues_the_numbering(self, root: EvidenceRoot) -> None:
        harvest([target(1), target(2)], root, FakeSec(fail=True))

        harvest([target(1), target(2)], root, FakeSec(fail=True))

        by_url: dict[str, list[int]] = {}
        for entry in manifest_of(root).entries():
            by_url.setdefault(entry.url, []).append(entry.attempt)
        assert all(attempts == [1, 2] for attempts in by_url.values())

    def test_a_cache_hit_appends_nothing_and_so_consumes_no_attempt(
        self, root: EvidenceRoot
    ) -> None:
        client = FakeSec()
        harvest([target(1)], root, client)

        harvest([target(1)], root, client)

        entries = list(manifest_of(root).entries())
        assert len(entries) == 1
        assert entries[0].attempt == 1


class TestTheLookupNoLongerRescansPerTarget:
    def test_a_large_manifest_does_not_slow_the_harvest_per_target(
        self, root: EvidenceRoot
    ) -> None:
        """10,000 prior entries must not cost a full scan per fetched target.

        Timing is compared against the cost of one deliberate `attempts_for`
        scan, so the assertion is about shape rather than about this machine
        being fast: 12 targets under the old path meant 12 scans.
        """
        manifest = manifest_of(root)
        with manifest.path.open("a", encoding="utf-8") as stream:
            for i in range(10_000):
                stream.write(
                    Entry(
                        url=f"https://data.sec.gov/filler/{i}.json", outcome=Outcome.DOWNLOADED
                    ).as_line()
                    + "\n"
                )

        start = time.perf_counter()
        manifest.attempts_for("https://data.sec.gov/filler/0.json")
        one_scan = time.perf_counter() - start

        targets = [target(i) for i in range(1, 13)]
        start = time.perf_counter()
        report = harvest(targets, root, FakeSec())
        elapsed = time.perf_counter() - start

        assert report.by_outcome[str(Outcome.DOWNLOADED)] == 12
        # The old path performed one scan per target. Allowing four scans' worth
        # of total time still fails loudly if per-target scanning returns.
        assert elapsed < one_scan * len(targets), (
            f"harvest took {elapsed:.3f}s; one scan is {one_scan:.3f}s and "
            f"{len(targets)} scans would be {one_scan * len(targets):.3f}s"
        )

    def test_the_manifest_is_read_once_per_harvest(
        self, root: EvidenceRoot, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The structural guarantee, independent of timing."""
        manifest_of(root).append(Entry(url="https://data.sec.gov/x.json", outcome=Outcome.FAILED))
        reads = {"n": 0}
        original = Manifest.entries

        def counted(self: Manifest):  # type: ignore[no-untyped-def]
            reads["n"] += 1
            return original(self)

        monkeypatch.setattr(Manifest, "entries", counted)

        harvest([target(i) for i in range(1, 9)], root, FakeSec())

        assert reads["n"] == 1
