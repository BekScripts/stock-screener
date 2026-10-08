"""What the strategy remembers between decisions.

Only what distinguishes one decision from the next: which decision was last
made, and what exposure it still wants. Nothing else, and in particular not
positions.

Broker positions are deliberately *not* strategy state. A strategy that read its
holdings from the venue would conflate what it wants with what it has, and those
differ for reasons that are none of its business — a risk cut, a partial fill, a
pending order. It would also mean a reconciliation break could silently change
the next decision's exits.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .errors import Code, StrategyError

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.portfolio_risk import SecurityIdentity

    from .models import StrategyDecision

__all__ = ["STRATEGY_STATE_VERSION", "StrategyState", "StrategyStateStore"]

#: The state contract.
STRATEGY_STATE_VERSION = "ALPHATRADER_STRATEGY_STATE_V1"


@dataclass(frozen=True)
class StrategyState:
    """The strategy's memory of its last decision.

    Attributes:
        strategy_id: Which strategy.
        strategy_version: Its version when the decision was made.
        decision_session: The session of the last decision.
        decision_at: Its instant.
        decision_fingerprint: Its digest.
        desired: The relative exposures it still wants, by identity label.
        identities: The identities behind those labels, so a reader never has to
            parse a label back into a key.
    """

    strategy_id: str
    strategy_version: str
    decision_session: str
    decision_at: str
    decision_fingerprint: str
    desired: dict[str, float] = field(default_factory=dict)
    identities: tuple[SecurityIdentity, ...] = ()

    @property
    def desired_identities(self) -> tuple[SecurityIdentity, ...]:
        """The identities carrying non-zero desired exposure, canonically ordered."""
        wanted = {label for label, weight in self.desired.items() if weight > 0.0}
        return tuple(
            sorted(
                (row for row in self.identities if row.label in wanted),
                key=lambda row: row.key,
            )
        )

    @classmethod
    def from_decision(cls, decision: StrategyDecision) -> StrategyState:
        """Build the state a decision leaves behind.

        Args:
            decision: The decision just made.

        Returns:
            The state. Exits are not carried: a name at desired zero is a name
            the strategy no longer wants, and keeping it would make it exit
            again at the next decision.
        """
        return cls(
            strategy_id=decision.strategy_id,
            strategy_version=decision.strategy_version,
            decision_session=decision.decision_session,
            decision_at=decision.decision_at,
            decision_fingerprint=decision.fingerprint(),
            desired={row.identity.label: row.desired_weight for row in decision.selections},
            identities=tuple(row.identity for row in decision.selections),
        )

    def body(self) -> dict[str, Any]:
        """Return the state's semantic content."""
        return {
            "version": STRATEGY_STATE_VERSION,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "decision_session": self.decision_session,
            "decision_at": self.decision_at,
            "decision_fingerprint": self.decision_fingerprint,
            "desired": dict(sorted(self.desired.items())),
            "identities": [row.as_json() for row in sorted(self.identities, key=lambda r: r.key)],
        }

    def fingerprint(self) -> str:
        """Digest the state."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the state with its fingerprint."""
        return {**self.body(), "state_fingerprint": self.fingerprint()}

    @classmethod
    def from_body(cls, body: dict[str, Any]) -> StrategyState:
        """Rebuild state from its stored form.

        Args:
            body: What was written.

        Returns:
            The state.

        Raises:
            StrategyError: `STRATEGY_STATE_UNREADABLE` if the record is not this
                contract version or a required field is absent. A partially
                readable state is refused rather than defaulted, because a
                defaulted previous selection would silently exit every holding.
        """
        from stock_screener.portfolio_risk import SecurityIdentity

        if str(body.get("version")) != STRATEGY_STATE_VERSION:
            raise StrategyError(
                Code.STRATEGY_STATE_UNREADABLE,
                f"state is {body.get('version')!r}, expected {STRATEGY_STATE_VERSION!r}",
            )
        try:
            return cls(
                strategy_id=str(body["strategy_id"]),
                strategy_version=str(body["strategy_version"]),
                decision_session=str(body["decision_session"]),
                decision_at=str(body["decision_at"]),
                decision_fingerprint=str(body["decision_fingerprint"]),
                desired={str(k): float(v) for k, v in dict(body.get("desired", {})).items()},
                identities=tuple(
                    SecurityIdentity(
                        kind=str(row["kind"]),
                        identity_id=int(row["identity_id"]),
                        symbol=row.get("symbol"),
                    )
                    for row in body.get("identities", ())
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise StrategyError(
                Code.STRATEGY_STATE_UNREADABLE,
                f"state could not be read: {type(exc).__name__}. It is refused rather "
                "than defaulted — a defaulted previous selection exits every holding",
            ) from exc


@dataclass
class StrategyStateStore:
    """Where strategy state lives.

    Attributes:
        root: The runtime root. One directory per deployed strategy instance.
    """

    root: Path

    def __post_init__(self) -> None:
        """Resolve the state path under the root."""
        from pathlib import Path as _Path

        self.root = _Path(self.root)
        self.path = self.root / "strategy_state.json"

    def write(self, state: StrategyState) -> Path:
        """Record the state durably.

        Args:
            state: The state.

        Returns:
            Where it was written.
        """
        from stock_screener.paper_operations import atomic_write_verified

        atomic_write_verified(self.path, state.as_json())
        return self.path

    def read(self) -> StrategyState | None:
        """Read the state.

        Returns:
            The state, or `None` when none has been written. A first decision
            legitimately has no predecessor, which is why absence is `None`
            rather than an error — but a present-and-corrupt record raises.

        Raises:
            StrategyError: `STRATEGY_STATE_UNREADABLE` if a record exists and
                cannot be read.
        """
        if not self.path.exists():
            return None
        try:
            body = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StrategyError(
                Code.STRATEGY_STATE_UNREADABLE,
                f"state at {self.path.name} could not be parsed: {type(exc).__name__}",
            ) from exc
        return StrategyState.from_body(body)
