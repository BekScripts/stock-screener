"""Which remote account this process is allowed to touch, and what it can do.

Two separate refusals live here, and conflating them is how a paper adapter
eventually sends an order somewhere nobody expected.

**The account.** A credential identifies an account, and the account it
identifies is not necessarily the one the local book describes. A binding names
the account the book belongs to, and every reply the adapter reads is checked
against it — a snapshot from another account is `BROKER_ACCOUNT_MISMATCH`, not
data. The account is held as a salted fingerprint rather than as its number,
because the binding is written into reports and the account number is not ours
to publish.

**The capabilities.** A provider either supports a thing or it does not, and the
adapter refuses by name rather than approximating. A fractional quantity sent to
a venue that rounds is not "nearly right": it is a different order, and the
difference lands in the book as a quantity nobody authorised.

Nothing here reaches a network. A binding is a statement of intent, checked
before a transport is ever reached, and `prove_paper_environment` has already run
by the time one exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.fingerprint import fingerprint_of

from .environment import BrokerEnvironment, EnvironmentProof, refuse_live

if TYPE_CHECKING:
    from decimal import Decimal

__all__ = [
    "ACCOUNT_FINGERPRINT_SALT",
    "ADAPTER_VERSION",
    "BrokerCapabilities",
    "ExternalBrokerAccountBinding",
    "account_fingerprint",
]

#: The adapter contract. Bumped when what the adapter *sends* or how it reads a
#: reply changes, independently of the engine version, because a provider can
#: change its API without this project changing its fill semantics.
ADAPTER_VERSION = "EXTERNAL_PAPER_ADAPTER_V1"

#: Domain separation for the account digest. A bare digest of an account number
#: could be confirmed by anyone who guessed the number; a salted one tells a
#: reader only whether two bindings name the same account, which is the only
#: question the binding needs to answer.
ACCOUNT_FINGERPRINT_SALT = "STOCK_SCREENER_EXTERNAL_PAPER_ACCOUNT_V1"


def account_fingerprint(account_id: str) -> str:
    """Digest a remote account id so it can be recorded without being disclosed.

    Args:
        account_id: The provider's account identifier.

    Returns:
        A salted hex digest, stable across runs so two bindings can be compared.

    Raises:
        ExecutionError: `BROKER_ACCOUNT_MISMATCH` if the id is blank — an empty
            account id would fingerprint consistently and match everything.
    """
    cleaned = account_id.strip()
    if not cleaned:
        raise ExecutionError(
            Code.BROKER_ACCOUNT_MISMATCH,
            "a remote account id is required; a blank id would match every account",
        )
    return hashlib.sha256(f"{ACCOUNT_FINGERPRINT_SALT}:{cleaned}".encode()).hexdigest()


@dataclass(frozen=True)
class BrokerCapabilities:
    """What a provider's paper venue actually supports.

    Declared rather than discovered, so an order that the venue would silently
    reshape is refused locally before it is sent. Every field defaults to the
    conservative answer: an undeclared capability is unsupported.

    Attributes:
        order_types: Supported order types.
        time_in_force: Supported time-in-force values.
        fractional_quantities: Whether a non-integer quantity may be sent.
        short_selling: Whether a sell may exceed the held quantity.
        extended_hours: Whether a session outside regular hours may be requested.
        notional_orders: Whether an order may be denominated in currency.
        cancel_supported: Whether an open order may be cancelled.
        client_order_id_limit: The longest client id the provider accepts.
    """

    order_types: frozenset[str] = frozenset({"MARKET"})
    time_in_force: frozenset[str] = frozenset({"DAY"})
    fractional_quantities: bool = False
    short_selling: bool = False
    extended_hours: bool = False
    notional_orders: bool = False
    cancel_supported: bool = True
    client_order_id_limit: int = 48

    def require_order_type(self, order_type: str) -> None:
        """Refuse an order type the venue does not support.

        Args:
            order_type: The requested type.

        Raises:
            ExecutionError: `BROKER_CAPABILITY_UNSUPPORTED` if unsupported.
        """
        if order_type not in self.order_types:
            raise ExecutionError(
                Code.BROKER_CAPABILITY_UNSUPPORTED,
                f"the venue does not support order type {order_type!r}; it supports "
                f"{sorted(self.order_types)}. An unsupported type is a different "
                "order, not a worse one",
            )

    def require_time_in_force(self, time_in_force: str) -> None:
        """Refuse a time-in-force the venue does not support.

        Args:
            time_in_force: The requested value.

        Raises:
            ExecutionError: `BROKER_CAPABILITY_UNSUPPORTED` if unsupported.
        """
        if time_in_force not in self.time_in_force:
            raise ExecutionError(
                Code.BROKER_CAPABILITY_UNSUPPORTED,
                f"the venue does not support time-in-force {time_in_force!r}; it "
                f"supports {sorted(self.time_in_force)}",
            )

    def require_quantity(self, quantity: Decimal) -> None:
        """Refuse a quantity the venue would reshape.

        A venue that rounds a fractional quantity does not fill the order that
        was authorised, and the difference arrives in the book as a position
        nobody sized.

        Args:
            quantity: The requested quantity.

        Raises:
            ExecutionError: `BROKER_FRACTIONAL_NOT_SUPPORTED` if the quantity is
                fractional and the venue does not support that.
        """
        if self.fractional_quantities:
            return
        if quantity != quantity.to_integral_value():
            raise ExecutionError(
                Code.BROKER_FRACTIONAL_NOT_SUPPORTED,
                f"the venue trades whole shares only and would reshape {quantity}; "
                "a rounded quantity is not the authorised order",
            )

    def as_json(self) -> dict[str, Any]:
        """Return the capabilities as a JSON-safe mapping."""
        return {
            "order_types": sorted(self.order_types),
            "time_in_force": sorted(self.time_in_force),
            "fractional_quantities": self.fractional_quantities,
            "short_selling": self.short_selling,
            "extended_hours": self.extended_hours,
            "notional_orders": self.notional_orders,
            "cancel_supported": self.cancel_supported,
            "client_order_id_limit": self.client_order_id_limit,
        }


@dataclass(frozen=True)
class ExternalBrokerAccountBinding:
    """The one remote account this adapter may read and submit to.

    Attributes:
        provider: Which provider, for the record.
        environment: Always `PAPER`. A `LIVE` binding cannot be constructed.
        account_fingerprint_hex: The salted account digest.
        capabilities: What the venue supports.
        base_url: The proven paper host's base URL.
        proof: The environment proof this binding was built on.
        adapter_version: The adapter contract.
        label: A human name for reports. Never an account number.
    """

    provider: str
    account_fingerprint_hex: str
    capabilities: BrokerCapabilities
    base_url: str
    proof: EnvironmentProof
    environment: BrokerEnvironment = BrokerEnvironment.PAPER
    adapter_version: str = ADAPTER_VERSION
    label: str = ""

    def __post_init__(self) -> None:
        """Refuse a binding that is not provably paper.

        Raises:
            ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` if the environment is
                anything but paper; `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` if the
                proof does not stand, or if it describes a different host than
                the one the binding would submit to.
        """
        refuse_live(self.environment, context="constructing a broker account binding")
        if not self.proof.is_proven:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                "a binding requires a standing paper-environment proof; "
                f"this proof failed its checks ({self.proof.failed_checks()})",
            )
        if self.proof.host not in self.base_url:
            raise ExecutionError(
                Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
                f"the proof certifies host {self.proof.host!r} but the binding would "
                f"submit to {self.base_url!r}; a proof of one host authorises no other",
            )
        if len(self.account_fingerprint_hex) != 64:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                "the account fingerprint is not a sha256 digest; it was probably "
                "built from something other than account_fingerprint()",
            )

    @property
    def is_live(self) -> bool:
        """Always false. Present so a caller can assert it rather than assume."""
        return self.environment is BrokerEnvironment.LIVE

    def require_account(self, account_id: str, *, context: str) -> None:
        """Refuse a reply that describes a different account.

        The check that matters most in this module. A credential rotated to
        another account, a copied configuration, a second paper account used for
        an unrelated experiment — each would otherwise have its positions
        reconciled against this book, and the break would look like a trading
        error rather than a wiring error.

        Args:
            account_id: The account id the provider reported.
            context: What was being read, for the message.

        Raises:
            ExecutionError: `BROKER_ACCOUNT_MISMATCH` if it is not this account.
        """
        if account_fingerprint(account_id) != self.account_fingerprint_hex:
            raise ExecutionError(
                Code.BROKER_ACCOUNT_MISMATCH,
                f"{context}: the provider answered for an account this binding does "
                "not name. Reconciling another account's positions against this book "
                "would report trading breaks for a wiring mistake",
            )

    def binding_fingerprint(self) -> str:
        """Digest the binding, so an artefact can name the account it used.

        Returns:
            The digest. Contains no credential and no account number.
        """
        return fingerprint_of(self.body())

    def body(self) -> dict[str, Any]:
        """Return the binding's semantic content.

        Returns:
            A JSON-safe mapping. Carries the account *fingerprint*, never the
            account id, and no credential of any kind.
        """
        return {
            "provider": self.provider,
            "environment": str(self.environment),
            "account_fingerprint": self.account_fingerprint_hex,
            "adapter_version": self.adapter_version,
            "base_url": self.base_url,
            "capabilities": self.capabilities.as_json(),
            "live_authorized": False,
        }

    def as_json(self) -> dict[str, Any]:
        """Return the binding for a report, including its own fingerprint."""
        return {
            **self.body(),
            "label": self.label,
            "binding_fingerprint": self.binding_fingerprint(),
            "proof": self.proof.as_json(),
        }

    @classmethod
    def paper(
        cls,
        *,
        provider: str,
        account_id: str,
        proof: EnvironmentProof,
        capabilities: BrokerCapabilities | None = None,
        label: str = "",
    ) -> ExternalBrokerAccountBinding:
        """Build a paper binding from a standing proof.

        The only constructor a caller should use. There is deliberately no
        `live()` counterpart, and adding one would have to defeat `refuse_live`
        in `__post_init__` to be of any use.

        Args:
            provider: Which provider.
            account_id: The provider's account id, fingerprinted immediately.
            proof: A standing paper proof.
            capabilities: What the venue supports. Conservative by default.
            label: A human name for reports.

        Returns:
            The binding.
        """
        return cls(
            provider=provider,
            account_fingerprint_hex=account_fingerprint(account_id),
            capabilities=capabilities or BrokerCapabilities(),
            base_url=f"https://{proof.host}",
            proof=proof,
            label=label,
        )
