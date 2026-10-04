---
type: reference
title: Paper execution engine
---

# Paper execution engine

`EXECUTION_ENGINE_V1`. The layer between an authorised portfolio target and an
accounting fill. Implemented in `src/stock_screener/execution/`.

It reaches no broker, holds no credential, places no live or remote paper order,
and introduces no randomness into a fill.

## The five authorities

| Authority | Decides |
| --- | --- |
| strategy | which opportunities exist |
| portfolio / risk | which targets are permitted |
| execution | how authorised deltas become orders |
| broker | what fills |
| accounting | holdings, cash, NAV and P&L |

None may impersonate another. In particular the paper broker holds no cash, no
positions and no P&L: buying power *is* accounting's cash and holdings *are*
accounting's holdings, so there is no second number that could disagree.

## Two invariants everything else rests on

**An order is not a position.** Creating, submitting or acknowledging one changes
no holding, no cash and no NAV. Only an admitted accounting `FillEvent` changes
the book.

**A target is not an order.** An `ApprovedPortfolio` and an `ExecutionIntent` are
inert. Orders exist only inside an `ExecutionPlan`, created by an explicit
planner call.

## Frozen contracts

| Contract | Version |
| --- | --- |
| Engine | `EXECUTION_ENGINE_V1` |
| Order sequencing | `SELLS_BEFORE_BUYS_V1` |
| Paper fill model | `PAPER_REFERENCE_PRICE_FILL_V1` |
| Paper fill policy | `PAPER_FULL_FILL_V1` |
| Fill attribution | `PROPORTIONAL_FILL_ATTRIBUTION_V1` |
| Journal hash chain | `EXECUTION_JOURNAL_HASH_CHAIN_V1` |
| Journal head anchor | `EXECUTION_JOURNAL_HEAD_ANCHOR_V1` |
| Store | `EXECUTION_STORE_V1` |
| Manifest | `EXECUTION_MANIFEST_V1` |
| Required accounting | `PORTFOLIO_ACCOUNTING_V2` |

## The order contract

`MARKET` and `DAY` only. Anything else is refused by name
(`UNSUPPORTED_ORDER_TYPE`, `UNSUPPORTED_TIME_IN_FORCE`) rather than approximated
— a limit order executed at market is not a near-miss, it is a trade nobody
authorised.

`BUY` and `SELL` only. There is no `SELL_SHORT` or `BUY_TO_COVER`, so a short is
unexpressible rather than merely unenforced; accounting refuses one regardless.

Positive `Decimal` quantities to eight places, so a fractional rebalance is
neither floored away nor rounded up past what was authorised. Identity is
`(kind, identity_id)`; the ticker is metadata and is excluded from every
fingerprint.

## Accounting version

**`PORTFOLIO_ACCOUNTING_V2` is demanded explicitly, never inherited.** The
accounting layer still defaults to V1 so ledgers written before P1.4 keep their
meaning; accepting that default here would open a new execution book under the
pooled-basis convention whose per-strategy figures P1.4 exists to correct. A
non-V2 book is `UNSUPPORTED_EXECUTION_ACCOUNTING_VERSION`, enforced on the book
binding, the session and the adapter.

## The execution plan

The authorisation boundary. It binds:

```
approved portfolio fingerprint   which targets were authorised
execution intents fingerprint    which deltas those implied
sizing snapshot fingerprint      what the quantities came from
valued snapshot fingerprint      what it was marked against
book binding                     ledger generation, engine version, state
execution policy versions        sequencing and attribution rules
```

Each order carries the plan's fingerprint, and the broker checks every order
against the plan it is executing. An order naming another plan is
`UNAUTHORIZED_ORDER`; one naming this plan with a stale fingerprint or altered
content is `EXECUTION_AUTHORIZATION_MISMATCH`.

The plan digests order *content* and not each order's back-reference to the plan,
because the back-reference is that digest — including it would make the two
mutually recursive.

There is no `force`, no `unsafe_submit` and no arbitrary-order path. Every public
broker method takes the plan, asserted by a test over the class's signatures.

### Drift

| Change | Response |
| --- | --- |
| this plan's own fills | expected; the expectation advances with them |
| any unrelated book event | `EXECUTION_STATE_DRIFT`; no further order is submitted |

Admitted fills stand. Rolling them back would mean un-trading a trade.

## Sizing

```
quantity = |notional delta| / sizing price
```

from an explicit `ExecutionSizingSnapshot`. The planner is pure: no price lookup,
no clock-dependent branch, no network. A planner that fetched a current price
would produce a different plan each run and no plan could be reproduced from its
inputs.

**The sizing price is not a fill price.** What the order fills at comes from the
execution snapshot and may differ. A non-positive, non-finite or absent sizing
price is `INVALID_EXECUTION_PRICE` — no order is created.

A `HOLD`, or any effectively zero delta, produces zero orders. A zero-quantity
order is not an order.

## Sequencing

`SELLS_BEFORE_BUYS_V1`. Exits and reductions are sequenced ahead of entries and
increases, because a no-margin book cannot fund purchases until sales have
settled cash into it. Within a group, orders run in durable-identity order.

No strategy is prioritised: positions are economic and aggregate, and executing
one strategy's orders first would hand it the better prices for no reason anybody
chose.

Input order is discarded entirely, so the plan's fingerprint is a property of the
portfolio delta rather than of how a caller assembled the list.

## The lifecycle

```
CREATED          -> SUBMITTED
SUBMITTED        -> ACKNOWLEDGED | REJECTED | CANCELLED
ACKNOWLEDGED     -> PARTIALLY_FILLED | FILLED | CANCELLED | REJECTED
PARTIALLY_FILLED -> PARTIALLY_FILLED | FILLED | CANCELLED
FILLED           -> (terminal)
CANCELLED        -> (terminal)
REJECTED         -> (terminal)
```

A frozen table, not scattered conditions. Anything absent is
`INVALID_ORDER_STATE_TRANSITION`; leaving a terminal state is
`ORDER_ALREADY_TERMINAL`, distinguished because it means something is
re-processing settled history rather than mis-sequencing a live order.

The lifecycle is walked even though the broker is in-process and answers
instantly. Collapsing states because a local simulation could would mean the
journal never records the sequence a real venue forces, and the first remote
adapter would exercise transitions nothing had tested.

`cumulative_filled_quantity`, `remaining_quantity` and `ordered_quantity` are
tracked, with `0 <= cumulative <= ordered`. A fill beyond the remainder is
`ORDER_OVERFILL` and is refused outright rather than clipped: a broker reporting
more than was ordered is a reconciliation problem, not a larger trade.

Fill prices are volume-weighted. An unweighted mean of a 1-share fill at 50 and a
999-share fill at 10 would read 30 instead of 10.04.

## The paper broker

Deterministic and in-process. It *produces* events and does not commit them —
durability, the chain, the anchor and pre-write validation belong to the store,
and a broker that also appended would write every event twice.

| Situation | Result |
| --- | --- |
| price stated, no availability limit | fills the remainder (`PAPER_FULL_FILL_V1`) |
| price stated, availability *n* | fills `min(remaining, n)` |
| availability explicitly zero | order stays `ACKNOWLEDGED`, nothing fills |
| no price for the security | `PAPER_EXECUTION_PRICE_MISSING` |
| snapshot earlier than acknowledgement | `PAPER_SNAPSHOT_BEFORE_ACKNOWLEDGEMENT` |

**A missing execution price is never replaced by the sizing price.** That
fallback is the most tempting shortcut in a paper engine and it destroys the
engine's purpose: every rebalance would execute at exactly its planned level and
the simulation could never show a cost.

Fees are explicit, default zero, and charged **per fill** — a venue charges per
execution, so an order filling in three parts costs three fees.

Cancelling a partly filled order keeps the filled quantity and stops the
remainder. A filled order cannot be cancelled. A rejected order produces no
accounting event at all.

## The accounting handoff

Two facts are kept distinct: *a fill happened* (the journal) and *a fill is in
the book* (the ledger).

```
broker produces fill evidence
-> fill event durable in the journal          (fill occurred)
-> accounting FillEvent admitted and durable  (fill is in the book)
-> FILL_APPLIED durable in the journal        (handoff confirmed)
```

Each step is durable before the next begins, so a crash anywhere leaves a state
recovery can read unambiguously:

| Crash point | Recovery |
| --- | --- |
| before the fill event | nothing happened |
| after the fill, before accounting | fill is `PENDING`; recovery admits it once |
| after accounting, before `FILL_APPLIED` | recognised as already applied; no second trade |
| after `FILL_APPLIED` | nothing to do |

Exactly-once rests on the accounting event id being **derived** from the fill id
(`EXEC_FILL:<fill_id>`) rather than generated. The accounting layer's own
idempotency then returns `written=False` on a retry. A generated id would make
every retry a second trade, and retries are the normal consequence of a crash.

A fill id reused with different content is `FILL_ID_CONFLICT`, in the journal and
in the adapter.

## Attribution

An order carries which strategies authorised which part of its quantity. A full
fill inherits those shares exactly; a partial fill divides them by
`PROPORTIONAL_FILL_ATTRIBUTION_V1` — proportionally to what each authorised, in
strategy order, with the last share taking the exact remainder so the parts sum
to the whole.

Proportional because it is the only division derivable from what the plan already
states. Filling one strategy's share first would hand it the whole of a small
fill.

Accounting is the final enforcement: `INSUFFICIENT_ATTRIBUTED_POSITION` on a sale
from a pool that does not hold it, and `INSUFFICIENT_CASH` on a buy the book
cannot fund.

## Cash

Buying power is accounting's cash. There is no margin and no settlement model.

If actual fill prices move against a buy, accounting refuses and execution fails
closed with `ACCOUNTING_ADMISSION_FAILED` wrapping `INSUFFICIENT_CASH`. **No
overdraft, no fabricated cash, no automatic resize** — resizing an authorised
order would mean trading a quantity the risk engine never approved.

## The journal

Append-only, hash-chained, and anchored on every committed event.

| Manipulation | Detected by |
| --- | --- |
| tamper | the stored event fingerprint no longer matches |
| deletion | `EXECUTION_JOURNAL_SEQUENCE_GAP` |
| reordering | sequence gap and a broken previous-hash link |
| insertion | `EXECUTION_JOURNAL_HASH_CHAIN_MISMATCH` |
| truncation | `EXECUTION_JOURNAL_TRUNCATION_DETECTED`, against the anchor |

Order state is *derived* by replaying the journal, never stored as a field
somebody updates. That is what makes "how did this order come to be cancelled" a
question with an answer.

Admissibility is decided **before any durable write** — the P1.4 lesson. A record
the state machine would always refuse leaves a journal that can never be replayed
again, which is worse than the refusal it was recording.

The anchor is mandatory, not scheduled, and never moves backward.

### What the anchor cannot do

> **`COORDINATED_LOCAL_ROLLBACK_DETECTION: NOT GUARANTEED.`** A journal and its
> local anchor rolled back *together* to a mutually consistent earlier state
> validate cleanly. Local files cannot establish that they are the latest state
> of themselves. The same limit applies to the accounting ledger, and closing
> either needs an independently trusted external anchor.

## Commands

```bash
stock-screener execution paper-run <request.json> --root <dir> [--output result.json]
stock-screener execution verify-journal <.execution>
stock-screener execution show-manifest
```

Golden fixtures in `fixtures/execution/`, each with a `.golden.json` the tests
reproduce byte for byte:

| Fixture | Proves |
| --- | --- |
| `clean_execution.json` | one of each action, sells before buys, HOLD omitted, all fills complete |
| `partial_fill_execution.json` | 200 ordered, 40 + 30 filled, cancelled; the book receives 70 |
| `rejected_execution.json` | a rejected order produces no accounting event |

## What this is, and is not

**It is a deterministic correctness simulator.** The same inputs produce the same
orders, fills and book every time, which is what lets a test establish that a
change to the lifecycle is safe.

**It is not a realistic fill simulator.** No spread, no slippage, no market
impact, no volume participation, no latency and no queue position. A strategy run
through it will look better than it would trade, by roughly those costs, and the
gap is not small. The manifest states this, because a simulated return that does
not disclose its cost assumptions invites being read as a forecast.

## Deliberately absent

No live broker adapter. No external paper brokerage — not even read-only. No
TWAP, VWAP scheduling, participation algorithm, smart routing or iceberg. No
market-impact model. No execution-parameter optimisation. Live trading is
`NOT_AUTHORIZED`.

## Known limitations

- **Coordinated local rollback is undetectable**, as above.
- **No execution realism at all**, as above. This is the whole of P2.1.
- **Order expiry is not simulated.** `DAY` is the only time in force and nothing
  expires an order at a session boundary; a plan is executed within one session's
  worth of snapshots.
- **One plan per journal generation.** Running a second plan against the same
  book needs a new execution store, and there is no cross-plan order view.
- **Journal events must be supplied in non-decreasing occurrence order.** The
  journal is ordered by occurrence, so a caller sequencing a sell's fill after a
  later buy's submission is refused. That matches the real sequence a no-margin
  book requires and is a constraint on callers rather than an internal one.
