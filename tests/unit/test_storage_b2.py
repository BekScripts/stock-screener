"""What is true of the Backblaze backend and of no other.

The contract tests say what every backend must do; `B2EvidenceStorage` passes
them beside the local and memory backends. These say how this one does it,
where that matters: which S3 call answers which contract operation, what a
404 means against what a 403 means, that an upload is not trusted until the
bucket has been asked what it holds, that SHA-256 comes from bytes and never
from an ETag or a metadata claim, that listing is paginated, sorted and
segment-aware regardless of what the server returns, and that an unsafe key
or a credential never reaches the wire.

Two clients stand in for Backblaze. `FakeS3Client` from `conftest` is a
stateful bucket in a dict — the semantics live there. `botocore.stub.Stubber`
wraps a real client for the handful of tests about what goes *on the wire*:
the exact request parameters, and the real SDK's own 404. Neither makes a
network request; there is no bucket, no account and no credential anywhere in
this file that is not a sentinel.
"""

from __future__ import annotations

import hashlib
import io
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber
from pydantic import SecretStr

from stock_screener.sec_evidence import (
    ArtifactNotFoundError,
    B2Configuration,
    B2EvidenceStorage,
    EvidenceWriteError,
    StorageUnavailableError,
    UnsafeStorageKeyError,
)
from stock_screener.sec_evidence.b2 import (
    CONNECT_TIMEOUT_SECONDS,
    MAX_RETRIES,
    READ_TIMEOUT_SECONDS,
    SHA256_METADATA,
    SINGLE_PUT_LIMIT,
    build_b2_client,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from conftest import FakeS3Client

pytestmark = pytest.mark.unit

KEY = "filings/0000320193/000032019324000123/aapl-20240928.htm"
ENDPOINT = "https://s3.us-west-004.backblazeb2.com"
BUCKET = "compounder-radar-evidence"
KEY_ID = "0041234567890abcdef0000000001"
APPLICATION_KEY = "K004sentinelApplicationKeyThatMustNeverAppearAnywhere"

CONFIG = B2Configuration(
    endpoint_url=ENDPOINT,
    bucket=BUCKET,
    key_id=SecretStr(KEY_ID),
    application_key=SecretStr(APPLICATION_KEY),
)

UNSAFE_KEYS = ["../etc/passwd", "/abs", "filings//x", "filings/./x", "filings\\x", ""]


def backend(client: FakeS3Client) -> B2EvidenceStorage:
    return B2EvidenceStorage(CONFIG, client=client)


def s3_error(code: str, status: int, operation: str) -> ClientError:
    """A `ClientError` shaped the way botocore raises one for an S3 response."""
    response: dict[str, Any] = {
        "Error": {"Code": code, "Message": code},
        "ResponseMetadata": {"HTTPStatusCode": status},
    }
    return ClientError(response, operation)  # type: ignore[arg-type]  # stubs want every metadata key


def forbidden(operation: str) -> ClientError:
    return s3_error("AccessDenied", 403, operation)


def server_error(operation: str) -> ClientError:
    return s3_error("InternalError", 500, operation)


def throttled(operation: str) -> ClientError:
    return s3_error("SlowDown", 429, operation)


class TestExists:
    def test_head_200_is_true(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        client.objects[KEY] = (b"x", {})

        assert backend(client).exists(KEY)
        assert client.calls == [("head_object", KEY)]

    def test_head_404_is_false(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()

        assert not backend(client).exists(KEY)
        assert client.calls == [("head_object", KEY)]

    @pytest.mark.parametrize(
        ("fault", "name"),
        [
            (forbidden("HeadObject"), "AccessDenied"),
            (server_error("HeadObject"), "InternalError"),
            (throttled("HeadObject"), "SlowDown"),
            (EndpointConnectionError(endpoint_url=ENDPOINT), "EndpointConnectionError"),
            (ConnectTimeoutError(endpoint_url=ENDPOINT), "ConnectTimeoutError"),
        ],
    )
    def test_uncertainty_raises_rather_than_answering_false(
        self, fake_s3: type[FakeS3Client], fault: BaseException, name: str
    ) -> None:
        """403 is not "absent". Neither is a dead endpoint."""
        client = fake_s3(failing={"head_object": fault})
        client.objects[KEY] = (b"x", {})

        with pytest.raises(StorageUnavailableError, match=name):
            backend(client).exists(KEY)

    @pytest.mark.parametrize("unsafe", UNSAFE_KEYS)
    def test_an_unsafe_key_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            backend(client).exists(unsafe)

        assert client.calls == []


class TestStat:
    def test_head_200_reports_the_exact_length(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        client.objects[KEY] = (b"a" * 1234, {})

        found = backend(client).stat(KEY)

        assert found.key == KEY
        assert found.bytes == 1234
        assert client.calls == [("head_object", KEY)]

    def test_head_404_is_not_found(self, fake_s3: type[FakeS3Client]) -> None:
        with pytest.raises(ArtifactNotFoundError, match="aapl"):
            backend(fake_s3()).stat(KEY)

    @pytest.mark.parametrize(
        "fault", [forbidden("HeadObject"), server_error("HeadObject")], ids=["403", "500"]
    )
    def test_a_fault_is_unavailable(
        self, fake_s3: type[FakeS3Client], fault: BaseException
    ) -> None:
        client = fake_s3(failing={"head_object": fault})

        with pytest.raises(StorageUnavailableError):
            backend(client).stat(KEY)

    @pytest.mark.parametrize("unsafe", UNSAFE_KEYS)
    def test_an_unsafe_key_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            backend(client).stat(unsafe)

        assert client.calls == []


class TestReadBytes:
    @pytest.mark.parametrize(
        "payload",
        [b"<html>cover</html>", b"", bytes(range(256)) * 41],
        ids=["text", "empty", "binary"],
    )
    def test_get_returns_the_exact_body(self, fake_s3: type[FakeS3Client], payload: bytes) -> None:
        client = fake_s3()
        client.objects[KEY] = (payload, {})

        assert backend(client).read_bytes(KEY) == payload
        assert client.calls == [("get_object", KEY)]

    def test_get_nosuchkey_is_not_found(self, fake_s3: type[FakeS3Client]) -> None:
        with pytest.raises(ArtifactNotFoundError, match="aapl"):
            backend(fake_s3()).read_bytes(KEY)

    @pytest.mark.parametrize(
        ("fault", "name"),
        [
            (forbidden("GetObject"), "AccessDenied"),
            (server_error("GetObject"), "InternalError"),
            (ReadTimeoutError(endpoint_url=ENDPOINT), "ReadTimeoutError"),
        ],
    )
    def test_a_fault_is_unavailable_not_missing(
        self, fake_s3: type[FakeS3Client], fault: BaseException, name: str
    ) -> None:
        client = fake_s3(failing={"get_object": fault})
        client.objects[KEY] = (b"x", {})

        with pytest.raises(StorageUnavailableError, match=name):
            backend(client).read_bytes(KEY)

    def test_a_body_that_ends_early_is_unavailable(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The SDK's IncompleteReadError is a BotoCoreError; it must not be a short read."""
        client = fake_s3()
        client.objects[KEY] = (b"the whole document", {})
        original = client.get_object

        def truncated(**kwargs: object) -> dict[str, object]:
            response = original(**kwargs)  # type: ignore[arg-type]
            response["Body"] = StreamingBody(io.BytesIO(b"the whole"), len(b"the whole document"))
            return response

        monkeypatch.setattr(client, "get_object", truncated)

        with pytest.raises(StorageUnavailableError, match="IncompleteReadError"):
            backend(client).read_bytes(KEY)

    def test_the_body_is_closed_after_reading(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        client.objects[KEY] = (b"x", {})
        closed: list[bool] = []
        original = client.get_object

        def tracking(**kwargs: object) -> dict[str, object]:
            response = original(**kwargs)  # type: ignore[arg-type]
            body = response["Body"]
            real_close = body.close

            def close() -> None:
                closed.append(True)
                real_close()

            body.close = close
            return response

        monkeypatch.setattr(client, "get_object", tracking)

        backend(client).read_bytes(KEY)

        assert closed == [True]

    @pytest.mark.parametrize("unsafe", UNSAFE_KEYS)
    def test_an_unsafe_key_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            backend(client).read_bytes(unsafe)

        assert client.calls == []


class TestDigest:
    @pytest.mark.parametrize(
        "payload",
        [b"the bytes that matter", b"", bytes(range(256)) * 4097],
        ids=["text", "empty", "binary"],
    )
    def test_the_digest_is_sha256_of_the_body(
        self, fake_s3: type[FakeS3Client], payload: bytes
    ) -> None:
        client = fake_s3()
        client.objects[KEY] = (payload, {})

        assert backend(client).digest(KEY) == hashlib.sha256(payload).hexdigest()
        assert client.calls == [("get_object", KEY)], "a GET, never a HEAD for metadata"

    def test_the_digest_comes_from_the_bytes_not_the_metadata_claim(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        """Metadata says one thing, the object holds another: the object wins."""
        client = fake_s3()
        client.objects[KEY] = (b"what is actually stored", {SHA256_METADATA: "a" * 64})

        assert backend(client).digest(KEY) == hashlib.sha256(b"what is actually stored").hexdigest()

    def test_same_length_different_content_digests_differently(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()
        client.objects["filings/0000000001/x/a.htm"] = (b"AAAAAAAA", {})
        client.objects["filings/0000000001/x/b.htm"] = (b"BBBBBBBB", {})
        storage = backend(client)

        assert storage.digest("filings/0000000001/x/a.htm") != storage.digest(
            "filings/0000000001/x/b.htm"
        )

    def test_nosuchkey_is_not_found(self, fake_s3: type[FakeS3Client]) -> None:
        with pytest.raises(ArtifactNotFoundError):
            backend(fake_s3()).digest(KEY)

    @pytest.mark.parametrize(
        "fault",
        [forbidden("GetObject"), ReadTimeoutError(endpoint_url=ENDPOINT)],
        ids=["403", "read-timeout"],
    )
    def test_a_fault_is_unavailable(
        self, fake_s3: type[FakeS3Client], fault: BaseException
    ) -> None:
        client = fake_s3(failing={"get_object": fault})
        client.objects[KEY] = (b"x", {})

        with pytest.raises(StorageUnavailableError):
            backend(client).digest(KEY)

    @pytest.mark.parametrize("unsafe", UNSAFE_KEYS)
    def test_an_unsafe_key_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            backend(client).digest(unsafe)

        assert client.calls == []


class TestWriteBytes:
    @pytest.mark.parametrize(
        "payload",
        [b"<html>cover</html>", b"", bytes(range(256)) * 41],
        ids=["text", "empty", "binary"],
    )
    def test_put_then_head_then_receipt(self, fake_s3: type[FakeS3Client], payload: bytes) -> None:
        client = fake_s3()

        stored = backend(client).write_bytes(KEY, payload)

        assert client.calls == [("put_object", KEY), ("head_object", KEY)]
        assert stored.key == KEY
        assert stored.bytes == len(payload)
        assert stored.sha256 == hashlib.sha256(payload).hexdigest()
        data, metadata = client.objects[KEY]
        assert data == payload
        assert metadata == {SHA256_METADATA: stored.sha256}

    def test_the_object_key_is_the_logical_key_with_nothing_in_front(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()

        backend(client).write_bytes(KEY, b"x")

        assert list(client.objects) == [KEY]
        assert client.buckets_seen == {BUCKET}

    def test_overwriting_leaves_the_new_bytes_and_the_new_digest(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()
        storage = backend(client)
        storage.write_bytes(KEY, b"first")

        stored = storage.write_bytes(KEY, b"second, longer")

        assert client.objects[KEY][0] == b"second, longer"
        assert stored.sha256 == hashlib.sha256(b"second, longer").hexdigest()
        assert client.objects[KEY][1][SHA256_METADATA] == stored.sha256

    def test_the_receipt_digest_is_never_the_etag(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()

        stored = backend(client).write_bytes(KEY, b"payload")

        assert stored.sha256 == hashlib.sha256(b"payload").hexdigest()
        assert "not-a-sha" not in stored.sha256

    def test_a_remote_length_mismatch_is_a_write_failure(self, fake_s3: type[FakeS3Client]) -> None:
        """The PUT succeeded; the bucket holds fewer bytes. No receipt."""
        client = fake_s3()
        payload = b"the complete document"
        client.tamper_after_put[KEY] = (
            b"the comp",
            {SHA256_METADATA: hashlib.sha256(payload).hexdigest()},
        )

        with pytest.raises(EvidenceWriteError, match="did not verify"):
            backend(client).write_bytes(KEY, payload)

    def test_a_remote_metadata_mismatch_is_a_write_failure(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()
        payload = b"the complete document"
        client.tamper_after_put[KEY] = (payload, {SHA256_METADATA: "f" * 64})

        with pytest.raises(EvidenceWriteError, match="digest differs"):
            backend(client).write_bytes(KEY, payload)

    def test_missing_remote_metadata_is_a_write_failure(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        client.tamper_after_put[KEY] = (b"payload", {})

        with pytest.raises(EvidenceWriteError, match="digest differs"):
            backend(client).write_bytes(KEY, b"payload")

    @pytest.mark.parametrize(
        ("fault", "name"),
        [
            (forbidden("PutObject"), "AccessDenied"),
            (server_error("PutObject"), "InternalError"),
            (EndpointConnectionError(endpoint_url=ENDPOINT), "EndpointConnectionError"),
        ],
    )
    def test_a_failed_put_is_a_write_failure_and_nothing_is_verified(
        self, fake_s3: type[FakeS3Client], fault: BaseException, name: str
    ) -> None:
        client = fake_s3(failing={"put_object": fault})

        with pytest.raises(EvidenceWriteError, match=name):
            backend(client).write_bytes(KEY, b"x")

        assert client.calls == [("put_object", KEY)]
        assert KEY not in client.objects

    @pytest.mark.parametrize(
        "fault",
        [
            forbidden("HeadObject"),
            server_error("HeadObject"),
            ReadTimeoutError(endpoint_url=ENDPOINT),
        ],
        ids=["403", "500", "timeout"],
    )
    def test_a_failed_verification_head_is_a_write_failure(
        self, fake_s3: type[FakeS3Client], fault: BaseException
    ) -> None:
        """Uploaded, perhaps; not proven. No receipt."""
        client = fake_s3(failing={"head_object": fault})

        with pytest.raises(EvidenceWriteError, match="could not be verified"):
            backend(client).write_bytes(KEY, b"x")

    def test_an_object_that_vanished_before_verification_is_a_write_failure(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        original = client.put_object

        def put_then_lose(**kwargs: object) -> dict[str, object]:
            response = original(**kwargs)  # type: ignore[arg-type]
            client.objects.clear()
            return response

        monkeypatch.setattr(client, "put_object", put_then_lose)

        with pytest.raises(EvidenceWriteError, match="ArtifactNotFoundError"):
            backend(client).write_bytes(KEY, b"x")

    def test_a_payload_past_a_single_put_is_refused_before_any_request(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        monkeypatch.setattr("stock_screener.sec_evidence.b2.SINGLE_PUT_LIMIT", 16)

        with pytest.raises(EvidenceWriteError, match="multipart"):
            backend(client).write_bytes(KEY, b"x" * 17)

        assert client.calls == []
        assert SINGLE_PUT_LIMIT == 5 * 1024**3

    @pytest.mark.parametrize("unsafe", UNSAFE_KEYS)
    def test_an_unsafe_key_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            backend(client).write_bytes(unsafe, b"x")

        assert client.calls == []

    def test_no_operation_ever_deletes(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()
        storage = backend(client)
        storage.write_bytes(KEY, b"first")
        storage.write_bytes(KEY, b"second")
        storage.exists(KEY)
        storage.stat(KEY)
        storage.read_bytes(KEY)
        storage.digest(KEY)
        list(storage.list_prefix(""))

        assert all(operation != "delete_object" for operation, _ in client.calls)


class TestListPrefix:
    def test_an_empty_bucket_lists_nothing(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3()

        assert list(backend(client).list_prefix("filings")) == []
        assert client.calls == [("list_objects_v2", "filings/")]

    def test_the_request_asks_for_the_prefix_with_its_separator(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        """`filings/1/` on the wire, so `filings/10/...` is never even returned."""
        client = fake_s3()
        client.objects["filings/1/a/x.htm"] = (b"1", {})
        client.objects["filings/10/a/y.htm"] = (b"10", {})

        assert list(backend(client).list_prefix("filings/1")) == ["filings/1/a/x.htm"]
        assert client.calls == [("list_objects_v2", "filings/1/")]

    def test_a_server_that_ignores_the_separator_is_still_filtered(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Segment-boundary semantics are enforced here, not trusted to the server."""
        client = fake_s3()
        client.objects["filings/1/a/x.htm"] = (b"1", {})
        client.objects["filings/10/a/y.htm"] = (b"10", {})
        paginator = client.get_paginator("list_objects_v2")

        def sloppy(**kwargs: object) -> list[dict[str, object]]:
            return [
                {
                    "Contents": [{"Key": k, "Size": 1} for k in sorted(client.objects)],
                    "KeyCount": 2,
                }
            ]

        monkeypatch.setattr(paginator, "paginate", sloppy)
        monkeypatch.setattr(client, "get_paginator", lambda name: paginator)

        assert list(backend(client).list_prefix("filings/1")) == ["filings/1/a/x.htm"]

    def test_listing_spans_pages_without_duplicates(self, fake_s3: type[FakeS3Client]) -> None:
        client = fake_s3(page_size=3)
        for n in range(10):
            client.objects[f"filings/{n:010d}/acc/doc.htm"] = (b"x", {})

        listed = list(backend(client).list_prefix("filings"))

        assert len(listed) == 10
        assert len(set(listed)) == 10
        assert listed == sorted(listed)

    def test_pages_arriving_out_of_order_still_yield_sorted_output(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3(page_size=4, reverse_pages=True)
        for n in range(9):
            client.objects[f"filings/{n:010d}/acc/doc.htm"] = (b"x", {})

        listed = list(backend(client).list_prefix(""))

        assert listed == sorted(listed)
        assert len(listed) == 9

    def test_a_server_that_repeats_a_key_across_pages_yields_it_once(
        self, fake_s3: type[FakeS3Client], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = fake_s3()
        paginator = client.get_paginator("list_objects_v2")

        def repeating(**kwargs: object) -> list[dict[str, object]]:
            page = {"Contents": [{"Key": "filings/1/a/x.htm", "Size": 1}], "KeyCount": 1}
            return [page, page]

        monkeypatch.setattr(paginator, "paginate", repeating)
        monkeypatch.setattr(client, "get_paginator", lambda name: paginator)

        assert list(backend(client).list_prefix("filings")) == ["filings/1/a/x.htm"]

    def test_backend_internal_and_foreign_keys_are_not_surfaced(
        self, fake_s3: type[FakeS3Client]
    ) -> None:
        client = fake_s3()
        client.objects["filings/1/a/x.htm"] = (b"1", {})
        client.objects["filings/1/a/.x.htm.tmp"] = (b"partial", {})
        client.objects["filings/1/a//double.htm"] = (b"?", {})

        assert list(backend(client).list_prefix("filings")) == ["filings/1/a/x.htm"]

    @pytest.mark.parametrize(
        ("fault", "name"),
        [
            (forbidden("ListObjectsV2"), "AccessDenied"),
            (server_error("ListObjectsV2"), "InternalError"),
            (EndpointConnectionError(endpoint_url=ENDPOINT), "EndpointConnectionError"),
        ],
    )
    def test_a_listing_fault_raises_rather_than_listing_nothing(
        self, fake_s3: type[FakeS3Client], fault: BaseException, name: str
    ) -> None:
        client = fake_s3(failing={"list_objects_v2": fault})
        client.objects[KEY] = (b"x", {})

        with pytest.raises(StorageUnavailableError, match=name):
            list(backend(client).list_prefix("filings"))

    @pytest.mark.parametrize("unsafe", ["../x", "/", "//", "filings//x", "a\\b"])
    def test_an_unsafe_prefix_issues_no_request(
        self, fake_s3: type[FakeS3Client], unsafe: str
    ) -> None:
        client = fake_s3()

        with pytest.raises(UnsafeStorageKeyError):
            list(backend(client).list_prefix(unsafe))

        assert client.calls == []


class TestSecretsNeverReachTheWireOrTheOperator:
    def test_the_repr_names_the_bucket_and_endpoint_only(self, fake_s3: type[FakeS3Client]) -> None:
        rendered = repr(backend(fake_s3())) + str(backend(fake_s3()))

        assert BUCKET in rendered
        assert ENDPOINT in rendered
        assert APPLICATION_KEY not in rendered
        assert KEY_ID not in rendered

    @pytest.mark.parametrize(
        ("operation", "fault"),
        [
            ("head_object", forbidden("HeadObject")),
            ("get_object", server_error("GetObject")),
            ("put_object", EndpointConnectionError(endpoint_url=ENDPOINT)),
            ("list_objects_v2", forbidden("ListObjectsV2")),
        ],
    )
    def test_no_exception_message_carries_a_credential(
        self, fake_s3: type[FakeS3Client], operation: str, fault: BaseException
    ) -> None:
        client = fake_s3(failing={operation: fault})
        client.objects[KEY] = (b"x", {})
        storage = backend(client)

        with pytest.raises(StorageUnavailableError) as caught:
            if operation == "head_object":
                storage.exists(KEY)
            elif operation == "get_object":
                storage.read_bytes(KEY)
            elif operation == "put_object":
                storage.write_bytes(KEY, b"x")
            else:
                list(storage.list_prefix("filings"))

        rendered = str(caught.value) + repr(caught.value) + repr(caught.value.__cause__)
        assert APPLICATION_KEY not in rendered
        assert KEY_ID not in rendered

    def test_no_log_line_carries_a_credential(
        self, fake_s3: type[FakeS3Client], caplog: pytest.LogCaptureFixture
    ) -> None:
        client = fake_s3(failing={"head_object": forbidden("HeadObject")})
        client.tamper_after_put[KEY] = (b"", {})
        storage = backend(client)
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(StorageUnavailableError):
                storage.exists(KEY)
            with pytest.raises(EvidenceWriteError):
                storage.write_bytes(KEY, b"x")

        assert APPLICATION_KEY not in caplog.text
        assert KEY_ID not in caplog.text

    def test_the_backend_holds_no_credential_attribute(self, fake_s3: type[FakeS3Client]) -> None:
        storage = backend(fake_s3())

        for value in vars(storage).values():
            assert value is not APPLICATION_KEY
            assert not (isinstance(value, str) and APPLICATION_KEY in value)


class TestTheRealClientIsBuiltExplicitly:
    """`build_b2_client` is offline; these read what it configured, not the network."""

    def test_the_client_is_bound_to_the_endpoint_region_and_signing(self) -> None:
        client = cast("S3Client", build_b2_client(CONFIG))
        options = vars(client.meta.config)

        assert client.meta.endpoint_url == ENDPOINT
        assert client.meta.region_name == "us-west-004"
        assert options["signature_version"] == "s3v4"
        assert options["s3"] == {"addressing_style": "path"}
        # botocore counts the first request too: total attempts is retries + 1.
        assert options["retries"] == {"mode": "standard", "total_max_attempts": MAX_RETRIES + 1}
        assert options["connect_timeout"] == CONNECT_TIMEOUT_SECONDS
        assert options["read_timeout"] == READ_TIMEOUT_SECONDS
        assert options["request_checksum_calculation"] == "when_required"
        assert options["response_checksum_validation"] == "when_required"

    def test_the_credentials_are_the_configured_ones_not_ambient(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The session is built with explicit credentials; AWS_* has nothing to fill."""
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ambient-key-id")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "ambient-secret")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-central-003")
        seen: dict[str, object] = {}

        class RecordingSession:
            def __init__(self, **kwargs: object) -> None:
                seen["session"] = kwargs

            def client(self, service: str, **kwargs: object) -> object:
                seen["client"] = {"service": service, **kwargs}
                return object()

        target = "stock_screener.sec_evidence.b2.boto3.session.Session"
        monkeypatch.setattr(target, RecordingSession)

        build_b2_client(CONFIG)

        assert seen["session"] == {
            "aws_access_key_id": KEY_ID,
            "aws_secret_access_key": APPLICATION_KEY,
            "region_name": "us-west-004",
        }
        client_kwargs = seen["client"]
        assert isinstance(client_kwargs, dict)
        assert client_kwargs["service"] == "s3"
        assert client_kwargs["endpoint_url"] == ENDPOINT

    def test_an_endpoint_without_a_derivable_region_is_refused_offline(self) -> None:
        config = B2Configuration(
            endpoint_url="https://s3.example.net",
            bucket=BUCKET,
            key_id=SecretStr(KEY_ID),
            application_key=SecretStr(APPLICATION_KEY),
        )

        with pytest.raises(StorageUnavailableError, match="signing region"):
            build_b2_client(config)

    def test_a_put_carries_the_digest_as_metadata_and_the_exact_length(self) -> None:
        """What goes on the wire, checked against the real SDK's parameter model."""
        client = cast("S3Client", build_b2_client(CONFIG))
        payload = b"<html>cover</html>"
        sha256 = hashlib.sha256(payload).hexdigest()
        with Stubber(client) as stub:
            stub.add_response(
                "put_object",
                {"ETag": '"not-a-sha"'},
                {
                    "Bucket": BUCKET,
                    "Key": KEY,
                    "Body": ANY,
                    "ContentLength": len(payload),
                    "Metadata": {SHA256_METADATA: sha256},
                },
            )
            stub.add_response(
                "head_object",
                {"ContentLength": len(payload), "Metadata": {SHA256_METADATA: sha256}},
                {"Bucket": BUCKET, "Key": KEY},
            )

            stored = B2EvidenceStorage(CONFIG, client=client).write_bytes(KEY, payload)

            stub.assert_no_pending_responses()
        assert stored.sha256 == sha256

    def test_the_real_sdks_head_404_reads_as_absent(self) -> None:
        client = cast("S3Client", build_b2_client(CONFIG))
        with Stubber(client) as stub:
            stub.add_client_error("head_object", service_error_code="404", http_status_code=404)

            assert not B2EvidenceStorage(CONFIG, client=client).exists(KEY)

    def test_the_real_sdks_head_403_reads_as_unavailable(self) -> None:
        client = cast("S3Client", build_b2_client(CONFIG))
        with Stubber(client) as stub:
            stub.add_client_error("head_object", service_error_code="403", http_status_code=403)

            with pytest.raises(StorageUnavailableError, match="403"):
                B2EvidenceStorage(CONFIG, client=client).exists(KEY)

    def test_the_real_sdks_get_nosuchkey_reads_as_not_found(self) -> None:
        client = cast("S3Client", build_b2_client(CONFIG))
        with Stubber(client) as stub:
            stub.add_client_error(
                "get_object", service_error_code="NoSuchKey", http_status_code=404
            )

            with pytest.raises(ArtifactNotFoundError):
                B2EvidenceStorage(CONFIG, client=client).read_bytes(KEY)
