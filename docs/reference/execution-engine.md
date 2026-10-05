---
type: reference
title: Paper execution engine
---

# Paper execution engine

`EXECUTION_ENGINE_V1_1` is current; `EXECUTION_ENGINE_V1` remains frozen and
reproducible. The layer between an authorised portfolio target and an accounting
fill. Implemented in `src/stock_screener/execution/`.

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

## Engine versions

| Version | Fill model | Models |
| --- | --- | --- |
| `EXECUTION_ENGINE_V1` | `PAPER_REFERENCE_PRICE_FILL_V1` | nothing; fills at one explicit price |
| `EXECUTION_ENGINE_V1_1` | `PAPER_SESSION_PARTICIPATION_FILL_V1` | spread, adverse slippage, share-volume participation, DAY expiry, multi-session rollover |

V1 is selected by supplying flat `ExecutionPrice` snapshots; V1.1 by supplying a
`PaperExecutionPolicy`, an `ExecutionCalendar` and `PaperMarketSessionSnapshot`s.
Nothing reinterprets a V1 run: its goldens reproduce byte for byte, and its
manifest keeps describing the V1 lifecycle (seven states, no `EXPIRED`) and the
V1 vocabulary of refusals, because a V1 order genuinely cannot expire and a V1
run genuinely cannot raise `PAPER_QUOTE_REQUIRED`.

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
| Manifest | `EXECUTION_MANIFEST_V1` / `EXECUTION_MANIFEST_V1_1` |
| Required accounting | `PORTFOLIO_ACCOUNTING_V2` |

V1.1 adds:

| Contract | Version |
| --- | --- |
| Realism policy | `PAPER_EXECUTION_POLICY_V1` |
| Participation | `SESSION_SHARE_VOLUME_PARTICIPATION_V1` |
| DAY expiry | `DAY_ORDER_SESSION_EXPIRY_V1` |
| Session rollover | `CHILD_DAY_ORDER_ROLLOVER_V1` |
| Session calendar | `EXECUTION_SESSION_CALENDAR_V1` |
| Cost report | `EXECUTION_COST_REPORT_V1` |

## Execution friction (V1.1)

Every value is **declared by the caller** and fingerprinted. Nothing in the
package reads a return, a price history or a P&L figure to choose one, because a
cost model fitted to make a strategy look good becomes a free parameter of that
strategy — and its apparent edge then partly comes from the assumption chosen to
flatter it. Every example value is `NOT_PRODUCTION_AUTHORIZED`.

```
adverse_slippage_bps        >= 0, strictly adverse
max_volume_participation    in (0, 1]
maximum_execution_sessions  >= 1, finite
spread_required             a quote is needed, with no substitute
volume_required             an unknown volume is not an unlimited one
```

A negative slippage is `INVALID_EXECUTION_POLICY`: a model that could improve a
fill is a model that can manufacture edge.

### Spread and slippage

```
mid       = (bid + ask) / 2          the cost reference, never a fill price
touch     = ask for a BUY, bid for a SELL
BUY fill  = ask * (1 + bps / 10000)
SELL fill = bid * (1 - bps / 10000)
```

With zero slippage a buy fills at the ask and a sell at the bid. A buyer never
pays less than the ask and a seller never receives more than the bid.

| Quote | Result |
| --- | --- |
| `ask >= bid` | valid; a zero spread is valid |
| `ask < bid` | `INVALID_PAPER_QUOTE` — a data fault, not a tradable market |
| a side missing, spread required | `PAPER_QUOTE_REQUIRED` |

**No fallback to a sizing price, a close or a last trade.** Substituting a single
price for a quote silently removes the spread cost — the main thing this model
exists to charge.

### Participation

```
session_capacity = observed_share_volume * max_volume_participation
```

in **shares**, not dollars — this is not P1's average dollar volume, and
confusing the two would misprice the ceiling by a factor of the price.

The ceiling is **global to `(identity, session)`**, not an allowance per order:
if three children touch one security in one session they share it, because
otherwise the ceiling would scale with however many orders a planner emitted.

Consumption is **derived from durable admitted fills, keyed by fill id**, never
tracked in a counter. A replayed fill therefore consumes capacity once, which
matters because retries are the normal consequence of a crash. It also avoids a
second liquidity authority to keep in step with the journal.

| Volume | Result |
| --- | --- |
| known | ceiling applies |
| zero | ceiling is zero; nothing fills and the order expires |
| unknown, volume required | `PAPER_VOLUME_REQUIRED` |

### Fill quantity

```
fill = min(remaining order, remaining session capacity, explicit availability)
```

Deterministic in every case. No stochastic fraction.

## DAY expiry and multi-session rollover (V1.1)

**DAY now means DAY.** When a session closes with quantity outstanding the order
becomes `EXPIRED`, terminally. P2.0 let a DAY order survive indefinitely, which
was wrong and is the limitation this closes.

`EXPIRED` is kept distinct from `CANCELLED`: a cancellation is a decision
somebody made, an expiry is the lifetime ending. Reporting them as one state
would make an unattended rollover indistinguishable from an operator pulling an
order, and only one of those needs looking into.

A `FILLED` order never expires. An `EXPIRED` order never fills again.

Residual quantity reaches the next session as a **new child DAY order**:

```
authorised quantity
  -> child, session 1   -> partial fill -> EXPIRED
  -> child, session 2   -> partial fill -> EXPIRED
  -> child, session 3   -> fill         -> FILLED
```

Each child is a distinct immutable order whose id is a digest over the plan
fingerprint, the security, the session, the generation and the authorised
residual. That makes child creation idempotent across a crash — recovery
recomputes the id and finds it already in the journal — and makes a fabricated
child impossible to pass, since changing the quantity, the session or the
security all change the digest. No UUID is involved.

Children are authorised by *derivation* rather than by list membership: the plan
cannot enumerate its children in advance, because how many there are depends on
fills that have not happened. `require_authorizes_child` recomputes the id and
checks the child against the plan's parent order for that security.

**Residual comes from admitted fills, never submitted quantity.** A child sent
for 400 that filled 250 leaves 750 outstanding on a 1,000-share parent, not 600.

**The next session comes from an `ExecutionCalendar`, never `date + 1`.** A
Friday residual rolls to Monday and a pre-holiday residual to the day after the
holiday; arithmetic gets both wrong and would place a child on a day the market
was shut. A session the calendar does not contain is
`UNKNOWN_EXECUTION_SESSION`.

**The horizon is finite.** When `maximum_execution_sessions` runs out with
quantity left, execution stops and the plan is `PARTIALLY_COMPLETED` — a real
outcome, not a failure. An unbounded rollover would keep trading against a
portfolio decision made arbitrarily long ago.

### Timing

A snapshot describes a **completed** session, and the execution opportunity it
represents is stamped at its own `as_of`. Bid, ask, volume and fill all belong to
that one instant, and no fill is backdated inside the session. Using a whole
session's volume while dating the fill earlier within it would be spending
information the fill could not have had — the same leakage the research layer
exists to prevent, which here would be called a backtest fill.

A snapshot for one session cannot execute another's order:
`PAPER_MARKET_SESSION_MISMATCH`.

## Execution cost (V1.1)

Measured against the midpoint of the quote the fill traded against:

```
spread cost   = (touch - mid) * quantity     BUY
              = (mid - touch) * quantity     SELL
slippage cost = (fill - touch) * quantity    BUY
              = (touch - fill) * quantity    SELL
shortfall     = (fill - mid) * quantity      BUY
              = (mid - fill) * quantity      SELL

shortfall           == spread + slippage          enforced
total execution cost == shortfall + fees
                     == spread + slippage + fees
```

Both costs are signed so a positive figure is money lost on either side, which
is what makes the totals addable. The identity is **enforced** on every report,
not asserted in prose: it holds by construction because the parts telescope
through the touch price, so a failure means a quantisation or sign bug and is
caught where it happens.

Worked example — `bid 99`, `ask 101`, 10bps, 100 shares:

| | BUY | SELL |
| --- | --- | --- |
| mid | 100 | 100 |
| touch | 101 | 99 |
| fill | 101.101 | 98.901 |
| spread cost | 100 | 100 |
| slippage cost | 10.1 | 9.9 |
| shortfall | 110.1 | 109.9 |

The slippage differs between the sides because the basis points apply to the ask
for a buy and the bid for a sell. That asymmetry is correct, not a rounding
artefact.

### The cost is posted to nothing

> The accounting ledger already received the **actual fill price** and the
> **actual fee**, so this cost is already inside cash, cost basis and realised
> P&L. Posting a spread or slippage expense alongside would charge it twice and
> make NAV wrong by exactly the amount being reported.

These figures *explain* the prices the book holds; they do not adjust them. The
opposite instinct is natural — a cost report that does not appear in the P&L
feels incomplete — but it appears there already, inside the price.

Per-session reporting gives identity, session, bid, ask, mid, observed volume,
participation ceiling, filled quantity, actual participation, fill price, spread
cost, slippage cost and fee. Where a session traded nothing, actual
participation is `UNDEFINED` rather than zero: zero participation of zero volume
is a different statement from taking none of a liquid session.

Per-identity reporting adds planned, filled and remaining quantity, sessions
used, and a volume-weighted VWAP across every child.

## Drift between sessions (V1.1)

| Change | Response |
| --- | --- |
| this plan's own fills | expected; the expectation advances with them |
| any unrelated book event | `EXECUTION_STATE_DRIFT`; no further child issued |

Admitted fills stand, and **no strategy or risk pass is re-run to adapt** —
adapting would mean trading on an authorisation nobody gave. A new plan is
required.



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
stock-screener execution paper-run-realism <request.json> --root <dir> [--output result.json]
stock-screener execution verify-journal <.execution>
stock-screener execution show-manifest
stock-screener execution show-realism-manifest
```

Golden fixtures in `fixtures/execution/`, each with a `.golden.json` the tests
reproduce byte for byte:

| Fixture | Proves |
| --- | --- |
| `clean_execution.json` | V1: one of each action, sells before buys, HOLD omitted, all fills complete |
| `partial_fill_execution.json` | V1: 200 ordered, 40 + 30 filled, cancelled; the book receives 70 |
| `rejected_execution.json` | V1: a rejected order produces no accounting event |
| `realism_cost_buy.json` | V1.1: bid 99 / ask 101 / 10bps BUY — spread 100, slippage 10.1, shortfall 110.1 |
| `realism_cost_sell.json` | V1.1: the SELL mirror — slippage 9.9, shortfall 109.9 |
| `realism_participation.json` | V1.1: 1000 ordered, volume 5000, 10% — 500 fills, then EXPIRED |
| `realism_multi_session.json` | V1.1: 400 + 350 + 250 across three children, COMPLETED |
| `realism_window_exhausted.json` | V1.1: the same plan with a 2-session horizon — 750 filled, 250 unfilled |

## What this is, and is not

**It is a deterministic simulator.** The same inputs produce the same orders,
fills, costs and book every time. Nothing is drawn from anywhere: the friction is
computed from declared values, not sampled.

**V1.1 models** quoted spread crossing, a declared adverse slippage, share-volume
participation, real DAY expiry and calendar-derived multi-session rollover.

**It still does not model** market impact, latency, queue position, hidden
liquidity, venue routing, auction behaviour or any intraday price path. The
friction is a *declared haircut*, not a measurement — impact in particular is a
function of an order's own size against available liquidity, and a flat haircut
is not that. A strategy run through this engine will still look better than it
would trade, by whatever those unmodelled costs are worth.

The manifest states all of this, because a simulated return that does not
disclose its cost assumptions invites being read as a forecast.

## Deliberately absent

No live broker adapter. No external paper brokerage — not even read-only. No
TWAP, VWAP scheduling, participation algorithm, smart routing or iceberg. No
market-impact model, no latency simulation, no queue position, no hidden
liquidity, no venue routing, no auction behaviour. No execution-parameter
optimisation and no execution alpha. Live trading is `NOT_AUTHORIZED`.

## Known limitations

Closed in P2.1:

- **Order expiry.** `DAY` now ends at the session close; P2.0 let an order live
  indefinitely.
- **No execution cost.** V1.1 charges spread, declared slippage and a
  participation ceiling. V1 charged nothing and remains frozen that way.

Still open:

- **Coordinated local rollback is undetectable.** A journal and its local anchor
  rolled back together validate cleanly, because local files cannot establish
  that they are the latest state of themselves. The same limit applies to the
  accounting ledger; an independently trusted external anchor would close both.
- **The friction is declared, not measured.** The slippage and participation
  figures are inputs. Nothing calibrates them, and nothing should calibrate them
  from a strategy's own returns. A realistic figure needs an execution-cost study
  this phase does not perform.
- **No market impact, latency or queue position.** Impact especially is a
  function of order size against liquidity; a flat haircut does not approximate
  it, and a large order will be modelled as cheaper than it would trade.
- **One intraday opportunity per session.** A session is one execution
  opportunity at its `as_of`, so an order cannot work the session's price path.
  That is what keeps the timing semantics honest, and it also means intra-session
  timing cannot be studied here.
- **One plan per journal generation.** Running a second plan against the same
  book needs a new execution store, and there is no cross-plan order view.
- **Journal events must arrive in non-decreasing occurrence order.** A caller
  sequencing a sell's fill after a later buy's submission is refused. That
  matches the sequence a no-margin book requires, but it is a real constraint on
  callers.
- **A session with no snapshot stops that security's rollover.** No child is
  created for a session the request did not supply a market for, because a child
  with nothing to fill against would expire having never had a chance. The plan
  reports `NO_MARKET_SNAPSHOT_FOR_SESSION` rather than skipping ahead.
