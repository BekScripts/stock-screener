"""The Backblaze B2 artifact backend, behind the same six-operation contract.

`B2EvidenceStorage` speaks to a B2 bucket through the S3-compatible API and
satisfies `EvidenceStorage` exactly as `LocalEvidenceStorage` does. Nothing
above it can tell the two apart, and the manifest could not either: the
object key *is* the logical artifact key — `filings/<cik>/<accession>/<doc>`
— with no bucket, backend or account prefix in front of it, so a manifest
written against a directory reads unchanged against a bucket.

## The trust model, stated once

An upload is trusted only after the bucket has been asked what it holds.
`write_bytes` computes SHA-256 over the bytes it was handed, sends them in a
single signed PUT with that digest as object metadata, then issues a HEAD and
requires the remote content length and the remote metadata digest to equal
what was sent. Only then does it return a `StoredArtifact`, and the receipt
describes the bytes that were sent, never a value the server produced. A
failed upload, a failed verification, or a mismatch of either field raises
`EvidenceWriteError` — a `StorageUnavailableError` — and the caller appends
nothing to the manifest.

Two things a bucket offers are deliberately not used as evidence:

- **ETag is not SHA-256.** It is an opaque server value (an MD5 for simple
  uploads, something else for multipart) and no code here reads it.
- **The metadata digest is not authoritative.** It is what the writer claimed
  at upload time — a verification aid that makes a corrupted or truncated
  upload detectable immediately. `digest(key)` never returns it; it downloads
  the object and hashes the bytes, so a disagreement between metadata and
  content is caught rather than trusted. Correctness outranks bandwidth in
  this phase; a verified-metadata shortcut can come later, if ever.

## Absent is not unreachable

A 404 is the one answer that means "not here". Everything else that stops an
operation — 403, a signature failure, a throttle after the SDK's retries, a
5xx, a connection or read timeout, a body that ends early — is
`StorageUnavailableError`. `exists()` returns False only for a 404 and raises
for all of that; a backend that answered "absent" to "forbidden" would make
the harvester refetch a whole corpus into a bucket it could not read.

## Credentials come from configuration and go nowhere else

The client is built from a validated `B2Configuration` with explicit
credentials; the SDK is never allowed to fall back to `~/.aws`, `AWS_*`
environment variables or an instance profile. The secrets stay wrapped, the
backend's `repr` names the bucket and endpoint and nothing more, and error
messages carry an operation, a key and a status code — never a server
message, which for a signature failure can echo the request that failed.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from stock_screener.sec_evidence.backend import (
    ArtifactNotFoundError,
    ArtifactStat,
    StoredArtifact,
    validate_key,
)
from stock_screener.sec_evidence.storage import EvidenceWriteError, StorageUnavailableError

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

    from botocore.response import StreamingBody

    from stock_screener.sec_evidence.wiring import B2Configuration

log = structlog.get_logger(__name__)

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "MAX_RETRIES",
    "READ_TIMEOUT_SECONDS",
    "SHA256_METADATA",
    "SINGLE_PUT_LIMIT",
    "B2EvidenceStorage",
    "S3Operations",
    "build_b2_client",
]

#: The object-metadata name the writer's SHA-256 is stored under. The S3 API
#: lowercases user metadata names, so it is lowercase from the start.
SHA256_METADATA = "sha256"

#: Bounded, and chosen for SEC evidence: a filing is kilobytes to tens of
#: megabytes, so a minute of silence on a read is a fault, not a large file.
CONNECT_TIMEOUT_SECONDS = 10
READ_TIMEOUT_SECONDS = 60

#: SDK retries in `standard` mode: exponential backoff with jitter on
#: throttles, 5xx and connection faults. Three retries after the first
#: attempt — four requests in all — then the error propagates as
#: `StorageUnavailableError`. No retry loop is layered on top.
MAX_RETRIES = 3

#: A single PUT carries at most 5 GiB. No SEC evidence artifact approaches it,
#: so multipart is not implemented; a payload past this is refused before any
#: request rather than handed to a code path this phase does not have.
SINGLE_PUT_LIMIT = 5 * 1024**3

#: Response codes that mean "the object is not there" and nothing else. HEAD
#: has no body, so the SDK reports its 404 as the bare status; GET reports
#: `NoSuchKey`. Everything outside this set is uncertainty.
_NOT_FOUND = frozenset({"404", "NoSuchKey", "NotFound"})


class _ListPaginator(Protocol):
    def paginate(self, *, Bucket: str, Prefix: str) -> Iterable[Mapping[str, Any]]:  # noqa: N803 - SDK spelling
        """Pages of a `ListObjectsV2`, each with `Contents` when non-empty."""
        ...


class S3Operations(Protocol):
    """The four calls this backend makes, and the only surface it needs.

    Named so that a test can hand in a stateful fake and mypy can hold the
    backend to exactly this: `head_object`, `get_object`, `put_object` and
    the `list_objects_v2` paginator. There is no `delete_object` here, and
    that absence is checkable. The real boto3 client satisfies this
    structurally.
    """

    def head_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:  # noqa: N803 - SDK spelling
        """HEAD: `ContentLength` and `Metadata`, or a `ClientError`."""
        ...

    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]:  # noqa: N803 - SDK spelling
        """GET: a `Body` to stream, or a `ClientError`."""
        ...

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 - SDK spelling
        Key: str,  # noqa: N803 - SDK spelling
        Body: bytes,  # noqa: N803 - SDK spelling
        ContentLength: int,  # noqa: N803 - SDK spelling
        Metadata: Mapping[str, str],  # noqa: N803 - SDK spelling
    ) -> Mapping[str, Any]:
        """PUT: the whole object in one request, with user metadata."""
        ...

    def get_paginator(self, operation_name: Literal["list_objects_v2"]) -> _ListPaginator:
        """The paginator for `ListObjectsV2`."""
        ...


def build_b2_client(config: B2Configuration) -> S3Operations:
    """Construct the S3-compatible client for one bucket's configuration.

    Explicit in every respect the SDK would otherwise fill from the ambient
    environment: credentials from the validated configuration, the region
    derived from the endpoint hostname, SigV4 signing, path-style addressing
    (deterministic URLs, no dependency on a bucket-name subdomain resolving),
    bounded timeouts, `standard` retries, and checksum calculation left at
    `when_required` — the SDK's newer default of attaching a CRC checksum to
    every PUT is not something every S3-compatible store accepts, and this
    backend verifies its own SHA-256 anyway.

    Constructing a client is offline. Nothing is contacted until an
    operation runs.

    Args:
        config: Validated B2 settings.

    Returns:
        A client bound to the endpoint. Never reads `~/.aws` or `AWS_*`.

    Raises:
        StorageUnavailableError: If the endpoint's hostname is not one a
            signing region can be derived from. That is a configuration the
            offline check accepted and this backend cannot use; the fix is
            a Backblaze-assigned endpoint, not a guessed region.
    """
    region = config.region
    if region is None:
        host = urlsplit(config.endpoint_url).hostname
        message = (
            f"cannot derive a signing region from B2_ENDPOINT_URL host {host!r}; "
            "expected https://s3.<region>.backblazeb2.com"
        )
        raise StorageUnavailableError(message)
    session = boto3.session.Session(
        aws_access_key_id=config.key_id.get_secret_value(),
        aws_secret_access_key=config.application_key.get_secret_value(),
        region_name=region,
    )
    return session.client(
        "s3",
        endpoint_url=config.endpoint_url,
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"mode": "standard", "max_attempts": MAX_RETRIES},
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=READ_TIMEOUT_SECONDS,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            user_agent_extra="compounder-radar-sec-evidence",
        ),
    )


class B2EvidenceStorage:
    """`EvidenceStorage` over one Backblaze B2 bucket.

    Operates on the configured bucket and no other: it never lists buckets,
    never discovers one, and never deletes an object — routine evidence
    operation has no use for deletion and the application key need not
    grant it.

    Args:
        config: Validated B2 settings. The bucket and endpoint are read from
            it; the credentials are handed to the client and not kept here.
        client: A ready client, for tests that substitute a fake or a
            stubbed one. Built from `config` when omitted.
    """

    def __init__(self, config: B2Configuration, *, client: S3Operations | None = None) -> None:
        self._bucket = config.bucket
        self._endpoint = config.endpoint_url
        self._client = client if client is not None else build_b2_client(config)

    def __repr__(self) -> str:
        """The endpoint and bucket. Never a credential — none is held here."""
        return f"B2EvidenceStorage(endpoint={self._endpoint!r}, bucket={self._bucket!r})"

    @property
    def bucket(self) -> str:
        """The one bucket this backend operates on."""
        return self._bucket

    # ------------------------------------------------------------------
    # Error classification: one place, so "absent" and "unreachable" can
    # never drift apart between operations.
    # ------------------------------------------------------------------

    @contextmanager
    def _remote(self, operation: str, key: str) -> Iterator[None]:
        """Translate SDK failures into the contract's two meanings.

        A 404 becomes `ArtifactNotFoundError`. Every other `ClientError`
        (403, 401, throttling past the SDK's retries, 5xx) and every
        `BotoCoreError` (connection refused, connect or read timeout, a body
        that ended early, a signing failure) becomes
        `StorageUnavailableError`. The message names the operation, the key
        and the code — never the server's text, which can quote the request.
        """
        try:
            yield
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in _NOT_FOUND or status == 404:
                raise ArtifactNotFoundError(key) from exc
            log.warning("B2 request failed", operation=operation, key=key, code=code, status=status)
            raise StorageUnavailableError(
                f"B2 {operation} {key!r} failed: {code or status or 'unknown error'}"
            ) from exc
        except BotoCoreError as exc:
            log.warning("B2 request failed", operation=operation, key=key, fault=type(exc).__name__)
            raise StorageUnavailableError(
                f"B2 {operation} {key!r} failed: {type(exc).__name__}"
            ) from exc

    # ------------------------------------------------------------------
    # The contract.
    # ------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """Whether the bucket holds the key. False means a definite 404."""
        validate_key(key)
        try:
            with self._remote("HEAD", key):
                self._client.head_object(Bucket=self._bucket, Key=key)
        except ArtifactNotFoundError:
            return False
        return True

    def stat(self, key: str) -> ArtifactStat:
        """Size from a HEAD. No ETag, no timestamps — the contract has neither."""
        validate_key(key)
        with self._remote("HEAD", key):
            head = self._client.head_object(Bucket=self._bucket, Key=key)
        return ArtifactStat(key=key, bytes=int(head["ContentLength"]))

    def read_bytes(self, key: str) -> bytes:
        """The complete object, from one GET. The body is closed either way."""
        validate_key(key)
        with self._remote("GET", key):
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: StreamingBody = response["Body"]
            try:
                return bytes(body.read())
            finally:
                body.close()

    def write_bytes(self, key: str, data: bytes) -> StoredArtifact:
        """Upload, then make the bucket prove it holds exactly these bytes.

        The receipt's digest is computed here from `data`, before anything is
        sent, and is what the manifest will record. The upload carries it as
        metadata; the HEAD that follows must report that same digest and the
        same byte count, or the write is a failure regardless of what the
        PUT returned.

        Raises:
            EvidenceWriteError: If the payload exceeds a single PUT, the
                upload fails, the verification HEAD fails, or the bucket
                reports a different length or digest. No `StoredArtifact`
                is returned in any of those cases.
        """
        validate_key(key)
        if len(data) > SINGLE_PUT_LIMIT:
            message = (
                f"{key!r} is {len(data)} bytes, past the {SINGLE_PUT_LIMIT}-byte single-PUT "
                "limit; multipart upload is not implemented"
            )
            raise EvidenceWriteError(message)
        sha256 = hashlib.sha256(data).hexdigest()
        try:
            with self._remote("PUT", key):
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=data,
                    ContentLength=len(data),
                    Metadata={SHA256_METADATA: sha256},
                )
        except StorageUnavailableError as exc:
            raise EvidenceWriteError(f"B2 upload of {key!r} failed: {exc}") from exc

        try:
            with self._remote("HEAD", key):
                head = self._client.head_object(Bucket=self._bucket, Key=key)
        except (ArtifactNotFoundError, StorageUnavailableError) as exc:
            raise EvidenceWriteError(
                f"B2 upload of {key!r} could not be verified: {type(exc).__name__}"
            ) from exc

        remote_bytes = int(head["ContentLength"])
        remote_sha256 = head.get("Metadata", {}).get(SHA256_METADATA)
        if remote_bytes != len(data) or remote_sha256 != sha256:
            log.error(
                "B2 upload verification failed",
                key=key,
                sent_bytes=len(data),
                remote_bytes=remote_bytes,
                digest_matches=remote_sha256 == sha256,
            )
            raise EvidenceWriteError(
                f"B2 upload of {key!r} did not verify: sent {len(data)} bytes, bucket reports "
                f"{remote_bytes}; digest {'matches' if remote_sha256 == sha256 else 'differs'}"
            )
        return StoredArtifact(key=key, bytes=len(data), sha256=sha256)

    def digest(self, key: str) -> str:
        """SHA-256 of the object's actual bytes, streamed from a GET.

        Never the metadata digest and never the ETag: both are claims, and
        this is the check that catches a claim that has stopped being true.
        """
        validate_key(key)
        sha = hashlib.sha256()
        with self._remote("GET", key):
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            body: StreamingBody = response["Body"]
            try:
                for chunk in body.iter_chunks(1024 * 1024):
                    sha.update(chunk)
            finally:
                body.close()
        return sha.hexdigest()

    def list_prefix(self, prefix: str) -> Iterator[str]:
        """Every key beneath a logical prefix, paginated, sorted, deduplicated.

        Segment-boundary semantics are enforced here rather than trusted to
        the server: the request asks for `prefix/`, and every returned key is
        checked to start with it, so `filings/1` can never list `filings/10/…`.
        The empty prefix lists the whole bucket. Keys that are not canonical
        artifact keys — a dot-prefixed segment, a doubled separator — are
        backend-internal or foreign and are not surfaced.
        """
        # A prefix naturally ends in a separator; a key never does. The check
        # runs on the original so that a bare "/" is refused as the empty key
        # it is, exactly as the local backend refuses it.
        if prefix:
            validate_key(prefix.removesuffix("/"))
        prefix = prefix.removesuffix("/")
        remote_prefix = f"{prefix}/" if prefix else ""
        keys: set[str] = set()
        with self._remote("LIST", remote_prefix):
            paginator = self._client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self._bucket, Prefix=remote_prefix):
                for item in page.get("Contents", []):
                    key = str(item["Key"])
                    if not key.startswith(remote_prefix) or not _is_visible(key):
                        continue
                    keys.add(key)
        yield from sorted(keys)


def _is_visible(key: str) -> bool:
    """Whether a remote key is a canonical artifact key a listing may surface."""
    try:
        validate_key(key)
    except ValueError:
        return False
    return True
