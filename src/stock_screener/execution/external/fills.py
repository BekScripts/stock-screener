"""Turning a venue's execution into the broker-neutral fill accounting admits.

The venue reports an `ExternalExecutionFill`: its own execution id, its own
order id, a ticker. Accounting admits an `ExecutionFill`: a local fill id, a
local order id, a `SecurityIdentity`. Something has to carry one into the other,
and that translation is the whole exactly-once story, so there is exactly one of
it and both callers use it — the P2.2E canary, which this was extracted from
unchanged, and the operational submission authority.

Two properties are load-bearing:

- **The fill id is derived from the venue's execution id, never generated.**
  `EXT:<remote_fill_id>` maps to accounting event `EXEC_FILL:EXT:<remote_fill_id>`,
  so re-reading the same execution produces the same event id and the ledger's
  own exactly-once check refuses the second one. A generated id would make every
  duplicate read a second trade.
- **A fee is declared or the fill is refused.** `require_fee` raises
  `BROKER_FILL_FEE_UNKNOWN` rather than substituting zero, because a silent zero
  on a venue that later starts charging leaves NAV short by exactly the fees.
  A caller that knows its venue charges nothing says so explicitly, and that
  declaration is recorded in the fill's provenance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..events import ExecutionFill
from ..models import AttributionShare
from .session import ExecutionVenue

if TYPE_CHECKING:
    from decimal import Decimal

    from stock_screener.portfolio_risk import SecurityIdentity

    from ..models import OrderSide
    from .models import ExternalExecutionFill

__all__ = ["FILL_TRANSLATION_VERSION", "execution_fill_from"]

#: The translation contract.
FILL_TRANSLATION_VERSION = "EXTERNAL_FILL_TRANSLATION_V1"


def execution_fill_from(
    remote: ExternalExecutionFill,
    *,
    order_id: str,
    identity: SecurityIdentity,
    side: OrderSide,
    strategy_id: str,
    fee_schedule: Decimal | None = None,
    venue: str = "",
) -> ExecutionFill:
    """Translate one venue execution into the fill accounting admits.

    Args:
        remote: The execution the venue reported.
        order_id: The *local* order id this filled. Supplied by the caller,
            which knows which of its own orders it asked the venue about: the
            venue's order id is not a local identity and must not become one.
        identity: The security, resolved locally from the venue symbol through
            the declared mapping rather than parsed from the ticker.
        side: Which way the local order traded. Taken from the order rather
            than the venue's echo of it.
        strategy_id: Who the filled quantity belongs to. The full quantity is
            attributed to one strategy, which is what the single-strategy
            runtime means; a multi-strategy caller must split it itself.
        fee_schedule: The fee to declare when the venue stated none. `None`
            means nothing is declared, and a fill with no stated fee is then
            refused rather than zeroed.
        venue: Which venue, for provenance. Defaults to the external paper
            venue.

    Returns:
        The broker-neutral fill, carrying a derived id and the digest of the
        venue evidence it came from.

    Raises:
        ExecutionError: `BROKER_FILL_FEE_UNKNOWN` if the venue stated no fee and
            `fee_schedule` declared none.
    """
    from stock_screener.execution import fingerprint_of

    priced = (
        remote if remote.fee_is_known or fee_schedule is None else remote.with_fee(fee_schedule)
    )
    return ExecutionFill(
        fill_id=f"EXT:{priced.remote_fill_id}",
        order_id=order_id,
        identity=identity,
        side=side,
        quantity=priced.quantity,
        price=priced.price,
        fee=priced.require_fee(),
        effective_at=priced.reported_at,
        attribution=(AttributionShare(strategy_id=strategy_id, quantity=priced.quantity),),
        source_fingerprint=fingerprint_of(priced.body()),
        venue=venue or str(ExecutionVenue.EXTERNAL_PAPER),
    )
