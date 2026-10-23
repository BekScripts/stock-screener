"""Translating a durable identity into the string a venue trades, and back.

The whole system addresses a security by `(kind, identity_id)` and treats the
ticker as a dated display attribute. A venue does the opposite: it trades
`"AAPL"`, and has no idea what this project's primary keys mean. Something has to
bridge that, and this is the only place it happens.

**There is no fallback to the identity's display symbol.** That fallback is one
line long, would make every test pass, and is the single most dangerous line this
phase could contain. A ticker is reassigned when a company is delisted and
another takes the string: `SecurityIdentity.symbol` holds whatever the ticker was
when that row was loaded, so routing on it would eventually send a real order for
company A to whatever company now answers to A's old ticker. The durable identity
exists precisely so that cannot happen, and honouring it here means refusing —
`BROKER_SYMBOL_MAPPING_UNRESOLVED` — rather than guessing.

So a mapping is an explicit, recorded decision. It is fingerprinted, and that
fingerprint goes into the execution report, so the answer to "what did this run
believe AAPL was" is in the record rather than in whatever the database held that
afternoon.

The reverse direction has its own failure. Two identities mapped to one venue
symbol is not a tie to break — it is an ambiguity that would attribute a remote
fill to the wrong company, so it is refused in both directions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.execution.errors import Code, ExecutionError
from stock_screener.execution.fingerprint import fingerprint_of

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "SYMBOL_MAPPING_POLICY",
    "SymbolMapping",
    "VenueSymbol",
]

#: How a durable identity becomes a venue symbol: by explicit declaration only.
#: Named in the manifest because the alternative — deriving it from the stored
#: display ticker — is a routing error waiting for a ticker to be reassigned.
SYMBOL_MAPPING_POLICY = "EXPLICIT_IDENTITY_TO_VENUE_SYMBOL_V1"


@dataclass(frozen=True)
class VenueSymbol:
    """One declared identity-to-symbol decision.

    Attributes:
        kind: The identity kind.
        identity_id: The durable primary key.
        symbol: The string the venue trades.
        tradable: Whether the venue currently accepts orders in it. A mapped but
            untradable asset is a distinct answer from an unmapped one: the first
            is a venue state, the second is a missing decision here.
        asset_class: What the venue calls the asset class, for the record.
        fractionable: Whether the venue trades it fractionally.
    """

    kind: str
    identity_id: int
    symbol: str
    tradable: bool = True
    asset_class: str = "us_equity"
    fractionable: bool = False

    def __post_init__(self) -> None:
        """Refuse a mapping that could not route an order.

        Raises:
            ExecutionError: `BROKER_SYMBOL_MAPPING_UNRESOLVED` if the symbol is
                blank or the identity id is not a positive key.
        """
        if not self.symbol.strip():
            raise ExecutionError(
                Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
                f"{self.kind}:{self.identity_id} is mapped to a blank symbol",
            )
        if self.identity_id <= 0:
            raise ExecutionError(
                Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
                f"a mapping needs a positive identity id, got {self.identity_id!r}",
            )

    @property
    def key(self) -> tuple[str, int]:
        """The durable key this mapping is for."""
        return (self.kind, self.identity_id)

    def body(self) -> dict[str, Any]:
        """Return the mapping's semantic content."""
        return {
            "identity": f"{self.kind}:{self.identity_id}",
            "symbol": self.symbol,
            "tradable": self.tradable,
            "asset_class": self.asset_class,
            "fractionable": self.fractionable,
        }


@dataclass(frozen=True)
class SymbolMapping:
    """Every declared mapping for one run, resolvable in both directions.

    Attributes:
        entries: The declared mappings, ordered by durable key.
    """

    entries: tuple[VenueSymbol, ...]

    def __post_init__(self) -> None:
        """Refuse a mapping table that cannot answer unambiguously.

        Raises:
            ExecutionError: `BROKER_SYMBOL_MAPPING_UNRESOLVED` if one identity
                is mapped twice, or if two identities share a venue symbol. The
                second is the one worth refusing loudly: a shared symbol would
                attribute a remote fill to whichever company happened to be
                found first.
        """
        by_key: dict[tuple[str, int], VenueSymbol] = {}
        by_symbol: dict[str, VenueSymbol] = {}
        for entry in self.entries:
            if entry.key in by_key:
                raise ExecutionError(
                    Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
                    f"{entry.kind}:{entry.identity_id} is mapped twice, to "
                    f"{by_key[entry.key].symbol!r} and {entry.symbol!r}",
                )
            upper = entry.symbol.upper()
            if upper in by_symbol:
                other = by_symbol[upper]
                raise ExecutionError(
                    Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
                    f"symbol {entry.symbol!r} is mapped from two identities, "
                    f"{other.kind}:{other.identity_id} and "
                    f"{entry.kind}:{entry.identity_id}. A shared symbol would "
                    "attribute a remote fill to whichever was found first",
                )
            by_key[entry.key] = entry
            by_symbol[upper] = entry

    @classmethod
    def of(cls, entries: Iterable[VenueSymbol]) -> SymbolMapping:
        """Build a mapping, ordered deterministically.

        Args:
            entries: The declared mappings, in any order.

        Returns:
            The mapping, ordered by durable key so its fingerprint does not
            depend on declaration order.
        """
        return cls(entries=tuple(sorted(entries, key=lambda e: e.key)))

    @classmethod
    def from_symbols(cls, declared: Mapping[tuple[str, int], str]) -> SymbolMapping:
        """Build a mapping from durable keys to symbols.

        Args:
            declared: `(kind, identity_id)` to venue symbol.

        Returns:
            The mapping, with venue attributes left at their conservative
            defaults.
        """
        return cls.of(
            VenueSymbol(kind=kind, identity_id=identity_id, symbol=symbol)
            for (kind, identity_id), symbol in declared.items()
        )

    def entry_for(self, identity: SecurityIdentity) -> VenueSymbol:
        """Resolve an identity to its declared mapping.

        Args:
            identity: The security, by durable key.

        Returns:
            The declared mapping.

        Raises:
            ExecutionError: `BROKER_SYMBOL_MAPPING_UNRESOLVED` if the identity
                was never mapped. Deliberately not satisfied from
                `identity.symbol`: that field holds a dated display ticker, and
                a reassigned ticker would route this order to another company.
        """
        for entry in self.entries:
            if entry.key == identity.key:
                return entry
        raise ExecutionError(
            Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
            f"{identity.label} has no declared venue symbol. The stored display "
            f"ticker ({identity.symbol!r}) is not used as a fallback: a ticker is "
            "reassigned when a company delists, so routing on it would eventually "
            "send this order to a different company",
        )

    def symbol_for(self, identity: SecurityIdentity) -> str:
        """Resolve an identity to a tradable venue symbol.

        Args:
            identity: The security.

        Returns:
            The venue symbol.

        Raises:
            ExecutionError: `BROKER_SYMBOL_MAPPING_UNRESOLVED` if unmapped;
                `BROKER_ASSET_NOT_TRADABLE` if the venue will not accept orders
                in it. Kept distinct because one is a missing decision here and
                the other is a condition at the venue.
        """
        entry = self.entry_for(identity)
        if not entry.tradable:
            raise ExecutionError(
                Code.BROKER_ASSET_NOT_TRADABLE,
                f"{identity.label} maps to {entry.symbol!r}, which the venue is not "
                "accepting orders in",
            )
        return entry.symbol

    def identity_key_for(self, symbol: str) -> tuple[str, int]:
        """Resolve a venue symbol back to a durable key.

        Used when reading remote state: a provider answers in symbols, and a
        position cannot be reconciled until it is attributed to an identity.

        Args:
            symbol: The venue symbol.

        Returns:
            The `(kind, identity_id)` key.

        Raises:
            ExecutionError: `BROKER_SYMBOL_MAPPING_UNRESOLVED` if this run never
                mapped that symbol.
        """
        upper = symbol.strip().upper()
        for entry in self.entries:
            if entry.symbol.upper() == upper:
                return entry.key
        raise ExecutionError(
            Code.BROKER_SYMBOL_MAPPING_UNRESOLVED,
            f"the venue answered for symbol {symbol!r}, which this run never mapped; "
            "it cannot be attributed to an identity",
        )

    def knows_symbol(self, symbol: str) -> bool:
        """Whether a venue symbol was mapped by this run.

        Args:
            symbol: The venue symbol.

        Returns:
            True if mapped. Lets reconciliation report an unmapped remote holding
            as a break rather than raising mid-sweep — a break is a finding, and
            a sweep that stops at the first one finds only the first one.
        """
        upper = symbol.strip().upper()
        return any(entry.symbol.upper() == upper for entry in self.entries)

    def fingerprint(self) -> str:
        """Digest the mapping, so a report records which one it used."""
        return fingerprint_of(self.body())

    def body(self) -> dict[str, Any]:
        """Return the mapping's semantic content."""
        return {
            "policy": SYMBOL_MAPPING_POLICY,
            "entries": [entry.body() for entry in self.entries],
        }

    def as_json(self) -> dict[str, Any]:
        """Return the mapping with its own fingerprint."""
        return {**self.body(), "mapping_fingerprint": self.fingerprint()}
