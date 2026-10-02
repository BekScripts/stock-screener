"""Reconciliation: compare the ledger to an external account, and never adopt it.

Reconciliation is where most accounting systems quietly fail. The temptation is
obvious — the broker says 300 shares, the ledger says 200, so set the ledger to
300 and the break disappears. It is also exactly wrong. The difference is
evidence that something happened which the ledger was never told about: a fill
that never arrived, a corporate action nobody recorded, a transfer, or a bug. The
figure is a symptom; adopting it destroys the only record of the symptom and
leaves the cause in place to recur.

So this module has no repair function. It cannot be configured to have one. It
compares, classifies, and reports — and a break stays open until a human decides
what event explains it and appends that event to the ledger, which is the only
mechanism by which state changes here.

Four kinds of break are distinguished, because they have different causes:

- `CASH_MISMATCH` — a cash movement the ledger does not know about;
- `POSITION_QUANTITY_MISMATCH` — both sides hold it, in different amounts;
- `EXTERNAL_POSITION_MISSING_INTERNAL` — the broker holds something the ledger
  has never heard of, which is usually a missed fill or a spinoff;
- `INTERNAL_POSITION_MISSING_EXTERNAL` — the ledger holds something the broker
  does not, which is usually a sale or delisting the ledger missed.

Incompleteness is also a result. An external statement that could not be fully
retrieved produces `INCOMPLETE_EXTERNAL`, never a clean reconciliation over the
part that arrived — a comparison against half an account reliably "proves" that
the other half does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_risk import SecurityIdentity

from .errors import Code
from .money import ZERO, as_decimal, quantise_cash, quantise_quantity

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from .state import PortfolioState

__all__ = [
    "RECONCILIATION_POLICY",
    "Break",
    "BreakSeverity",
    "ExternalPosition",
    "ExternalStatement",
    "ReconciliationResult",
    "reconcile",
    "statement_from",
]

#: The reconciliation contract. There is deliberately no "auto-repair" variant.
RECONCILIATION_POLICY = "PORTFOLIO_RECONCILIATION_V1"


class BreakSeverity(StrEnum):
    """How much a break threatens the books.

    A quantity break is `CRITICAL` because every weight, every risk figure and
    every P&L line computed over that position is wrong until it is resolved.
    A cash break is `CRITICAL` for the same reason. Nothing here is `INFO`:
    a reconciliation difference is never routine.
    """

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"


@dataclass(frozen=True)
class ExternalPosition:
    """One holding as an external account reports it.

    Attributes:
        identity: The security, resolved to durable identity by the caller. The
            resolution is the caller's job because a broker reports a ticker, and
            mapping a ticker to an identity is exactly the step where a reassigned
            symbol silently merges two companies.
        quantity: Shares the external account says are held.
        reported_symbol: The symbol the statement used, kept for the report.
    """

    identity: SecurityIdentity
    quantity: Decimal
    reported_symbol: str | None = None

    def as_json(self) -> dict[str, Any]:
        """Return the position as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "quantity": str(quantise_quantity(self.quantity)),
            "reported_symbol": self.reported_symbol,
        }


@dataclass(frozen=True)
class ExternalStatement:
    """What an external account says, at a stated time.

    Attributes:
        as_of: When the statement describes, ISO 8601 with offset.
        source: Which account or custodian.
        cash: The cash balance it reports.
        positions: The holdings it reports.
        complete: Whether the whole statement was retrieved. False produces
            `INCOMPLETE_EXTERNAL` rather than a clean comparison over a part.
        notes: Anything the retrieval wants recorded.
    """

    as_of: str
    source: str
    cash: Decimal
    positions: tuple[ExternalPosition, ...] = ()
    complete: bool = True
    notes: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        """Return the statement as a JSON-safe mapping."""
        return {
            "as_of": self.as_of,
            "source": self.source,
            "cash": str(quantise_cash(self.cash)),
            "position_count": len(self.positions),
            "complete": self.complete,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Break:
    """One difference between the ledger and an external account.

    Attributes:
        code: Which kind of break.
        severity: How much it threatens the books.
        identity_label: The security, or None for a cash break.
        internal: What the ledger says.
        external: What the statement says.
        difference: `external - internal`.
        likely_causes: What usually produces this break, as investigation
            starting points. Never a repair instruction.
    """

    code: str
    severity: BreakSeverity
    identity_label: str | None
    internal: str
    external: str
    difference: str
    likely_causes: tuple[str, ...]

    def as_json(self) -> dict[str, Any]:
        """Return the break as a JSON-safe mapping."""
        return {
            "code": self.code,
            "severity": str(self.severity),
            "identity": self.identity_label,
            "internal": self.internal,
            "external": self.external,
            "difference": self.difference,
            "likely_causes": list(self.likely_causes),
        }


@dataclass(frozen=True)
class ReconciliationResult:
    """The outcome of comparing a ledger to an external account.

    Attributes:
        reconciled: True only when there are no breaks and both sides are
            complete.
        as_of: The statement's time.
        source: The statement's source.
        breaks: Every difference found.
        ledger_sequence: Which ledger record the internal side was derived at.
        ledger_head_hash: That record's chain hash, so a reconciliation can be
            re-run against exactly the same history.
        checked: What was compared, for the audit trail.
        remediation: What may be done about a break. Deliberately a single
            sentence, and deliberately not an action this module can take.
    """

    reconciled: bool
    as_of: str
    source: str
    breaks: tuple[Break, ...]
    ledger_sequence: int
    ledger_head_hash: str
    checked: Mapping[str, Any] = field(default_factory=dict)
    remediation: str = (
        "a break is resolved by deciding what event explains it and appending "
        "that event to the ledger. This engine never adopts the external figure: "
        "overwriting the ledger would erase the only evidence that something "
        "happened which it was not told about, and leave the cause in place."
    )

    @property
    def critical_count(self) -> int:
        """How many breaks are critical."""
        return sum(1 for row in self.breaks if row.severity is BreakSeverity.CRITICAL)

    def as_json(self) -> dict[str, Any]:
        """Return the result as a JSON-safe mapping."""
        return {
            "policy": RECONCILIATION_POLICY,
            "reconciled": self.reconciled,
            "as_of": self.as_of,
            "source": self.source,
            "break_count": len(self.breaks),
            "critical_count": self.critical_count,
            "breaks": [row.as_json() for row in self.breaks],
            "ledger_sequence": self.ledger_sequence,
            "ledger_head_hash": self.ledger_head_hash,
            "checked": dict(self.checked),
            "remediation": self.remediation,
            "auto_repair": "NOT_IMPLEMENTED_BY_DESIGN",
        }


def reconcile(
    state: PortfolioState,
    statement: ExternalStatement,
    *,
    cash_tolerance: Decimal | None = None,
) -> ReconciliationResult:
    """Compare the ledger's state to an external statement.

    Args:
        state: The replayed ledger state.
        statement: What the external account says.
        cash_tolerance: An absolute cash difference to treat as reconciled.
            Defaults to zero. A tolerance is accepted because some custodians
            round interest accruals, but it is never defaulted to something
            permissive: a tolerance that hides a cent today hides a thousand
            when the units change.

    Returns:
        The reconciliation result. Breaks are reported, never repaired.
    """
    tolerance = quantise_cash(cash_tolerance) if cash_tolerance is not None else ZERO
    breaks: list[Break] = []

    internal_cash = quantise_cash(state.cash)
    external_cash = quantise_cash(as_decimal(statement.cash, field="external cash"))
    cash_difference = quantise_cash(external_cash - internal_cash)
    if abs(cash_difference) > tolerance:
        breaks.append(
            Break(
                code=Code.CASH_MISMATCH,
                severity=BreakSeverity.CRITICAL,
                identity_label=None,
                internal=str(internal_cash),
                external=str(external_cash),
                difference=str(cash_difference),
                likely_causes=(
                    "a fill, fee or interest posting the ledger was never told about",
                    "a deposit or withdrawal that reached the account but not the ledger",
                    "a dividend credited externally and not recorded as an event",
                    "the statement describes a different instant than the replayed state",
                ),
            )
        )

    external_by_key = {row.identity.key: row for row in statement.positions}
    internal_by_key = {key: row for key, row in state.positions.items() if row.is_open}

    for key in sorted(set(internal_by_key) | set(external_by_key)):
        internal_row = internal_by_key.get(key)
        external_row = external_by_key.get(key)
        if internal_row is not None and external_row is not None:
            internal_quantity = quantise_quantity(internal_row.quantity)
            external_quantity = quantise_quantity(
                as_decimal(external_row.quantity, field="external quantity")
            )
            if internal_quantity != external_quantity:
                breaks.append(
                    Break(
                        code=Code.POSITION_QUANTITY_MISMATCH,
                        severity=BreakSeverity.CRITICAL,
                        identity_label=internal_row.identity.label,
                        internal=str(internal_quantity),
                        external=str(external_quantity),
                        difference=str(quantise_quantity(external_quantity - internal_quantity)),
                        likely_causes=(
                            "a partial fill recorded in full, or a fill not recorded at all",
                            "a split or stock dividend applied by the custodian and not here",
                            "a transfer in or out that was never recorded as an event",
                        ),
                    )
                )
        elif external_row is not None:
            breaks.append(
                Break(
                    code=Code.EXTERNAL_POSITION_MISSING_INTERNAL,
                    severity=BreakSeverity.CRITICAL,
                    identity_label=external_row.identity.label,
                    internal="0",
                    external=str(quantise_quantity(external_row.quantity)),
                    difference=str(quantise_quantity(external_row.quantity)),
                    likely_causes=(
                        "a fill that never reached the ledger",
                        "a spinoff or merger consideration delivered as a new security",
                        "a transfer in",
                        "a symbol resolved to the wrong durable identity, which would "
                        "pair with an INTERNAL_POSITION_MISSING_EXTERNAL for the other",
                    ),
                )
            )
        else:
            assert internal_row is not None  # noqa: S101 - one of the two must exist
            breaks.append(
                Break(
                    code=Code.INTERNAL_POSITION_MISSING_EXTERNAL,
                    severity=BreakSeverity.CRITICAL,
                    identity_label=internal_row.identity.label,
                    internal=str(quantise_quantity(internal_row.quantity)),
                    external="0",
                    difference=str(quantise_quantity(-internal_row.quantity)),
                    likely_causes=(
                        "a sale that never reached the ledger",
                        "a delisting, liquidation or mandatory exchange",
                        "a transfer out",
                        "the statement is incomplete and this holding was not in "
                        "the part that was retrieved",
                    ),
                )
            )

    if not statement.complete:
        breaks.append(
            Break(
                code=Code.INCOMPLETE_EXTERNAL,
                severity=BreakSeverity.CRITICAL,
                identity_label=None,
                internal=f"{len(internal_by_key)} open positions",
                external=f"{len(external_by_key)} positions reported",
                difference="UNKNOWN",
                likely_causes=(
                    "the statement retrieval was truncated, paginated or partly failed",
                    "a comparison against part of an account reliably 'proves' that "
                    "the rest of it does not exist, so no clean result is reported",
                ),
            )
        )
    if not state.is_established:
        breaks.append(
            Break(
                code=Code.INCOMPLETE_INTERNAL,
                severity=BreakSeverity.CRITICAL,
                identity_label=None,
                internal="no opening balance",
                external=f"{len(external_by_key)} positions reported",
                difference="UNKNOWN",
                likely_causes=(
                    "the ledger has no opening balance, so there is no internal "
                    "position to compare: every external holding would look like a "
                    "missing fill",
                ),
            )
        )

    return ReconciliationResult(
        reconciled=not breaks,
        as_of=statement.as_of,
        source=statement.source,
        breaks=tuple(breaks),
        ledger_sequence=state.ledger_sequence,
        ledger_head_hash=state.ledger_head_hash,
        checked={
            "cash": True,
            "cash_tolerance": str(tolerance),
            "internal_open_positions": len(internal_by_key),
            "external_positions": len(external_by_key),
            "identities_compared": len(set(internal_by_key) | set(external_by_key)),
            "external_complete": statement.complete,
            "internal_established": state.is_established,
        },
    )


def statement_from(
    rows: Sequence[Mapping[str, Any]],
    *,
    as_of: str,
    source: str,
    cash: Any,
    complete: bool = True,
) -> ExternalStatement:
    """Build an external statement from its JSON form.

    Args:
        rows: `[{"kind", "identity_id", "symbol", "quantity"}]`.
        as_of: When the statement describes.
        source: The account or custodian.
        cash: The reported cash balance.
        complete: Whether the whole statement was retrieved.

    Returns:
        The statement.
    """
    return ExternalStatement(
        as_of=as_of,
        source=source,
        cash=as_decimal(cash, field="external cash"),
        positions=tuple(
            ExternalPosition(
                identity=SecurityIdentity(
                    kind=str(row["kind"]),
                    identity_id=int(row["identity_id"]),
                    symbol=row.get("symbol"),
                ),
                quantity=as_decimal(row["quantity"], field="external quantity"),
                reported_symbol=row.get("symbol"),
            )
            for row in rows
        ),
        complete=complete,
    )
