"""Marks, and the rules about when a mark may be used.

A valuation is a claim about one instant, and the integrity of that claim rests
on something easy to lose: every mark must have been *knowable* at the valuation
time. A price stamped after it describes a world the valuation could not have
seen, and using one is the same leakage the research layer spends its whole
temporal apparatus preventing — here it would simply be called a NAV.

So marks are bounded rather than merely collected:

- `MARK_AFTER_VALUATION_TIME` refuses a price stamped later than the valuation;
- `VALUATION_INCOMPLETE` refuses a partial valuation rather than valuing the
  priced holdings and quietly treating the rest as worthless. A NAV missing one
  position is not a smaller NAV, it is a wrong one;
- `MARK_STALE` records a mark older than the staleness bound. It is a warning on
  the snapshot rather than a refusal, because a thinly traded security's last
  print genuinely is its last price — but a NAV resting on week-old marks must
  say so.

Nothing here fetches a price. Marks arrive as evidence, exactly like fills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_risk import SecurityIdentity

from .errors import AccountingError, Code
from .money import ZERO, as_decimal, quantise_cash, quantise_price

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from decimal import Decimal

    from .state import PortfolioState

__all__ = [
    "DEFAULT_STALENESS_LIMIT_HOURS",
    "MARK_POLICY_ID",
    "MarkPrice",
    "MarkPriceSnapshot",
    "ValuationCoverage",
    "marks_from",
    "value_positions",
]

#: The mark-admissibility contract.
MARK_POLICY_ID = "PORTFOLIO_MARK_ADMISSIBILITY_V1"

#: How old a mark may be before the snapshot reports it stale. Four calendar
#: days covers a long weekend, which is an ordinary gap rather than a problem.
DEFAULT_STALENESS_LIMIT_HOURS = 96


def _parse_instant(value: str, *, field_name: str) -> datetime:
    """Parse an aware ISO 8601 instant.

    Args:
        value: The timestamp.
        field_name: What to name in an error.

    Returns:
        The aware moment.

    Raises:
        AccountingError: If it is unparseable or naive.
    """
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AccountingError(Code.MARK_INVALID, f"{field_name} {value!r} is not ISO 8601") from exc
    if moment.tzinfo is None:
        raise AccountingError(
            Code.MARK_INVALID,
            f"{field_name} must carry a UTC offset; a naive price timestamp cannot "
            "be compared against a valuation instant in another zone, and the "
            "comparison is the whole control",
        )
    return moment


@dataclass(frozen=True)
class MarkPrice:
    """One security's price, with the time it was good as of.

    Attributes:
        identity: The security.
        price: The mark. Must be positive: a zero mark is a claim that the
            holding is worthless, which is a decision rather than a price.
        as_of: When the price was good as of, ISO 8601 with offset.
        source: Where it came from.
    """

    identity: SecurityIdentity
    price: Decimal
    as_of: str
    source: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        """Normalise the price and refuse one that cannot be used.

        Raises:
            AccountingError: On a non-positive price or an unusable timestamp.
        """
        price = quantise_price(as_decimal(self.price, field="mark price"))
        if price <= ZERO:
            raise AccountingError(
                Code.MARK_INVALID,
                f"{self.identity.label}: a mark must be positive, got {price}. A zero "
                "mark asserts the holding is worthless, which is a write-down "
                "decision and not a price.",
            )
        object.__setattr__(self, "price", price)
        _parse_instant(self.as_of, field_name=f"{self.identity.label} mark as_of")

    @property
    def moment(self) -> datetime:
        """The mark's instant."""
        return _parse_instant(self.as_of, field_name="mark as_of")

    def as_json(self) -> dict[str, Any]:
        """Return the mark as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "price": str(self.price),
            "as_of": self.as_of,
            "source": self.source,
        }


@dataclass(frozen=True)
class MarkPriceSnapshot:
    """The marks offered for one valuation instant.

    Attributes:
        valuation_time: The instant being valued, ISO 8601 with offset.
        marks: The prices, one per security.
        staleness_limit_hours: How old a mark may be before it is reported stale.
    """

    valuation_time: str
    marks: tuple[MarkPrice, ...] = ()
    staleness_limit_hours: int = DEFAULT_STALENESS_LIMIT_HOURS

    def __post_init__(self) -> None:
        """Validate the instant and every mark against it.

        Raises:
            AccountingError: `MARK_AFTER_VALUATION_TIME` for a mark stamped after
                the valuation; `MARK_INVALID` for a duplicate identity or a bad
                instant.
        """
        valued_at = _parse_instant(self.valuation_time, field_name="valuation_time")
        if self.staleness_limit_hours <= 0:
            raise AccountingError(
                Code.MARK_INVALID,
                f"the staleness limit must be positive, got {self.staleness_limit_hours}",
            )
        seen: set[tuple[str, int]] = set()
        for mark in self.marks:
            if mark.identity.key in seen:
                raise AccountingError(
                    Code.MARK_INVALID,
                    f"{mark.identity.label} is marked twice; whichever was applied "
                    "last would decide the NAV",
                )
            seen.add(mark.identity.key)
            if mark.moment > valued_at:
                raise AccountingError(
                    Code.MARK_AFTER_VALUATION_TIME,
                    f"{mark.identity.label} is marked at {mark.as_of}, after the "
                    f"valuation instant {self.valuation_time}. A price from after the "
                    "instant describes a world the valuation could not have seen; "
                    "admitting it is price leakage reported as a NAV.",
                )

    @property
    def moment(self) -> datetime:
        """The valuation instant."""
        return _parse_instant(self.valuation_time, field_name="valuation_time")

    def mark_for(self, identity: SecurityIdentity) -> MarkPrice | None:
        """The mark for one security, if offered.

        Args:
            identity: The security.

        Returns:
            Its mark, or None.
        """
        for mark in self.marks:
            if mark.identity.key == identity.key:
                return mark
        return None

    def stale_marks(self) -> tuple[MarkPrice, ...]:
        """Marks older than the staleness bound."""
        cutoff = self.moment - timedelta(hours=self.staleness_limit_hours)
        return tuple(mark for mark in self.marks if mark.moment < cutoff)

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping."""
        return {
            "mark_policy": MARK_POLICY_ID,
            "valuation_time": self.valuation_time,
            "staleness_limit_hours": self.staleness_limit_hours,
            "mark_count": len(self.marks),
            "marks": [mark.as_json() for mark in sorted(self.marks, key=lambda m: m.identity)],
        }


@dataclass(frozen=True)
class ValuationCoverage:
    """Which holdings a valuation could price.

    Attributes:
        valued: Market value per security, keyed by durable identity.
        missing: Holdings with no admissible mark.
        stale: Holdings priced by a mark older than the bound.
        market_value: The total.
    """

    valued: Mapping[tuple[str, int], Decimal] = field(default_factory=dict)
    missing: tuple[SecurityIdentity, ...] = ()
    stale: tuple[str, ...] = ()
    market_value: Decimal = ZERO

    def as_json(self) -> dict[str, Any]:
        """Return the coverage as a JSON-safe mapping."""
        return {
            "priced_count": len(self.valued),
            "missing": [identity.label for identity in self.missing],
            "stale": list(self.stale),
            "market_value": str(quantise_cash(self.market_value)),
        }


def value_positions(
    state: PortfolioState,
    snapshot: MarkPriceSnapshot,
    *,
    require_complete: bool = True,
) -> ValuationCoverage:
    """Price the open holdings against a mark snapshot.

    Args:
        state: The portfolio to value.
        snapshot: The marks offered.
        require_complete: Refuse a partial valuation. The default, because a NAV
            missing one position is wrong rather than small — the alternative
            treats an unpriced holding as worth nothing.

    Returns:
        What could be priced, and what could not.

    Raises:
        AccountingError: `VALUATION_INCOMPLETE` if a holding has no admissible
            mark and completeness is required.
    """
    valued: dict[tuple[str, int], Decimal] = {}
    missing: list[SecurityIdentity] = []
    stale_keys = {mark.identity.key for mark in snapshot.stale_marks()}
    stale: list[str] = []
    total = ZERO
    for key in sorted(state.positions):
        position = state.positions[key]
        if not position.is_open:
            continue
        mark = snapshot.mark_for(position.identity)
        if mark is None:
            missing.append(position.identity)
            continue
        value = quantise_cash(position.quantity * mark.price)
        valued[key] = value
        total = quantise_cash(total + value)
        if key in stale_keys:
            stale.append(position.identity.label)
    if missing and require_complete:
        raise AccountingError(
            Code.VALUATION_INCOMPLETE,
            f"{len(missing)} of {len(state.positions)} holdings have no admissible "
            f"mark at {snapshot.valuation_time} "
            f"({', '.join(identity.label for identity in missing)}). A NAV computed "
            "over the rest would treat them as worthless, which is not a smaller "
            "NAV but a wrong one, so the valuation is refused instead.",
        )
    return ValuationCoverage(
        valued=valued,
        missing=tuple(missing),
        stale=tuple(sorted(stale)),
        market_value=total,
    )


def marks_from(
    rows: Sequence[Mapping[str, Any]],
    *,
    valuation_time: str,
    staleness_limit_hours: int | None = None,
) -> MarkPriceSnapshot:
    """Build a mark snapshot from its JSON form.

    Args:
        rows: `[{"kind", "identity_id", "symbol", "price", "as_of", "source"}]`.
        valuation_time: The instant being valued.
        staleness_limit_hours: Override the default bound.

    Returns:
        The snapshot.
    """
    marks = tuple(
        MarkPrice(
            identity=SecurityIdentity(
                kind=str(row["kind"]),
                identity_id=int(row["identity_id"]),
                symbol=row.get("symbol"),
            ),
            price=as_decimal(row["price"], field="mark price"),
            as_of=str(row["as_of"]),
            source=str(row.get("source", "UNSPECIFIED")),
        )
        for row in rows
    )
    return MarkPriceSnapshot(
        valuation_time=valuation_time,
        marks=marks,
        staleness_limit_hours=(
            staleness_limit_hours
            if staleness_limit_hours is not None
            else DEFAULT_STALENESS_LIMIT_HOURS
        ),
    )
