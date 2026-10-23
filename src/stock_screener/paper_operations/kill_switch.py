"""The persistent kill switch, and the three things it deliberately does not do.

A kill switch is only worth having if it is believed when it cannot be read. So
the default here is not "active unless killed" but the reverse: a missing file,
a corrupt file, an unparseable state and an unverifiable one all resolve to
`KILLED`. That inversion is the whole design. Any other default means a disk
error grants permission to trade.

Three things it does not do, each of which is a real temptation:

**It does not liquidate.** A kill stops activity; it does not sell anything. A
filled position is an accounting fact that an authorised portfolio decision
created, and only another authorised decision may unwind it. A kill switch that
flattened the book would turn a precautionary stop into the largest unreviewed
trade the system ever made — at exactly the moment something is already known
to be wrong.

**It does not cancel what it cannot prove it owns.** An open order at the venue
that this system's journal does not account for belongs to someone else. It is
reported as `UNEXPECTED_REMOTE_ORDER` and left alone. Cancelling a stranger's
order is both outside this system's authority and irreversible.

**It does not become inactive by restarting.** `KILLED` survives a process
restart, and leaving it requires an explicit operator recovery with
reconciliation — not a state transition and not a new process.

Pause is the narrower sibling: it blocks new submissions and cancels nothing.
Both are recorded so a later reader can see which was used and why.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import Code, OperationsError
from .persistence import atomic_write_verified, guard_operations_root, read_json
from .versions import KILL_SWITCH_CONTRACT

if TYPE_CHECKING:
    from .clock import Clock

__all__ = [
    "CRITICAL_AUTO_KILL_REASONS",
    "KILL_FILENAME",
    "KillState",
    "KillSwitchStore",
    "PaperKillSwitch",
]

#: Where the kill state lives inside the operational root.
KILL_FILENAME = "kill_switch.json"

#: The only conditions that may activate the kill switch without an operator.
#: Frozen, and deliberately short. Each one means the system has lost track of
#: what is true — the account is not the one we think, the environment is not
#: provably paper, a ledger does not verify, or an execution has been admitted
#: twice. Ordinary trouble (stale input, an unreachable provider, a
#: reconciliation break) *blocks* instead: it stops new submissions without
#: escalating to a state that needs operator recovery to leave, because
#: escalating routine problems trains operators to clear kills without reading
#: them.
CRITICAL_AUTO_KILL_REASONS: frozenset[str] = frozenset(
    {
        "BROKER_ACCOUNT_MISMATCH",
        "BROKER_ENVIRONMENT_NOT_PROVEN_PAPER",
        "LIVE_TRADING_NOT_AUTHORIZED",
        "LEDGER_INTEGRITY_FAILURE",
        "EXECUTION_JOURNAL_INTEGRITY_FAILURE",
        "DUPLICATE_ACCOUNTING_FILL",
        "CONTROL_STATE_UNVERIFIABLE",
    }
)


class KillState(StrEnum):
    """Whether operations may proceed."""

    ACTIVE = "ACTIVE"
    """Operations may proceed, subject to every other gate."""

    PAUSED = "PAUSED"
    """New submissions are blocked. Nothing is cancelled, nothing is sold."""

    KILLED = "KILLED"
    """New submissions are blocked and owned open orders are cancelled.
    Positions are untouched. Leaving this state needs an operator recovery."""

    KILLED_WITH_UNRESOLVED_REMOTE_ORDER = "KILLED_WITH_UNRESOLVED_REMOTE_ORDER"
    """Killed, and at least one owned order's fate at the venue could not be
    established. Strictly worse than `KILLED` and must not be collapsed into it:
    there may be a working order nobody is tracking."""

    @property
    def may_submit(self) -> bool:
        """Whether a new external submission is permitted."""
        return self is KillState.ACTIVE

    @property
    def is_killed(self) -> bool:
        """Whether this is a kill rather than a pause."""
        return self in {
            KillState.KILLED,
            KillState.KILLED_WITH_UNRESOLVED_REMOTE_ORDER,
        }

    @property
    def needs_operator_recovery(self) -> bool:
        """Whether leaving this state requires explicit operator action."""
        return self.is_killed


@dataclass(frozen=True)
class PaperKillSwitch:
    """The recorded control state.

    Attributes:
        state: Whether operations may proceed.
        reason_code: Why, as a stable code.
        detail: A sanitised explanation. Never a credential.
        changed_at: When the state was set, ISO-8601.
        changed_by: `OPERATOR` or `AUTOMATIC`.
        account_binding_fingerprint: Which account this state governs.
        unresolved_remote_orders: Owned orders whose fate is unknown.
        cancelled_remote_orders: Owned orders proven terminal by the kill.
    """

    state: KillState
    reason_code: str = ""
    detail: str = ""
    changed_at: str = ""
    changed_by: str = "OPERATOR"
    account_binding_fingerprint: str = ""
    unresolved_remote_orders: tuple[str, ...] = ()
    cancelled_remote_orders: tuple[str, ...] = ()
    contract: str = KILL_SWITCH_CONTRACT

    @property
    def may_submit(self) -> bool:
        """Whether a new external submission is permitted."""
        return self.state.may_submit

    def require_submission_permitted(self, *, context: str) -> None:
        """Refuse a submission while paused or killed.

        Called before every mutation rather than once per run. A process that
        was permitted at startup has not been permitted for the rest of time —
        an operator may have killed it thirty seconds ago, and the whole point
        of a persistent switch is that the next check sees it.

        Args:
            context: What was being attempted.

        Raises:
            OperationsError: `PAPER_OPERATIONS_KILLED`,
                `KILLED_WITH_UNRESOLVED_REMOTE_ORDER` or
                `PAPER_OPERATIONS_PAUSED` as appropriate.
        """
        if self.state.may_submit:
            return
        code = {
            KillState.PAUSED: Code.PAPER_OPERATIONS_PAUSED,
            KillState.KILLED: Code.PAPER_OPERATIONS_KILLED,
            KillState.KILLED_WITH_UNRESOLVED_REMOTE_ORDER: (
                Code.KILLED_WITH_UNRESOLVED_REMOTE_ORDER
            ),
        }[self.state]
        suffix = (
            f" Unresolved owned orders: {list(self.unresolved_remote_orders)}."
            if self.unresolved_remote_orders
            else ""
        )
        raise OperationsError(
            code,
            f"{context}: operations are {self.state}"
            + (f" ({self.reason_code})" if self.reason_code else "")
            + f".{suffix} No position is liquidated by this state and no unknown "
            "order is touched",
        )

    def body(self) -> dict[str, Any]:
        """Return the state's semantic content."""
        return {
            "contract": self.contract,
            "state": str(self.state),
            "reason_code": self.reason_code,
            "detail": self.detail,
            "changed_by": self.changed_by,
            "account_binding_fingerprint": self.account_binding_fingerprint,
            "unresolved_remote_orders": list(self.unresolved_remote_orders),
            "cancelled_remote_orders": list(self.cancelled_remote_orders),
            "liquidates_positions": False,
            "cancels_unknown_orders": False,
        }

    def fingerprint(self) -> str:
        """Digest the state. Excludes `changed_at` so it is a value."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the state with its own fingerprint."""
        return {
            **self.body(),
            "changed_at": self.changed_at,
            "kill_state_fingerprint": self.fingerprint(),
        }

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> PaperKillSwitch:
        """Rebuild a state read back from disk.

        Args:
            body: What was written.

        Returns:
            The state.

        Raises:
            OperationsError: `CONTROL_STATE_UNVERIFIABLE` if the state name is
                unknown or the stored fingerprint does not match. Both resolve
                to killed at the call site — an unreadable switch is never
                permission.
        """
        raw = str(body.get("state", ""))
        try:
            state = KillState(raw)
        except ValueError as exc:
            raise OperationsError(
                Code.CONTROL_STATE_UNVERIFIABLE,
                f"the kill switch records state {raw!r}, which this build does not "
                "recognise. An unrecognised control state is treated as killed",
            ) from exc
        switch = cls(
            state=state,
            reason_code=str(body.get("reason_code", "")),
            detail=str(body.get("detail", "")),
            changed_at=str(body.get("changed_at", "")),
            changed_by=str(body.get("changed_by", "OPERATOR")),
            account_binding_fingerprint=str(body.get("account_binding_fingerprint", "")),
            unresolved_remote_orders=tuple(body.get("unresolved_remote_orders", ()) or ()),
            cancelled_remote_orders=tuple(body.get("cancelled_remote_orders", ()) or ()),
        )
        stored = str(body.get("kill_state_fingerprint", ""))
        if stored and stored != switch.fingerprint():
            raise OperationsError(
                Code.CONTROL_STATE_UNVERIFIABLE,
                "the kill switch does not match its own fingerprint. A tampered or "
                "truncated control record is treated as killed rather than honoured",
            )
        return switch


@dataclass
class KillSwitchStore:
    """The durable home of the kill state, with a fail-closed read.

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
        """Where the kill state is stored."""
        return self.root / KILL_FILENAME

    def current(self) -> PaperKillSwitch:
        """Read the kill state, failing closed on anything unreadable.

        Never raises and never returns `None`. Every failure mode — the file is
        missing, unparseable, truncated, tampered, or names a state this build
        does not know — resolves to a killed switch carrying the reason. That is
        the inversion the whole module rests on: a disk error must not be able
        to grant permission to trade.

        Returns:
            The state. `KILLED` when it could not be established.
        """
        if not self.path.exists():
            return PaperKillSwitch(
                state=KillState.KILLED,
                reason_code=Code.CONTROL_STATE_UNVERIFIABLE,
                detail=(
                    "no kill-switch record exists. A fresh or wiped control root is "
                    "treated as killed: absent control state is not permission"
                ),
                changed_by="AUTOMATIC",
            )
        try:
            return PaperKillSwitch.from_body(read_json(self.path))
        except OperationsError as exc:
            return PaperKillSwitch(
                state=KillState.KILLED,
                reason_code=Code.CONTROL_STATE_UNVERIFIABLE,
                detail=f"the kill-switch record could not be verified: {exc}",
                changed_by="AUTOMATIC",
            )

    def _write(self, switch: PaperKillSwitch) -> PaperKillSwitch:
        """Persist a state, verified.

        Args:
            switch: The state to record.

        Returns:
            The state.
        """
        atomic_write_verified(self.path, switch.as_json())
        return switch

    def activate(
        self,
        *,
        clock: Clock,
        account_binding_fingerprint: str,
        reason_code: str = "OPERATOR_ARMED",
        detail: str = "",
    ) -> PaperKillSwitch:
        """Set the switch to `ACTIVE`.

        Args:
            clock: The operational clock.
            account_binding_fingerprint: Which account this governs.
            reason_code: Why.
            detail: A sanitised note.

        Returns:
            The state.
        """
        return self._write(
            PaperKillSwitch(
                state=KillState.ACTIVE,
                reason_code=reason_code,
                detail=detail,
                changed_at=clock.iso(),
                changed_by="OPERATOR",
                account_binding_fingerprint=account_binding_fingerprint,
            )
        )

    def pause(
        self,
        *,
        clock: Clock,
        reason_code: str,
        detail: str = "",
        account_binding_fingerprint: str = "",
    ) -> PaperKillSwitch:
        """Block new submissions without cancelling anything.

        Args:
            clock: The operational clock.
            reason_code: Why.
            detail: A sanitised note.
            account_binding_fingerprint: Which account this governs.

        Returns:
            The state.
        """
        return self._write(
            PaperKillSwitch(
                state=KillState.PAUSED,
                reason_code=reason_code,
                detail=detail
                or "new submissions blocked; nothing cancelled and no position touched",
                changed_at=clock.iso(),
                changed_by="OPERATOR",
                account_binding_fingerprint=account_binding_fingerprint,
            )
        )

    def kill(
        self,
        *,
        clock: Clock,
        reason_code: str,
        detail: str = "",
        account_binding_fingerprint: str = "",
        changed_by: str = "OPERATOR",
    ) -> PaperKillSwitch:
        """Record a kill durably, before anything else is attempted.

        Durability comes first deliberately. Cancelling owned orders needs the
        provider, and the provider may be down — so a kill that tried to cancel
        first could fail to become locally killed at all. Writing the state
        first means a provider outage cannot prevent the system from stopping.

        Args:
            clock: The operational clock.
            reason_code: Why.
            detail: A sanitised note.
            account_binding_fingerprint: Which account this governs.
            changed_by: `OPERATOR` or `AUTOMATIC`.

        Returns:
            The recorded state. Cancellation of owned orders is a separate step
            the caller takes afterwards.
        """
        return self._write(
            PaperKillSwitch(
                state=KillState.KILLED,
                reason_code=reason_code,
                detail=detail
                or (
                    "new submissions blocked; owned open orders will be cancelled. "
                    "No position is liquidated and no unknown order is touched"
                ),
                changed_at=clock.iso(),
                changed_by=changed_by,
                account_binding_fingerprint=account_binding_fingerprint,
            )
        )

    def record_cancellation_outcome(
        self,
        *,
        clock: Clock,
        cancelled: tuple[str, ...],
        unresolved: tuple[str, ...],
        account_binding_fingerprint: str = "",
    ) -> PaperKillSwitch:
        """Record what became of the owned orders a kill tried to cancel.

        Args:
            clock: The operational clock.
            cancelled: Owned orders proven terminal.
            unresolved: Owned orders whose fate could not be established.
            account_binding_fingerprint: Which account this governs.

        Returns:
            The state — `KILLED_WITH_UNRESOLVED_REMOTE_ORDER` when anything is
            unresolved, which is strictly worse than `KILLED` and is not
            collapsed into it.
        """
        state = KillState.KILLED_WITH_UNRESOLVED_REMOTE_ORDER if unresolved else KillState.KILLED
        return self._write(
            PaperKillSwitch(
                state=state,
                reason_code=(
                    Code.KILLED_WITH_UNRESOLVED_REMOTE_ORDER if unresolved else "KILL_COMPLETE"
                ),
                detail=(
                    "at least one owned order's state at the venue could not be "
                    "established; operations stay stopped and are not resumed"
                    if unresolved
                    else "every owned open order was proven terminal"
                ),
                changed_at=clock.iso(),
                changed_by="AUTOMATIC",
                account_binding_fingerprint=account_binding_fingerprint,
                unresolved_remote_orders=unresolved,
                cancelled_remote_orders=cancelled,
            )
        )

    def require_resume_authorized(self, *, reconciled: bool, open_blocking_incidents: int) -> None:
        """Refuse to re-activate without the conditions that justify it.

        Args:
            reconciled: Whether a clean three-way reconciliation has been done.
            open_blocking_incidents: How many blocking incidents remain open.

        Raises:
            OperationsError: `KILL_RESUME_NOT_AUTHORIZED` if the switch is
                killed and the conditions are not met. Restarting a process is
                not a recovery, and neither is a single command — leaving a kill
                requires verified local integrity, a reachable venue, a clean
                reconciliation and no open blocking incident.
        """
        current = self.current()
        if not current.state.needs_operator_recovery:
            return
        problems: list[str] = []
        if not reconciled:
            problems.append("no clean three-way reconciliation has been recorded")
        if open_blocking_incidents:
            problems.append(f"{open_blocking_incidents} blocking incident(s) remain open")
        if current.unresolved_remote_orders:
            problems.append(
                f"owned orders remain unresolved at the venue "
                f"{list(current.unresolved_remote_orders)}"
            )
        if problems:
            raise OperationsError(
                Code.KILL_RESUME_NOT_AUTHORIZED,
                "operations are killed and may not be re-armed yet: "
                + "; ".join(problems)
                + ". A restart is not a recovery",
            )
