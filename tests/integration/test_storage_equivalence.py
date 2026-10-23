"""Proof that `EvidenceStorage` preserves the ordinary evidence lifecycle.

Phase B2.2 put a storage contract between the harvester and the disk. The
whole value of that change is that it is invisible: the same target must land
under the same key, hash to the same digest, produce the same manifest record,
be found as a cache hit on replay, parse to the same rows, and reconcile
clean — exactly as it did against the pre-B2 tag.

B2.4 intentionally tightens unsafe-key and storage-failure handling; those
safety corrections are tested separately in `test_storage_neutral`.

This is one test file rather than assertions scattered through the others so
that the equivalence claim is stated in one place and can be pointed at.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from api_clients.sec_documents import FetchedDocument
from stock_screener.config import Settings
from stock_screener.sec_evidence import (
    EVIDENCE_MANIFEST,
    Budget,
    LocalEvidenceStorage,
    Manifest,
    Outcome,
    RequestKind,
    Target,
    extract_filing,
    filing_key,
    harvest,
    open_evidence_root,
    reconcile,
)

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence import EvidenceRoot

pytestmark = pytest.mark.integration

COVER = (
    b'<html><body><ix:nonNumeric contextRef="c1" name="dei:Security12bTitle">Common Stock'
    b'</ix:nonNumeric><ix:nonNumeric contextRef="c1" name="dei:TradingSymbol">AAPL'
    b'</ix:nonNumeric><ix:nonNumeric contextRef="c1" name="dei:SecurityExchangeName">'
    b"The Nasdaq Stock Market LLC</ix:nonNumeric></body></html>"
)

TARGET = Target(
    url="https://www.sec.gov/Archives/edgar/data/320193/000032019324000123/aapl-20240928.htm",
    kind=RequestKind.FILING,
    cik=320193,
    accession="0000320193-24-000123",
    document="aapl-20240928.htm",
    form="10-K",
    filing_date="2024-11-01",
)


class Source:
    """A SEC client that serves one fixed document and counts its calls."""

    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.calls = 0

    def fetch(self, url: str) -> FetchedDocument:
        self.calls += 1
        return FetchedDocument(
            url=url,
            status=200,
            body=self.payload,
            sha256=hashlib.sha256(self.payload).hexdigest(),
            content_type="text/html",
            etag='"opaque-not-sha256"',
            last_modified=None,
            retrieved_at=datetime(2026, 9, 14, tzinfo=UTC),
        )

    def close(self) -> None:
        return None


@pytest.fixture
def root(tmp_path: Path) -> EvidenceRoot:
    base = tmp_path / "sec"
    base.mkdir()
    return open_evidence_root(Settings(sec_evidence_dir=str(base)))


class TestTheLifecycleIsUnchanged:
    def test_harvest_store_manifest_replay_parse_reconcile(self, root: EvidenceRoot) -> None:
        source = Source(COVER)
        budget = Budget(max_requests=10, max_bytes=10**6)

        # 1. harvest: one download, one manifest record
        first = harvest([TARGET], root, source, budget=budget)
        assert first.by_outcome == {"DOWNLOADED": 1}
        assert source.calls == 1

        # 2. the artifact landed under the storage-independent key
        key = filing_key(320193, "0000320193-24-000123", "aapl-20240928.htm")
        assert key == "filings/0000320193/000032019324000123/aapl-20240928.htm"
        assert root.storage.exists(key)
        assert root.storage.digest(key) == hashlib.sha256(COVER).hexdigest()

        # 3. the manifest record is the pre-B2 shape: relative key, SHA, bytes
        (entry,) = Manifest(root.manifests / EVIDENCE_MANIFEST).entries()
        assert entry.path == key
        assert entry.sha256 == hashlib.sha256(COVER).hexdigest()
        assert entry.etag == '"opaque-not-sha256"'
        assert entry.sha256 != entry.etag
        assert entry.bytes == len(COVER)
        assert entry.outcome is Outcome.DOWNLOADED
        assert entry.cik == 320193
        assert entry.accession == "0000320193-24-000123"

        # 4. replay is a cache hit with no request
        second = harvest([TARGET], root, source, budget=Budget(max_requests=10, max_bytes=10**6))
        assert second.by_outcome == {"CACHE_HIT": 1}
        assert source.calls == 1, "a trusted artifact is never re-fetched"

        # 5. the parser reads through storage and yields the same rows
        payload = root.storage.read_bytes(entry.path or "")
        filing, rows = extract_filing(entry, payload.decode())
        assert filing.parsed
        assert [r.normalized_symbol for r in rows] == ["AAPL"]
        assert rows[0].security_class == "COMMON_STOCK"

        # 6. reconciliation, digests included, is clean
        state = reconcile(root, Manifest(root.manifests / EVIDENCE_MANIFEST), verify_digests=True)
        assert state.clean
        assert state.trusted == 1
        assert state.files_on_disk == 1

    def test_a_tampered_artifact_is_still_refetched_on_replay(self, root: EvidenceRoot) -> None:
        """The cache-hit rule — bytes must still hash — survives the abstraction."""
        source = Source(COVER)
        harvest([TARGET], root, source, budget=Budget(max_requests=10, max_bytes=10**6))
        key = filing_key(320193, "0000320193-24-000123", "aapl-20240928.htm")
        (root.path / key).write_bytes(b"tampered")

        replay = harvest([TARGET], root, source, budget=Budget(max_requests=10, max_bytes=10**6))

        assert replay.by_outcome == {"INVALID_CACHE": 1}
        assert source.calls == 2
        assert root.storage.digest(key) == hashlib.sha256(COVER).hexdigest(), "repaired"

    def test_a_missing_artifact_is_still_refetched_on_replay(self, root: EvidenceRoot) -> None:
        source = Source(COVER)
        harvest([TARGET], root, source, budget=Budget(max_requests=10, max_bytes=10**6))
        key = filing_key(320193, "0000320193-24-000123", "aapl-20240928.htm")
        (root.path / key).unlink()

        replay = harvest([TARGET], root, source, budget=Budget(max_requests=10, max_bytes=10**6))

        assert replay.by_outcome == {"INVALID_CACHE": 1}
        assert root.storage.exists(key)


class TestManifestSerialisationIsByteCompatible:
    def test_the_record_carries_no_backend_metadata(self, root: EvidenceRoot) -> None:
        """No `backend=`, no `bucket=`, no absolute path — the record is storage-neutral."""
        harvest([TARGET], root, Source(COVER), budget=Budget(max_requests=10, max_bytes=10**6))

        line = (root.manifests / EVIDENCE_MANIFEST).read_text().splitlines()[0]
        record = json.loads(line)

        assert set(record) == set(
            asdict(Manifest(root.manifests / EVIDENCE_MANIFEST).entries().__next__())
        )
        assert "backend" not in record
        assert "bucket" not in record
        assert not record["path"].startswith("/")
        assert str(root.path) not in line

    def test_the_key_written_equals_the_key_the_old_code_would_have_written(
        self, root: EvidenceRoot
    ) -> None:
        """Pre-B2 wrote `str(destination.relative_to(root))`. Same string, by construction."""
        harvest([TARGET], root, Source(COVER), budget=Budget(max_requests=10, max_bytes=10**6))

        (entry,) = Manifest(root.manifests / EVIDENCE_MANIFEST).entries()
        old_style = str(
            root.filing_path(320193, "0000320193-24-000123", "aapl-20240928.htm").relative_to(
                root.path
            )
        )

        assert entry.path == old_style


class TestStorageDoesNotDecideTrust:
    def test_a_write_alone_creates_no_manifest_entry(self, root: EvidenceRoot) -> None:
        """Provenance is the harvester's job; the backend must never append."""
        storage = LocalEvidenceStorage(root.path)

        storage.write_bytes("filings/0000000001/x/a.htm", b"bytes without provenance")

        manifest = root.manifests / EVIDENCE_MANIFEST
        assert not manifest.exists() or manifest.read_text() == ""
        state = reconcile(root, Manifest(manifest))
        assert state.orphan_files == 1, "unvouched bytes are reported, not trusted"
