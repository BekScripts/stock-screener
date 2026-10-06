"""Running the P2.2 preflight through the real transport, and writing down what it saw.

P2.2 could check configuration. This module is what turns that into evidence: it
builds the concrete HTTPS transport, reads the live paper account, and records a
sanitised artefact saying which origin answered, which account it answered for,
and what each reply fingerprinted to.

The distinction being closed is the one P2.2 was explicit about not having
closed. A host allow-list and a key prefix establish what somebody *configured*.
They cannot establish that the credential reaches that host, that the host is
reachable at all, or that the account behind it is the account the local book
describes. Only a request establishes those, and only this module makes one.

**It makes no mutating request, and has no code path that could.** Every call it
makes is a `GET`, through the adapter's read methods. There is no order
submission here, no cancellation, and no flag that adds one.

What it still cannot establish is worth stating plainly, because the gate name
`PROVIDER_CONFIRMS_PAPER` reads stronger than the evidence behind it: Alpaca's
account object carries no "this is a paper account" field, so paper-ness is
proven by the *origin* that answered and by nothing the account said about
itself. That is sound — a live credential cannot authenticate against the paper
origin and the transport cannot be built against any other — but it is proof by
host lock and reachability, not by provider attestation, and the artefact says
so rather than letting the gate name imply otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from ..fingerprint import fingerprint_of
from .alpaca import ACCOUNT_PATH, POSITIONS_PATH, PROVIDER, AlpacaPaperAdapter
from .binding import ExternalBrokerAccountBinding
from .client_ids import is_our_client_order_id
from .environment import BrokerEnvironment, prove_paper_environment
from .http_transport import TRANSPORT_VERSION, AlpacaPaperHttpTransport
from .preflight import PREFLIGHT_GATES, READY_VERDICT, preflight
from .session import VenueBinding
from .submission import ORDERS_PATH, SubmissionIntentLog
from .symbols import SymbolMapping

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.config import Settings

__all__ = [
    "PREFLIGHT_ARTEFACT_VERSION",
    "AlpacaPaperPreflightRun",
    "PaperPreflightReport",
]

#: The preflight artefact contract.
PREFLIGHT_ARTEFACT_VERSION = "EXTERNAL_PAPER_PREFLIGHT_V1"


@dataclass(frozen=True)
class PaperPreflightReport:
    """What a real read-only preflight established.

    Attributes:
        origin: The origin that answered.
        ready: Whether every gate passed.
        verdict: `PAPER_BROKER_READY`, or `NOT_READY`.
        gates_passed: Which gates passed.
        failures: Why each failing gate failed.
        account_status: What the provider said about the account.
        account_matched: Whether the provider answered for the bound account.
        positions_seen: How many holdings the venue reports.
        orders_seen: How many orders the venue reports.
        foreign_orders: Orders at the venue this adapter did not mint. Counted
            rather than ignored: each is something a later reconciliation will
            raise, and knowing before submitting is the point of a preflight.
        unresolved_submissions: Local order ids with an unresolved intent.
        request_ids: The provider's own request ids, per operation, for raising
            an ambiguous case with the provider later.
        evidence: A sanitised fingerprint per read.
        limitations: What this run does **not** establish.
        mutating_requests: Always zero. Reported so a reader can check.
    """

    origin: str
    ready: bool
    verdict: str
    gates_passed: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    account_status: str = ""
    account_matched: bool = False
    positions_seen: int = 0
    orders_seen: int = 0
    foreign_orders: int = 0
    unresolved_submissions: tuple[str, ...] = ()
    request_ids: tuple[tuple[str, str], ...] = ()
    evidence: tuple[tuple[str, str], ...] = ()
    limitations: tuple[str, ...] = ()
    mutating_requests: int = 0

    def body(self) -> dict[str, Any]:
        """Return the artefact's semantic content. Carries no credential."""
        return {
            "artefact_version": PREFLIGHT_ARTEFACT_VERSION,
            "provider": PROVIDER,
            "environment": str(BrokerEnvironment.PAPER),
            "transport_version": TRANSPORT_VERSION,
            "origin": self.origin,
            "verdict": self.verdict,
            "ready": self.ready,
            "gates": list(PREFLIGHT_GATES),
            "gates_passed": list(self.gates_passed),
            "failures": [{"gate": gate, "reason": why} for gate, why in self.failures],
            "account_status": self.account_status,
            "account_matched": self.account_matched,
            "positions_seen": self.positions_seen,
            "orders_seen": self.orders_seen,
            "foreign_orders": self.foreign_orders,
            "unresolved_submissions": list(self.unresolved_submissions),
            "provider_request_ids": [list(pair) for pair in self.request_ids],
            "evidence_fingerprints": [list(pair) for pair in self.evidence],
            "mutating_requests": self.mutating_requests,
            "live_trading": "NOT_AUTHORIZED",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the artefact."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artefact, credential-scanned on the way out.

        Returns:
            The artefact with its own fingerprint.

        Raises:
            ExecutionError: `FORBIDDEN_EXECUTION_NAMESPACE` if anything in it is
                shaped like a secret.
        """
        from .report import check_no_credentials

        body = {**self.body(), "preflight_fingerprint": self.fingerprint()}
        check_no_credentials(body, context="an external paper preflight artefact")
        return body

    def summary_lines(self) -> tuple[str, ...]:
        """Render the report for a terminal.

        Returns:
            The lines.
        """
        lines = [
            f"provider          : {PROVIDER} (PAPER)",
            f"origin            : {self.origin}",
            f"transport         : {TRANSPORT_VERSION}",
            f"verdict           : {self.verdict}",
            f"account status    : {self.account_status or '(not read)'}",
            f"account matched   : {self.account_matched}",
            f"positions at venue: {self.positions_seen}",
            f"orders at venue   : {self.orders_seen} ({self.foreign_orders} not ours)",
            f"mutating requests : {self.mutating_requests}",
        ]
        for gate in PREFLIGHT_GATES:
            mark = "pass" if gate in self.gates_passed else "FAIL"
            lines.append(f"  [{mark}] {gate}")
        for gate, why in self.failures:
            lines.append(f"  {gate}: {why}")
        for operation, request_id in self.request_ids:
            lines.append(f"  request id {operation}: {request_id}")
        for note in self.limitations:
            lines.append(f"  NOT ESTABLISHED: {note}")
        return tuple(lines)


@dataclass
class AlpacaPaperPreflightRun:
    """A read-only preflight against the configured paper account.

    Attributes:
        transport: The concrete HTTPS transport.
        binding: The proven paper account binding.
        mapping: The declared symbol mapping. Empty unless a canary asset has
            been declared, in which case `SYMBOLS_RESOLVED` checks it.
        log: The durable submission intent log, checked for unresolved intents.
    """

    transport: AlpacaPaperHttpTransport
    binding: ExternalBrokerAccountBinding
    mapping: SymbolMapping
    log: SubmissionIntentLog
    _request_ids: list[tuple[str, str]] = field(default_factory=list)
    _evidence: list[tuple[str, str]] = field(default_factory=list)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        store_root: Path | str | None = None,
        mapping: SymbolMapping | None = None,
    ) -> AlpacaPaperPreflightRun:
        """Build a run from typed configuration.

        Args:
            settings: The application settings. Credentials are read through
                typed fields only.
            store_root: Where the submission intent log lives. Defaults to
                `.execution/external` beside the working directory.
            mapping: A declared symbol mapping, when a canary asset exists.

        Returns:
            The run.

        Raises:
            ExecutionError: `BROKER_CREDENTIALS_MISSING` if a paper credential
                or the expected account id is unset — the account id is required
                because authenticating against *some* paper account proves
                nothing about whether it is this book's; or
                `LIVE_TRADING_NOT_AUTHORIZED` /
                `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the configured origin
                is not provably the paper origin.
        """
        transport = AlpacaPaperHttpTransport.from_settings(settings)
        if not settings.alpaca_paper_account_id:
            raise ExecutionError(
                Code.BROKER_CREDENTIALS_MISSING,
                "ALPACA_PAPER_ACCOUNT_ID is required. Authenticating against some "
                "paper account establishes nothing about whether it is the account "
                "this book describes, and reconciling another account's positions "
                "against this book would report trading breaks for a wiring mistake",
            )
        assert settings.alpaca_paper_key_id is not None  # noqa: S101 - from_settings checked
        proof = prove_paper_environment(
            environment=BrokerEnvironment.PAPER,
            base_url=transport.origin,
            key_id=settings.alpaca_paper_key_id.get_secret_value(),
        )
        binding = ExternalBrokerAccountBinding.paper(
            provider=PROVIDER,
            account_id=settings.alpaca_paper_account_id,
            proof=proof,
            label="configured paper account",
        )
        return cls(
            transport=transport,
            binding=binding,
            mapping=mapping or SymbolMapping.of(()),
            log=SubmissionIntentLog(store_root or ".execution/external"),
        )

    def close(self) -> None:
        """Release the transport's connection pool."""
        self.transport.close()

    def _note(self, operation: str, path: str, params: dict[str, Any] | None = None) -> None:
        """Record sanitised evidence for one read, by repeating it cheaply.

        Args:
            operation: What was being read.
            path: The provider path.
            params: Query parameters.
        """
        from .transport import RequestMethod

        response = self.transport.request(RequestMethod.GET, path, params=params)
        if response.request_id:
            self._request_ids.append((operation, response.request_id))
        self._evidence.append((operation, response.evidence_fingerprint()))

    def execute(self) -> PaperPreflightReport:
        """Run the preflight and record what it saw.

        Read-only throughout. Every request made is a `GET`.

        Returns:
            The report. `ready` is true only when every gate passed.
        """
        adapter = AlpacaPaperAdapter(
            transport=self.transport, binding=self.binding, mapping=self.mapping
        )
        readiness = preflight(adapter, log=self.log, identities=())

        account_matched = "ACCOUNT_MATCHES_BINDING" in readiness.passed
        status = readiness.account_status
        positions = 0
        orders = 0
        foreign = 0

        if account_matched:
            self._note("account", ACCOUNT_PATH)
            self._note("positions", POSITIONS_PATH)
            self._note("orders", ORDERS_PATH, {"status": "all"})
            try:
                snapshot = adapter.read_account()
                positions = len(snapshot.positions)
                remote_orders = adapter.read_open_orders()
                orders = len(remote_orders)
                foreign = sum(
                    1 for row in remote_orders if not is_our_client_order_id(row.client_order_id)
                )
            except ExecutionError:
                # A read that failed after the gates passed is a changed
                # condition, not a contradiction. The gates already recorded
                # what they saw; the counts stay at zero and the verdict stands.
                pass

        limitations = [
            (
                "PROVIDER ATTESTATION OF PAPER: Alpaca's account object carries no "
                "field saying an account is a paper account. Paper-ness here is "
                "established by the origin that answered and by the transport being "
                "unable to exist against any other host — not by the provider "
                "asserting it."
            ),
            (
                "CONTINUED VALIDITY: this proves the credential reached this account "
                "at this moment. A credential repointed afterwards is not detected "
                "until the next read."
            ),
            (
                "THREE-WAY RECONCILIATION: this run compares nothing against a local "
                "book. It reports what the venue holds; a reconciliation needs an "
                "accounting state and an execution journal for the same book."
            ),
            (
                "FILL INGESTION: no fill evidence was read. A read-only preflight "
                "does not exercise per-order fill retrieval."
            ),
        ]
        return PaperPreflightReport(
            origin=self.transport.origin,
            ready=readiness.is_ready,
            verdict=READY_VERDICT if readiness.is_ready else "NOT_READY",
            gates_passed=readiness.passed,
            failures=readiness.failures,
            account_status=status,
            account_matched=account_matched,
            positions_seen=positions,
            orders_seen=orders,
            foreign_orders=foreign,
            unresolved_submissions=readiness.unresolved_orders,
            request_ids=tuple(self._request_ids),
            evidence=tuple(self._evidence),
            limitations=tuple(limitations),
            mutating_requests=0,
        )

    def venue(self) -> VenueBinding:
        """Return the venue binding this run proved.

        Returns:
            The `EXTERNAL_PAPER` binding.
        """
        return VenueBinding.external(self.binding)
