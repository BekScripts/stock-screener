---
type: reference
title: Paper execution engine
---

# Paper execution engine

`EXECUTION_ENGINE_V1_2` is current; `EXECUTION_ENGINE_V1_1` and
`EXECUTION_ENGINE_V1` remain frozen and reproducible. The layer between an
authorised portfolio target and an accounting fill. Implemented in
`src/stock_screener/execution/`, with the external paper adapter in
`src/stock_screener/execution/external/`.

The **core** reaches no broker, holds no credential, places no order anywhere,
and introduces no randomness into a fill. The `external/` subpackage is the one
exception and only in one direction: it reaches a provider's **paper** venue.
Live trading is `NOT_AUTHORIZED` in every version, and in V1.2 that is a
structural property rather than a policy — there is no flag, host, key prefix or
configuration value that enables it.

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
| `EXECUTION_ENGINE_V1_2` | unchanged from V1.1 locally | a second **venue**: a provider's paper endpoint, reached through `external/` |

V1 is selected by supplying flat `ExecutionPrice` snapshots; V1.1 by supplying a
`PaperExecutionPolicy`, an `ExecutionCalendar` and `PaperMarketSessionSnapshot`s.
Nothing reinterprets a V1 run: its goldens reproduce byte for byte, and its
manifest keeps describing the V1 lifecycle (seven states, no `EXPIRED`) and the
V1 vocabulary of refusals, because a V1 order genuinely cannot expire and a V1
run genuinely cannot raise `PAPER_QUOTE_REQUIRED`.

V1.2 changes no local arithmetic. What it changes is where a fill can come from,
and that is the more consequential difference: **a V1.2 run bound to
`EXTERNAL_PAPER` is not reproducible.** Its fills came from a venue's matching
engine at a moment that will not recur. The V1.2 manifest states this rather
than implying otherwise — it is an audit record of what was bound and what
happened, not a claim that re-running it reproduces it. A V1.2 run bound to
`LOCAL_DETERMINISTIC_PAPER` is a V1.1 run under a new name and reproduces
normally.

Each manifest version keeps its own answers. `EXECUTION_MANIFEST_V1_1` still
reports `external_paper_broker: NOT_IMPLEMENTED`, because a V1.1 engine has no
adapter and cannot reach a venue; `EXECUTION_MANIFEST_V1_2` reports
`IMPLEMENTED_PAPER_ONLY`. Editing the older manifest would make every V1.1
artefact claim a capability its engine did not have.

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

## The external paper venue (V1.2)

`src/stock_screener/execution/external/` is the only code in this project that
can cause anything to happen outside the process. Everything about it is shaped
around containing that.

### Live trading is refused by construction

`BrokerEnvironment` can name `LIVE` — refusing something requires being able to
say it — and `is_authorized` is false for it everywhere. `refuse_live` runs when
a binding is built, when an adapter is constructed, and when an order is
submitted. There is no `USE_LIVE`, no `--live`, no `mode=live`, no
`fallback_to_live`, and no configuration value that changes the answer.

Configuration is not treated as proof. Three independent checks must agree:

| Check | Refusal when it fails |
| --- | --- |
| the requested environment is `PAPER` | `LIVE_TRADING_NOT_AUTHORIZED` |
| the host is on the paper allow-list | a live host → `LIVE_TRADING_NOT_AUTHORIZED`; an unknown host → `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` |
| the credential id carries a paper prefix | a live prefix → `LIVE_TRADING_NOT_AUTHORIZED`; an unknown prefix → `BROKER_ENVIRONMENT_NOT_PROVEN_PAPER` |

An unknown host or prefix is refused rather than assumed safe, because the
failure mode of guessing wrong is a live order. Only the credential's **key id**
is examined, never the secret, and nothing is logged.

A proof from configuration alone is `is_proven`. It becomes
`is_fully_certified` only when the account itself answers a read-only request,
which `preflight` does. The two are different claims and are kept apart.

### The submission protocol

`DURABLE_INTENT_THEN_LOOKUP_NEVER_RESEND_V1`.

A `POST` that times out has three possible histories: it never arrived, it
arrived and was refused, or it arrived and created a live order. The transport
cannot tell them apart, and resending converts "I don't know" into "two orders".

| Step | Why in this order |
| --- | --- |
| validate locally | a refusable order leaves no intent behind |
| derive the client order id | the order needs a name decided before the request |
| write the intent durably | a crash during the request leaves a record naming the order that may now exist |
| send **once** | exactly one mutating request per call, always |
| classify, and resolve by reading | a lookup establishes what happened; nothing is resent |

The client order id is a pure function of the local order id —
`DERIVED_CLIENT_ORDER_ID_SHA256_V1`, no UUID and no clock — so after any crash
the order can still be named and asked about. It deliberately does not depend on
the account binding: a binding's fingerprint moves when a capability
declaration changes, and an id that moved with it would let the same order be
submitted again under a new name after a configuration edit.

Five outcomes, and keeping the last apart from the first is the point:

| Outcome | Means | May submit again |
| --- | --- | --- |
| `NOT_SUBMITTED` | nothing was sent, or a lookup established the venue has no such order | yes |
| `SUBMISSION_INTENT_DURABLE` | the intent is on disk and the request has not been made | — |
| `REMOTE_CONFIRMED` | the venue has the order | no |
| `REMOTE_REJECTED` | the venue refused it explicitly | no |
| `REMOTE_STATE_UNKNOWN` | the venue may or may not have it, and asking did not settle it | **no** |

An unknown is never downgraded to `NOT_SUBMITTED` for tidiness, and the intent
stays unresolved on disk. `preflight` withholds readiness while any intent is
unresolved, because planning new orders while one exists sizes them against a
book that may be missing a position.

### Translation refuses to guess

| Translation | Policy | Unknown input |
| --- | --- | --- |
| identity → venue symbol | `EXPLICIT_IDENTITY_TO_VENUE_SYMBOL_V1` | `BROKER_SYMBOL_MAPPING_UNRESOLVED` |
| provider status → local state | `EXPLICIT_PROVIDER_STATUS_MAP_V1` | `BROKER_STATUS_UNMAPPED` |

**There is no fallback to the identity's display symbol.** That fallback is one
line and would make every test pass. A ticker is reassigned when a company
delists and another takes the string, and `SecurityIdentity.symbol` holds
whatever the ticker was when the row was loaded — so routing on it would
eventually send a real order for company A to whatever company now answers to
A's old ticker. Two identities mapped to one venue symbol is refused in both
directions, because a shared symbol would attribute a remote fill to whichever
was found first.

The status map is closed by default. A default branch mapping anything
unrecognised to `REJECTED` would, the day the provider adds a status, report
working orders as dead and release their quantity into the next plan.
`pending_cancel` maps to `ACKNOWLEDGED`, not `CANCELLED`: a cancel that has been
asked for has not happened, and the order can still fill. `pending_new` and
`accepted_for_bidding` map to **no** local state — there is no state for "being
thought about", and inventing one would widen the frozen lifecycle.

A fee the venue did not state is `BROKER_FILL_FEE_UNKNOWN`, never zero. A
commission-free venue really does charge nothing, so assuming zero would usually
be right — and the day a venue starts reporting a regulatory fee, a silent zero
keeps a real cost out of the cost report and leaves NAV short by exactly the
fees.

### Three-way reconciliation, and no repair

`EXTERNAL_PAPER_THREE_WAY_RECONCILIATION_V1` compares the journal, the
accounting book and the venue. Two-way reconciliation cannot localise a
disagreement: if the book and the venue differ on a position, the cause might be
a fill the venue never reported, a fill the book never admitted, or an order
placed by hand in the provider's console, and those have different responses.

| Severity | Means |
| --- | --- |
| `BLOCKING` | execution stops; the book cannot be trusted to size another order |
| `WARNING` | a real difference that does not invalidate the next decision |
| `INFO` | an expected, explained difference, recorded so it is not rediscovered |

**Nothing repairs anything.** There is no function that adopts the venue's
figure, and a test asserts its absence. A break means one of the three records is
wrong, and which is an investigation; overwriting the book does not resolve the
break, it destroys the evidence that there was one — and if the broker was the
wrong one, it writes a false position into an append-only ledger that will carry
it forever.

The cash tolerance defaults to exactly zero. Widening it is a decision someone
has to make and record, because a default tolerance is how a drift becomes
invisible.

### One venue per session, and no fallback

`SINGLE_VENUE_PER_SESSION_NO_FALLBACK_V1`. `ExecutionVenue` has exactly two
members — `LOCAL_DETERMINISTIC_PAPER` and `EXTERNAL_PAPER` — and no `AUTO`. A
run that fell back to the simulator when its venue was unreachable would produce
a journal in which some fills were simulated and some were real with nothing
saying which, and every figure derived from it would be a blend of two
incompatible things. An artefact from one venue entering a session bound to the
other is `EXTERNAL_VENUE_MISMATCH`. A local session may not even hold a
credentialed binding.

### Preflight

Seven gates, all required, every one read-only. `preflight` collects every
failure rather than raising the first, so one call names everything that needs
fixing.

| Gate | Asks |
| --- | --- |
| `ENVIRONMENT_PROVEN_PAPER` | do the three configuration checks agree? |
| `ACCOUNT_REACHABLE` | does the account answer at all? |
| `ACCOUNT_MATCHES_BINDING` | is it the account the binding names? |
| `PROVIDER_CONFIRMS_PAPER` | does the provider's own record say it is paper? |
| `TRADING_NOT_BLOCKED` | is the venue accepting trades? |
| `SYMBOLS_RESOLVED` | does every intended identity have a declared symbol? |
| `NO_UNRESOLVED_SUBMISSIONS` | is any earlier intent still unknown? |

All seven passing is `PAPER_BROKER_READY`.

### Deliberately absent from the adapter

No funding or transfer call, so no code path moves money into or out of the
account. No close-all or liquidate convenience — a one-call "flatten everything"
is unrecoverable when invoked by accident, and the ordinary path already does it
with every check applied. No replace or amend, since the lifecycle has no
amendment state. No streaming subscription, because a push stream delivers
events in an order the adapter cannot verify.

No credential, authorization header, token or cookie is persisted or logged. A
report is scanned for anything shaped like a secret on the way out, and the
refusal deliberately does not quote what it found.

### The concrete transport (P2.2C)

`AlpacaPaperHttpTransport` in `external/http_transport.py` is the one real
`BrokerTransport`. It is the only code in this repository that opens a socket to
a broker, and `ALPACA_PAPER_HTTPS_TRANSPORT_V1` versions it separately from the
engine and the adapter, because how a request is made can change without any
execution semantics moving.

Four properties, each load-bearing:

| Property | How |
| --- | --- |
| host locked | `approved_paper_origin` runs in the constructor, before a credential is attached and before any socket exists |
| environment cannot re-point it | `trust_env=False`, so `HTTPS_PROXY` / `ALL_PROXY` cannot tunnel a submission elsewhere |
| redirects refused | `follow_redirects=False`, and a 3xx is reported as unusable rather than followed |
| one request per call | there is no retry loop in the module at all |

The host lock refuses, by name, a live or brokerage host
(`LIVE_TRADING_NOT_AUTHORIZED`) and, as not-provably-paper
(`BROKER_ENVIRONMENT_NOT_PROVEN_PAPER`), a non-HTTPS scheme, a URL carrying
userinfo, an explicit non-443 port, an IP literal, `localhost`, an unknown host,
and a path given as an absolute URL. The `trust_env` point is the least obvious
and matters as much as the rest: with the default, an environment variable could
route an order request to any host while the URL still read
`paper-api.alpaca.markets`.

Two refusals happen *before* any I/O, so the request provably did not arrive: a
payload JSON cannot represent (a `Decimal`, a NaN) is
`EXECUTION_EVENT_INVALID`, and an absolute-URL path is a host-lock violation.
Classifying either as a transport failure would mark it ambiguous and strand a
submission intent over a local defect that never reached the venue.

Every network failure classifies to an outcome whose `is_ambiguous` is true, and
none to a definite refusal — a timeout on a `POST` stays *unknown*, never
*failed*. Failure details name the exception's **class**, never its message,
which can carry the full request URL.

Each reply carries the provider's `X-Request-ID` where it sent one, plus an
allow-list of safe response headers and an `evidence_fingerprint()` over status,
those headers and the normalised body. Request headers are excluded by
construction: the request carried the credential, and a digest over it would be
a credential derivative sitting in an audit record forever.

### Proving the paper venue answers

`stock-screener execution external-paper-preflight --confirm` runs the seven
gates through the real transport. It is read-only — every request is a `GET`,
asserted by counting methods rather than by reading the code — and produces an
`EXTERNAL_PAPER_PREFLIGHT_V1` artefact recording the origin that answered, the
account match, the provider request ids, an evidence fingerprint per read, and
what the run does **not** establish.

It needs `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET_KEY` and
`ALPACA_PAPER_ACCOUNT_ID`. The account id is required rather than optional:
authenticating against *some* paper account establishes nothing about whether it
is the account this book describes, and reconciling another account's positions
against this book would report trading breaks for a wiring mistake.

One gate name overstates its evidence, and the artefact says so.
`PROVIDER_CONFIRMS_PAPER` passes by *absence*: Alpaca's account object has no
`is_paper` field, because paper and live are distinguished only by host. Paper-ness
is therefore established by the origin that answered and by the transport being
unable to exist against any other — proof by host lock and reachability, not by
provider attestation.

### The four defects P2.2D closed

The first real preflight found three defects, and running it found a fourth.
Each had passed 211 fake-transport tests, because a fake answers whatever it is
told to and therefore agrees with whatever the adapter asks for. Only the
documented provider surface disagreed.

**`read_fills` filtered by the wrong parameter.** It sent `client_order_id` to
`/v2/account/activities/FILL`, where the documented filter is `order_id` — the
provider UUID. An unrecognised query parameter is ignored, so the read looked
successful while returning the account's FILL activities broadly, and each row
was then stamped with the requested order's client id. On an account with any
prior activity that posts unrelated trades into this book.

The repair is structural, not a parameter swap. `read_fills` now takes
`remote_order_id` and there is deliberately **no fallback** to the client id —
`REMOTE_PROVIDER_ORDER_ID_REQUIRED` rather than a second attempt, because the
failure mode of a silently-ignored filter is invisible in the response. The
provider id is resolved from the durable submission resolution the protocol
already wrote, so it survives a restart. And because an ignored filter cannot be
detected from the reply, **every returned row is checked against the order it
was requested for**: a row naming another order, no order, or an unmapped symbol
is `REMOTE_FILL_ORDER_MISMATCH` and is refused rather than attributed. A row
that does not positively declare itself a fill — a dividend, a fee, a journal
entry on a mixed page — is skipped rather than posted as an execution.

**`cancel_order` cancelled by the wrong identifier, and trusted the status
code.** It built `DELETE /v2/orders/{client_order_id}` where the path parameter
is the provider UUID, so a cancel would have 404'd. It recorded no durable
intent, so a crash during the request left nothing naming the order it may have
cancelled. And it returned the HTTP result as though a `204` meant the order was
terminal — which it does not: a cancellation can be accepted and still lose a
race to a fill.

It now mirrors submission: validate, record a durable cancellation intent, send
**one** `DELETE`, then establish the outcome by *reading the order back*. The
mutation's own status is never the answer.

| Outcome | Means | May request again |
| --- | --- | --- |
| `REMOTE_CANCELLED` | the venue reports it terminal and unfilled | — |
| `REMOTE_FILLED` | it filled before the cancel landed; the fills stand | — |
| `REMOTE_STILL_OPEN` | still working, so the cancel is **not** complete | yes — on proven state |
| `REMOTE_STATE_UNKNOWN` | could not be read back | **no** |

**`PROVIDER_CONFIRMS_PAPER` could not fail.** It checked `is_paper is False`,
and Alpaca's account object has no `is_paper` field — paper and live are
distinguished only by host. The gate passed by *absence*, reporting provider
attestation nobody had gathered. A gate that cannot fail is worse than no gate:
it occupies the place where a check should be.

It is now backed by `PROVIDER_PAPER_ENVIRONMENT_PROOF_V1`, which can only exist
as the result of a successful external interaction — the approved paper origin
answered, the credential authenticated, and the account matched this binding.
There is no constructor that takes configuration, and a response from a live
origin raises `LIVE_TRADING_NOT_AUTHORIZED` rather than producing a proof. What
the proof does not claim is recorded in it: this is host lock plus reachability
plus account identity, not the provider saying so, and it describes one moment.

**The preflight read every endpoint twice.** Once to fingerprint, once for the
values it reported — so the digests in the artefact described responses it had
not reported. Nothing was wrong with either read and the artefact was still
useless as evidence.

`ExternalReadEvidence[T]` now carries a normalised value and the fingerprint of
the same response, and `preflight` does the reads and hands the evidence back
rather than the caller re-reading. Against the real account the run makes
exactly three requests where it previously made six.

**And the counts were mislabelled.** The preflight asked for `status=all` and
reported the result as "orders at venue" — on the real account that was 50 rows
of mostly-terminal history presented as live state. Working orders and order
history are now separate concepts with separate queries, and reconciliation
cares about the former: a terminal order from an earlier session is history, not
a break.

### Certified (P2.2E)

The external paper path is **certified**. One authorised order went to a real
Alpaca paper account and back, through the whole chain, and the venue finished
where it started.

`stock-screener execution external-paper-canary --external-paper-canary` runs
it. The flag is required and there is no corresponding live mode.

What the canary proves that a fake transport cannot:

| | |
| --- | --- |
| the authority chain | the order originated from an accounting-V2 book, through an authorised plan, to the submission protocol. The canary never calls the transport to trade. |
| deterministic identity | the derived client order id was sent, and a lookup by that id returned the same provider order |
| fill ingestion | the fill was read by provider `order_id` and posted once, under an accounting event id derived from the provider's execution id |
| exactly once | the executions were re-read and re-admitted; the ledger length, the cash and the position did not move |
| three-way reconciliation | `RECONCILED` after the fill and after cleanup |
| cleanup | a second authorised order closed the position — not a cancel, not a liquidation |

Three things the live run taught that the offline suite had not:

**The order status and the activities feed are not simultaneous.** The venue
reported the order `filled` about twenty milliseconds after submission, and the
matching FILL activity was not yet queryable. A single activities read straight
after a terminal status returns nothing, and reading that as "no fill" is wrong
about a trade that happened — the first run reported `ENTRY_NOT_FILLED` while
the venue had bought. The executions are now polled in their own bounded loop,
and an exhausted window is reported rather than concluded from.

**A deterministic client order id makes a canary non-repeatable, by design.**
The second run derived the same venue-facing id, the venue refused the duplicate,
the lookup returned the first run's filled order, and accounting refused that
fill for taking effect before the new book opened. Every layer was correct. The
resolution is a run-scoped plan id — stable *within* a run, so a retry after a
crash stays idempotent — not a random one.

**Fractionability is a property of the instrument, not the venue.** A
venue-level capability default is necessarily wrong for one side or the other,
so the canary declares it from the asset read: the venue is asked whether the
symbol is fractionable, and the capability follows that answer.

Two differences are declared rather than absorbed. The fee is a schedule value
(zero for a US equity here), and the execution still records
`fee_is_known=False`, because the venue stated none. And the canary declares a
one-cent cash tolerance with its reason — the venue rounds its cash to whole
cents, so a fractional fill leaves a sub-cent difference that is rounding, not a
missing trade. The global default stays exactly zero.

### What certification does not establish

The paper endpoint is **implemented and uncertified**. The transport is complete
and verified offline against `httpx.MockTransport`; nothing in this repository
has yet spoken to a real venue. The opt-in canary in
`tests/live/test_paper_broker_live.py` stays inert behind `--live-paper`, and
`make check` never passes that flag nor makes any external request.

Certification is one observation, not a guarantee. It establishes that this
code, against this provider, at one moment, did what it claims. It does not make
an external run reproducible: the fills came from a venue's matching engine at a
moment that will not recur, and the artefact says so rather than implying
otherwise — code and configuration semantics are reproducible, the external
observations are recorded and fingerprinted, and the provider's responses are
not recreatable.

It also says nothing about execution *quality*. A provider's paper engine is the
provider's model of itself: it may fill more readily than its live engine would,
it is not bound by this project's participation ceiling, and its queue behaviour
is unobservable from here. A paper fill is evidence the plumbing works, not
evidence the trade was achievable.

And a canary needs an account holding nothing this book does not know about.
Each pre-existing holding reconciles as `REMOTE_POSITION_MISMATCH` (BLOCKING)
and each foreign working order as `UNEXPECTED_REMOTE_ORDER`. The preflight
reports `canary_ready` and names the blockers; clearing them is the account
owner's decision and nothing here does it automatically.

## Deliberately absent

No live broker adapter and no live endpoint. No TWAP, VWAP scheduling,
participation algorithm, smart routing or iceberg. No market-impact model, no
latency simulation, no queue position, no hidden liquidity, no venue routing, no
auction behaviour. No execution-parameter optimisation and no execution alpha.
No account funding, transfer, close-all or liquidate call. Live trading is
`NOT_AUTHORIZED`.

## Known limitations

Closed in P2.1:

- **Order expiry.** `DAY` now ends at the session close; P2.0 let an order live
  indefinitely.
- **No execution cost.** V1.1 charges spread, declared slippage and a
  participation ceiling. V1 charged nothing and remains frozen that way.

Closed in P2.2:

- **No external paper brokerage.** `external/` reaches a provider's paper venue,
  read-mostly, with one mutating path.

Still open:

- **An external run is not reproducible.** Its fills came from a venue at a
  moment that will not recur. The V1.2 manifest is an audit record, not a
  reproducibility claim, and no test can assert an external fill's price.
- **A provider's paper engine is the provider's model of itself.** It may fill
  more readily than its live engine would, it is not bound by this project's
  participation ceiling, and its queue behaviour is unobservable from here. A
  paper fill is evidence that the plumbing works, not evidence that the trade
  was achievable.
- **Proof of paper from configuration is not proof of reach.** A host allow-list
  and a key prefix establish what was configured. Only the account's own answer
  adds more, and only for as long as the credential is unchanged — a credential
  repointed after preflight is not detected until the next read.
- **The paper endpoint is implemented but uncertified.** P2.2C adds the concrete
  HTTPS transport, verified offline; nothing has yet spoken to a real venue.
- **Per-order fill retrieval is wrong against the real API.** `read_fills` sends
  `client_order_id` to an endpoint whose documented filter is `order_id`, so on a
  real account it would return unrelated fills and attribute them to one order.
  This is what blocks certification, and it needs a contract change to fix.
- **`cancel_order` cancels by the wrong identifier.** It sends a client order id
  where Alpaca's path wants the provider UUID. Fails closed, and off the
  certification path, since cleanup uses a compensating order.
- **An unresolved submission needs a person.** The protocol establishes that the
  state is unknown and refuses to guess; settling it is a deliberate decision,
  and nothing auto-resolves it.
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
