"""How much of a session's volume this system has already taken.

The participation ceiling is global to an `(identity, session)` pair, not an
allowance each order gets separately. If three child orders touch the same
security in the same session, together they may take at most the session's
capacity — otherwise the ceiling would scale with however many orders a planner
happened to emit, which would make it no ceiling at all.

**Consumption is derived, never tracked.** It is read back out of the durable
admitted fills in the journal, keyed by fill id, and that choice does the work:
a replayed fill is the *same* fill id, so it consumes capacity once no matter
how many times it is presented. A mutable counter incremented per fill would
double-consume on every retry, and retries are the normal consequence of a crash.
It would also be a second liquidity authority to keep in step with the journal.

Reading from the journal costs a replay. That is the right trade: a ceiling that
is occasionally wrong in the direction of permitting more trading is worse than
one that is always right and slower to compute.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import quantise_quantity

from .errors import Code, ExecutionError

if TYPE_CHECKING:
    from stock_screener.portfolio_risk import SecurityIdentity

    from .journal import OrderLedger
    from .realism_policy import PaperExecutionPolicy
    from .session_market import SessionQuote

__all__ = [
    "PARTICIPATION_CONTRACT",
    "SessionCapacity",
    "capacity_for",
    "consumed_in_session",
]

#: The participation contract.
PARTICIPATION_CONTRACT = "SESSION_SHARE_VOLUME_PARTICIPATION_V1"


def consumed_in_session(
    derived: OrderLedger,
    *,
    identity: SecurityIdentity,
    session: str,
    order_sessions: dict[str, str],
) -> Decimal:
    """How much of a security's capacity this session's fills have used.

    Derived from the durable admitted fills rather than from a counter, so a fill
    presented twice consumes capacity once: the second presentation carries the
    same fill id and contributes the same single entry.

    Args:
        derived: The order state replayed from the journal.
        identity: The security.
        session: The session.
        order_sessions: `{order_id: session}` — which session each child order
            belongs to, from the journal's own orders.

    Returns:
        The quantity already filled in that `(identity, session)` pair.
    """
    total = Decimal(0)
    for fill_id in sorted(derived.fills):
        fill = derived.fills[fill_id]
        if fill.identity.key != identity.key:
            continue
        if order_sessions.get(fill.order_id) != session:
            continue
        total += fill.quantity
    return quantise_quantity(total)


@dataclass(frozen=True)
class SessionCapacity:
    """What remains fillable for one security in one session.

    Attributes:
        identity: The security.
        session: The session.
        observed_share_volume: Shares traded in the session.
        ceiling: `volume * max_volume_participation`.
        consumed: What this system has already filled, from durable fills.
    """

    identity: SecurityIdentity
    session: str
    observed_share_volume: Decimal
    ceiling: Decimal
    consumed: Decimal

    @property
    def remaining(self) -> Decimal:
        """How much capacity is left. Never negative."""
        return quantise_quantity(max(Decimal(0), self.ceiling - self.consumed))

    @property
    def is_exhausted(self) -> bool:
        """Whether nothing more may fill in this session."""
        return self.remaining <= Decimal(0)

    def require_available(self, *, order_id: str) -> Decimal:
        """The remaining capacity, or a refusal when there is none.

        Args:
            order_id: For the message.

        Returns:
            The remaining capacity.

        Raises:
            ExecutionError: `PARTICIPATION_CAPACITY_EXHAUSTED` when none is left
                and the session actually traded. A session that traded nothing is
                a different situation — there was no market, not a ceiling
                reached — and is left to the caller as a zero fill.
        """
        if self.is_exhausted and self.observed_share_volume > Decimal(0):
            raise ExecutionError(
                Code.PARTICIPATION_CAPACITY_EXHAUSTED,
                f"{order_id}: this system has already filled {self.consumed} of "
                f"{self.identity.label} in session {self.session}, against a "
                f"ceiling of {self.ceiling} ({self.observed_share_volume} shares "
                "traded). The ceiling is global to the security and the session, "
                "not an allowance per order, so further orders do not get more.",
            )
        return self.remaining

    def as_json(self) -> dict[str, Any]:
        """Return the capacity as a JSON-safe mapping."""
        return {
            "contract": PARTICIPATION_CONTRACT,
            "identity": self.identity.label,
            "session": self.session,
            "observed_share_volume": str(self.observed_share_volume),
            "volume_unit": "SHARES",
            "ceiling": str(self.ceiling),
            "consumed": str(self.consumed),
            "remaining": str(self.remaining),
            "scope": "GLOBAL_PER_IDENTITY_AND_SESSION",
            "consumption_source": (
                "durable admitted fills, keyed by fill id — so a replayed fill "
                "consumes capacity once rather than again"
            ),
        }


def capacity_for(
    quote: SessionQuote,
    *,
    session: str,
    policy: PaperExecutionPolicy,
    derived: OrderLedger,
    order_sessions: dict[str, str],
) -> SessionCapacity:
    """Compute what remains fillable for one security in one session.

    Args:
        quote: The session's quote and volume.
        session: The session.
        policy: The friction policy.
        derived: The order state replayed from the journal.
        order_sessions: Which session each child order belongs to.

    Returns:
        The capacity.

    Raises:
        ExecutionError: `PAPER_VOLUME_REQUIRED` if the volume is unknown and the
            policy requires one.
    """
    volume = quote.require_volume(policy=policy)
    return SessionCapacity(
        identity=quote.identity,
        session=session,
        observed_share_volume=volume,
        ceiling=quantise_quantity(volume * policy.max_volume_participation),
        consumed=consumed_in_session(
            derived,
            identity=quote.identity,
            session=session,
            order_sessions=order_sessions,
        ),
    )
