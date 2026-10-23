"""Deriving market signals from the point-in-time feature record.

The pure half — what a signal means, which thresholds apply, how a percentile is
computed — lives in `domain.signals` and never touches a database. This package
is the other half: it reads one day's immutable feature snapshots, builds the
cross-section, and writes the result as immutable signal rows.

It reads `pit_feature_snapshots` and nothing else. That is the whole
point-in-time guarantee, and it is structural rather than careful: the
operational tables that get rewritten as better information arrives —
`financial_snapshots`, `score_snapshots`, `companies` — are not consulted here
at all, so a signal for a past date cannot drift when they change.
"""

from stock_screener.signals.derivation import (
    TRADING_EVIDENCE,
    SignalDerivationResult,
    derive_historical_market_signals,
    derive_market_signals,
    derive_market_signals_v2,
    explain_peer_membership,
)

__all__ = [
    "TRADING_EVIDENCE",
    "SignalDerivationResult",
    "derive_historical_market_signals",
    "derive_market_signals",
    "derive_market_signals_v2",
    "explain_peer_membership",
]
