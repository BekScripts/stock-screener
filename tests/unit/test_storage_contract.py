"""The storage contract every backend must honour.

These tests are written against `EvidenceStorage` and nothing else. A backend
passes them or it is not a backend. Three implementations run: the local
directory backend, the dict-backed `MemoryEvidenceStorage` from `conftest`
(which exists to prove nothing here secretly needs a filesystem), and
`B2EvidenceStorage` over the stateful `FakeS3Client` from `conftest` (which
proves the bucket backend keeps every promise the directory one does, with no
network anywhere). That is the point of having a contract: the guarantees are
stated once, in a place a new implementation cannot avoid.

Two things are deliberately absent. Nothing here reads a `Path`, because a
backend that needed one would not be storage-neutral. And nothing here appends
a manifest, because storage does not decide trust.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from pydantic import SecretStr

from stock_screener.sec_evidence import (
    ArtifactNotFoundError,
    B2Configuration,
    B2EvidenceStorage,
    LocalEvidenceStorage,
    UnsafeStorageKeyError,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from conftest import FakeS3Client, MemoryEvidenceStorage
    from stock_screener.sec_evidence import EvidenceStorage

pytestmark = pytest.mark.integration

KEY = "filings/0000320193/000032019324000123/aapl-20240928.htm"


B2_CONFIG = B2Configuration(
    endpoint_url="https://s3.us-west-004.backblazeb2.com",
    bucket="contract-test-bucket",
    key_id=SecretStr("contract-key-id"),
    application_key=SecretStr("contract-application-key"),
)


@pytest.fixture(params=["local", "memory", "b2"])
def storage(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    memory_storage: type[MemoryEvidenceStorage],
    fake_s3: type[FakeS3Client],
) -> EvidenceStorage:
    """One backend per parameter. Add the next implementation here, nowhere else."""
    factories: dict[str, Callable[[], EvidenceStorage]] = {
        "local": lambda: LocalEvidenceStorage(tmp_path),
        "memory": memory_storage,
        "b2": lambda: B2EvidenceStorage(B2_CONFIG, client=fake_s3()),
    }
    return factories[request.param]()


class TestRoundTrip:
    def test_bytes_read_back_are_the_bytes_written(self, storage: EvidenceStorage) -> None:
        payload = b"<html>cover</html>"

        storage.write_bytes(KEY, payload)

        assert storage.read_bytes(KEY) == payload

    def test_binary_content_survives_unchanged(self, storage: EvidenceStorage) -> None:
        payload = bytes(range(256)) * 41

        storage.write_bytes(KEY, payload)

        assert storage.read_bytes(KEY) == payload

    def test_an_empty_artifact_is_stored_and_read_as_empty(self, storage: EvidenceStorage) -> None:
        """Zero bytes is a real, reportable state — never an error, never skipped."""
        storage.write_bytes(KEY, b"")

        assert storage.exists(KEY)
        assert storage.read_bytes(KEY) == b""
        assert storage.stat(KEY).bytes == 0

    def test_writing_a_key_twice_leaves_the_second_payload(self, storage: EvidenceStorage) -> None:
        storage.write_bytes(KEY, b"first")
        storage.write_bytes(KEY, b"second, longer")

        assert storage.read_bytes(KEY) == b"second, longer"
        assert storage.stat(KEY).bytes == 14


class TestExistence:
    def test_a_key_does_not_exist_before_it_is_written(self, storage: EvidenceStorage) -> None:
        assert not storage.exists(KEY)

    def test_a_key_exists_after_it_is_written(self, storage: EvidenceStorage) -> None:
        storage.write_bytes(KEY, b"x")

        assert storage.exists(KEY)

    def test_reading_a_missing_key_raises(self, storage: EvidenceStorage) -> None:
        with pytest.raises(ArtifactNotFoundError, match="aapl"):
            storage.read_bytes(KEY)

    def test_stat_of_a_missing_key_raises(self, storage: EvidenceStorage) -> None:
        with pytest.raises(ArtifactNotFoundError, match="aapl"):
            storage.stat(KEY)

    def test_digest_of_a_missing_key_raises(self, storage: EvidenceStorage) -> None:
        with pytest.raises(ArtifactNotFoundError, match="aapl"):
            storage.digest(KEY)


class TestIntegrity:
    def test_stat_reports_the_exact_byte_count(self, storage: EvidenceStorage) -> None:
        storage.write_bytes(KEY, b"a" * 1234)

        assert storage.stat(KEY).bytes == 1234
        assert storage.stat(KEY).key == KEY

    def test_digest_is_the_sha256_of_the_stored_bytes(self, storage: EvidenceStorage) -> None:
        """SHA-256 is the integrity contract; nothing weaker may stand in for it."""
        payload = b"the bytes that matter"

        storage.write_bytes(KEY, payload)

        assert storage.digest(KEY) == hashlib.sha256(payload).hexdigest()

    def test_write_returns_what_it_vouches_for(self, storage: EvidenceStorage) -> None:
        payload = b"vouched"

        stored = storage.write_bytes(KEY, payload)

        assert stored.key == KEY
        assert stored.bytes == len(payload)
        assert stored.sha256 == hashlib.sha256(payload).hexdigest()


class TestListing:
    def test_listing_a_prefix_returns_keys_beneath_it(self, storage: EvidenceStorage) -> None:
        storage.write_bytes("filings/0000000001/a/x.htm", b"1")
        storage.write_bytes("filings/0000000001/b/y.htm", b"2")
        storage.write_bytes("submissions/CIK0000000001.json", b"3")

        assert list(storage.list_prefix("filings")) == [
            "filings/0000000001/a/x.htm",
            "filings/0000000001/b/y.htm",
        ]

    def test_listing_returns_keys_not_paths(self, storage: EvidenceStorage) -> None:
        """A caller must never learn where the backend keeps things."""
        storage.write_bytes(KEY, b"x")

        (listed,) = storage.list_prefix("filings")

        assert listed == KEY
        assert not listed.startswith("/")
        assert "\\" not in listed

    def test_listing_is_in_ascending_lexicographic_order(self, storage: EvidenceStorage) -> None:
        for name in ("c.htm", "a.htm", "b.htm"):
            storage.write_bytes(f"filings/0000000001/x/{name}", b"x")

        listed = list(storage.list_prefix("filings"))

        assert listed == sorted(listed)

    def test_listing_an_empty_prefix_enumerates_everything(self, storage: EvidenceStorage) -> None:
        storage.write_bytes("filings/0000000001/x/a.htm", b"x")
        storage.write_bytes("submissions/CIK0000000001.json", b"y")

        assert len(list(storage.list_prefix(""))) == 2

    def test_listing_a_prefix_with_nothing_under_it_is_empty(
        self, storage: EvidenceStorage
    ) -> None:
        assert list(storage.list_prefix("fulltext")) == []

    def test_a_trailing_separator_on_the_prefix_is_accepted(self, storage: EvidenceStorage) -> None:
        storage.write_bytes(KEY, b"x")

        assert list(storage.list_prefix("filings/")) == [KEY]

    def test_empty_storage_lists_nothing_under_any_prefix(self, storage: EvidenceStorage) -> None:
        assert list(storage.list_prefix("")) == []
        assert list(storage.list_prefix("filings")) == []
        assert list(storage.list_prefix("filings/0000000001/x")) == []

    def test_a_single_key_is_listed_under_every_prefix_above_it(
        self, storage: EvidenceStorage
    ) -> None:
        storage.write_bytes(KEY, b"x")

        assert list(storage.list_prefix("")) == [KEY]
        assert list(storage.list_prefix("filings")) == [KEY]
        assert list(storage.list_prefix("filings/0000320193")) == [KEY]
        assert list(storage.list_prefix("filings/0000320193/000032019324000123")) == [KEY]

    def test_listing_is_recursive_through_every_nesting_level(
        self, storage: EvidenceStorage
    ) -> None:
        """A prefix names a subtree, not one level of it — as object storage does."""
        storage.write_bytes("filings/a/one.htm", b"1")
        storage.write_bytes("filings/a/b/two.htm", b"2")
        storage.write_bytes("filings/a/b/c/three.htm", b"3")

        assert list(storage.list_prefix("filings")) == [
            "filings/a/b/c/three.htm",
            "filings/a/b/two.htm",
            "filings/a/one.htm",
        ]
        assert list(storage.list_prefix("filings/a/b")) == [
            "filings/a/b/c/three.htm",
            "filings/a/b/two.htm",
        ]

    def test_a_prefix_matches_on_a_segment_boundary_not_a_shared_spelling(
        self, storage: EvidenceStorage
    ) -> None:
        """`filings/1` must not list `filings/10/...` — a CIK is not a spelling."""
        storage.write_bytes("filings/1/a/x.htm", b"1")
        storage.write_bytes("filings/10/a/y.htm", b"10")
        storage.write_bytes("filings/100/a/z.htm", b"100")

        assert list(storage.list_prefix("filings/1")) == ["filings/1/a/x.htm"]
        assert list(storage.list_prefix("filings/10")) == ["filings/10/a/y.htm"]

    def test_an_exact_prefix_lists_only_its_own_subtree(self, storage: EvidenceStorage) -> None:
        storage.write_bytes("filings/0000000001/aaa/x.htm", b"1")
        storage.write_bytes("filings/0000000001/aab/y.htm", b"2")
        storage.write_bytes("filings/0000000002/aaa/z.htm", b"3")

        assert list(storage.list_prefix("filings/0000000001/aaa")) == [
            "filings/0000000001/aaa/x.htm"
        ]

    def test_a_key_equal_to_the_prefix_is_not_beneath_it(self, storage: EvidenceStorage) -> None:
        """A prefix denotes a container; the artifact with that name is not inside itself."""
        storage.write_bytes("filings/0000000001/x/doc.htm", b"x")

        assert list(storage.list_prefix("filings/0000000001/x/doc.htm")) == []

    def test_a_nonexistent_prefix_beside_real_keys_is_empty(self, storage: EvidenceStorage) -> None:
        storage.write_bytes("filings/0000000001/x/doc.htm", b"x")

        assert list(storage.list_prefix("filings/0000000009")) == []
        assert list(storage.list_prefix("submissions")) == []

    def test_listing_never_repeats_a_key(self, storage: EvidenceStorage) -> None:
        for cik in range(1, 8):
            storage.write_bytes(f"filings/{cik:010d}/x/doc.htm", b"x")
        storage.write_bytes("filings/0000000003/x/doc.htm", b"rewritten")

        listed = list(storage.list_prefix("filings"))

        assert len(listed) == len(set(listed)) == 7

    def test_two_listings_of_the_same_state_are_identical(self, storage: EvidenceStorage) -> None:
        for name in ("z.htm", "m.htm", "a.htm"):
            storage.write_bytes(f"filings/0000000001/x/{name}", b"x")

        assert list(storage.list_prefix("filings")) == list(storage.list_prefix("filings"))
        assert list(storage.list_prefix("")) == list(storage.list_prefix(""))

    def test_listing_shows_exactly_what_was_written_and_nothing_internal(
        self, storage: EvidenceStorage
    ) -> None:
        """Whatever a backend keeps for itself — temporaries, markers — is invisible."""
        written = {
            "filings/0000000001/x/a.htm",
            "filings/0000000001/x/b.htm",
            "submissions/CIK0000000001.json",
            "fulltext/q.json",
        }
        for key in written:
            storage.write_bytes(key, b"x")

        assert set(storage.list_prefix("")) == written

    @pytest.mark.parametrize(
        "unsafe", ["../x", "/abs", "a\\b", "filings//x", "filings/./x", "/", "filings//"]
    )
    def test_an_unsafe_prefix_is_rejected(self, storage: EvidenceStorage, unsafe: str) -> None:
        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|absolute|backslash|empty"):
            list(storage.list_prefix(unsafe))


class TestKeysAreRefusedNotRepaired:
    """A key that could leave the namespace is rejected by every operation."""

    @pytest.mark.parametrize(
        "unsafe",
        [
            "../etc/passwd",
            "filings/../../etc/passwd",
            "/etc/passwd",
            "C:\\Windows\\system32",
            "filings\\0000000001\\x.htm",
            "filings//0000000001/x.htm",
            "filings/./x.htm",
            "filings/0000000001/",
            "",
            "   ",
            "filings/\x00/x.htm",
            "filings/.hidden.htm",
            "filings/._sidecar.htm",
            "filings/.internal/doc.htm",
        ],
    )
    def test_an_unsafe_key_is_rejected_on_write(
        self, storage: EvidenceStorage, unsafe: str
    ) -> None:
        with pytest.raises(
            UnsafeStorageKeyError, match=r"dot segment|absolute|backslash|empty|NUL|reserved hidden"
        ):
            storage.write_bytes(unsafe, b"x")

    @pytest.mark.parametrize("unsafe", ["../x", "/abs", "a//b", "a/./b"])
    def test_an_unsafe_key_is_rejected_on_read(self, storage: EvidenceStorage, unsafe: str) -> None:
        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|absolute|empty"):
            storage.read_bytes(unsafe)

    @pytest.mark.parametrize("unsafe", ["../x", "/abs"])
    def test_an_unsafe_key_is_rejected_on_exists_stat_and_digest(
        self, storage: EvidenceStorage, unsafe: str
    ) -> None:
        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|absolute"):
            storage.exists(unsafe)
        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|absolute"):
            storage.stat(unsafe)
        with pytest.raises(UnsafeStorageKeyError, match=r"dot segment|absolute"):
            storage.digest(unsafe)

    def test_a_deeply_nested_canonical_key_is_accepted(self, storage: EvidenceStorage) -> None:
        deep = "a/b/c/d/e/f/g/h.htm"

        storage.write_bytes(deep, b"x")

        assert storage.exists(deep)

    def test_rejection_happens_before_anything_is_stored(self, storage: EvidenceStorage) -> None:
        """Refusing after a partial write would be worse than not refusing."""
        with pytest.raises(UnsafeStorageKeyError, match="dot segment"):
            storage.write_bytes("../escape.htm", b"x")

        assert list(storage.list_prefix("")) == []
