"""Deciding whether this process may submit anything, before it submits anything.

`prove_paper_environment` checks configuration, which is cheap and which no
amount of care makes sufficient: a host name and a key prefix are strings
somebody typed, and a credential can point anywhere its owner has since
repointed it. Preflight is where that gap closes, by asking the venue itself —
with a read — who it thinks is calling.

Seven gates, all required, every one read-only:

1. the environment is provably paper by configuration;
2. the account answers at all;
3. the account the provider answers for is the one the binding names;
4. the provider's own account record says it is a paper account;
5. the venue is not refusing trades;
6. every identity this run intends to trade has a declared venue symbol;
7. no submission intent from an earlier run is still unresolved.

The seventh is the one that is easy to leave out and expensive to omit. An
unresolved intent is an order that may be working at the venue with nothing local
tracking it. Planning new orders while one exists sizes them against a book that
is missing a position, so readiness is withheld until it is settled — and
settling it is a decision, not something preflight does on its own.

Nothing here mutates. The verdict is `PAPER_BROKER_READY` or it is not, and a
`NOT_READY` verdict carries every reason rather than the first.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from .alpaca import ACCOUNT_PATH
from .paper_proof import ProviderPaperEnvironmentProof

if TYPE_CHECKING:
    from collections.abc import Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

    from .alpaca import AlpacaPaperAdapter
    from .environment import EnvironmentProof
    from .models import (
        ExternalOrderSnapshot,
        ExternalPaperAccountSnapshot,
        ExternalPosition,
    )
    from .submission import SubmissionIntentLog
    from .transport import ExternalReadEvidence

__all__ = [
    "PREFLIGHT_GATES",
    "READY_VERDICT",
    "PaperBrokerReadiness",
    "preflight",
]

#: The verdict a fully passing preflight produces. Named so a caller asserts the
#: string rather than a truthy object.
READY_VERDICT = "PAPER_BROKER_READY"

#: Every gate, in the order they are checked. Ordered so the cheapest and most
#: dangerous failure — a configuration that is not paper — is refused before any
#: request is made.
PREFLIGHT_GATES: tuple[str, ...] = (
    "ENVIRONMENT_PROVEN_PAPER",
    "ACCOUNT_REACHABLE",
    "ACCOUNT_MATCHES_BINDING",
    "PROVIDER_CONFIRMS_PAPER",
    "TRADING_NOT_BLOCKED",
    "SYMBOLS_RESOLVED",
    "NO_UNRESOLVED_SUBMISSIONS",
)


@dataclass(frozen=True)
class PaperBrokerReadiness:
    """What preflight established.

    Attributes:
        passed: Which gates passed.
        failures: Why each failing gate failed, in gate order.
        proof: The environment proof, upgraded to fully certified when the
            account answered.
        unresolved_orders: Local order ids with an unresolved submission intent.
        account_status: What the provider said about the account.
        paper_proof: The evidence that satisfied `PROVIDER_CONFIRMS_PAPER`, when
            one was supplied. Recorded so a reader can check what the gate was
            satisfied *by* rather than take the gate's word for it.
        account: The account read, with its own evidence. Returned so a caller
            reports from the response the gates were decided on rather than
            re-reading — which is what produced an artefact whose fingerprints
            described responses it had not reported.
        positions: The holdings read, with its evidence.
        open_orders: The working orders read, with its evidence. Working orders
            only; terminal history is not fetched, because a closed order from
            an earlier session is not a reconciliation break.
        mutations_made: Always zero. Reported so a test asserts it.
    """

    passed: tuple[str, ...] = ()
    failures: tuple[tuple[str, str], ...] = ()
    proof: EnvironmentProof | None = None
    unresolved_orders: tuple[str, ...] = ()
    account_status: str = ""
    paper_proof: ProviderPaperEnvironmentProof | None = None
    account: ExternalReadEvidence[ExternalPaperAccountSnapshot] | None = None
    positions: ExternalReadEvidence[tuple[ExternalPosition, ...]] | None = None
    open_orders: ExternalReadEvidence[tuple[ExternalOrderSnapshot, ...]] | None = None
    mutations_made: int = field(default=0)

    @property
    def verdict(self) -> str:
        """`PAPER_BROKER_READY`, or the reason it is not."""
        return READY_VERDICT if self.is_ready else "NOT_READY"

    @property
    def is_ready(self) -> bool:
        """Whether every gate passed."""
        return not self.failures and set(self.passed) == set(PREFLIGHT_GATES)

    def require_ready(self) -> None:
        """Refuse to continue unless every gate passed.

        Raises:
            ExecutionError: `EXTERNAL_SUBMISSION_BLOCKED` naming every failure.
                Every one, not the first: fixing one gate and rediscovering the
                next is how a configuration takes four runs to correct.
        """
        if self.is_ready:
            return
        detail = "; ".join(f"{gate}: {why}" for gate, why in self.failures) or "gates did not run"
        raise ExecutionError(
            Code.EXTERNAL_SUBMISSION_BLOCKED,
            f"the paper venue is not ready, so nothing will be submitted. {detail}",
        )

    def as_json(self) -> dict[str, Any]:
        """Return the readiness as a JSON-safe mapping. Carries no credential."""
        return {
            "verdict": self.verdict,
            "ready": self.is_ready,
            "gates": list(PREFLIGHT_GATES),
            "passed": list(self.passed),
            "failures": [{"gate": gate, "reason": why} for gate, why in self.failures],
            "proof": self.proof.as_json() if self.proof else None,
            "unresolved_orders": list(self.unresolved_orders),
            "account_status": self.account_status,
            "paper_proof": self.paper_proof.as_json() if self.paper_proof else None,
            "mutations_made": self.mutations_made,
            "live_authorized": False,
        }


def preflight(
    adapter: AlpacaPaperAdapter,
    *,
    log: SubmissionIntentLog,
    origin: str,
    identities: Sequence[SecurityIdentity] = (),
    now: str = "",
) -> PaperBrokerReadiness:
    """Decide whether this process may submit to the paper venue.

    Read-only, and **exactly one request per logical endpoint**: the account,
    the holdings, and the working orders. The evidence for each is returned on
    the result, so a caller reports from the same responses these gates were
    decided on. Nothing here re-reads for a fingerprint.

    Every failure is collected rather than raised, so one call names everything
    that needs fixing.

    Args:
        adapter: The provider adapter, over a proven paper binding.
        log: The durable intent log, checked for unresolved submissions.
        origin: The origin the transport actually reached. Required because the
            paper proof binds to it, and the `BrokerTransport` protocol
            deliberately declares no attributes to read it from.
        identities: The securities this run intends to trade. Checked against
            the declared symbol mapping now, because discovering an unmapped
            identity mid-plan means a plan that is half submitted.
        now: When the run is being made, ISO-8601. Operational metadata only.

    Returns:
        What was established, ready or not, with the read evidence attached.
    """
    passed: list[str] = []
    failures: list[tuple[str, str]] = []
    binding = adapter.binding
    environment_proof = binding.proof

    if environment_proof.is_proven:
        passed.append("ENVIRONMENT_PROVEN_PAPER")
    else:
        failures.append(
            (
                "ENVIRONMENT_PROVEN_PAPER",
                "; ".join(environment_proof.failed_checks()) or "the proof does not stand",
            )
        )

    account: ExternalReadEvidence[ExternalPaperAccountSnapshot] | None = None
    positions: ExternalReadEvidence[tuple[ExternalPosition, ...]] | None = None
    open_orders: ExternalReadEvidence[tuple[ExternalOrderSnapshot, ...]] | None = None
    paper_proof: ProviderPaperEnvironmentProof | None = None
    account_status = ""

    try:
        account = adapter.account_evidence(received_at=now)
    except ExecutionError as exc:
        gate = (
            "ACCOUNT_MATCHES_BINDING"
            if exc.code == Code.BROKER_ACCOUNT_MISMATCH
            else "PROVIDER_CONFIRMS_PAPER"
            if exc.code == Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER
            else "ACCOUNT_REACHABLE"
        )
        failures.append((gate, str(exc)))
    else:
        snapshot = account.value
        account_status = snapshot.status
        passed.extend(("ACCOUNT_REACHABLE", "ACCOUNT_MATCHES_BINDING"))

        # The paper proof is built from this same response. It cannot be built
        # from configuration: the gate it replaces checked an `is_paper` field
        # the provider does not send, so it could never fail and reported
        # attestation nobody had gathered.
        try:
            paper_proof = ProviderPaperEnvironmentProof.from_account_evidence(
                provider=adapter.binding.provider,
                origin=origin,
                endpoint=ACCOUNT_PATH,
                account_binding_fingerprint=binding.binding_fingerprint(),
                account_status=snapshot.status,
                evidence=account,
                verified_at=now,
            )
        except ExecutionError as exc:
            failures.append(("PROVIDER_CONFIRMS_PAPER", str(exc)))
        else:
            passed.append("PROVIDER_CONFIRMS_PAPER")

        if snapshot.trading_blocked:
            failures.append(
                (
                    "TRADING_NOT_BLOCKED",
                    f"the venue is refusing trades (status {snapshot.status!r})",
                )
            )
        else:
            passed.append("TRADING_NOT_BLOCKED")

        # The configuration-level proof is upgraded to fully certified now that a
        # read-only call has confirmed the account. Kept alongside the stronger
        # ProviderPaperEnvironmentProof rather than replaced by it: this one says
        # "the configured checks held and the account answered", which is a
        # different and weaker claim, and collapsing the two would lose the
        # distinction P1.4 onwards has been careful to keep.
        environment_proof = replace(
            environment_proof,
            account_reachable=True,
            account_status=snapshot.status,
            checks=(*environment_proof.checks, "the paper account answered a read-only request"),
        )

        try:
            positions = adapter.positions_evidence(received_at=now)
            open_orders = adapter.open_orders_evidence(received_at=now)
        except ExecutionError as exc:
            failures.append(("ACCOUNT_REACHABLE", str(exc)))

    unmapped: list[str] = []
    for identity in identities:
        try:
            adapter.mapping.symbol_for(identity)
        except ExecutionError:
            unmapped.append(identity.label)
    if unmapped:
        failures.append(
            (
                "SYMBOLS_RESOLVED",
                f"no declared venue symbol for {unmapped}; the display ticker is "
                "deliberately not used as a fallback",
            )
        )
    else:
        passed.append("SYMBOLS_RESOLVED")

    unresolved = log.unresolved()
    if unresolved:
        failures.append(
            (
                "NO_UNRESOLVED_SUBMISSIONS",
                f"{len(unresolved)} submission intent(s) are unresolved "
                f"({[intent.order_id for intent in unresolved]}). Each may be a "
                "working order the book does not know about, and planning around "
                "one sizes the next trade against money that is not there",
            )
        )
    else:
        passed.append("NO_UNRESOLVED_SUBMISSIONS")

    ordered_failures = tuple(sorted(failures, key=lambda row: PREFLIGHT_GATES.index(row[0])))
    return PaperBrokerReadiness(
        passed=tuple(gate for gate in PREFLIGHT_GATES if gate in set(passed)),
        failures=ordered_failures,
        proof=environment_proof,
        unresolved_orders=tuple(intent.order_id for intent in unresolved),
        account_status=account_status,
        paper_proof=paper_proof,
        account=account,
        positions=positions,
        open_orders=open_orders,
    )
