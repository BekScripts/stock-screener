"""Quotes and volume for one market session, and the prices they imply.

Three things come from here and nowhere else: what a fill costs to cross the
spread, how far past the touch it lands, and how much of the session's volume
this system may take. Each is computed from explicit inputs on an immutable
snapshot, so the same snapshot always produces the same fill.

**No fallbacks.** If a quote side is missing and the policy requires a spread,
the answer is `PAPER_QUOTE_REQUIRED` — not the sizing price, not a close, not a
last trade. Substituting a single price silently removes the spread cost, which
is the main thing this module exists to charge. The same applies to volume: an
unknown volume is an unknown capacity, not an unlimited one.

**One timing semantic.** A snapshot describes a *completed* session, and the
execution opportunity it represents is stamped at its own `as_of`. Every figure
on it — bid, ask, volume — belongs to that same opportunity, and the fill is
stamped there too. The alternative, using a whole session's volume while dating
the fill earlier inside it, would be spending information the fill could not have
had. It is the same leakage the research layer spends its whole temporal
apparatus preventing; here it would be called a backtest fill.

A snapshot for one session cannot execute another session's order
(`PAPER_MARKET_SESSION_MISMATCH`), because a quote from Tuesday says nothing
about what Wednesday's order could have traded at.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from stock_screener.portfolio_accounting import as_decimal, quantise_price, quantise_quantity

from .errors import Code, ExecutionError
from .fingerprint import fingerprint_of
from .models import OrderSide
from .versions import PAPER_SESSION_FILL_MODEL

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from stock_screener.portfolio_risk import SecurityIdentity

    from .realism_policy import PaperExecutionPolicy

__all__ = [
    "PaperMarketSessionSnapshot",
    "SessionQuote",
    "fill_price_for",
    "quotes_from",
    "touch_price_for",
]


@dataclass(frozen=True)
class SessionQuote:
    """One security's quote and traded volume for one session.

    Attributes:
        identity: The security.
        bid: The best bid. Positive.
        ask: The best ask. Positive, and never below the bid.
        observed_share_volume: Shares traded in the session. **Shares**, not
            dollars — this is not P1's average dollar volume, and confusing the
            two would size a participation ceiling by a factor of the price.
            None means unknown, which is not the same as zero.
        source_fingerprint: Where the quote came from.
    """

    identity: SecurityIdentity
    bid: Decimal | None = None
    ask: Decimal | None = None
    observed_share_volume: Decimal | None = None
    source_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Validate the quote.

        Raises:
            ExecutionError: `INVALID_PAPER_QUOTE` on a non-positive or
                non-finite side, a crossed quote, or a negative volume. A
                crossed quote (`ask < bid`) is refused rather than normalised: it
                is a data fault, and silently swapping the sides would invent a
                spread with the wrong sign.
        """
        for name in ("bid", "ask"):
            value = getattr(self, name)
            if value is None:
                continue
            parsed = as_decimal(value, field=name)
            if not parsed.is_finite() or parsed <= Decimal(0):
                raise ExecutionError(
                    Code.INVALID_PAPER_QUOTE,
                    f"{self.identity.label}: {name} is {parsed}; a quote side must "
                    "be positive and finite",
                )
            object.__setattr__(self, name, quantise_price(parsed))
        if self.bid is not None and self.ask is not None and self.ask < self.bid:
            raise ExecutionError(
                Code.INVALID_PAPER_QUOTE,
                f"{self.identity.label}: the quote is crossed — ask {self.ask} is "
                f"below bid {self.bid}. That is a data fault, not a tradable "
                "market, and swapping the sides would invent a spread with the "
                "wrong sign. A zero spread is valid; a negative one is not.",
            )
        if self.observed_share_volume is not None:
            volume = quantise_quantity(
                as_decimal(self.observed_share_volume, field="observed_share_volume")
            )
            if volume < Decimal(0):
                raise ExecutionError(
                    Code.INVALID_PAPER_QUOTE,
                    f"{self.identity.label}: observed share volume is {volume}; "
                    "zero means nothing traded, and negative means nothing at all",
                )
            object.__setattr__(self, "observed_share_volume", volume)

    @property
    def has_quote(self) -> bool:
        """Whether both sides are present."""
        return self.bid is not None and self.ask is not None

    @property
    def mid(self) -> Decimal | None:
        """The midpoint, or None without both sides.

        The midpoint is the *cost reference* and never a fill price. Filling at
        the mid would mean crossing no spread, which is the one cost this model
        is built to charge.
        """
        if self.bid is None or self.ask is None:
            return None
        return quantise_price((self.bid + self.ask) / Decimal(2))

    @property
    def spread(self) -> Decimal | None:
        """`ask - bid`, or None without both sides."""
        if self.bid is None or self.ask is None:
            return None
        return quantise_price(self.ask - self.bid)

    def require_quote(self, *, policy: PaperExecutionPolicy) -> tuple[Decimal, Decimal]:
        """Both sides of the quote, or a refusal.

        Args:
            policy: The friction policy, which decides whether a quote is needed.

        Returns:
            `(bid, ask)`.

        Raises:
            ExecutionError: `PAPER_QUOTE_REQUIRED` if a side is missing and the
                policy requires a spread. Nothing is substituted — a single price
                standing in for a quote removes exactly the cost this charges.
        """
        if self.bid is None or self.ask is None:
            raise ExecutionError(
                Code.PAPER_QUOTE_REQUIRED,
                f"{self.identity.label} has "
                f"{'no bid' if self.bid is None else 'no ask'} and policy "
                f"{policy.policy_id} requires a spread. No sizing price, close or "
                "last trade is substituted: a single price in place of a quote "
                "would silently remove the spread cost this model exists to "
                "charge.",
            )
        return self.bid, self.ask

    def require_volume(self, *, policy: PaperExecutionPolicy) -> Decimal:
        """The observed share volume, or a refusal.

        Args:
            policy: The friction policy.

        Returns:
            The volume, which may legitimately be zero.

        Raises:
            ExecutionError: `PAPER_VOLUME_REQUIRED` if the volume is unknown and
                the policy requires one. Unknown is not unlimited.
        """
        if self.observed_share_volume is None:
            raise ExecutionError(
                Code.PAPER_VOLUME_REQUIRED,
                f"{self.identity.label} has no observed share volume and policy "
                f"{policy.policy_id} requires one. An unknown volume is an unknown "
                "capacity, not an unlimited one, so no participation ceiling can "
                "be computed and nothing fills.",
            )
        return self.observed_share_volume

    def capacity(self, *, policy: PaperExecutionPolicy) -> Decimal:
        """How much of this session's volume the policy permits.

        Args:
            policy: The friction policy.

        Returns:
            `observed_share_volume * max_volume_participation`. A ceiling on
            everything this system trades in the security that session, not a
            target and not a per-order allowance.

        Raises:
            ExecutionError: `PAPER_VOLUME_REQUIRED` if the volume is unknown.
        """
        return quantise_quantity(
            self.require_volume(policy=policy) * policy.max_volume_participation
        )

    def as_json(self) -> dict[str, Any]:
        """Return the quote as a JSON-safe mapping."""
        return {
            "identity": self.identity.label,
            "bid": None if self.bid is None else str(self.bid),
            "ask": None if self.ask is None else str(self.ask),
            "mid": None if self.mid is None else str(self.mid),
            "spread": None if self.spread is None else str(self.spread),
            "observed_share_volume": (
                None if self.observed_share_volume is None else str(self.observed_share_volume)
            ),
            "volume_unit": "SHARES",
            "source_fingerprint": self.source_fingerprint,
        }


def touch_price_for(side: OrderSide, *, bid: Decimal, ask: Decimal) -> Decimal:
    """The price a market order crosses the spread to reach.

    A buyer pays the ask and a seller receives the bid. That asymmetry *is* the
    spread cost, and it is why neither side ever trades at the midpoint.

    Args:
        side: Buy or sell.
        bid: The best bid.
        ask: The best ask.

    Returns:
        The touch price.
    """
    return ask if side is OrderSide.BUY else bid


def fill_price_for(
    side: OrderSide, *, bid: Decimal, ask: Decimal, policy: PaperExecutionPolicy
) -> Decimal:
    """The price a fill lands at: the touch, worsened by declared slippage.

    Strictly adverse on both sides — a buyer pays more than the ask and a seller
    receives less than the bid. There is no price-improvement branch, because a
    paper engine that could improve a fill can manufacture edge, and the policy
    refuses a negative slippage for the same reason.

    Args:
        side: Buy or sell.
        bid: The best bid.
        ask: The best ask.
        policy: The friction policy.

    Returns:
        The fill price.

    Raises:
        ExecutionError: `INVALID_PAPER_QUOTE` if the resulting price is not
            positive, which a large enough slippage on a penny quote could
            otherwise produce for a sell.
    """
    fraction = policy.slippage_fraction
    touch = touch_price_for(side, bid=bid, ask=ask)
    price = (
        touch * (Decimal(1) + fraction)
        if side is OrderSide.BUY
        else touch * (Decimal(1) - fraction)
    )
    quantised = quantise_price(price)
    if quantised <= Decimal(0):
        raise ExecutionError(
            Code.INVALID_PAPER_QUOTE,
            f"a {side} fill at bid {bid} / ask {ask} with "
            f"{policy.adverse_slippage_bps}bps of slippage computes to {quantised}, "
            "which is not a tradable price",
        )
    return quantised


@dataclass(frozen=True)
class PaperMarketSessionSnapshot:
    """Quotes and volume for one completed market session.

    Attributes:
        session: The session this describes, as an ISO date.
        as_of: The execution opportunity this snapshot represents, ISO 8601 with
            offset. Every figure here belongs to this instant, and a fill against
            it is stamped here — never backdated inside the session.
        quotes: One quote per security.
        source: Where the snapshot came from.
    """

    session: str
    as_of: str
    quotes: tuple[SessionQuote, ...] = ()
    source: str = "UNSPECIFIED"

    def __post_init__(self) -> None:
        """Validate the session and refuse a duplicated security.

        Raises:
            ExecutionError: `EXECUTION_EVENT_INVALID` on an unparseable session
                or instant, or a naive instant; `INVALID_PAPER_QUOTE` if a
                security is quoted twice.
        """
        try:
            date.fromisoformat(self.session)
        except ValueError as exc:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                f"session {self.session!r} is not an ISO date",
            ) from exc
        try:
            moment = datetime.fromisoformat(self.as_of)
        except ValueError as exc:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID, f"as_of {self.as_of!r} is not ISO 8601"
            ) from exc
        if moment.tzinfo is None:
            raise ExecutionError(
                Code.EXECUTION_EVENT_INVALID,
                "a market session snapshot's as_of must carry a UTC offset; a naive "
                "instant cannot be ordered against an order's acknowledgement",
            )
        seen: set[tuple[str, int]] = set()
        for quote in self.quotes:
            if quote.identity.key in seen:
                raise ExecutionError(
                    Code.INVALID_PAPER_QUOTE,
                    f"{quote.identity.label} is quoted twice for session "
                    f"{self.session}; whichever was read last would decide the fill",
                )
            seen.add(quote.identity.key)

    @property
    def session_date(self) -> date:
        """The session as a date."""
        return date.fromisoformat(self.session)

    @property
    def moment(self) -> datetime:
        """The execution opportunity this snapshot represents."""
        return datetime.fromisoformat(self.as_of)

    def quote_for(self, identity: SecurityIdentity) -> SessionQuote | None:
        """The quote for one security, if present.

        Args:
            identity: The security.

        Returns:
            Its quote, or None.
        """
        for quote in self.quotes:
            if quote.identity.key == identity.key:
                return quote
        return None

    def require_session(self, session: str, *, order_id: str) -> None:
        """Refuse a snapshot describing a different session than the order's.

        Args:
            session: The session the order belongs to.
            order_id: For the message.

        Raises:
            ExecutionError: `PAPER_MARKET_SESSION_MISMATCH` on any difference. A
                quote from one session says nothing about what another session's
                order could have traded at, and filling across the boundary would
                be trading on the wrong day's market.
        """
        if self.session != session:
            raise ExecutionError(
                Code.PAPER_MARKET_SESSION_MISMATCH,
                f"{order_id} belongs to session {session} and this snapshot "
                f"describes {self.session}. A quote from one session says nothing "
                "about what another session's order could have traded at.",
            )

    def body(self) -> dict[str, Any]:
        """The fingerprinted body."""
        return {
            "fill_model": PAPER_SESSION_FILL_MODEL,
            "session": self.session,
            "as_of": self.as_of,
            "source": self.source,
            "quotes": [quote.as_json() for quote in sorted(self.quotes, key=lambda q: q.identity)],
            "timing": (
                "a completed session; the execution opportunity is this as_of, and "
                "bid, ask, volume and fill all belong to it. No fill is backdated "
                "inside the session."
            ),
        }

    def fingerprint(self) -> str:
        """The snapshot's deterministic digest."""
        return fingerprint_of(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the snapshot as a JSON-safe mapping."""
        return {**self.body(), "snapshot_fingerprint": self.fingerprint()}


def quotes_from(
    rows: Sequence[Mapping[str, Any]], *, identities: Mapping[str, Any]
) -> tuple[SessionQuote, ...]:
    """Build session quotes from their JSON form.

    Args:
        rows: `[{"identity", "bid", "ask", "observed_share_volume"}]`.
        identities: Label-to-identity lookup.

    Returns:
        The quotes.
    """
    return tuple(
        SessionQuote(
            identity=identities[str(row["identity"])],
            bid=None if row.get("bid") is None else as_decimal(row["bid"], field="bid"),
            ask=None if row.get("ask") is None else as_decimal(row["ask"], field="ask"),
            observed_share_volume=(
                None
                if row.get("observed_share_volume") is None
                else as_decimal(row["observed_share_volume"], field="volume")
            ),
            source_fingerprint=str(row.get("source_fingerprint", "")),
        )
        for row in rows
    )
