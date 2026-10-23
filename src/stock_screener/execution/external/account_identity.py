"""`EXTERNAL_BROKER_ACCOUNT_IDENTITY_V1` — which account answered, and nothing else.

This contract exists because P2.3.1H2 found two different authorities wearing one
fingerprint. `ExternalBrokerAccountBinding.binding_fingerprint()` digests the
account *and* the capabilities declared beside it, and the operations
environment compared that whole digest to decide whether the venue had answered
for the right account. While the runtime was conservatively whole-share-only
that was harmless. It stopped being harmless the moment a deployment proved
fractional support for a narrower set of instruments: widening the adapter's
capability to send those fractions changed the digest, and the identity check
then reported *"the venue answered for an account this runtime is not bound
to"* — about the correct account.

The question "is this my account?" and the question "may this deployment send a
fractional quantity?" have different subjects, different evidence and different
owners. Conflating them means a capability change can only be expressed by
appearing to change accounts.

So identity is given its own digest, over provider, environment and the account
fingerprint alone. **Capabilities are deliberately absent**, along with the
deployment universe, the symbol mapping, the risk policy and the strategy: a
fingerprint that moved when any of those moved would be the same defect again
under a new name.

Nothing here is a weakening. The account fingerprint is still a salted digest of
the provider's own account id, it is still compared exactly, and a different
account still fails. What changes is only that it stops failing for reasons that
have nothing to do with the account.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from stock_screener.execution.errors import Code, ExecutionError

from .environment import BrokerEnvironment, refuse_live

__all__ = [
    "ACCOUNT_IDENTITY_CONTRACT",
    "ExternalBrokerAccountIdentity",
    "account_identity_of",
]

#: The account-identity contract.
ACCOUNT_IDENTITY_CONTRACT = "EXTERNAL_BROKER_ACCOUNT_IDENTITY_V1"


@dataclass(frozen=True)
class ExternalBrokerAccountIdentity:
    """Which brokerage account, at which venue, in which environment.

    Attributes:
        provider: The venue. `ALPACA` here.
        environment: `PAPER`. A live environment is refused at construction, so
            an identity for one cannot be built to be compared against.
        account_fingerprint_hex: The salted digest of the provider's account id.
            Never the account number: a fingerprint is comparable without being
            disclosable, which is the only property the identity check needs.
        contract: The contract version, carried in the body so a stored identity
            of another shape is refused rather than parsed hopefully.
    """

    provider: str
    environment: BrokerEnvironment
    account_fingerprint_hex: str
    contract: str = ACCOUNT_IDENTITY_CONTRACT

    def __post_init__(self) -> None:
        """Refuse an identity that could not legitimately exist.

        Raises:
            ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` for a live
                environment; `BROKER_ACCOUNT_MISMATCH` if the fingerprint is not
                a sha256 digest, because a short or blank value would compare
                equal to things it does not describe.
        """
        refuse_live(self.environment, context="constructing a broker account identity")
        if len(self.account_fingerprint_hex) != 64:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                "the account fingerprint is not a sha256 digest; it was probably "
                "built from something other than account_fingerprint()",
            )

    def body(self) -> dict[str, Any]:
        """The identity's semantic content, for fingerprinting.

        Deliberately four keys. Every omission is load-bearing: capabilities,
        the deployment universe, the symbol mapping, the risk policy and the
        strategy are all absent, so none of them can move this digest.
        """
        return {
            "contract": self.contract,
            "provider": self.provider,
            "environment": str(self.environment),
            "account_fingerprint": self.account_fingerprint_hex,
        }

    def fingerprint(self) -> str:
        """A digest over the account's identity alone."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the identity as a JSON-safe mapping.

        Carries no credential and no account number — only the fingerprint.
        """
        return {
            **self.body(),
            "account_identity_fingerprint": self.fingerprint(),
            "capability_fields_included": [],
            "scope": (
                "ACCOUNT_IDENTITY_ONLY — proves which brokerage account, venue and "
                "environment answered. It makes no claim about what may be traded, "
                "in what quantity, or by which strategy: an execution capability is "
                "a separate authority with separate evidence."
            ),
            "live_trading": "NOT_IMPLEMENTED_NOT_AUTHORIZED",
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> ExternalBrokerAccountIdentity:
        """Rebuild an identity from its stored body.

        Args:
            body: The stored mapping.

        Returns:
            The identity, which re-digests to the stored fingerprint when
            nothing has been altered.

        Raises:
            ExecutionError: `BROKER_ACCOUNT_MISMATCH` for a body of another
                contract version, an unknown environment, or one missing a
                required field. Read field by field rather than splatted, so a
                field added to the writer and not here fails loudly instead of
                surviving as a silently dropped authority.
        """
        if str(body.get("contract", "")) != ACCOUNT_IDENTITY_CONTRACT:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"this account identity declares contract {body.get('contract')!r} "
                f"and this build reads {ACCOUNT_IDENTITY_CONTRACT}",
            )
        try:
            environment = BrokerEnvironment(str(body["environment"]))
        except (KeyError, ValueError) as exc:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"this account identity names environment {body.get('environment')!r}, "
                "which is not a known environment. An unrecognised environment is "
                "refused rather than defaulted",
            ) from exc
        try:
            return cls(
                provider=str(body["provider"]),
                environment=environment,
                account_fingerprint_hex=str(body["account_fingerprint"]),
                contract=str(body["contract"]),
            )
        except KeyError as exc:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"this account identity omits {exc.args[0]!r}, which the contract "
                "requires. A missing identity field is not an empty one",
            ) from exc

    def require_matches(self, other: ExternalBrokerAccountIdentity, *, context: str) -> None:
        """Refuse unless another identity describes exactly this account.

        Args:
            other: The identity the venue's answer produced.
            context: What was being proven, for the message.

        Raises:
            ExecutionError: `BROKER_ACCOUNT_MISMATCH` if the provider, the
                environment or the account differs. Each is named separately,
                because "wrong account" and "right account at the wrong venue"
                need different operator responses.
        """
        if self.provider != other.provider:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"{context}: this runtime is bound to provider {self.provider!r} and "
                f"the answer came from {other.provider!r}. A provider change is not a "
                "deployment change and is never inferred",
            )
        if self.environment is not other.environment:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"{context}: this runtime is bound to {self.environment} and the "
                f"answer came from {other.environment}",
            )
        if self.account_fingerprint_hex != other.account_fingerprint_hex:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"{context}: the venue answered for an account this runtime is not "
                "bound to. Reconciling another account's positions against this book "
                "would report trading breaks for a wiring mistake",
            )


def account_identity_of(binding: Any) -> ExternalBrokerAccountIdentity:
    """The account identity an existing broker binding describes.

    Derived from the binding rather than stored beside it, so there is one
    source for which account a runtime talks to. It reads only the three fields
    identity is made of, which is why a binding whose capabilities have been
    widened for a deployment still produces the same identity.

    Args:
        binding: An `ExternalBrokerAccountBinding`.

    Returns:
        Its account identity.
    """
    return ExternalBrokerAccountIdentity(
        provider=str(binding.provider),
        environment=binding.environment,
        account_fingerprint_hex=str(binding.account_fingerprint_hex),
    )
