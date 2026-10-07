"""Whether the coordinator may operate at all — and nothing more than that.

The distinction this module exists to keep is easy to lose and expensive to
lose. An arming authorization permits the *scheduler* to run. It does not
authorize a security, a quantity, a side or an order. Every order still needs
the whole existing chain — portfolio and risk approval, an `ExecutionPlan` bound
to the book's state, an `Order` that plan authorises — and arming grants none of
it.

The reason to be strict is that an operations authorization is the broadest
permission in the system, so it is the one most tempting to treat as sufficient.
If arming could authorize an order, then one operator action would stand between
a scheduled process and an arbitrary trade. Instead arming answers only "may
this coordinator wake up and look", and the answer to "may it buy this" is
decided five layers down by code that has not changed.

The safe default is unarmed. A fresh install, a missing file, a corrupt file and
an expired authorization all mean the same thing: no.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .clock import parse_instant
from .errors import Code, OperationsError
from .models import OperationsMode
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import (
    OPERATIONS_AUTHORIZATION_CONTRACT,
    PAPER_OPERATIONS_ENGINE_VERSION,
)

if TYPE_CHECKING:
    from .clock import Clock

__all__ = [
    "ARMING_FILENAME",
    "ArmingStore",
    "PaperOperationsAuthorization",
]

#: Where the authorization lives inside the operational root.
ARMING_FILENAME = "arming.json"


@dataclass(frozen=True)
class PaperOperationsAuthorization:
    """An operator's standing permission for the coordinator to operate.

    Attributes:
        authorization_id: A stable name for this grant.
        provider: Which provider it covers.
        account_binding_fingerprint: Which paper account. Never a wildcard and
            never an account number — a grant that covered "any account" would
            survive a credential being repointed, which is the one case it most
            needs to fail.
        allowed_mode: The widest mode permitted. `EXTERNAL_PAPER` permits
            observe-only too; `OBSERVE_ONLY` permits only observation.
        operations_policy_fingerprint: Which operational rules were in force.
        code_commit: Which build the operator approved.
        created_at: When the grant was made, ISO-8601.
        expires_at: When it lapses, if the operator chose a limit. No default is
            invented: a grant that silently expired would strand a scheduler,
            and one that silently never expired would outlive its reason. The
            operator picks.
        environment: Always `PAPER`.
        contract: The arming contract version.
    """

    authorization_id: str
    provider: str
    account_binding_fingerprint: str
    operations_policy_fingerprint: str
    code_commit: str
    created_at: str
    allowed_mode: OperationsMode = OperationsMode.OBSERVE_ONLY
    expires_at: str | None = None
    environment: str = "PAPER"
    contract: str = OPERATIONS_AUTHORIZATION_CONTRACT
    engine_version: str = PAPER_OPERATIONS_ENGINE_VERSION

    def __post_init__(self) -> None:
        """Refuse a grant that could not be checked, or that is not paper.

        Raises:
            OperationsError: `LIVE_OPERATIONS_NOT_AUTHORIZED` if the environment
                is anything but paper;
                `PAPER_OPERATIONS_AUTHORIZATION_INVALID` if a required binding
                is missing or the account is a wildcard.
        """
        if self.environment != "PAPER":
            raise OperationsError(
                Code.LIVE_OPERATIONS_NOT_AUTHORIZED,
                f"an operations authorization names environment "
                f"{self.environment!r}; only PAPER is authorised, and no "
                "configuration value changes that",
            )
        if not self.authorization_id.strip():
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID,
                "an authorization needs an id so a later reader can say which grant "
                "a run operated under",
            )
        if self.account_binding_fingerprint.strip() in {"", "*", "any", "ANY"}:
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID,
                "an authorization must name one account binding fingerprint. A "
                "wildcard would keep authorising after a credential was repointed "
                "at a different account, which is exactly when it must stop",
            )
        if not self.code_commit.strip():
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID,
                "an authorization must name the commit the operator approved; "
                "otherwise it silently covers code nobody looked at",
            )
        if self.expires_at is not None:
            parse_instant(self.expires_at)
        parse_instant(self.created_at)

    def is_expired(self, now: str) -> bool:
        """Whether this grant has lapsed.

        Args:
            now: The operational instant.

        Returns:
            True when an explicit expiry has passed. A grant with no expiry
            never lapses on its own — that was the operator's choice, and
            inventing a default would be overriding it.
        """
        if self.expires_at is None:
            return False
        return parse_instant(now) >= parse_instant(self.expires_at)

    def permits(self, mode: OperationsMode) -> bool:
        """Whether this grant covers a mode.

        Args:
            mode: The mode a run wants.

        Returns:
            True when the grant is at least as wide. Observation is always
            covered by a paper grant; the reverse is not true.
        """
        if mode is OperationsMode.OBSERVE_ONLY:
            return True
        return self.allowed_mode is OperationsMode.EXTERNAL_PAPER

    def require(self, *, mode: OperationsMode, now: str, account_fingerprint: str) -> None:
        """Refuse to operate unless this grant covers the attempt.

        Args:
            mode: The mode the run wants.
            now: The operational instant.
            account_fingerprint: The account the run would act on.

        Raises:
            OperationsError: `PAPER_OPERATIONS_AUTHORIZATION_EXPIRED` if it has
                lapsed; `OPERATIONS_ACCOUNT_BINDING_MISMATCH` if it names a
                different account; `PAPER_OPERATIONS_NOT_ARMED` if it does not
                cover the mode.
        """
        if self.is_expired(now):
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_EXPIRED,
                f"authorization {self.authorization_id} expired at {self.expires_at}. "
                "An expired grant is not a narrow grant — nothing is authorised "
                "until an operator renews it",
            )
        if account_fingerprint != self.account_binding_fingerprint:
            raise OperationsError(
                Code.OPERATIONS_ACCOUNT_BINDING_MISMATCH,
                f"authorization {self.authorization_id} covers a different account "
                "binding than this run would act on. Operating anyway would apply an "
                "operator's permission to an account they did not approve",
            )
        if not self.permits(mode):
            raise OperationsError(
                Code.PAPER_OPERATIONS_NOT_ARMED,
                f"authorization {self.authorization_id} permits {self.allowed_mode} "
                f"and this run wants {mode}. Arming is explicit and is not widened "
                "by a runtime flag",
            )

    def body(self) -> dict[str, Any]:
        """Return the grant's semantic content. Carries no credential."""
        return {
            "contract": self.contract,
            "engine_version": self.engine_version,
            "authorization_id": self.authorization_id,
            "environment": self.environment,
            "provider": self.provider,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "allowed_mode": str(self.allowed_mode),
            "operations_policy_fingerprint": self.operations_policy_fingerprint,
            "code_commit": self.code_commit,
            "expires_at": self.expires_at,
            "live_trading": "NOT_AUTHORIZED",
            "authorises": "the coordinator to operate — never a security, quantity or order",
        }

    def fingerprint(self) -> str:
        """Digest the grant. Excludes `created_at` so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the grant with its own fingerprint and creation time."""
        return {
            **self.body(),
            "created_at": self.created_at,
            "authorization_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperOperationsAuthorization:
        """Rebuild a grant read back from disk.

        Args:
            body: What was written.

        Returns:
            The grant.

        Raises:
            OperationsError: `PAPER_OPERATIONS_AUTHORIZATION_INVALID` if a
                required field is missing, or the stored fingerprint does not
                match the content. A tampered grant is refused rather than
                honoured — it is the broadest permission in the system.
        """
        required = (
            "authorization_id",
            "provider",
            "account_binding_fingerprint",
            "operations_policy_fingerprint",
            "code_commit",
            "created_at",
        )
        missing = [name for name in required if not body.get(name)]
        if missing:
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID,
                f"a stored authorization is missing {missing}; it cannot be checked "
                "and so authorises nothing",
            )
        grant = cls(
            authorization_id=str(body["authorization_id"]),
            provider=str(body["provider"]),
            account_binding_fingerprint=str(body["account_binding_fingerprint"]),
            operations_policy_fingerprint=str(body["operations_policy_fingerprint"]),
            code_commit=str(body["code_commit"]),
            created_at=str(body["created_at"]),
            allowed_mode=OperationsMode(str(body.get("allowed_mode", "OBSERVE_ONLY"))),
            expires_at=(str(body["expires_at"]) if body.get("expires_at") else None),
            environment=str(body.get("environment", "PAPER")),
        )
        stored = str(body.get("authorization_fingerprint", ""))
        if stored and stored != grant.fingerprint():
            raise OperationsError(
                Code.PAPER_OPERATIONS_AUTHORIZATION_INVALID,
                "a stored authorization does not match its own fingerprint. It is "
                "refused rather than honoured: this is the broadest permission in "
                "the system and a tampered one must not be the one that stands",
            )
        return grant


@dataclass
class ArmingStore:
    """The durable home of the operations authorization.

    Absence means unarmed. So does corruption. Neither is treated as permission,
    and `current` says which it found rather than collapsing them.

    Attributes:
        root: The operational root.
    """

    root: Path

    def __post_init__(self) -> None:
        """Guard the root.

        Raises:
            OperationsError: `FORBIDDEN_OPERATIONS_NAMESPACE` if it lies in
                another trust domain.
        """
        self.root = Path(self.root)
        guard_operations_root(self.root)

    @property
    def path(self) -> Path:
        """Where the authorization is stored."""
        return self.root / ARMING_FILENAME

    def arm(self, authorization: PaperOperationsAuthorization) -> Path:
        """Record an operator's grant durably.

        Args:
            authorization: The grant.

        Returns:
            Where it was written.
        """
        atomic_write_verified(self.path, authorization.as_json())
        return self.path

    def disarm(self, *, clock: Clock, reason: str = "") -> Path:
        """Withdraw the grant, leaving a record that it was withdrawn.

        Disarming is not a kill. It stops *future* scheduled runs from being
        authorised; it cancels nothing and it does not touch positions. The
        difference is recorded in the file so an operator reading it later does
        not have to remember which one they used.

        Args:
            clock: The operational clock.
            reason: Why, for the record.

        Returns:
            Where the record was written.
        """
        atomic_write_verified(
            self.path,
            {
                "contract": OPERATIONS_AUTHORIZATION_CONTRACT,
                "disarmed": True,
                "disarmed_at": clock.iso(),
                "reason": reason,
                "effect": (
                    "future scheduled runs are not authorised. Nothing is cancelled "
                    "and no position is touched — that is what a kill does"
                ),
            },
        )
        return self.path

    def current(self) -> PaperOperationsAuthorization | None:
        """Read the standing grant, if there is one.

        Returns:
            The grant, or `None` when the system is unarmed — which includes a
            missing file and an explicit disarm.

        Raises:
            OperationsError: `CONTROL_STATE_UNVERIFIABLE` if the file exists but
                cannot be read, and
                `PAPER_OPERATIONS_AUTHORIZATION_INVALID` if it can be read and
                does not hold a valid grant. Deliberately raised rather than
                returning `None`: "unarmed" and "the control state is damaged"
                need different operator responses, and only the first is routine.
        """
        if not self.path.exists():
            return None
        body = read_json(self.path)
        if body.get("disarmed"):
            return None
        return PaperOperationsAuthorization.from_body(body)

    def is_armed(self, *, mode: OperationsMode, now: str) -> bool:
        """Whether a mode is currently authorised.

        Args:
            mode: The mode.
            now: The operational instant.

        Returns:
            True only for a present, valid, unexpired grant that covers it.
        """
        grant = self.current()
        if grant is None:
            return False
        return not grant.is_expired(now) and grant.permits(mode)
