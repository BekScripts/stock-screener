"""The execution engine's frozen contracts.

Each name here is a decision that downstream artefacts bind to. An execution
report that cannot say which fill model produced its prices, or which sequencing
rule decided that a sale preceded a purchase, is not reproducible — and a
simulated result nobody can reproduce is worth very little.

The baseline is deliberately narrow: market orders, day only, one price per
security, no slippage and no randomness. Narrow is what makes it *checkable*. A
simulator that models spread, impact and latency has many places for a bug to
hide behind plausibility; this one has almost none, and when its arithmetic is
wrong a test can say so exactly.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DAY_EXPIRY_POLICY",
    "EXECUTION_ENGINE_VERSION",
    "EXECUTION_ENGINE_VERSION_V1_1",
    "EXECUTION_ENGINE_VERSION_V1_2",
    "FILL_ATTRIBUTION_RULE",
    "JOURNAL_CHAIN_VERSION",
    "ORDER_SEQUENCING_RULE",
    "PAPER_FILL_MODEL",
    "PAPER_FILL_POLICY",
    "PAPER_SESSION_FILL_MODEL",
    "QUANTITY_SCALE_PLACES",
    "REQUIRED_ACCOUNTING_VERSION",
    "SESSION_ROLLOVER_POLICY",
    "engine_policy",
]

#: The P2.0 execution engine contract. Frozen: it models no execution cost at
#: all, and its goldens must keep reproducing byte for byte.
EXECUTION_ENGINE_VERSION = "EXECUTION_ENGINE_V1"

#: The P2.1 contract, which adds spread crossing, deterministic adverse
#: slippage, share-volume participation, real DAY expiry and calendar-derived
#: multi-session rollover. A separate version because every one of those changes
#: what a fill *is*: the same plan under V1 and V1.1 produces different prices,
#: different quantities and a different number of orders, so neither can be
#: reproduced from the other's rules.
EXECUTION_ENGINE_VERSION_V1_1 = "EXECUTION_ENGINE_V1_1"

#: The P2.2 contract, which adds a second *venue*: a provider's paper endpoint.
#: A separate version because it changes something more fundamental than a
#: price. V1 and V1.1 are reproducible — the same inputs give the same fills
#: forever. A V1.2 run at an external venue is not: its fills came from a real
#: market at a real moment, and nothing in this repository can reproduce them.
#: The local fill model is unchanged, so a V1.2 run bound to the local venue is
#: a V1.1 run under a new name; it is the external binding that makes the
#: difference, which is why the venue is recorded in every artefact.
EXECUTION_ENGINE_VERSION_V1_2 = "EXECUTION_ENGINE_V1_2"

#: Which accounting rules a new execution-managed book must use. Named rather
#: than defaulted: P1.4 left V1 as the accounting default so pre-existing ledgers
#: kept their meaning, and inheriting that default here would silently open new
#: books under the basis convention P1.4 exists to replace.
REQUIRED_ACCOUNTING_VERSION = "PORTFOLIO_ACCOUNTING_V2"

#: How planned orders are ordered. Sells release cash before buys consume it,
#: which is the only sequencing a no-margin book can actually execute.
ORDER_SEQUENCING_RULE = "SELLS_BEFORE_BUYS_V1"

#: How a V1 market paper order is priced: at the explicit price the execution
#: snapshot supplies, and at nothing else. No spread, no slippage, no capacity.
PAPER_FILL_MODEL = "PAPER_REFERENCE_PRICE_FILL_V1"

#: How a V1.1 market paper order is priced and sized: crossing the quoted spread,
#: worsened by a declared adverse slippage, and capped by a share-volume
#: participation ceiling shared across every order touching that security in that
#: session. Still fully deterministic — the friction is computed from explicit
#: inputs, not drawn from anywhere.
PAPER_SESSION_FILL_MODEL = "PAPER_SESSION_PARTICIPATION_FILL_V1"

#: How a DAY order ends when a session closes with quantity outstanding. Named
#: because the alternative — letting it survive — is what P2.0 did, and the
#: difference is the whole point of V1.1's lifecycle change.
DAY_EXPIRY_POLICY = "DAY_ORDER_SESSION_EXPIRY_V1"

#: How residual quantity reaches a later session: as a new child DAY order, never
#: by extending the life of an expired one.
SESSION_ROLLOVER_POLICY = "CHILD_DAY_ORDER_ROLLOVER_V1"

#: What quantity a paper fill takes when the snapshot states no availability
#: limit: all of the remaining quantity.
PAPER_FILL_POLICY = "PAPER_FULL_FILL_V1"

#: How a partial fill divides across the strategies that authorised the order.
FILL_ATTRIBUTION_RULE = "PROPORTIONAL_FILL_ATTRIBUTION_V1"

#: The journal's hash-chain contract.
JOURNAL_CHAIN_VERSION = "EXECUTION_JOURNAL_HASH_CHAIN_V1"

#: Quantities are carried to this many decimal places, matching the accounting
#: layer's quantity scale so a fill quantity never changes shape when it crosses.
QUANTITY_SCALE_PLACES = 8


def engine_policy() -> dict[str, Any]:
    """Describe every frozen execution contract.

    Returns:
        The policy, for the manifest and for anyone asking what this engine
        actually simulates.
    """
    return {
        "engine_version": EXECUTION_ENGINE_VERSION,
        "order_contracts": {
            "order_types": ["MARKET"],
            "time_in_force": ["DAY"],
            "sides": ["BUY", "SELL"],
            "short_selling": "NOT_SUPPORTED",
            "fractional_quantities": "SUPPORTED — positive Decimal, 8 places",
            "durable_identity": "(kind, identity_id); a ticker is metadata",
        },
        "sequencing": ORDER_SEQUENCING_RULE,
        "paper_fill_model": PAPER_FILL_MODEL,
        "paper_fill_policy": PAPER_FILL_POLICY,
        "fill_attribution": FILL_ATTRIBUTION_RULE,
        "journal_chain": JOURNAL_CHAIN_VERSION,
        "required_accounting_version": REQUIRED_ACCOUNTING_VERSION,
        "what_this_is": (
            "a deterministic correctness simulator. Given the same inputs it "
            "produces the same orders, the same fills and the same book, every "
            "time, which is what makes an execution bug findable."
        ),
        "what_this_is_not": (
            "This is not a realistic fill simulator. It models no spread, no slippage, no "
            "market impact, no volume participation, no latency and no queue "
            "position. A backtest run through it will be optimistic by exactly "
            "the costs it does not model, and that gap is not small."
        ),
        "never_does": [
            "contact a broker, paper or live",
            "hold a broker credential",
            "place a live order",
            "place a remote paper order",
            "introduce randomness into a fill",
            "estimate market impact",
            "optimise an execution parameter",
            "maintain cash, positions or P&L of its own",
            "change the book without an admitted accounting FillEvent",
        ],
    }
