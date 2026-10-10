"""The one concrete `BrokerTransport`: HTTPS to Alpaca's paper host, and nowhere else.

This is the module that turns the P2.2 adapter from a tested design into
something that can reach a venue. Everything it does is one request and one
classified answer; everything it refuses is the point of it existing.

**The host is locked by construction, not by configuration.** The transport is
built around one origin — `https://paper-api.alpaca.markets` — and validates
that origin at construction, before a credential is attached and before any
socket exists. A live host, a sandbox host, an IP literal, a `http://` scheme, a
URL carrying userinfo, a redirect to anywhere else: each is refused. The check
runs in the constructor rather than per request because a transport that could
be *re-pointed* after construction is a transport whose host lock depends on
nobody making a mistake later.

**The environment cannot re-point it either.** `trust_env=False` is passed to
the client, which is less obvious than the rest of this and matters just as
much: with the default, `HTTPS_PROXY` or `ALL_PROXY` in the environment would
route every request — order submissions included — through whatever host that
variable named, while the URL still read `paper-api.alpaca.markets`. A host lock
that an environment variable can tunnel out of is not a host lock.

**Redirects are refused outright.** Not "followed only to the same origin" —
refused. A 3xx on a mutation is reported as an answer the adapter cannot use,
and a 3xx on a read is a failure. The permissive version of this is how an order
request ends up at `api.alpaca.markets`.

**One request per call, with no retry of a mutation.** The transport has no
retry logic at all. Bounded retry for reads lives one layer up in
`read_with_retry`, which is GET-only; mutation recovery belongs to the
submission protocol, which resolves by *asking* rather than resending. A retry
helper here — even a careful one — is the thing a future caller in a hurry would
point at a `POST`.

**Credentials exist only in the request headers.** They are held as `SecretStr`,
attached at the network boundary, and never reach a message, a `repr`, a
fingerprint, an evidence body or a report. Failures are described by exception
*class*, never by rendering an exception that might have interpolated a URL or a
header.
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlsplit

import httpx

from ..errors import Code, ExecutionError
from .environment import LIVE_HOSTS, PAPER_HOSTS
from .transport import BrokerResponse, RequestMethod, TransportOutcome

if TYPE_CHECKING:
    from pydantic import SecretStr

    from stock_screener.config import Settings

__all__ = [
    "CONNECT_TIMEOUT_SECONDS",
    "KEY_ID_HEADER",
    "READ_TIMEOUT_SECONDS",
    "SAFE_RESPONSE_HEADERS",
    "SECRET_KEY_HEADER",
    "TRANSPORT_VERSION",
    "AlpacaPaperHttpTransport",
    "approved_paper_origin",
]

#: The transport contract. Versioned separately from the engine and the adapter
#: because how a request is made can change — a timeout, a header, a client —
#: without any execution semantics moving, and an artefact should be able to say
#: which of the three changed.
TRANSPORT_VERSION: Final = "ALPACA_PAPER_HTTPS_TRANSPORT_V1"

#: Alpaca's documented Trading API authentication headers. The key id and the
#: secret are sent as headers and exist nowhere else in this process's output.
KEY_ID_HEADER: Final = "APCA-API-KEY-ID"
# S105 is suppressed because this is the *name* of the header the secret travels
# in, not a secret. The value is never a literal anywhere in this repository —
# it is read from a SecretStr at the boundary, in _headers().
SECRET_KEY_HEADER: Final = "APCA-API-SECRET-KEY"  # noqa: S105

#: How long to wait to establish a connection.
CONNECT_TIMEOUT_SECONDS: Final = 10.0

#: How long to wait for a reply once connected. Generous rather than tight: a
#: short read timeout on a `POST` manufactures exactly the ambiguity this phase
#: works hardest to avoid, so the transport would rather wait than not know.
READ_TIMEOUT_SECONDS: Final = 30.0

#: Response headers this adapter will record. An allow-list, because a
#: deny-list acquires a leak the day the provider adds a header.
SAFE_RESPONSE_HEADERS: Final = frozenset({"x-request-id", "content-type", "date", "retry-after"})


def approved_paper_origin(base_url: str) -> str:
    """Validate a base URL and return the one origin a transport may reach.

    Runs before a credential is attached and before any socket exists, so a
    rejected configuration never gets as far as authenticating.

    Args:
        base_url: The configured base URL.

    Returns:
        The approved origin, `https://<host>`, with no path and no port.

    Raises:
        ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` if the host is a known
            live or brokerage host — named explicitly so the refusal says *why*
            rather than merely that the host was unknown.
            `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` for anything else that is not
            provably the paper origin: a non-HTTPS scheme, a URL carrying
            userinfo, an explicit non-443 port, an IP literal, `localhost`, or
            any host not on the paper allow-list.
    """
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "").lower()

    if host in LIVE_HOSTS or host.startswith(("api.", "broker-api.")):
        raise ExecutionError(
            Code.LIVE_TRADING_NOT_AUTHORIZED,
            f"{host!r} is a live or brokerage trading host. This transport exists "
            "to reach a paper venue and will not be constructed against any other, "
            "whatever the configuration asks for",
        )
    if parts.scheme != "https":
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"the scheme is {parts.scheme!r}; a credential and an order may only "
            "cross a verified TLS connection",
        )
    if parts.username or parts.password:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            "the base URL carries userinfo. Credentials belong in headers, and a "
            "URL that holds one would be logged by every intermediary that saw it",
        )
    if parts.port is not None and parts.port != 443:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"the base URL names port {parts.port}; the paper origin is reached on "
            "443 and an explicit other port is a redirection this will not make",
        )
    if host not in PAPER_HOSTS:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"{host!r} is not the approved paper origin "
            f"({sorted(PAPER_HOSTS)}). An unrecognised host — an IP literal, a "
            "local address, a proxy, a sandbox — is refused rather than assumed "
            "safe, because the cost of guessing wrong is a real order",
        )
    return f"https://{host}"


@dataclass(frozen=True)
class _Failure:
    """One classified network failure, described without its exception text.

    Attributes:
        outcome: What the caller should conclude.
        detail: A sanitised description, built from the exception's class name
            only. The exception's own message is deliberately not rendered: it
            can carry the full request URL and, for some client internals,
            header state, and neither belongs in a durable record.
    """

    outcome: TransportOutcome
    detail: str


def _classify(error: Exception) -> _Failure:
    """Classify a transport-level failure.

    A timeout on a mutation must stay *unknown* rather than becoming *failed*:
    the request may have been accepted and the reply lost, and the entire
    submission protocol is built on that distinction. So every classification
    here maps to an outcome whose `is_ambiguous` is true, and none of them maps
    to a definite refusal.

    Args:
        error: The client-level exception.

    Returns:
        The classification.
    """
    name = type(error).__name__
    if isinstance(error, httpx.TimeoutException):
        return _Failure(
            TransportOutcome.TIMED_OUT,
            f"no reply within the timeout ({name}); if this was a mutation its "
            "fate is unknown and it must not be resent",
        )
    if isinstance(error, httpx.ConnectError):
        return _Failure(TransportOutcome.UNREACHABLE, f"no connection was made ({name})")
    if isinstance(error, httpx.RemoteProtocolError | httpx.ReadError | httpx.WriteError):
        return _Failure(
            TransportOutcome.UNREACHABLE,
            f"the connection ended mid-exchange ({name}); a mutation's fate is unknown",
        )
    if isinstance(error, httpx.TooManyRedirects):
        return _Failure(
            TransportOutcome.UNREACHABLE,
            "the provider redirected and this transport does not follow redirects",
        )
    return _Failure(TransportOutcome.UNREACHABLE, f"the request did not complete ({name})")


@dataclass
class AlpacaPaperHttpTransport:
    """HTTPS `BrokerTransport` bound to one Alpaca paper account.

    Satisfies the existing protocol exactly: one `request` method, one
    `BrokerResponse` out, and no exception for a provider-level failure — an
    exception there would collapse "refused" and "unknown" into one thing.

    Construct it with `from_settings`. The constructor validates the origin
    before anything else happens, so a misconfigured transport fails at build
    time rather than at submit time.

    Attributes:
        origin: The approved paper origin. Validated, and the only host reached.
        audit: A `RequestAuditStore`, or `None`. When present, every mutation is
            recorded *before* dispatch and the request is not sent if that
            record cannot be persisted — an unaudited mutation is worse than a
            missed one, because a missed mutation is visible and an unaudited
            one is not. `None` leaves the transport unaudited, which is correct
            for a one-shot preflight read and not for an operational run.
        operations_run_id: Which operational run owns these requests, for the
            audit's causal linkage.
        account_binding_fingerprint: Which paper account, digested.
        key_id: The paper key id, held as a secret.
        secret_key: The paper secret, held as a secret.
        connect_timeout: Seconds to establish a connection.
        read_timeout: Seconds to wait for a reply.
        client: The underlying client. Injectable so the offline tests can mount
            a stub at the HTTP layer and still exercise this module's own logic —
            URL building, query encoding, JSON handling, classification,
            redaction — rather than a different code path.
    """

    origin: str
    key_id: SecretStr
    secret_key: SecretStr
    audit: Any = None
    operations_run_id: str = ""
    account_binding_fingerprint: str = ""
    connect_timeout: float = CONNECT_TIMEOUT_SECONDS
    read_timeout: float = READ_TIMEOUT_SECONDS
    client: httpx.Client | None = None

    def __post_init__(self) -> None:
        """Re-validate the origin and build a locked-down client.

        Raises:
            ExecutionError: As `approved_paper_origin` does. Re-validated here
                even though `from_settings` already did, because a caller can
                construct this class directly and the host lock may not depend
                on which constructor was used.
        """
        object.__setattr__(self, "origin", approved_paper_origin(self.origin))
        if self.client is None:
            self.client = httpx.Client(
                base_url=self.origin,
                timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
                # TLS verification on, redirects refused, and the environment
                # denied any say in routing. Each is load-bearing; see the
                # module docstring on trust_env.
                verify=True,
                follow_redirects=False,
                trust_env=False,
            )

    @classmethod
    def from_settings(cls, settings: Settings) -> AlpacaPaperHttpTransport:
        """Build a transport from typed configuration.

        Args:
            settings: The application settings. Read through typed fields;
                nothing here touches the environment directly.

        Returns:
            The transport.

        Raises:
            ExecutionError: `BROKER_CREDENTIALS_MISSING` if either paper
                credential is unset, or as `approved_paper_origin` does for a
                host that is not provably the paper origin.
        """
        if settings.alpaca_paper_key_id is None or settings.alpaca_paper_secret_key is None:
            raise ExecutionError(
                Code.BROKER_CREDENTIALS_MISSING,
                "ALPACA_PAPER_KEY_ID and ALPACA_PAPER_SECRET_KEY are both required "
                "to reach the paper venue; neither is read from the environment "
                "directly and neither is logged",
            )
        return cls(
            origin=settings.alpaca_paper_base_url,
            key_id=settings.alpaca_paper_key_id,
            secret_key=settings.alpaca_paper_secret_key,
        )

    def _headers(self) -> dict[str, str]:
        """Build the request headers.

        The only place in this process where a credential is rendered to a
        string. The result is handed straight to the client and is never
        returned, logged, stored or digested.

        Returns:
            The headers.
        """
        return {
            KEY_ID_HEADER: self.key_id.get_secret_value(),
            SECRET_KEY_HEADER: self.secret_key.get_secret_value(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def __repr__(self) -> str:
        """Describe the transport without its credentials.

        Returns:
            A representation naming the origin only. `SecretStr` already masks
            itself, and this makes that independent of that behaviour.
        """
        return f"AlpacaPaperHttpTransport(origin={self.origin!r}, credentials=<redacted>)"

    def request(
        self,
        method: RequestMethod,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> BrokerResponse:
        """Make exactly one request to the paper venue.

        One request per call, always. There is no retry here for any method, and
        in particular none for a mutation: a `POST` whose fate is unknown is
        resolved by the submission protocol asking the venue about the derived
        client order id, never by this method sending it again.

        Args:
            method: The verb.
            path: The provider path, without a host. Joined to the approved
                origin; an absolute URL is refused rather than honoured.
            params: Query parameters.
            json_body: A JSON payload, for a mutation.

        Returns:
            The classified reply. Never raises for a provider-level failure.

        Raises:
            ExecutionError: `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the path is
                an absolute URL or is otherwise not a path on the approved
                origin — that is a host-lock violation, not a provider failure,
                so it is refused loudly rather than classified.
        """
        assert self.client is not None  # noqa: S101 - built in __post_init__
        target = self._resolve(path)
        content = self._encode(json_body)
        attempt = self._open_audit(method=method, path=path, json_body=json_body)
        # Only the network call is inside the try. Encoding happens above it on
        # purpose: a payload that cannot be serialised fails before any I/O, so
        # the request provably did not arrive. Classifying that as a transport
        # failure would mark it ambiguous, and the submission protocol would then
        # leave an unresolved intent — blocking the next run over a local bug
        # that never reached the venue.
        try:
            response = self.client.request(
                str(method),
                target,
                params=params,
                content=content,
                headers=self._headers(),
            )
        except Exception as error:
            failure = _classify(error)
            self._close_audit(attempt, result=str(failure.outcome), status=None)
            return BrokerResponse(outcome=failure.outcome, detail=failure.detail)
        reply = self._read(response, method)
        self._close_audit(attempt, result=str(reply.outcome), status=reply.status_code)
        return reply

    def _open_audit(
        self,
        *,
        method: RequestMethod,
        path: str,
        json_body: dict[str, Any] | None,
    ) -> Any:
        """Record the attempt before anything is dispatched.

        Args:
            method: The verb.
            path: The provider path.
            json_body: The payload, read only for its client order id.

        Returns:
            The pre-dispatch record, or `None` when no audit store is bound.

        Raises:
            ExecutionError: `EXTERNAL_REQUEST_AUDIT_FAILED` when a **mutation**
                cannot be audited. A read whose audit fails is allowed to
                proceed: a read changes nothing, and failing a reconciliation
                read closed would block operations over a disk problem. A
                mutation is the opposite — it is the thing that must never
                happen unobserved.
        """
        if self.audit is None:
            return None
        from datetime import UTC, datetime

        from .request_audit import (
            MUTATING_METHODS,
            OperationClass,
            RequestAuditRecord,
            classify_destination,
            route_class_for,
        )

        mutating = str(method) in MUTATING_METHODS
        route = route_class_for(path)
        now = datetime.now(UTC).isoformat()
        client_order_id = str((json_body or {}).get("client_order_id", "") or "")
        record = RequestAuditRecord(
            attempt_id=_attempt_id(
                origin=self.origin,
                method=str(method),
                route=route,
                client_order_id=client_order_id,
                attempted_at=now,
            ),
            destination=classify_destination(self.origin),
            operation=OperationClass.MUTATION if mutating else OperationClass.READ,
            method=str(method),
            route_class=route,
            attempted_at=now,
            client_order_id=client_order_id,
            account_binding_fingerprint=self.account_binding_fingerprint,
            operations_run_id=self.operations_run_id,
        )
        try:
            self.audit.record_attempt(record)
        except Exception:
            if mutating:
                raise
            return None
        return record

    def _close_audit(self, attempt: Any, *, result: str, status: int | None) -> None:
        """Append the outcome beside its attempt, never overwriting it.

        A failure to write the *result* is deliberately swallowed: the mutation
        has already happened, and raising here would turn a successful trade
        into an exception the caller would have to interpret. The attempt record
        remains, which is what makes the request discoverable as unresolved.
        """
        if attempt is None or self.audit is None:
            return
        import dataclasses
        from datetime import UTC, datetime

        completed = dataclasses.replace(
            attempt,
            sent=True,
            result=result,
            status_code=status,
            completed_at=datetime.now(UTC).isoformat(),
        )
        with contextlib.suppress(Exception):
            # The request already happened; raising here would turn a completed
            # mutation into an exception the caller must interpret. The attempt
            # record remains, which is what makes it discoverable as unresolved.
            self.audit.record_result(completed)

    @staticmethod
    def _encode(json_body: dict[str, Any] | None) -> bytes | None:
        """Serialise a request payload, refusing one that cannot be rendered.

        Args:
            json_body: The payload, or `None` for a read.

        Returns:
            The encoded body, or `None`.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` if the payload holds a
                value JSON cannot represent — a `Decimal` or a NaN among them.
                Raised rather than classified: this happens before any I/O, so
                the request definitely did not arrive, and reporting it as a
                transport ambiguity would strand a submission intent over a
                local defect. Callers send quantities and prices as exact
                strings for exactly this reason.
        """
        if json_body is None:
            return None
        try:
            return json.dumps(json_body, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError) as exc:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                "the request payload could not be serialised as JSON "
                f"({type(exc).__name__}); nothing was sent. Decimal and NaN are "
                "refused rather than coerced — send exact strings",
            ) from exc

    def _resolve(self, path: str) -> str:
        """Refuse anything that is not a path on the approved origin.

        Args:
            path: The requested path.

        Returns:
            The path, unchanged, when it is safe to join to the origin.

        Raises:
            ExecutionError: `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the path
                carries a scheme or an authority. A client with a `base_url`
                still honours an absolute URL passed as the target, so this is
                the check that stops a caller reaching another host through a
                host-locked transport.
        """
        if "://" in path or path.startswith("//"):
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "a request path may not be an absolute URL: this transport is "
                f"locked to {self.origin} and an absolute target would leave it",
            )
        return path if path.startswith("/") else f"/{path}"

    def _read(self, response: httpx.Response, method: RequestMethod) -> BrokerResponse:
        """Classify a reply the provider actually sent.

        Args:
            response: The client response.
            method: The verb, so a redirect on a mutation can be described as
                the hazard it is.

        Returns:
            The classified reply.
        """
        safe_headers = tuple(
            sorted(
                (name.lower(), value)
                for name, value in response.headers.items()
                if name.lower() in SAFE_RESPONSE_HEADERS
            )
        )
        request_id = response.headers.get("x-request-id")

        if 300 <= response.status_code < 400:
            # Refused rather than followed. A redirect that moved a submission
            # to another origin is the precise failure the host lock exists for.
            return BrokerResponse(
                outcome=TransportOutcome.UNREACHABLE,
                status_code=response.status_code,
                detail=(
                    f"the provider redirected a {method} and this transport does not "
                    "follow redirects; the request was not repeated anywhere"
                ),
                request_id=request_id,
                safe_headers=safe_headers,
            )

        if response.status_code == 429:
            return BrokerResponse(
                outcome=TransportOutcome.RATE_LIMITED,
                status_code=response.status_code,
                detail="the provider refused for volume",
                request_id=request_id,
                safe_headers=safe_headers,
            )

        body: Any = None
        detail: str | None = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                # Deliberately not treated as a refusal. A 2xx with an
                # unreadable body leaves `ok` true and `body` None, and the
                # adapter then refuses it as REMOTE_STATE_UNKNOWN — which is the
                # honest reading: the provider answered and we cannot tell what
                # it said.
                detail = f"the reply was not JSON ({len(response.content)} bytes)"

        if detail is None and not (200 <= response.status_code < 300):
            detail = f"the provider answered {response.status_code}"

        return BrokerResponse(
            outcome=TransportOutcome.ANSWERED,
            status_code=response.status_code,
            body=body,
            detail=detail,
            request_id=request_id,
            safe_headers=safe_headers,
        )

    def close(self) -> None:
        """Close the underlying connection pool."""
        if self.client is not None:
            self.client.close()

    def __enter__(self) -> AlpacaPaperHttpTransport:
        """Enter a context that closes the pool on exit.

        Returns:
            Self.
        """
        return self

    def __exit__(self, *_: object) -> None:
        """Close the pool."""
        self.close()


def _attempt_id(
    *, origin: str, method: str, route: str, client_order_id: str, attempted_at: str
) -> str:
    """Derive an identity for one request attempt.

    Derived from the attempt's own inputs rather than generated, so a retry of
    the same semantic request is recognisable — and the instant is included
    because two legitimate reads of one route are different attempts.

    Args:
        origin: The destination origin.
        method: The verb.
        route: The route class.
        client_order_id: The derived client order id, where there is one.
        attempted_at: When.

    Returns:
        The id.
    """
    import hashlib

    digest = hashlib.sha256(
        f"{origin}|{method}|{route}|{client_order_id}|{attempted_at}".encode()
    ).hexdigest()
    return f"req-{digest[:24]}"
