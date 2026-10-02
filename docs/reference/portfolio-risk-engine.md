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
