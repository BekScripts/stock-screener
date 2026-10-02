---
type: reference
title: Portfolio accounting and reconciliation
---

# Portfolio accounting and reconciliation

`PORTFOLIO_ACCOUNTING_V1`. The layer that answers *what is actually owned*, as
distinct from what the risk engine would authorise. Implemented in
`src/stock_screener/portfolio_accounting/`.

A target is not a holding. P1.0–P1.2 decide what a portfolio should contain; none
of that is evidence that anything was bought. Only an event on the ledger changes
a holding.

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
shorter chain, so the chain alone cannot detect it. Checkpoints close that hole:
each records a head hash and a record count, and `CHECKPOINT_UNVERIFIABLE` fires
when a ledger no longer matches one.

Append is idempotent on `event_id`. The same id with a *different* payload is
`EVENT_ID_CONFLICT`, because one of the two is wrong and the ledger cannot know
which.

## Identities

Checked on every valuation. A violation means a bug in this layer, not bad input.

```
cash  = opening + deposits - withdrawals - purchases + sales - fees + dividends
NAV   = cash + market value of holdings
P&L   = realised + unrealised + income - fees
NAV - inception NAV at cost = P&L + net external flows - unpriced cost basis
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
stock-screener books show-manifest
```

`replay` derives state from events and changes nothing. It appends no event,
simulates no fill, and has no broker credentials available to it.

Golden fixtures: `fixtures/portfolio_accounting/example_ledger.json` and
`reconciliation_example.json`, each with a `.golden.json` the tests reproduce
byte for byte.

## What this layer never does

Places or cancels an order. Simulates a fill. Counts an external cash flow as
P&L. Repairs a reconciliation break. Infers a dividend entitlement. Writes a risk
target into a holding. Touches a research database.

## Known limitations

- **Weighted-average cost pools basis across strategies.** When two strategies
  hold the same security at different prices, each one's reported cost basis is
  the pooled average rather than what it actually paid. Portfolio-level realised
  P&L is exact; the per-strategy split is exact only when co-owners entered at
  the same price. Fixing this needs per-strategy tax lots, which changes what a
  position is.
- **Realised P&L on a fully closed position loses its owner.** Once every share
  is sold the ledger no longer records who held it, so that position's realised
  result is reported as unattributed.
- **No tax lots, no wash-sale rules, no tax character.** `WEIGHTED_AVERAGE_COST_V1`
  is an accounting basis, not a tax basis.
- **No accrued interest, no margin, no multi-currency.** Cash is a single balance
  in one implied currency, and the long-only baseline has no borrowing.
- **A checkpoint must be written to make truncation detectable.** A ledger with
  no checkpoint can be truncated and will still verify.
