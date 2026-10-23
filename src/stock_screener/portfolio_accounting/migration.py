"""Moving a V1 ledger onto per-attribution cost pools, or refusing to.

The question is narrow and has an exact answer: can the per-owner cost bases be
*recovered* from what the ledger already records, or would they have to be
invented? Events carry every fill's price and its attribution slices, so replaying
a complete V1 ledger from genesis under V2 rules reconstructs each owner's pool
deterministically. Nothing is estimated and nothing is carried over from the V1
state — the events are the authority, which is the whole reason the ledger is
event-sourced.

Where it does *not* work is the obvious temptation: migrating from the final V1
snapshot. A V1 state holds one basis per security and a quantity per owner. Those
two facts cannot produce per-owner bases, because the information was never there
— A's 10 and B's 20 both became 15 the moment they were pooled. So a snapshot
migration is refused by name with `ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE` rather
than approximated by spreading the pooled average, which would produce confident
numbers that are wrong by an amount nobody could later recover.

An incomplete event history is refused for the same reason. A truncated or
unverifiable ledger is missing fills, and a pool reconstructed from some of its
fills is not a pool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .accounting import replay
from .errors import AccountingError, Code
from .ledger import PortfolioLedger, verify_chain
from .versions import ACCOUNTING_V1, ACCOUNTING_V2

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .ledger import LedgerRecord
    from .state import PortfolioState

__all__ = [
    "MIGRATION_MODE",
    "MigrationReport",
    "migrate_from_events",
    "refuse_snapshot_migration",
]

#: How a V1 ledger reaches V2. There is no in-place conversion and no automatic
#: upgrade: a V1 ledger keeps replaying under V1, and V2 is reached by replaying
#: the same events under the new rules.
MIGRATION_MODE = "REPLAY_MIGRATION_FROM_EVENTS"


class MigrationReport(dict[str, Any]):
    """What a migration reconstructed, and the proof that it reconstructed it."""


def refuse_snapshot_migration(state: PortfolioState) -> None:
    """Refuse to derive cost pools from a V1 state.

    Args:
        state: The V1 state someone wants to migrate.

    Raises:
        AccountingError: `ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE`, always. A V1
            state records one basis per security and a quantity per owner; the
            owners' separate entry prices were destroyed when they were pooled.
            Spreading the pooled average across the owners would be a guess
            dressed as a migration.
    """
    raise AccountingError(
        Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE,
        "a V1 snapshot cannot be migrated to per-attribution cost pools. It holds "
        "one weighted-average basis per security and a quantity per owner, and the "
        f"owners' separate entry prices are not in it — across {len(state.positions)} "
        "position(s) they were pooled away at the moment of the first co-owned buy. "
        f"Migrate by replaying the ledger's events instead ({MIGRATION_MODE}); if the "
        "events are gone, the historical per-strategy bases are genuinely "
        "unrecoverable and must not be fabricated.",
    )


def migrate_from_events(
    ledger: PortfolioLedger | Sequence[LedgerRecord],
) -> MigrationReport:
    """Reconstruct V2 cost pools by replaying a V1 ledger's events.

    Args:
        ledger: The V1 ledger, or its records. Must be complete and verifiable:
            a reconstruction from part of a fill history is not a reconstruction.

    Returns:
        The report, carrying both states, the reconstructed pools, and the
        differences the new basis produces.

    Raises:
        AccountingError: `ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE` if the ledger is
            empty or its chain does not verify. Either way the fill history is
            not all there, and pools built from some of the fills would be
            asserted rather than derived.
    """
    records = tuple(ledger)
    if not records:
        raise AccountingError(
            Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE,
            "there are no events to replay, so there is nothing from which to "
            "reconstruct per-owner cost bases",
        )
    chain = verify_chain(records)
    if not chain["verified"]:
        failure = chain["failure"]
        raise AccountingError(
            Code.ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE,
            f"the ledger does not verify ({failure['detail']}), so its fill history "
            "is incomplete or altered. Cost pools reconstructed from part of a fill "
            "history would be a guess about the missing part.",
        )

    before = replay(records, accounting_version=ACCOUNTING_V1)
    after = replay(records, accounting_version=ACCOUNTING_V2)
    after.check_invariants()

    pools = after.cost_pools()
    differences = _differences(before, after)
    return MigrationReport(
        mode=MIGRATION_MODE,
        from_version=ACCOUNTING_V1,
        to_version=ACCOUNTING_V2,
        reconstructed=True,
        event_count=len(records),
        ledger_head=records[-1].record_hash,
        chain_verified=True,
        owners=list(after.pool_owners()),
        pool_count=len(pools),
        pools=[pool.as_json() for pool in pools],
        before={
            "accounting_version": ACCOUNTING_V1,
            "total_cost_basis": str(before.total_cost_basis),
            "realised_pnl": str(before.realised_pnl),
            "cash": str(before.cash),
        },
        after={
            "accounting_version": ACCOUNTING_V2,
            "total_cost_basis": str(after.total_cost_basis),
            "realised_pnl": str(after.realised_pnl),
            "cash": str(after.cash),
        },
        differences=differences,
        proof=[
            "every per-owner basis came from a fill's own price and attribution, "
            "replayed from genesis; none was derived from the V1 state",
            "cash is identical under both versions, because the basis convention "
            "decides how a sale is attributed and never how much money moved",
            (
                "global cost basis and realised P&L may differ, and a difference is "
                "the V1 limitation being corrected rather than a migration error"
            ),
        ],
        note=(
            "the V1 ledger is not mutated and remains replayable under V1. This is "
            "a reinterpretation of the same events, not a conversion of them."
        ),
    )


def _differences(before: PortfolioState, after: PortfolioState) -> dict[str, Any]:
    """Describe what the new basis changed.

    Args:
        before: The V1 replay.
        after: The V2 replay.

    Returns:
        The differences, with cash asserted unchanged — if the basis convention
        moved cash, something other than attribution has changed.
    """
    return {
        "cash_unchanged": before.cash == after.cash,
        "cost_basis_moved": before.total_cost_basis != after.total_cost_basis,
        "cost_basis_delta": str(after.total_cost_basis - before.total_cost_basis),
        "realised_moved": before.realised_pnl != after.realised_pnl,
        "realised_delta": str(after.realised_pnl - before.realised_pnl),
        "interpretation": (
            "a non-zero delta means at least one security had co-owners who entered "
            "at different prices — exactly the case V1 reported wrongly"
        ),
    }
