---
type: reference
title: P1.0 portfolio and risk engine
---

# P1.0 portfolio and risk engine

`stock_screener.portfolio_risk` is the deterministic authority between a
strategy's output and anything that could become a trade. Run by
`stock-screener portfolio evaluate <request.json>`.

This is `PORTFOLIO_RISK_ENGINE_V1`: a foundation, not a production risk system.
No policy in it is authorised for live capital.

## The authority chain

```text
Strategy          signal authority          what is worth holding
PortfolioEngine   allocation authority      what would express that
RiskEngine        veto and resize authority what is permitted
ExecutionEngine   order-planning authority  later phase
BrokerAdapter     external-action authority later phase
```

A strategy may request. The risk engine may approve, resize or reject. There is
no fourth option and no way round it: `evaluate` is the only path through, and no
function in the package accepts a `force`, `bypass`, `override` or `ignore_limits`
argument — asserted by a test that walks every signature, because an override
that exists is an override that gets used during the incident it would worsen.

## Construction and risk are different questions

| Stage | Question |
| --- | --- |
| construction | what portfolio would best express these intents? |
| risk | what portion of it is permitted? |

Collapsing them is the usual shape and it makes results unexplainable: a position
that comes out at 4% could be a 4% request, a capped 25% request, or a scaled 8%
request, and a single opaque function cannot tell you which. Keeping them apart is
why every final weight carries the reason codes that produced it.

## The constraint model

`DETERMINISTIC_STAGED_CONSTRAINTS_V1`. Limits are applied as one pass over frozen
stages, not a chain of mutations whose order changes the answer:

1. **Count** — rank by priority descending, then durable identity; keep the first
   `max_positions`, reject the rest with `MAX_POSITION_COUNT`. Ranking first means
   the surviving set does not depend on which names were later resized.
2. **Per-name cap** — clamp to `max_position_weight`. Reducing only.
3. **Budget** — fit the total into `investable_budget`, the tighter of the gross
   cap and what the cash reserve leaves. Over-budget scales down proportionally,
   which cannot breach stage 2 because it only reduces. Under-budget is left alone
   unless the policy declared the weights relative, in which case the excess is
   redistributed among names below their cap until the budget is met or no
   capacity remains — a fixed point bounded by the number of names.
4. **Dust** — drop targets below `min_position_weight`. The freed budget becomes
   cash rather than being redistributed, because redistributing could push another
   name below the floor and the loop would not obviously terminate.
5. **Trade size** — pull any target whose change exceeds
   `max_trade_notional_weight` back toward the current weight.
6. **Mode** — see below.
7. **Exits** — any held position with no approved target gets a target of zero.

## Modes

| Mode | Increases and entries | Reductions and exits |
| --- | --- | --- |
| `NORMAL` | permitted | permitted |
| `REDUCE_ONLY` | rejected | permitted |
| `HALTED` | rejected | rejected |

**Halting is not liquidating.** A halted policy holds every position exactly
where it stands and emits no sale. The distinction is the one an operator depends
on: a kill switch that liquidated would dump the book at whatever the market was
doing at the moment someone reached for it. Liquidation is a separate, explicit
act this layer deliberately cannot perform.

## Normalisation is declared, never inferred

Weights summing to 0.60 might be a strategy deliberately holding 40% cash, or a
ranking expressing relative conviction. Those want opposite treatment and the
numbers cannot distinguish them, so the policy states which:

- `PRESERVE_ABSOLUTE_WEIGHTS` — 60% invested means 60% invested.
- `NORMALIZE_TO_INVESTABLE_BUDGET` — scale to fill the budget.

Cash is a first-class asset throughout: `sum(targets) + cash == 1` is asserted,
not assumed, so nothing is ever implicitly fully invested.

## Identity

The durable key is `(kind, identity_id)`, inherited from the research layer. A
ticker is display metadata and never a key, because a symbol is reassigned when a
company delists and whoever inherits it is a different company. Two identities
sharing a ticker stay separate, and a test constructs exactly that case.

## Reason codes

Machine-readable and stable, so a monitor counting `MAX_POSITION_WEIGHT` keeps
working when someone improves a message: `MAX_POSITION_WEIGHT`,
`MAX_POSITION_COUNT`, `MAX_GROSS_EXPOSURE`, `MAX_NET_EXPOSURE`,
`MIN_CASH_RESERVE`, `MIN_POSITION_WEIGHT`, `MAX_TRADE_NOTIONAL`, `BUDGET_SCALED`,
`LONG_ONLY_VIOLATION`, `TRADING_HALTED`, `REDUCE_ONLY`,
`DUPLICATE_STRATEGY_INTENT`, `MULTI_STRATEGY_ALLOCATION_POLICY_REQUIRED`,
`AS_OF_MISMATCH`, `NOT_REQUESTED`, `WITHIN_LIMITS`, and the rest in
`REASON_CODES`.

## Fail closed

A malformed request is refused, never repaired. Two intents for one identity are
not summed — a strategy that disagrees with itself produces a position size nobody
chose. A negative weight under long-only is not clamped to flat. A missing source
fingerprint is not filled in. A missing policy or snapshot authorises nothing.

## Arithmetic

`PORTFOLIO_ARITHMETIC_V1`: float64, with every weight quantised to ten decimal
places before it is hashed or serialised, so a digest is a function of the decided
portfolio rather than of summation order. `Decimal` was considered and not used —
every input model already carries floats, so holding `Decimal` internally would
add a conversion seam at both boundaries without the exactness guarantee
surviving either. Tolerances are centralised: `WEIGHT_TOLERANCE` for one weight,
`EXPOSURE_TOLERANCE` for sums, and `HOLD_TOLERANCE` — a *policy* choice at one
basis point, saying what drift is not worth trading.

## Nothing here can trade

An `ExecutionIntent` is inert: no quantity in shares, no broker field, no time in
force, no side effect. It carries `is_an_order: false` and the package imports no
HTTP client, no socket, no broker SDK and no database — asserted by tests that
walk the imports and by tests that run the engine with every socket and every
`sqlite3.connect` monkeypatched to raise.

Share quantisation, lot sizing, spread, impact, latency, partial fills and
commissions all belong to an execution phase that does not exist yet. So do
volatility targeting, correlation constraints and Kelly sizing. Sector caps are
deliberately absent: sector history is a known weak area in this project and a cap
built on it would inherit that weakness silently.

## Turnover

`PORTFOLIO_WEIGHT_TURNOVER_ESTIMATE_V1` is
`0.5 × Σ|target_weight − current_weight|`. It models no spread, no impact, no
partial fill and no commission, so it is **not** comparable with a realised
execution turnover, nor with the research layer's one-way rebalance turnover. The
name exists so the two can never be quietly equated.

## Auditability

Every output binds all three inputs — the intent set, the portfolio snapshot and
the risk policy — so a changed limit or a changed holding necessarily changes the
output digest. `PortfolioDecisionManifest` records each fingerprint alongside the
commit. Operational timestamps sit beside the fingerprints, never inside them: a
hash that moves with the clock can never establish that two runs agreed.

Given any approved target you can answer which strategy requested it, what it
requested, what was held, which policy applied, what changed, and why anything was
resized or rejected.

## Commands

| Command | Does |
| --- | --- |
| `portfolio evaluate <request.json>` | reports what would be authorised |
| `portfolio show-policy` | prints the example policy and its fingerprint |

Named `evaluate` rather than `execute` deliberately. There is no `--force`.

---

# P1.1 — volatility, liquidity, covariance and drawdown

`PORTFOLIO_RISK_ENGINE_V1_1` makes the authority aware of market risk. Supplying
no risk snapshot keeps V1 semantics exactly, so a V1 decision and its golden
output still reproduce byte for byte — V1 is a version, not a former state of the
code.

## What P1.1 does not do

Stated plainly, because a risk layer invites the assumption that it does more than
it does. It does **not**:

- forecast volatility — it consumes a declared value and its fingerprint;
- forecast or estimate correlation — likewise;
- estimate market impact, slippage, spread, latency, partial fills or commissions;
- choose production thresholds — every number here is a test constant;
- optimise a portfolio, or search history for a limit;
- submit an order, or hold a credential;
- enforce sector or industry caps;
- apply Kelly sizing, volatility *targeting*, beta targets or VIX scaling.

## Risk inputs

| Type | Carries |
| --- | --- |
| `SecurityRiskMetrics` | annualised volatility, average daily dollar volume, price |
| `CovarianceSnapshot` | an ordered identity list and an annualised matrix |
| `PortfolioRiskSnapshot` | NAV, peak NAV, the metrics and the covariance |

Units are frozen and validated. Volatility is an **annualised decimal**, so 25% is
`0.25`; a value above `MAX_PLAUSIBLE_VOLATILITY` is refused as a probable unit
error, because `25` meaning 25% and `25` meaning 2500% differ by a hundred and
nothing downstream could tell which was meant. ADV is **US dollars per session**,
never share volume.

**Missing is a state.** `MetricState` is `KNOWN`, `UNKNOWN` or `NOT_APPLICABLE`.
An unknown volatility is not zero, an unknown ADV is not infinite, and an absent
covariance row is not an absent correlation — each would convert ignorance into
permission. A `KNOWN` state with no value, or a value under an `UNKNOWN` state, is
a contradiction and is refused.

**A metric cannot describe the future.** `metric_as_of` must not postdate the
decision, and a snapshot that does raises `RISK_SNAPSHOT_AFTER_DECISION`. A
freshness bound is supported but has no default: P1.1 defines no production
staleness threshold and does not invent one.

## Covariance validation

Square, symmetric within `COVARIANCE_TOLERANCE`, finite, non-negative diagonal,
unique identities, explicit ordering. Nothing is repaired: an asymmetric matrix is
not silently averaged and a negative variance is not clamped, because both are
symptoms of a fault in whatever produced them and hiding the symptom keeps the
fault. A matrix covering a wider universe is subselected deterministically; a
*missing* required row is `RISK_METRIC_MISSING`, never a zero.

## Portfolio volatility

`sigma_p = sqrt(w' * Sigma * w)` over the risky weights. **Cash contributes
zero** — this is a market-risk model and cash carries no market exposure in it,
which understates risk in a currency crisis and is stated rather than hidden.

When `sigma_p` exceeds `max_portfolio_volatility`, one scale is applied:
`limit / sigma_p`. Because volatility is homogeneous of degree one in the weights,
that single multiplication lands exactly on the limit, preserves every relative
weight, and cannot raise any target. When `sigma_p` is already inside the limit
nothing happens: **the limit is a ceiling, not a target.** Volatility targeting is
a later phase and a different decision.

## Liquidity capacity

Two separate limits, because holding a large position and acquiring one in a day
are different risks:

| Limit | Meaning |
| --- | --- |
| `max_position_adv_fraction` | `target_notional <= ADV × fraction` |
| `max_trade_adv_fraction` | `abs(notional change) <= ADV × fraction` |

Both are **capacity controls, not impact models**. Existing risk and new risk are
distinguished: a position already above its capacity is not an instruction to buy
up to the limit, and is never prevented from being reduced or exited. The ceiling
is the greater of the limit and what is already held, so liquidity constrains the
*increase*.

An unknown ADV follows `missing_liquidity_policy`: `REJECT_INCREASE` (the
fail-safe default — hold where you stand, reduction still available),
`IGNORE_METRIC`, or `REQUIRE_METRIC` (refuse the whole decision).

## Drawdown overlay

`drawdown = nav / peak - 1`, necessarily non-positive; a peak below the current
NAV is refused. Ordered `DrawdownBand`s each carry a threshold, a gross multiplier
in `[0, 1]`, and a mode. The deepest triggered band is the one in force.

**The overlay can only tighten.** A multiplier above one is refused. A band that
allows more gross, or a weaker mode, at greater depth is refused. The effective
mode is the stricter of the base mode and the band's, by the frozen severity
ordering `NORMAL < REDUCE_ONLY < HALTED` — a policy already `HALTED` is never
relaxed by a band that would have said `NORMAL`.

**Halting still does not liquidate.** A band escalating to `HALTED` holds the book
and emits no sale.

The base policy is never mutated. `EffectiveRiskPolicy` is derived from it and the
risk snapshot, carries its own fingerprint, and records which band produced it, so
a tightened limit always traces to the loss that caused it.

## The frozen constraint order

Reordering these changes results, so it is a semantic change requiring an engine
version bump — not a refactor:

```text
1  validate inputs and risk timing
2  seed ceilings at pre-risk weights
3  derive effective policy from drawdown
4  position count cap
5  per-name weight cap
6  position liquidity capacity
7  trade liquidity capacity
8  missing risk metric ceilings
9  effective gross and cash budget
10 portfolio volatility ceiling
11 dust floor
12 trade notional cap
13 effective operating mode
14 forced exits
15 invariants
```

Liquidity precedes volatility scaling because scaling only reduces: applying it
first would be undone by a later cap and the result could sit above the ceiling.

## Risk controls only reduce risk

The property that makes the staged model trustworthy, and the one that needed the
most care. Every ceiling is **seeded at the pre-risk weight** and only ever
lowered, so no later stage can hand back exposure an earlier one removed.

That matters most for the normalising construction mode. Filling the investable
budget is exactly the mechanism that would otherwise defeat a liquidity cap:
capping one name frees budget, and the redistribution would give it to another
name — leaving aggregate risk unchanged. Under V1.1 the budget a cap frees becomes
**cash**. An invariant asserts that no approved target exceeds its pre-risk value,
where the pre-risk reference is the constructed weight for a requested name and
the current weight for a position merely retained.

## Reason codes are additive

A name constrained by the weight cap, then its position capacity, then its trade
capacity, carries all three codes in the order they were imposed. Earlier reasons
are never overwritten, because the question afterwards is not *which limit won* but
*what happened*.

## The risk report

`PortfolioRiskReport` shows both sides of every number: requested, constructed and
approved exposure; volatility before and after; cash before and after; the names
each control touched; per-position weight × volatility; and covariance diagnostics
— average and maximum pairwise correlation, and component variances that sum
exactly to the portfolio variance. All diagnostics: P1.1 enforces no correlation
limit and no risk budget, because each needs its own design and its own evidence.

## Example policy

`EXAMPLE_ADVANCED_RISK_POLICY_V1` is **`NOT_PRODUCTION_AUTHORIZED`**. Every number
in it was chosen to make a control observable in a test. None was derived from
historical return, Sharpe or drawdown — deriving them that way is the parameter
fitting this programme exists to avoid. Promoting any of them requires a dedicated
risk-policy phase with its own evidence.
