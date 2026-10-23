"""One mutation-capable run at a time, and why a stale lease is not free to take.

The lease stops two processes from running the same account concurrently. The
subtle part is what happens when a lease looks abandoned.

The tempting rule is "if the lease is older than N minutes, take it" — and it is
wrong, because an expired lease and a crashed run are the same observation from
outside. A process that steals a stale lease and starts planning has no idea
whether the previous run got as far as submitting an order; if it did, the new
run will plan against a book that does not yet reflect it and may submit a
second one. So age alone never grants the lease. The recorded run must be
*recovered* first — journal read, venue queried, reconciliation done — and only
then may the lease be released and reissued.

The lease is also deliberately durable rather than an in-memory lock. An
in-process mutex protects against two threads and nothing against two
processes, which is the case that actually happens when a cron job overlaps a
manual invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Code, OperationsError
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import RUN_LEASE_CONTRACT

if TYPE_CHECKING:
    from .clock import Clock

__all__ = [
    "LEASE_FILENAME",
    "LeaseState",
    "RunLease",
    "RunLeaseStore",
]

#: Where the lease lives inside the operational root.
LEASE_FILENAME = "run_lease.json"


class LeaseState(StrEnum):
    """Whether a lease is held."""

    HELD = "HELD"
    RELEASED = "RELEASED"


@dataclass(frozen=True)
class RunLease:
    """The durable claim on one account's mutation capability.

    Attributes:
        run_id: Which run holds it.
        account_binding_fingerprint: Which account it covers.
        state: Whether it is held.
        acquired_at: When it was taken, ISO-8601.
        heartbeat_at: When it was last refreshed.
        released_at: When it was released, if it was.
        contract: The lease contract version.
    """

    run_id: str
    account_binding_fingerprint: str
    state: LeaseState = LeaseState.HELD
    acquired_at: str = ""
    heartbeat_at: str = ""
    released_at: str | None = None
    contract: str = RUN_LEASE_CONTRACT

    @property
    def is_held(self) -> bool:
        """Whether this lease is currently held."""
        return self.state is LeaseState.HELD

    def body(self) -> dict[str, Any]:
        """Return the lease's semantic content."""
        return {
            "contract": self.contract,
            "run_id": self.run_id,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "state": str(self.state),
            "stale_lease_policy": (
                "age never grants a lease; the recorded run must be recovered first"
            ),
        }

    def fingerprint(self) -> str:
        """Digest the lease. Excludes timestamps so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the lease with its timestamps and fingerprint."""
        return {
            **self.body(),
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "released_at": self.released_at,
            "lease_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> RunLease:
        """Rebuild a lease read back from disk.

        Args:
            body: What was written.

        Returns:
            The lease.

        Raises:
            OperationsError: `OPERATIONS_PARTIAL_RECORD` if it cannot be read.
        """
        try:
            return cls(
                run_id=str(body["run_id"]),
                account_binding_fingerprint=str(body["account_binding_fingerprint"]),
                state=LeaseState(str(body.get("state", "HELD"))),
                acquired_at=str(body.get("acquired_at", "")),
                heartbeat_at=str(body.get("heartbeat_at", "")),
                released_at=(str(body["released_at"]) if body.get("released_at") else None),
            )
        except (KeyError, ValueError) as exc:
            raise OperationsError(
                Code.OPERATIONS_PARTIAL_RECORD, f"a run lease could not be read: {exc}"
            ) from exc


@dataclass
class RunLeaseStore:
    """The durable home of the run lease.

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
        """Where the lease is stored."""
        return self.root / LEASE_FILENAME

    def current(self) -> RunLease | None:
        """Read the lease, if there is one.

        Returns:
            The lease, or `None` when none was ever taken.
        """
        if not self.path.exists():
            return None
        return RunLease.from_body(read_json(self.path))

    def acquire(self, *, run_id: str, account_binding_fingerprint: str, clock: Clock) -> RunLease:
        """Take the lease for one run, or recover the run already holding it.

        Args:
            run_id: Which run wants it.
            account_binding_fingerprint: Which account.
            clock: The operational clock.

        Returns:
            The lease, held by `run_id`. Re-acquiring a lease this same run
            already holds is a no-op that refreshes the heartbeat — which is
            what makes a retried invocation of the same slot safe.

        Raises:
            OperationsError: `RUN_ALREADY_ACTIVE` if a *different* run holds it.
                Deliberately not resolved by age: an old lease and a crashed run
                look identical from here, and a run that stole the lease could
                submit a second order for work the previous run had already
                sent. The holder must be recovered first.
        """
        existing = self.current()
        now = clock.iso()
        if existing is not None and existing.is_held and existing.run_id != run_id:
            raise OperationsError(
                Code.RUN_ALREADY_ACTIVE,
                f"run {existing.run_id} holds the mutation lease for this account "
                f"(taken {existing.acquired_at}). Run {run_id} will not start "
                "alongside it, and the lease is not taken on age: a stale lease and "
                "a crashed run look identical from here, and the crashed run may "
                "already have submitted. Recover it first",
            )
        lease = RunLease(
            run_id=run_id,
            account_binding_fingerprint=account_binding_fingerprint,
            state=LeaseState.HELD,
            acquired_at=existing.acquired_at if existing and existing.run_id == run_id else now,
            heartbeat_at=now,
        )
        atomic_write_verified(self.path, lease.as_json())
        return lease

    def heartbeat(self, *, run_id: str, clock: Clock) -> RunLease:
        """Refresh the lease held by a run.

        Args:
            run_id: Which run.
            clock: The operational clock.

        Returns:
            The refreshed lease.

        Raises:
            OperationsError: `RUN_LEASE_NOT_HELD` if this run does not hold it.
        """
        existing = self.current()
        if existing is None or not existing.is_held or existing.run_id != run_id:
            raise OperationsError(
                Code.RUN_LEASE_NOT_HELD,
                f"run {run_id} does not hold the mutation lease and cannot refresh it",
            )
        lease = RunLease(
            run_id=existing.run_id,
            account_binding_fingerprint=existing.account_binding_fingerprint,
            state=LeaseState.HELD,
            acquired_at=existing.acquired_at,
            heartbeat_at=clock.iso(),
        )
        atomic_write_verified(self.path, lease.as_json())
        return lease

    def release(self, *, run_id: str, clock: Clock) -> RunLease:
        """Release the lease a run holds.

        Args:
            run_id: Which run.
            clock: The operational clock.

        Returns:
            The released lease.

        Raises:
            OperationsError: `RUN_LEASE_HELD_ELSEWHERE` if another run holds it.
                A run never releases somebody else's claim.
        """
        existing = self.current()
        if existing is not None and existing.is_held and existing.run_id != run_id:
            raise OperationsError(
                Code.RUN_LEASE_HELD_ELSEWHERE,
                f"the lease is held by {existing.run_id}; run {run_id} will not "
                "release another run's claim",
            )
        lease = RunLease(
            run_id=run_id,
            account_binding_fingerprint=(existing.account_binding_fingerprint if existing else ""),
            state=LeaseState.RELEASED,
            acquired_at=existing.acquired_at if existing else "",
            heartbeat_at=existing.heartbeat_at if existing else "",
            released_at=clock.iso(),
        )
        atomic_write_verified(self.path, lease.as_json())
        return lease

    def held_by(self) -> str | None:
        """Which run holds the lease, if any.

        Returns:
            The run id, or `None`.
        """
        existing = self.current()
        return existing.run_id if existing is not None and existing.is_held else None
