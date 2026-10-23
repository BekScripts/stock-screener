"""The paper-only gate. Live trading is refused before a client can exist.

This is the most safety-critical module in the programme so far, because it is
the first one whose failure would move real money. The design principle is that
*live trading must be structurally impossible rather than merely disabled*: a
flag that could be flipped, a config value that could be overridden, or an
endpoint that could be substituted would all make "paper only" a matter of
nobody having flipped it yet.

So three things hold at once:

**There is no executable live path.** `BrokerEnvironment.LIVE` exists only so a
later phase cannot claim the enum was silently widened — and every function that
could reach a network refuses it with `LIVE_TRADING_NOT_AUTHORIZED` before a
client is constructed, before credentials are read, and before any socket could
open.

**Configuration is not proof.** `environment = PAPER` in a settings object is a
claim, not evidence. The endpoint host is checked against an allow-list of known
paper hosts, the key id is checked against the paper prefix the provider
documents, and a live host is refused by name. A configuration that merely
*says* paper while pointing at `api.alpaca.markets` is
`BROKER_ENVIRONMENT_NOT_PROVEN_PAPER`.

**There is no fallback.** No `USE_LIVE`, no `mode=live`, no `--live`, no
`fallback_to_live`. Nothing in this package reads an environment variable that
could unlock live trading, and a test asserts that by scanning the source.

What this cannot do, stated plainly: host and key-prefix checks establish that
the *configured endpoint and credential* are the provider's paper ones. They
cannot prove the provider has not changed what that host does. That is the
strongest proof the provider's API makes available, and it is recorded as such
rather than described as certainty.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from urllib.parse import urlparse

from stock_screener.execution.errors import Code, ExecutionError

__all__ = [
    "LIVE_HOSTS",
    "PAPER_HOSTS",
    "PAPER_KEY_PREFIXES",
    "BrokerEnvironment",
    "EnvironmentProof",
    "prove_paper_environment",
    "refuse_live",
]


class BrokerEnvironment(StrEnum):
    """Which brokerage environment an adapter addresses.

    `LIVE` is representable and never executable. It exists so a later phase
    adding live support has to do so deliberately and visibly, rather than by
    widening an enum that only had one member — and so that a configuration
    naming it is refused by name instead of falling through to a default.
    """

    PAPER = "PAPER"
    """A provider's paper environment. The only environment P2.2 will address."""

    LIVE = "LIVE"
    """Real money. Refused everywhere in P2.2, before any client is built."""

    @property
    def is_authorized(self) -> bool:
        """Whether this engine may address the environment at all."""
        return self is BrokerEnvironment.PAPER


#: Hosts known to serve a provider's paper environment. An allow-list rather
#: than a deny-list: an unrecognised host is refused, because the failure mode of
#: guessing wrong is a live order.
PAPER_HOSTS: frozenset[str] = frozenset({"paper-api.alpaca.markets"})

#: Hosts known to serve real money. Named so a live endpoint is refused with the
#: reason rather than merely failing the allow-list.
LIVE_HOSTS: frozenset[str] = frozenset({"api.alpaca.markets", "broker-api.alpaca.markets"})

#: Key-id prefixes the provider documents as paper. Alpaca's paper keys begin
#: `PK` and its live keys `AK`, which the repository's own `.env.example` records.
PAPER_KEY_PREFIXES: frozenset[str] = frozenset({"PK"})

#: Live key-id prefixes, refused by name.
LIVE_KEY_PREFIXES: frozenset[str] = frozenset({"AK"})


def refuse_live(environment: BrokerEnvironment | str, *, context: str) -> None:
    """Refuse anything but paper, before a client or credential is touched.

    Called at the top of every entry point that could reach a network. Placing
    it there rather than at the request site is deliberate: a refusal that
    happens after a client is built has already read credentials, and one that
    happens at the request site has already been reached by code that believed
    live was possible.

    Args:
        environment: The environment being requested.
        context: What was being attempted, for the message.

    Raises:
        ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` for anything that is not
            `PAPER` — including an unrecognised string, since a typo must not
            fall through to a default.
    """
    resolved = (
        environment
        if isinstance(environment, BrokerEnvironment)
        else _parse_environment(environment, context=context)
    )
    if not resolved.is_authorized:
        raise ExecutionError(
            Code.LIVE_TRADING_NOT_AUTHORIZED,
            f"{context} requests the {resolved} environment. This engine has no "
            "live-trading authority and no executable live path: there is no "
            "flag, config value or endpoint that unlocks one. The refusal happens "
            "here, before any client is constructed and before any credential is "
            "read, so no live request can be made even accidentally.",
        )


def _parse_environment(value: str, *, context: str) -> BrokerEnvironment:
    """Resolve an environment name, refusing anything unrecognised.

    Args:
        value: The name.
        context: What was being attempted.

    Returns:
        The environment.

    Raises:
        ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` for an unknown name. An
            unrecognised environment is treated as unauthorised rather than as
            paper, because the safe default when the intent is unclear is to do
            nothing.
    """
    try:
        return BrokerEnvironment(str(value).upper())
    except ValueError as exc:
        raise ExecutionError(
            Code.LIVE_TRADING_NOT_AUTHORIZED,
            f"{context} names environment {value!r}, which this engine does not "
            "recognise. An unknown environment is treated as unauthorised rather "
            "than as paper: when the intent is unclear the safe action is none.",
        ) from exc


@dataclass(frozen=True)
class EnvironmentProof:
    """What was actually checked to establish a paper environment.

    Attributes:
        environment: Always `PAPER` when this exists.
        host: The endpoint host that was checked.
        host_allowlisted: Whether the host is a known paper host.
        key_prefix_paper: Whether the credential id carries a paper prefix.
        account_reachable: Whether the paper account answered. False until a
            read-only call has been made, so a proof built from configuration
            alone says so.
        account_status: What the provider reported about the account.
        checks: Every check performed, for the audit trail.
        limitations: What this proof does **not** establish.
    """

    environment: BrokerEnvironment
    host: str
    host_allowlisted: bool
    key_prefix_paper: bool
    account_reachable: bool = False
    account_status: str | None = None
    checks: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()

    @property
    def is_proven(self) -> bool:
        """Whether every configuration-level check passed."""
        return (
            self.environment is BrokerEnvironment.PAPER
            and self.host_allowlisted
            and self.key_prefix_paper
        )

    @property
    def is_fully_certified(self) -> bool:
        """Whether a read-only call has additionally confirmed the account.

        Separate from `is_proven` because the two are different claims:
        configuration can be checked offline, while reachability cannot. A
        preflight that has not made a call reports the first and not the second.
        """
        return self.is_proven and self.account_reachable

    def failed_checks(self) -> tuple[str, ...]:
        """Name the configuration checks that did not pass.

        Returns:
            One entry per failed check, empty when the proof stands. Reported
            rather than summarised so a refusal says *which* of the three
            independent checks failed, which is the difference between a typo in
            a host name and a credential pointing at the wrong environment.
        """
        failures: list[str] = []
        if self.environment is not BrokerEnvironment.PAPER:
            failures.append(f"requested environment is {self.environment}, not PAPER")
        if not self.host_allowlisted:
            failures.append(f"host {self.host!r} is not on the paper allow-list")
        if not self.key_prefix_paper:
            failures.append("the credential id does not carry a paper key prefix")
        return tuple(failures)

    def as_json(self) -> dict[str, Any]:
        """Return the proof as a JSON-safe mapping. Contains no credential."""
        return {
            "environment": str(self.environment),
            "host": self.host,
            "host_allowlisted": self.host_allowlisted,
            "key_prefix_paper": self.key_prefix_paper,
            "account_reachable": self.account_reachable,
            "account_status": self.account_status,
            "proven": self.is_proven,
            "fully_certified": self.is_fully_certified,
            "checks": list(self.checks),
            "failed_checks": list(self.failed_checks()),
            "limitations": list(self.limitations),
        }


def prove_paper_environment(
    *,
    environment: BrokerEnvironment | str,
    base_url: str,
    key_id: str,
    context: str = "the external broker adapter",
) -> EnvironmentProof:
    """Establish that an endpoint and credential are the provider's paper ones.

    Three independent checks, all required. Any one of them passing alone would
    be weak: a paper-looking config can point at a live host, a paper host can be
    addressed with a live key, and a paper key says nothing about where the
    request goes.

    Args:
        environment: The requested environment.
        base_url: The endpoint that would be addressed.
        key_id: The credential **identifier** — never the secret. Only its
            documented prefix is examined and nothing is logged.
        context: What was being attempted.

    Returns:
        The proof, carrying what was checked and what it does not establish.

    Raises:
        ExecutionError: `LIVE_TRADING_NOT_AUTHORIZED` for a live environment or a
            known live host; `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` when the host
            is unrecognised or the key prefix is not a paper one;
            `BROKER_CREDENTIALS_MISSING` when there is no key id to check.
    """
    refuse_live(environment, context=context)
    if not key_id:
        raise ExecutionError(
            Code.BROKER_CREDENTIALS_MISSING,
            f"{context} has no credential identifier, so nothing can establish "
            "which environment it belongs to. No request is made.",
        )

    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"{context} has no resolvable host in {base_url!r}, so the endpoint "
            "cannot be checked against the known paper hosts",
        )
    if host in LIVE_HOSTS:
        raise ExecutionError(
            Code.LIVE_TRADING_NOT_AUTHORIZED,
            f"{context} points at {host}, which is a real-money endpoint. It is "
            "refused by name rather than by failing an allow-list, so the reason "
            "is unambiguous. No credential is used and no request is made.",
        )
    if host not in PAPER_HOSTS:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"{context} points at {host}, which is not a known paper endpoint "
            f"({', '.join(sorted(PAPER_HOSTS))}). An unrecognised host is refused "
            "rather than assumed safe: the failure mode of guessing wrong is a "
            "live order.",
        )

    prefix = key_id[:2].upper()
    if prefix in LIVE_KEY_PREFIXES:
        raise ExecutionError(
            Code.LIVE_TRADING_NOT_AUTHORIZED,
            f"{context} uses a credential whose identifier begins {prefix!r}, "
            "which the provider documents as a live key. A live credential is "
            "refused even against a paper host.",
        )
    if prefix not in PAPER_KEY_PREFIXES:
        raise ExecutionError(
            Code.BROKER_ENVIRONMENT_NOT_PROVEN_PAPER,
            f"{context} uses a credential whose identifier begins {prefix!r}, "
            f"which is not a documented paper prefix "
            f"({', '.join(sorted(PAPER_KEY_PREFIXES))}). The environment cannot be "
            "established, so no request is made.",
        )

    return EnvironmentProof(
        environment=BrokerEnvironment.PAPER,
        host=host,
        host_allowlisted=True,
        key_prefix_paper=True,
        checks=(
            f"requested environment is {BrokerEnvironment.PAPER} and not LIVE",
            f"endpoint host {host} is on the paper allow-list",
            f"endpoint host {host} is not a known live host",
            f"credential identifier prefix {prefix} is a documented paper prefix",
            "credential identifier prefix is not a documented live prefix",
        ),
        limitations=(
            "host and key-prefix checks establish that the configured endpoint "
            "and credential are the provider's paper ones. They cannot prove the "
            "provider has not changed what that host serves — no provider API "
            "offers a stronger assertion than this — so the proof is recorded as "
            "what was checked rather than as certainty.",
            "account_reachable stays false until a read-only call has actually "
            "answered; configuration alone cannot establish reachability.",
        ),
    )
