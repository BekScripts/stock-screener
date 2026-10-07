"""The only place in the execution package that may reach a network.

Everything else — the planner, the lifecycle, the journal, the cost model, the
reconciliation — takes a transport by injection and never constructs one. That
containment is the point: a test can give the whole adapter a fake transport and
exercise every path, and the core execution modules continue to import no HTTP
client at all, which a test asserts by scanning their imports.

The transport is deliberately dumb. It knows how to make a request and how to
report that it could not; it knows nothing about orders, environments or
accounts. Everything about *whether* a request is permitted is decided before a
transport is reached.

**Mutating requests are not retried here.** A `GET` that times out can be
repeated safely, and bounded retry for reads lives in this module. A `POST` that
times out may or may not have been accepted, and repeating it is how a duplicate
order gets placed — so the transport reports the ambiguity and the submission
protocol resolves it by *querying* rather than by resending. There is
deliberately no generic retry wrapper that could be pointed at a mutation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.fingerprint import fingerprint_of

__all__ = [
    "READ_RETRY_LIMIT",
    "BrokerResponse",
    "BrokerTransport",
    "ExternalReadEvidence",
    "FakeTransport",
    "RequestMethod",
    "TransportOutcome",
    "canonical_evidence",
    "read_with_retry",
]

#: How many times a read may be retried. Bounded, because an unbounded loop
#: against a rate-limited provider is a way to get a credential suspended.
READ_RETRY_LIMIT = 3


def canonical_evidence(value: Any) -> Any:
    """Normalise a provider payload so a digest over it is stable.

    Every number is rendered as a string. A provider sends its figures as
    strings already, but a payload that mixed `"10"` and `10` across two reads
    of the same state would otherwise fingerprint differently and read as the
    venue having changed its answer. Keys are left to `fingerprint_of`, which
    sorts them.

    Args:
        value: A decoded JSON payload, or any part of one.

    Returns:
        The normalised form.
    """
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int | float | Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): canonical_evidence(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical_evidence(item) for item in value]
    return value


class RequestMethod(StrEnum):
    """Which HTTP verb a request uses.

    Split into read and mutate rather than carried as a bare string so the
    retry policy can refuse to apply itself to a mutation. A string would let a
    generic retry wrapper be pointed at a `POST` by accident.
    """

    GET = "GET"
    POST = "POST"
    DELETE = "DELETE"

    @property
    def mutates(self) -> bool:
        """Whether repeating this request could change the world twice."""
        return self is not RequestMethod.GET


class TransportOutcome(StrEnum):
    """What happened to a request, from the caller's point of view."""

    ANSWERED = "ANSWERED"
    """The provider replied, whatever the status code."""

    UNREACHABLE = "UNREACHABLE"
    """No connection was made. For a mutation this is still ambiguous — the
    request may have arrived and the reply been lost."""

    TIMED_OUT = "TIMED_OUT"
    """No reply arrived in time. For a mutation the provider may well have
    accepted it, which is why this is never grounds for resending."""

    RATE_LIMITED = "RATE_LIMITED"
    """The provider refused for volume. A read may back off; a mutation may not
    be resent without first establishing remote state."""


@dataclass(frozen=True)
class BrokerResponse:
    """One provider reply, normalised and free of credentials.

    Attributes:
        outcome: What happened.
        status_code: The HTTP status, where there was one.
        body: The decoded payload, where there was one.
        detail: A sanitised description of a failure.
        attempts: How many times the request was made.
        request_id: The provider's own id for this request, where it sent one.
            Operational metadata only — it is reported so an ambiguous
            submission can be raised with the provider, and it is never read by
            any decision, never part of a financial figure, and never a
            credential.
        safe_headers: The response headers this adapter is willing to record.
            An allow-list, not a filter: a header is absent unless it was
            explicitly deemed safe, because a deny-list grows a leak the day a
            provider adds a header.
    """

    outcome: TransportOutcome
    status_code: int | None = None
    body: Any = None
    detail: str | None = None
    attempts: int = 1
    request_id: str | None = None
    safe_headers: tuple[tuple[str, str], ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the provider answered successfully."""
        return (
            self.outcome is TransportOutcome.ANSWERED
            and self.status_code is not None
            and 200 <= self.status_code < 300
        )

    @property
    def not_found(self) -> bool:
        """Whether the provider answered that the resource does not exist.

        Distinguished from every other failure because it is the one answer that
        lets a submission protocol conclude a request never arrived.
        """
        return self.outcome is TransportOutcome.ANSWERED and self.status_code == 404

    @property
    def is_ambiguous(self) -> bool:
        """Whether this reply leaves a mutation's fate unknown.

        A timeout, an unreachable host or a rate-limit after a `POST` all mean
        the provider may or may not have acted. Nothing may be resent on this
        basis.
        """
        return self.outcome in {
            TransportOutcome.TIMED_OUT,
            TransportOutcome.UNREACHABLE,
            TransportOutcome.RATE_LIMITED,
        }

    def evidence_body(self) -> dict[str, Any]:
        """Return what this reply is evidence of, for fingerprinting.

        Deliberately built from the *response* only: the status, the headers
        that were allow-listed, and the normalised body. Request headers are
        excluded by construction, because the request carried the credential and
        a digest over it would be a credential derivative sitting in an audit
        record forever.

        Returns:
            The evidence body.
        """
        return {
            "outcome": str(self.outcome),
            "status_code": self.status_code,
            "safe_headers": [list(pair) for pair in self.safe_headers],
            "body": canonical_evidence(self.body),
        }

    def evidence_fingerprint(self) -> str:
        """Digest this reply so an artefact can name what it saw.

        Returns:
            The digest. Carries no credential, since `evidence_body` excludes
            every request header.
        """
        return fingerprint_of(self.evidence_body())

    def as_json(self) -> dict[str, Any]:
        """Return the reply as a JSON-safe mapping. Carries no credential."""
        return {
            "outcome": str(self.outcome),
            "status_code": self.status_code,
            "ok": self.ok,
            "is_ambiguous": self.is_ambiguous,
            "detail": self.detail,
            "attempts": self.attempts,
            "request_id": self.request_id,
            "safe_headers": [list(pair) for pair in self.safe_headers],
            "evidence_fingerprint": self.evidence_fingerprint(),
        }


@dataclass(frozen=True)
class ExternalReadEvidence[T]:
    """One normalised read and the evidence for it, from the same response.

    The envelope exists because of a defect worth naming. The first version of
    the preflight read each endpoint twice — once to fingerprint, once to get
    the values it reported — so the fingerprint in the artefact was of a
    *different* response than the figures beside it. Nothing was wrong with
    either read, and the artefact was still useless as evidence: it bound a
    digest to data it had not seen.

    So a read now yields one object. The normalised value and the fingerprint
    come from one response, and a caller cannot accidentally pair them from two.

    Attributes:
        value: The normalised result.
        status_code: The status that carried it.
        request_id: The provider's own request id, where it sent one.
        safe_headers: The allow-listed response headers.
        fingerprint: The digest of that same response.
        received_at: When the read was made, ISO-8601. Operational metadata; it
            is deliberately absent from the fingerprint so the same observation
            digests identically whenever it was taken.
    """

    value: T
    status_code: int | None
    fingerprint: str
    request_id: str | None = None
    safe_headers: tuple[tuple[str, str], ...] = ()
    received_at: str = ""

    @classmethod
    def of(
        cls, value: T, response: BrokerResponse, *, received_at: str = ""
    ) -> ExternalReadEvidence[T]:
        """Wrap a normalised value with the evidence of the response it came from.

        Args:
            value: The normalised result.
            response: The reply it was read from.
            received_at: When the read was made.

        Returns:
            The envelope.
        """
        return cls(
            value=value,
            status_code=response.status_code,
            fingerprint=response.evidence_fingerprint(),
            request_id=response.request_id,
            safe_headers=response.safe_headers,
            received_at=received_at,
        )

    def as_json(self) -> dict[str, Any]:
        """Return the evidence as a JSON-safe mapping. Carries no credential."""
        return {
            "status_code": self.status_code,
            "fingerprint": self.fingerprint,
            "request_id": self.request_id,
            "safe_headers": [list(pair) for pair in self.safe_headers],
            "received_at": self.received_at,
        }


@runtime_checkable
class BrokerTransport(Protocol):
    """Whatever can make a request to a broker.

    Implemented by a real HTTP client in a later phase and by `FakeTransport`
    here. The adapter never constructs one, so every test drives the whole
    adapter without a socket.
    """

    def request(
        self,
        method: RequestMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> BrokerResponse:
        """Make one request.

        Args:
            method: The verb.
            path: The provider path, without a host.
            params: Query parameters.
            json_body: A JSON payload, for a mutation.

        Returns:
            The normalised reply. Never raises for a provider-level failure —
            an exception would lose the distinction between "refused" and
            "unknown", and that distinction is the whole basis of the
            submission protocol.
        """
        ...


@dataclass
class FakeTransport:
    """A scripted transport, for exercising every path without a network.

    Responses are queued per `(method, path)` and consumed in order, so a test
    can script a timeout followed by a successful lookup — which is exactly the
    sequence the submission protocol has to survive. Every request is recorded,
    so a test can assert that no mutation was retried.

    Attributes:
        responses: Queued replies per route.
        default: What to answer for an unscripted route.
        calls: Every request made, in order.
    """

    responses: dict[tuple[str, str], list[BrokerResponse]] = field(default_factory=dict)
    default: BrokerResponse | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def script(self, method: RequestMethod, path: str, *responses: BrokerResponse) -> FakeTransport:
        """Queue replies for a route.

        Args:
            method: The verb.
            path: The path.
            responses: Replies, consumed in order.

        Returns:
            Self, for chaining.
        """
        self.responses.setdefault((str(method), path), []).extend(responses)
        return self

    def request(
        self,
        method: RequestMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> BrokerResponse:
        """Answer from the script.

        Args:
            method: The verb.
            path: The path.
            params: Query parameters, recorded.
            json_body: A payload, recorded.

        Returns:
            The next scripted reply, or the default.

        Raises:
            ExecutionError: `BROKER_UNREACHABLE` when nothing is scripted and
                there is no default — a silent success would make a test pass by
                accident.
        """
        self.calls.append(
            {
                "method": str(method),
                "path": path,
                "params": dict(params or {}),
                "json_body": dict(json_body or {}),
                "mutates": method.mutates,
            }
        )
        queued = self.responses.get((str(method), path))
        if queued:
            return queued.pop(0)
        if self.default is not None:
            return self.default
        raise ExecutionError(
            Code.BROKER_UNREACHABLE,
            f"nothing is scripted for {method} {path} and there is no default; a "
            "silent success here would let a test pass by accident",
        )

    @property
    def mutations(self) -> list[dict[str, Any]]:
        """Every mutating request made. A test asserts this never duplicates."""
        return [row for row in self.calls if row["mutates"]]

    def mutation_count(self, path: str) -> int:
        """How many mutating requests reached one path.

        Args:
            path: The path.

        Returns:
            The count.
        """
        return sum(1 for row in self.mutations if row["path"] == path)


def read_with_retry(
    transport: BrokerTransport,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    limit: int = READ_RETRY_LIMIT,
    sleep: Any = time.sleep,
    backoff_seconds: float = 0.0,
) -> BrokerResponse:
    """Make a read, retrying a transient failure a bounded number of times.

    Reads only. There is deliberately no mutating equivalent in this module: a
    `POST` that may or may not have been accepted is resolved by querying remote
    state, never by resending, and providing a retry helper that *could* be
    pointed at a mutation is how that rule gets broken by a future caller in a
    hurry.

    Args:
        transport: The transport.
        path: The path.
        params: Query parameters.
        limit: How many attempts at most.
        sleep: How to wait between attempts. Injected so tests do not.
        backoff_seconds: How long to wait. Zero in tests.

    Returns:
        The last reply, with the attempt count recorded.

    Raises:
        ExecutionError: `BROKER_CAPABILITY_UNSUPPORTED` if asked for a
            non-positive attempt limit.
    """
    if limit < 1:
        raise ExecutionError(
            Code.BROKER_CAPABILITY_UNSUPPORTED,
            f"a read needs at least one attempt, got {limit}",
        )
    last: BrokerResponse | None = None
    for attempt in range(1, limit + 1):
        last = transport.request(RequestMethod.GET, path, params=params)
        if last.ok or last.not_found:
            return replace(last, attempts=attempt)
        if attempt < limit and backoff_seconds > 0:
            sleep(backoff_seconds * attempt)
    assert last is not None  # noqa: S101 - the loop runs at least once
    return replace(last, attempts=limit)
