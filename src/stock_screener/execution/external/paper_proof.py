"""Proof that this really is a paper account, from evidence rather than by name.

P2.2 had a gate called `PROVIDER_CONFIRMS_PAPER` whose check was "fail if the
account says `is_paper` is false". Running it against the real API showed the
problem: Alpaca's account object has no `is_paper` field at all — paper and live
are distinguished only by which host answers — so the gate could never fail. It
passed by *absence*, and reported provider attestation nobody had gathered.

A gate that cannot fail is worse than no gate. It occupies the place where a
check should be, and reads in a report as though something was verified.

So this module replaces it with something that can only exist as the result of a
successful external interaction. Constructing a proof requires a response: the
approved paper origin answered, the credential authenticated, and the account it
answered for is the one the binding names. Configuration cannot produce one —
there is no constructor that takes settings — and that is the whole design.

What the proof does **not** claim is stated in it rather than left to be
assumed. It is not provider attestation, because no such field exists. It is
proof by host lock plus reachability plus account identity, and it is a
statement about one moment: a credential repointed afterwards is not detected
until the next read.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..errors import Code, ExecutionError
from ..fingerprint import fingerprint_of
from .environment import LIVE_HOSTS, PAPER_HOSTS, BrokerEnvironment

if TYPE_CHECKING:
    from .transport import ExternalReadEvidence

__all__ = [
    "PAPER_PROOF_VERSION",
    "ProviderPaperEnvironmentProof",
    "origin_fingerprint",
]

#: The proof contract.
PAPER_PROOF_VERSION = "PROVIDER_PAPER_ENVIRONMENT_PROOF_V1"

#: Domain separation for the origin digest.
_ORIGIN_SALT = "STOCK_SCREENER_EXTERNAL_PAPER_ORIGIN_V1"


def origin_fingerprint(origin: str) -> str:
    """Digest an origin so a proof can name it without re-stating a URL.

    Args:
        origin: The origin that answered.

    Returns:
        A salted hex digest. The origin itself is not secret and is recorded in
        full beside this; the digest exists so two proofs can be compared as
        values.
    """
    return hashlib.sha256(f"{_ORIGIN_SALT}:{origin.strip().lower()}".encode()).hexdigest()


@dataclass(frozen=True)
class ProviderPaperEnvironmentProof:
    """Evidence that a specific paper account answered a specific request.

    There is deliberately no way to build one of these from configuration. The
    only constructor takes read evidence, which only a successful authenticated
    response produces.

    Attributes:
        provider: Which provider answered.
        origin: The origin that answered. Validated to be the approved paper
            origin.
        origin_fingerprint_hex: That origin, digested.
        endpoint: Which endpoint established the proof.
        account_binding_fingerprint: Which account binding it matched. Carries
            no account number.
        account_status: What the provider said about the account.
        response_evidence_fingerprint: The digest of the response that
            established this, taken from the same read whose values were used.
        request_id: The provider's own request id, where it sent one.
        status_code: The status that carried the proof.
        verified_at: When the proof was taken. Operational metadata, excluded
            from the fingerprint so the same observation digests identically.
        limitations: What this proof does not establish.
    """

    provider: str
    origin: str
    origin_fingerprint_hex: str
    endpoint: str
    account_binding_fingerprint: str
    response_evidence_fingerprint: str
    account_status: str = ""
    request_id: str | None = None
    status_code: int | None = None
    verified_at: str = ""
    limitations: tuple[str, ...] = field(default_factory=tuple)

    @property
    def environment(self) -> BrokerEnvironment:
        """Always `PAPER`. A proof cannot exist for anything else."""
        return BrokerEnvironment.PAPER

    @property
    def is_proven(self) -> bool:
        """Whether this proof stands.

        True whenever the object exists with its required evidence present: the
        constructor refuses to build one otherwise, so this is a re-check rather
        than the decision.
        """
        return bool(
            self.origin
            and self.response_evidence_fingerprint
            and self.account_binding_fingerprint
            and self.origin.lower().removeprefix("https://") in PAPER_HOSTS
        )

    @classmethod
    def from_account_evidence(
        cls,
        *,
        provider: str,
        origin: str,
        endpoint: str,
        account_binding_fingerprint: str,
        evidence: ExternalReadEvidence[Any],
        account_status: str = "",
        verified_at: str = "",
    ) -> ProviderPaperEnvironmentProof:
        """Build a proof from a successful, account-matched account read.

        Args:
            provider: Which provider answered.
            origin: The origin the transport actually reached.
            endpoint: Which endpoint answered.
            account_binding_fingerprint: The binding the account matched.
            evidence: The read evidence. Its fingerprint is the proof's, so the
                digest recorded here is of the response whose values were used.
            account_status: What the provider said about the account.
            verified_at: When the proof was taken.

        Returns:
            The proof.

        Raises:
            ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` if the origin is a
                live or brokerage host — a successful response from there is not
                a paper proof, it is an incident.
                `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the origin is not the
                approved paper origin, if the response did not succeed, or if no
                account binding was matched.
        """
        host = origin.strip().lower().removeprefix("https://").removeprefix("http://").split("/")[0]
        if host in LIVE_HOSTS:
            raise ExecutionError(
                Code.LIVE_TRADING_NOT_AUTHORIZED,
                f"a response from {host!r} cannot establish a paper proof. A "
                "successful authenticated reply from a live host is not evidence of "
                "paper trading",
            )
        if host not in PAPER_HOSTS:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                f"{host!r} is not the approved paper origin ({sorted(PAPER_HOSTS)}), "
                "so a response from it proves nothing about the paper environment",
            )
        if evidence.status_code is None or not (200 <= evidence.status_code < 300):
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                f"the account read answered {evidence.status_code}; only a successful "
                "authenticated response establishes the paper environment",
            )
        if not evidence.fingerprint:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "the account read carries no response evidence, so there is nothing "
                "for the proof to bind to",
            )
        if not account_binding_fingerprint:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "a paper proof must name the account binding the response matched; "
                "proving that *some* paper account answered establishes nothing about "
                "whether it is this book's",
            )
        return cls(
            provider=provider,
            origin=f"https://{host}",
            origin_fingerprint_hex=origin_fingerprint(host),
            endpoint=endpoint,
            account_binding_fingerprint=account_binding_fingerprint,
            response_evidence_fingerprint=evidence.fingerprint,
            account_status=account_status,
            request_id=evidence.request_id,
            status_code=evidence.status_code,
            verified_at=verified_at,
            limitations=(
                (
                    "NOT PROVIDER ATTESTATION: the provider's account object carries "
                    "no field asserting an account is a paper account. This proof is "
                    "host lock plus reachability plus account identity — which is "
                    "sound, and is not the provider saying so."
                ),
                (
                    "ONE MOMENT ONLY: this establishes that the credential reached "
                    "this account at this instant. A credential repointed afterwards "
                    "is not detected until the next read."
                ),
            ),
        )

    def body(self) -> dict[str, Any]:
        """Return the proof's semantic content. Carries no credential."""
        return {
            "proof_version": PAPER_PROOF_VERSION,
            "provider": self.provider,
            "environment": str(self.environment),
            "origin": self.origin,
            "origin_fingerprint": self.origin_fingerprint_hex,
            "endpoint": self.endpoint,
            "status_code": self.status_code,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "account_status": self.account_status,
            "response_evidence_fingerprint": self.response_evidence_fingerprint,
            "request_id": self.request_id,
            "proven": self.is_proven,
            "live_trading": "NOT_AUTHORIZED",
            "limitations": list(self.limitations),
        }

    def fingerprint(self) -> str:
        """Digest the proof. Excludes `verified_at` so it is a value."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the proof with its own fingerprint and the time it was taken."""
        return {
            **self.body(),
            "verified_at": self.verified_at,
            "proof_fingerprint": self.fingerprint(),
        }
