---
type: reference
title: Portfolio accounting and reconciliation
---

# Portfolio accounting and reconciliation

`PORTFOLIO_ACCOUNTING_V2` is current; `PORTFOLIO_ACCOUNTING_V1` remains
executable and reproducible. The layer that answers *what is actually owned*, as
distinct from what the risk engine would authorise. Implemented in
`src/stock_screener/portfolio_accounting/`.

A target is not a holding. P1.0–P1.2 decide what a portfolio should contain; none
of that is evidence that anything was bought. Only an event on the ledger changes
a holding.

## Accounting versions

V1 keeps one weighted-average basis per security and divides it among owners by
quantity. V2 keeps a basis per attribution owner. The difference is not additive,
which is why it is a major version:

|  | V1 (pooled) | V2 (per-attribution) |
| --- | --- | --- |
| global realised | 15 | 20 |
| global remaining basis | 15 | 20 |

for `A buys 1 @ 10, B buys 1 @ 20, A sells 1 @ 30`. The *portfolio's own* figures
move, not only their division across owners, so V1 cannot be reproduced from V2's
rules and the `V1_1`/`V1_2` additive pattern does not apply.

**V1 is the default.** A ledger or request that declares no version replays as V1,
so books written before P1.4 keep their meaning without carrying a field they
could not have known to write. V2 is selected explicitly — the version is a
property of a ledger generation, not of the installed code.

| Version | Basis | Per-strategy realised P&L |
| --- | --- | --- |
| `PORTFOLIO_ACCOUNTING_V1` | one pool per security | exact only when co-owners entered at the same price |
| `PORTFOLIO_ACCOUNTING_V2` | one pool per `(identity, owner)` | exact under this internal convention |

Neither is tax accounting. There is no FIFO, no LIFO, no specific-lot
identification, no holding period and no wash-sale rule.

## Attribution cost pools (V2)

Each `(identity, owner)` pair keeps its own weighted-average pool, where `owner`
is a `strategy_id` or `UNATTRIBUTED_EXISTING`. Within a pool the arithmetic is
exactly V1's; what changed is that there is one pool per owner.

**The pools are the single authority.** Global quantity, cost basis, realised
P&L, market value and unrealised P&L are all *derived* by summing them, never
maintained alongside them — two independently mutating authorities would
eventually disagree, and the disagreement would look like a reconciliation
failure rather than a bug.

```
global quantity    = Σ pool.quantity
global cost basis  = Σ pool.total_cost_basis
global realised    = Σ pool.realised_trading_pnl_gross
global unrealised  = Σ pool.unrealised_pnl(mark)
global mark value  = Σ pool.market_value(mark)
```

A pool's unrealised P&L is `market value − total_cost_basis`, **not**
`quantity × (mark − average_cost)`. `average_cost` is derived at eight places
while `total_cost_basis` is authoritative, so the second form differs by a
quantisation residue whenever the average does not divide exactly — a residue
that accumulates across pools and would surface as a false P&L decomposition
failure.

A sale debits each named owner's pool at **that owner's** basis.
`INSUFFICIENT_ATTRIBUTED_POSITION` refuses a sale beyond one owner's pool even
when the security's global quantity is sufficient: taking the shortfall from a
co-owner would move one strategy's position and P&L into another's.

A pool that reaches zero keeps its realised P&L and its owner's name. It is never
relabelled `UNATTRIBUTED_EXISTING` — a strategy that realised a result still
realised it, and no longer holding the shares does not change who did.

### Migration

`REPLAY_MIGRATION_FROM_EVENTS`. Fills carry their price and their attribution, so
replaying a complete V1 ledger from genesis under V2 reconstructs each owner's
pool deterministically. Nothing is carried over from the V1 state.

Migrating from a V1 *snapshot* is refused with
`ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE`: a V1 state holds one basis per security
and a quantity per owner, and the owners' separate entry prices were destroyed at
the first co-owned buy. An incomplete or unverifiable event history is refused for
the same reason. There is no automatic migration, and the V1 ledger is never
mutated.

## Policy versions

Every policy carries a version. A change to any of them moves the manifest
fingerprint, so a set of books can always name the rules that produced it.

| Policy | Version |
| --- | --- |
| Accounting engine | `PORTFOLIO_ACCOUNTING_V1` |
| Ledger hash chain | `PORTFOLIO_LEDGER_HASH_CHAIN_V1` |
| Money representation | `PORTFOLIO_ACCOUNTING_DECIMAL_V1` |
| Cost basis | `WEIGHTED_AVERAGE_COST_V1` |
| Mark admissibility | `PORTFOLIO_MARK_ADMISSIBILITY_V1` |
| P&L decomposition | `PORTFOLIO_PNL_DECOMPOSITION_V1` |
| Strategy attribution | `PORTFOLIO_STRATEGY_ATTRIBUTION_V1` |
| Corporate actions | `PORTFOLIO_CORPORATE_ACTIONS_V1` |
| Reconciliation | `PORTFOLIO_RECONCILIATION_V1` |
| Store | `PORTFOLIO_STORE_V1` |
| Risk adapter | `PORTFOLIO_ACCOUNTING_TO_RISK_V1` |
| Manifest | `PORTFOLIO_ACCOUNTING_MANIFEST_V1` |

## Events

Five event types change state. There is no generic "set the balance" event,
because such an event makes every invariant below unenforceable.

| Event | Effect |
| --- | --- |
| `OPENING_BALANCE` | Establishes the ledger. Exactly one, and it must be first. |
| `FILL` | A buy or sell. Fees are expensed, never capitalised. |
| `EXTERNAL_CASH_FLOW` | A deposit or withdrawal. Changes NAV, never P&L. |
| `CASH_DIVIDEND` | Income. Cost basis untouched. |
| `SPLIT` | Quantity scales, total cost does not. No P&L created. |

`CORPORATE_ACTION` exists only so an unsupported action can be refused by name.

Every event binds `event_id`, `event_type`, `effective_at`, `source_id`,
`source_fingerprint` and a semantic payload, and carries a fingerprint over
those. Ingestion timestamps sit outside the fingerprint: a digest that moves with
the wall clock can never establish that two ledgers agree.

## The ledger

Append-only, with a hash chain binding `sequence`, the event fingerprint and the
previous record's hash. Four manipulations are detectable from the records alone:

| Manipulation | Detected by |
| --- | --- |
| Tamper | the stored event fingerprint no longer matches the event |
| Deletion | `LEDGER_SEQUENCE_GAP` |
| Reordering | `LEDGER_SEQUENCE_GAP` and a broken previous-hash link |
| Insertion | `LEDGER_HASH_CHAIN_MISMATCH` |

**Truncation is the exception.** A ledger with its tail removed is a valid
shorter chain, so the chain alone cannot detect it. The durable head anchor
closes that hole, and does so without depending on anyone remembering to schedule
anything.

Append is idempotent on `event_id`. The same id with a *different* payload is
`EVENT_ID_CONFLICT`, because one of the two is wrong and the ledger cannot know
which.

## The durable head anchor

`PORTFOLIO_LEDGER_HEAD_ANCHOR_V1`. A hash chain proves its records are internally
consistent; it cannot prove there were no more of them. P1.3's answer was a
checkpoint, which worked and depended on an operator remembering to write one — an
integrity guarantee that holds only when somebody remembered is a habit, not a
guarantee.

The anchor is mandatory. It binds:

```
ledger generation · sequence · ledger head hash
state fingerprint · engine version · previous anchor fingerprint
```

and carries its own digest. The writing clock is outside that digest: a
fingerprint that moved with the clock could never establish that two anchors
agree. The state fingerprint covers accounting content only — the display symbol
is excluded, because the ledger does not persist a ticker and an anchor written
from memory must match the same ledger read back from disk.

### The commit protocol

```
decide admissibility        (refuse before anything durable is written)
write the ledger record  -> fsync -> read back and rehash
replay the state
write the head anchor    -> fsync -> read back and verify
                         -> only now is the event committed
```

An event is operationally committed only when both the record and the anchor are
durable. The ordering is deliberate: a record without an anchor is a recoverable
crash, while an anchor without its record would assert history that does not
exist. Admissibility is decided *first*, so a refused event leaves the ledger,
the anchor and the in-memory state untouched — persisting a record the state
machine will always refuse would leave a ledger that can never be replayed again.

### Recovery

| Found | Status | Action |
| --- | --- | --- |
| anchor and ledger agree | `CONSISTENT` | open |
| ledger ahead of anchor | `RECOVERY_REQUIRED` | verify the tail, replay, advance the anchor |
| anchor ahead of ledger | `LEDGER_TRUNCATION_DETECTED` | fail closed |
| same sequence, different head | `LEDGER_HEAD_ANCHOR_MISMATCH` | fail closed |
| same events, different state | `STATE_FINGERPRINT_MISMATCH` | fail closed |
| records with no anchor | `ANCHOR_MISSING` | fail closed |
| anchor from another lineage | `LEDGER_GENERATION_MISMATCH` | fail closed |

**The anchor never moves backward.** Lowering it to match a shorter ledger would
erase the only local evidence that committed records are missing, turning a
detected truncation into a clean ledger. A ledger that has lost records is a
question for a person.

Checkpoints remain, as an optional cache for skipping replay. They are no longer
load-bearing for completeness: `verify()` reports `verified` (the chain and the
checkpoints, P1.3's question) and `tail_completeness_established` (the anchor,
P1.4's) separately, because a store written before anchors existed has the first
and not the second. A checkpoint may not claim more history than the anchor
witnesses.

### What the anchor does not do

> **`COORDINATED_LOCAL_ROLLBACK_DETECTION: NOT GUARANTEED.`** A ledger and its
> local anchor rolled back *together* to a mutually consistent earlier state
> validate cleanly. Anyone able to delete ledger records is generally able to
> rewrite the file beside them, and local files cannot establish that they are
> the latest state of themselves. This is a property of where they live, not an
> implementation gap.

Detecting that needs an anchor held where the ledger's writer cannot reach it.
`ExternalAnchorSink` is the interface for one — a protocol plus an in-memory
implementation for tests, with no network capability in the accounting package at
all. A remote implementation lives outside this package and is injected. P1.4
ships none and requires none.

## Identities

Checked on every valuation. A violation means a bug in this layer, not bad input.

```
cash  = opening + deposits - withdrawals - purchases + sales - fees + dividends
NAV   = cash + market value of holdings
P&L   = realised + unrealised + income - fees
NAV - inception NAV at cost = P&L + net external flows - unpriced cost basis
```

and under V2, four more:

```
global quantity              = Σ pool quantities
global cost basis            = Σ pool cost bases
global realised trading P&L  = Σ pool realised P&L
global unrealised P&L        = Σ pool unrealised P&L
```

`realised` is gross of fees and fees are subtracted once, so a purchase
commission is charged exactly as a sale commission is. The last identity is the
strongest check in the package: it compares the P&L story against the balance
sheet rather than against itself, and any handler that moved cash without
recording a matching P&L term fails it.

`inception NAV at cost` is opening cash plus the imported holdings' cost basis.
It is a cost basis rather than a market value because an opening balance states
the former and cannot state the latter — so an imported position's pre-existing
gain is reported as unrealised P&L earned here.

`unpriced cost basis` is zero for a complete valuation, which is the default.

## Refusals

| Code | Refused |
| --- | --- |
| `INSUFFICIENT_CASH` | a buy the balance cannot fund, or a withdrawal beyond it |
| `INSUFFICIENT_POSITION` | a sale beyond the quantity held |
| `SELL_ATTRIBUTION_REQUIRED` | a sale from a co-owned position that does not say whose shares it sold |
| `ATTRIBUTION_EXCEEDS_SLICE` | a strategy selling more than it holds |
| `MARK_AFTER_VALUATION_TIME` | a price stamped after the valuation instant |
| `VALUATION_INCOMPLETE` | a valuation missing any held position |
| `UNSUPPORTED_CORPORATE_ACTION` | an action whose economics are not modelled |
| `EVENT_TIME_REGRESSION` | an event taking effect before the ledger's last |
| `OPENING_BALANCE_ALREADY_EXISTS` | a second opening balance |
| `PARTIAL_EVENT_RECORD` | overwriting a stored record |
| `FORBIDDEN_LEDGER_NAMESPACE` | a store rooted in a research or evidence tree |
| `INSUFFICIENT_ATTRIBUTED_POSITION` | a sale beyond one owner's pool, even when the security has enough |
| `LEDGER_TRUNCATION_DETECTED` | a ledger shorter than its anchor witnesses |
| `LEDGER_HEAD_ANCHOR_MISMATCH` | an anchor disagreeing with the ledger head at its own sequence |
| `ANCHOR_MISSING` | records nothing witnesses |
| `ACCOUNTING_VERSION_UNKNOWN` | a ledger naming rules this build does not implement |
| `ATTRIBUTION_BASIS_NOT_RECONSTRUCTABLE` | a migration that would have to invent per-owner bases |

Fees count toward whether a buy can be funded. A stale mark is a reported warning
(`MARK_STALE`), not a refusal: a thinly traded security's last print genuinely is
its last price, but a NAV resting on week-old marks must say so.

## Corporate actions

Supported: `CASH_DIVIDEND` and `SPLIT`. Everything else is refused by name with
the terms it would have needed — a merger's exchange ratio and cash component, a
spinoff's basis allocation, a rights distribution's subscription price. A guessed
cost basis is indistinguishable from a correct one downstream, and an ignored
action leaves the ledger describing a security that no longer exists as recorded.

Entitlement is the producer's responsibility. Inferring a dividend from current
holdings would miss a position sold between the ex-date and the pay date and
would credit one bought after it.

## Attribution

Per-strategy slices, summing to the quantity held. Nothing is inferred: a sale
says whose shares it sold or it is refused. An imported holding whose owner is
unknown sits under `UNATTRIBUTED_EXISTING` and is reported as exactly that.

Cash is deliberately not attributed. It is fungible, it funds whichever strategy
trades next, and splitting it by any rule would make a strategy's reported
exposure depend on an allocation nobody decided.

## Reconciliation

Compares the ledger to an external statement and **never repairs**. There is no
repair function and it cannot be configured to have one.

| Break | Usually means |
| --- | --- |
| `CASH_MISMATCH` | a cash movement the ledger does not know about |
| `POSITION_QUANTITY_MISMATCH` | a partial fill recorded in full, or a custodian-applied action |
| `EXTERNAL_POSITION_MISSING_INTERNAL` | a missed fill, a spinoff, or a transfer in |
| `INTERNAL_POSITION_MISSING_EXTERNAL` | a missed sale, a delisting, or a transfer out |
| `INCOMPLETE_EXTERNAL` | the statement was not fully retrieved |
| `INCOMPLETE_INTERNAL` | the ledger has no opening balance |

A break is resolved by deciding what event explains it and appending that event.
Adopting the external figure would erase the only evidence that something
happened the ledger was not told about, and leave the cause in place to recur.

An incomplete statement never reconciles cleanly: a comparison against part of an
account reliably "proves" that the rest of it does not exist.

## Arithmetic

Accounting is `Decimal`, quantised once: cash to 4 places, prices and quantities
to 8, ratios to 10, with 50 digits of working precision. Identities are compared
*exactly* — every figure entering them was quantised, so a residue is a real
error rather than floating-point noise.

The risk layer is `float64`. The boundary is one adapter, in one direction:
accounting → risk, never back. `to_risk_snapshot` produces a
`PortfolioSnapshot`; nothing writes a risk target into a holding.

## Storage

One JSON file per record under `.portfolio/{ledger,checkpoints,reconciliation}`.
Chosen over SQLite deliberately: a database would also provide `UPDATE`, which is
the operation this design forbids, and the append-only guarantee would then rest
on discipline rather than on the format.

Every write is flushed, `fsync`ed, atomically renamed and the directory
`fsync`ed, so a crash leaves either the old state or the new one. Each append is
then read back and rehashed, catching a write that succeeded but landed wrong
while it is still one record.

The display symbol is not persisted. Identity is `(kind, identity_id)`; storing a
ticker would invite a later reader to treat a reassigned symbol as the same
company. A reloaded state carries `symbol=None` and every accounting figure is
byte-identical.

## Commands

```bash
stock-screener books replay <request.json> [--output books.json]
stock-screener books verify-store <.portfolio>
stock-screener books verify-anchor <.portfolio> [--accounting-version ...]
stock-screener books migrate-attribution <request.json> [--output report.json]
stock-screener books show-manifest
```

`replay` derives state from events and changes nothing. It appends no event,
simulates no fill, and has no broker credentials available to it.

Golden fixtures, each with a `.golden.json` the tests reproduce byte for byte:

| Fixture | Proves |
| --- | --- |
| `example_ledger.json` | V1 accounting, unchanged since P1.3 |
| `reconciliation_example.json` | all four break kinds, V1 |
| `multi_strategy_basis.json` | per-owner bases at differing entry prices, and the four pool identities |
| `full_strategy_exit.json` | a fully exited strategy keeps its realised P&L and its name |
| `anchor_ledger.json` | the anchored commit path, its trail, and identical recovery |

## What this layer never does

Places or cancels an order. Simulates a fill. Counts an external cash flow as
P&L. Repairs a reconciliation break. Infers a dividend entitlement. Writes a risk
target into a holding. Touches a research database.

## Known limitations

Closed in P1.4, and recorded here because the V1 engine still carries them:

- **V1 pools cost across strategies**, so a V1 ledger's per-strategy realised P&L
  is exact only when co-owners entered at the same price. V2 closes this.
- **V1 loses the owner of a closed position's realised P&L.** V2 retains it.
- **V1 truncation needed a checkpoint.** The mandatory head anchor closes this for
  anchored operation.

Still open:

- **Coordinated local rollback is not detectable.** A ledger and its local anchor
  rolled back together validate cleanly. An independently trusted external anchor
  is required; `ExternalAnchorSink` is the seam and P1.4 implements no remote
  sink.
- **No tax accounting.** `PER_ATTRIBUTION_WEIGHTED_AVERAGE_COST_V1` is an
  accounting basis, not a tax basis: no lot selection, no holding periods, no
  wash-sale rules, no tax character.
- **Per-strategy dividend income is not claimed.** The event contract states an
  aggregate entitlement, so a dividend is credited to the unknown-owner pool
  rather than divided across today's holders — who are not necessarily the owners
  who earned it. Global dividend income stays exact.
- **An opening position with several owners and one stated basis seeds every pool
  at that basis.** That is faithful to what the event asserts; the owners'
  separate entry prices are not in it and are not invented.
- **No accrued interest, no margin, no multi-currency.** Cash is a single balance
  in one implied currency, and the long-only baseline has no borrowing.
