"""Which listings belong in the scannable universe.

Compounder Radar screens U.S.-listed common stock. Everything else on the tape —
funds, warrants, rights, units, preferred lines — has fundamentals that either do
not exist or do not mean what the metric engine assumes they mean.

The checks here are heuristics on the symbol and the registered name, because
that is what market-data providers reliably supply. They are deliberately shallow.
Chasing every exotic security structure is a rabbit hole with no bottom, and the
cost of wrongly excluding an obscure listing is one missed candidate, while the
cost of wrongly *including* a warrant is a nonsense row near the top of a
ranking.
"""

from __future__ import annotations

import re

SUPPORTED_EXCHANGES = frozenset({"NASDAQ", "NYSE", "AMEX", "NYSE AMERICAN", "NYSEAMERICAN"})
"""Exchange codes accepted into the universe.

`AMEX` is what Alpaca returns for NYSE American; the longer spellings cover
providers that write it out. Notably absent is `ARCA`, which is where U.S. ETFs
list.
"""

#: Suffixes brokers append to a root symbol to mark a non-common line. Matched
#: after the separator so `BRK.B`, an ordinary class-B share, is not caught.
_NON_COMMON_SUFFIXES = frozenset({"W", "WS", "WT", "R", "RT", "U", "UN", "P", "PR"})

_SUFFIX_PATTERN = re.compile(r"[.\-/+]([A-Z]{1,2})$")

#: Words in a registered name that identify a fund or a non-equity instrument.
_NON_COMMON_NAME_WORDS = (
    "etf",
    "etn",
    "exchange traded",
    "exchange-traded",
    "index fund",
    "mutual fund",
    " fund",
    "unit trust",
    "royalty trust",
    "warrant",
    "warrants",
    "rights",
    "preferred",
    "depositary",
    "acquisition corp unit",
    " units",
)


def is_supported_exchange(exchange: str | None) -> bool:
    """Return whether the listing venue is one this project screens.

    Args:
        exchange: Exchange code as reported by the provider. None counts as
            unsupported — an unknown venue is not assumed to be a good one.

    Returns:
        True when the exchange is in `SUPPORTED_EXCHANGES`.
    """
    if exchange is None:
        return False
    return exchange.strip().upper() in SUPPORTED_EXCHANGES


def is_common_stock(ticker: str, name: str) -> bool:
    """Return whether a listing looks like ordinary common stock.

    Args:
        ticker: The exchange symbol.
        name: The registered security name.

    Returns:
        False when the symbol carries a warrant, right, unit or preferred suffix,
        or the name contains a word identifying a fund or derivative line.
    """
    match = _SUFFIX_PATTERN.search(ticker.strip().upper())
    if match and match.group(1) in _NON_COMMON_SUFFIXES:
        return False

    lowered = f" {name.strip().lower()} "
    return not any(word in lowered for word in _NON_COMMON_NAME_WORDS)


def is_supported_listing(ticker: str, name: str, exchange: str | None) -> bool:
    """Return whether a listing is both on a supported venue and common stock.

    Args:
        ticker: The exchange symbol.
        name: The registered security name.
        exchange: Exchange code as reported by the provider.

    Returns:
        True only when both `is_supported_exchange` and `is_common_stock` pass.
    """
    return is_supported_exchange(exchange) and is_common_stock(ticker, name)
