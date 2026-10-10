"""A durable, sanitised record of every external request this process makes.

The `live_mutations` soak gate asks a question nothing could answer: did any
request ever reach a live endpoint? The transport is host-locked in its
constructor, so a live request is structurally impossible — but structural
impossibility is a prevention mechanism, not evidence. Asked for proof, the
system could only have offered an argument about its own source code.

So mutations are audited *before* they are dispatched, and the audit write is
part of the mutation's precondition: if the record cannot be persisted, the
request is not sent. That ordering is the whole point. An audit written
afterwards would be missing exactly when it mattered most — a crash mid-flight,
which is also the case where a duplicate becomes possible.

Reads are audited too, more cheaply, because a soak that can say "three GETs,
zero mutations" is more useful than one that can only say "no mutation was
recorded".

Nothing here carries a credential. The account appears as a fingerprint, the
route as a class rather than a URL, and the body never appears at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "EXTERNAL_REQUEST_AUDIT_VERSION",
    "MUTATING_METHODS",
    "Destination",
    "OperationClass",
    "RequestAuditRecord",
    "RequestAuditStore",
    "classify_destination",
]

#: The audit contract.
EXTERNAL_REQUEST_AUDIT_VERSION = "EXTERNAL_REQUEST_AUDIT_V1"

#: Methods that change state at the venue. A method outside this set is a read.
MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class Destination(StrEnum):
    """Where a request was addressed, classified deterministically."""

    PAPER = "PAPER"
    """An approved paper origin."""

    LIVE = "LIVE"
    """A known live or brokerage origin. Structurally unreachable, and
    classified anyway so an attempt is recordable rather than merely refused."""

    OTHER = "OTHER"
    """Anything else. Refused by the transport; classified so the refusal has a
    reason rather than a shrug."""


class OperationClass(StrEnum):
    """Whether a request changes anything."""

    READ = "READ"
    MUTATION = "MUTATION"


def classify_destination(origin: str) -> Destination:
    """Classify an origin against the known host sets.

    Deterministic and exact, never a substring test: `paper-api.alpaca.markets`
    and `api.alpaca.markets` differ by a prefix, and a substring rule that
    matched "alpaca.markets" would classify the live host as paper.

    Args:
        origin: The scheme-and-host the request was addressed to.

    Returns:
        The classification.
    """
    from .environment import LIVE_HOSTS, PAPER_HOSTS

    host = origin.split("://")[-1].split("/")[0].split(":")[0].strip().lower()
    if host in {row.lower() for row in PAPER_HOSTS}:
        return Destination.PAPER
    if host in {row.lower() for row in LIVE_HOSTS}:
        return Destination.LIVE
    return Destination.OTHER


@dataclass(frozen=True)
class RequestAuditRecord:
    """One external request, as a record that carries no secret.

    Attributes:
        attempt_id: A derived identity for this attempt, so the pre-dispatch
            record and its result refer to each other without a clock.
        destination: `PAPER`, `LIVE` or `OTHER`.
        operation: `READ` or `MUTATION`.
        method: The HTTP verb.
        route_class: The provider path with identifiers removed — a class, not
            a URL, so an order id never lands in an audit file.
        sent: Whether the request left the process. `False` on a refusal and on
            a pre-dispatch record that has no result yet.
        refused: Whether the transport refused before dispatch.
        result: `ANSWERED`, `REFUSED`, `TIMEOUT`, `UNREACHABLE`, `UNRESOLVED`.
            `UNRESOLVED` is the honest state of a pre-dispatch record whose
            outcome never arrived, and is never rewritten to "not submitted" —
            that fact is established by the submission protocol asking the
            venue, not by an audit guessing.
        status_code: The HTTP status, where one came back.
        client_order_id: The derived client order id, for a submission. Safe:
            it is this system's own identifier, not the venue's account.
        account_binding_fingerprint: Which paper account, digested.
        operations_run_id: Which operational run, where one applies.
        attempted_at / completed_at: Instants.
    """

    attempt_id: str
    destination: Destination
    operation: OperationClass
    method: str
    route_class: str
    attempted_at: str
    sent: bool = False
    refused: bool = False
    result: str = "UNRESOLVED"
    status_code: int | None = None
    client_order_id: str = ""
    account_binding_fingerprint: str = ""
    operations_run_id: str = ""
    completed_at: str = ""
    detail: str = ""

    #: Field names that must never appear in a serialised audit, asserted by a
    #: test rather than trusted to review.
    forbidden_fields: tuple[str, ...] = field(
        default_factory=lambda: (
            "api_key",
            "secret_key",
            "authorization",
            "headers",
            "cookies",
            "body",
            "account_id",
            "account_number",
        )
    )

    def body(self) -> dict[str, Any]:
        """Return the record's semantic content. Carries no credential."""
        return {
            "version": EXTERNAL_REQUEST_AUDIT_VERSION,
            "attempt_id": self.attempt_id,
            "destination": str(self.destination),
            "operation": str(self.operation),
            "method": self.method,
            "route_class": self.route_class,
            "sent": self.sent,
            "refused": self.refused,
            "result": self.result,
            "status_code": self.status_code,
            "client_order_id": self.client_order_id,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "operations_run_id": self.operations_run_id,
            "attempted_at": self.attempted_at,
            "completed_at": self.completed_at,
            "detail": self.detail,
            "credentials_recorded": False,
        }

    def fingerprint(self) -> str:
        """Digest the record."""
        from ..fingerprint import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the record with its fingerprint."""
        return {**self.body(), "audit_fingerprint": self.fingerprint()}


def route_class_for(path: str) -> str:
    """Reduce a provider path to a route class.

    Identifiers are replaced rather than kept: an order id in an audit filename
    or body is operational data leaking into a record that gets copied around.

    Args:
        path: The provider path.

    Returns:
        The class, e.g. `/v2/orders/{id}`.
    """
    import re

    cleaned = path.split("?")[0].rstrip("/") or "/"
    cleaned = re.sub(
        r"/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        "/{id}",
        cleaned,
    )
    return re.sub(r"/\d+", "/{n}", cleaned)


@dataclass
class RequestAuditStore:
    """Where request audits are written, append-only.

    Attributes:
        root: The directory this store owns.
    """

    root: Path

    def __post_init__(self) -> None:
        """Resolve the directory."""
        from pathlib import Path as _Path

        self.root = _Path(self.root)

    def path_for(self, attempt_id: str) -> Path:
        """Where one attempt's record lives."""
        return self.root / f"{attempt_id}.json"

    def record_attempt(self, record: RequestAuditRecord) -> Path:
        """Persist a pre-dispatch record, or raise so the caller does not send.

        Args:
            record: The attempt.

        Returns:
            Where it was written.

        Raises:
            ExecutionError: `EXTERNAL_REQUEST_AUDIT_FAILED` if the record could
                not be persisted. The caller must treat this as a refusal to
                send: an unaudited mutation is worse than a missed one, because
                a missed mutation is visible and an unaudited one is not.
        """
        from stock_screener.paper_operations import atomic_write_verified

        try:
            atomic_write_verified(self.path_for(record.attempt_id), record.as_json())
        except Exception as exc:
            raise ExecutionError(
                Code.EXTERNAL_REQUEST_AUDIT_FAILED,
                f"the pre-dispatch audit for {record.route_class} could not be "
                f"persisted ({type(exc).__name__}), so the request is not sent. An "
                "unaudited mutation is worse than a missed one: a missed mutation is "
                "visible and an unaudited one is not",
            ) from exc
        return self.path_for(record.attempt_id)

    def record_result(self, record: RequestAuditRecord) -> Path:
        """Append the outcome beside its attempt, never overwriting it.

        Args:
            record: The completed record.

        Returns:
            Where the result was written.
        """
        from stock_screener.paper_operations import atomic_write_verified

        path = self.root / f"{record.attempt_id}.result.json"
        atomic_write_verified(path, record.as_json())
        return path

    def all(self) -> tuple[dict[str, Any], ...]:
        """Every audit record, attempts and results, oldest first."""
        if not self.root.exists():
            return ()
        rows = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
        ]
        return tuple(sorted(rows, key=lambda row: str(row.get("attempted_at", ""))))

    def mutations(self) -> tuple[dict[str, Any], ...]:
        """Every mutation attempt recorded."""
        return tuple(
            row for row in self.all() if str(row.get("operation")) == str(OperationClass.MUTATION)
        )

    def live_mutations_sent(self) -> tuple[dict[str, Any], ...]:
        """Mutations that actually left this process for a live destination.

        Returns:
            The records. This is the evidence the `live_mutations` gate needs,
            and it should always be empty — the transport refuses a live origin
            in its constructor. An empty result from a store that recorded
            every mutation is evidence; an empty result from no store at all
            was the thing P2.4R3 refused to score.
        """
        return tuple(
            row
            for row in self.mutations()
            if str(row.get("destination")) == str(Destination.LIVE) and bool(row.get("sent"))
        )
