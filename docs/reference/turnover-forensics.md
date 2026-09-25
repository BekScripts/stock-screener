---
type: reference
title: R1.3 turnover forensics
---

# R1.3 turnover forensics

`stock_screener.turnover_forensics` explains why R1.2 found the frozen turnover
criterion C3 unmet. It reads the R1.0, R1.1 and R1.2 records, rebuilds the
figure under investigation from the ledgers, and writes only descriptions. It
executes no strategy and selects no parameter.

Run by `stock-screener backtest review-turnover`. Reads only.

## Why it cannot simulate

A forensic phase that could run a strategy would be a strategy phase with a
different name. `binding.simulation_reachable` reads this package's own source
and reports every import of a name that could execute one — `simulate`,
`PriceBook`, `Rebalance`, `run_r1_0`, `run_experiment`. `bind_r1_2` calls it
before reading any value and refuses when the list is non-empty. The check reads
source rather than introspecting a loaded module, so it sees an import that
exists on a path that never runs.

## The turnover definition

Read off the implementation, not assumed from convention.

| Part | Value |
| --- | --- |
| per rebalance | `Σ|notional|` over every BUY and SELL leg ÷ `2 × pre-trade NAV` |
| direction | one-way; replacing every holding once scores 1.0 |
| numerator excludes | terminal settlements, which execute on their own date |
| denominator | pre-trade NAV at the rebalance open — not average, not closing |
| annualisation | mean per-rebalance turnover × nominal rebalances per year |
| cost treatment | notional is pre-cost; commission is never part of turnover |

The denominator is in no ledger, so it is recovered: a holding carries
`market_value` and `actual_weight` against the post-trade portfolio, so their
quotient is that portfolio's NAV, and under the gross cost model trading at one
set of prices conserves value — which makes post-trade NAV at the open equal to
pre-trade NAV. That identity is what lets an independent path land on the same
bits as the stored metric.

### Two annualisation conventions

The frozen record contains both, and they are not interchangeable:

| Convention | Quarterly | Monthly, matched window |
| --- | --- | --- |
| nominal frequency (mean × N) | 2.2540× | 3.7642× |
| elapsed years (Σ ÷ years) | 2.3621× | 3.7900× |

C3's anchor — 3.90× from H1's premise C — is a nominal-frequency figure, and so
is the quarterly number compared against it, so the criterion compares like with
like. R1.2's matched comparator reported the elapsed-years figure instead, which
is why its 3.7949× differs from 3.7642× for the same window. The verdict is
invariant: quarterly exceeds 1.95× under either.

## Attributing membership change

Trading follows from the target set changing, so the phase asks what changed it.
Calling every entrant and exit "ranking churn" would attribute to the score a
decision the universe made, so each change is read from the cross-section row
that applied at the other decision:

| Cause | Means |
| --- | --- |
| `RANKING` | present, priced, score-ready and tradable, and simply outside the selected fraction — the only case the score is responsible for |
| `TRADABILITY` | `NOT_TRADABLE_UNVERIFIED` or `NOT_TRADABLE_UNTRADABLE` |
| `SCORE_READINESS` | not eligible, insufficient data, unsupported sector, no signal snapshot |
| `FEATURE_FRESHNESS` | `STALE_FUNDAMENTALS` |
| `EXECUTION_AVAILABILITY` | `NO_EXECUTION_PRICE` |
| `EVIDENCE_TIMING` | `SAME_DAY_HISTORICAL_FILING` |
| `NOT_IN_UNIVERSE` | absent from the cross-section entirely |

`CAUSE_BY_EXCLUSION` deliberately maps nothing to `RANKING`; that label is
reached only by an executable row.

## The lower bound

The question C3 turns on is whether the ceiling was reachable at all. An exit
must be sold in full and an entrant bought to its target, so both are forced by
the target set; only resizing a position held on both sides could in principle
be avoided. Removing every resizing trade while holding the executed selections
fixed gives the floor for those selections under equal weighting.

`lower_bound` reports that floor and whether it clears the ceiling. It is
arithmetic on trades that already happened — no trade is generated, no selection
changes, and the floor is not a strategy.

## Two populations for stability

Rank persistence is measured twice, because the two populations differ by about
a factor of two and reporting one silently would look like a disagreement with
R1.1:

- **all scored** — every identity carrying a score at both decisions;
- **executable only** — identities no exclusion applied to, which is what R1.1
  measured and the only population the selection can draw from.

The executable figure reproduces R1.1's month-to-month rank correlation exactly.

## What the phase refuses to do

| Refused | Why |
| --- | --- |
| choose a hysteresis width, buffer, minimum holding period or position count | these data are the outcome such a rule would be judged against; choosing here fits the parameter to the result |
| call a working mechanism a defect | `classify` returns `M1` only on positive evidence — a figure that does not reproduce, a decomposition that does not close, or a broken accounting invariant |
| read four met criteria as H1 passing | the frozen specification states no rule for combining its five criteria, so none is applied |
| upgrade the holdout | it remains `CONTAMINATED_FOR_H1_CONFIRMATION` |
| return H1 confirmed | no member of `H1_STATUSES` is confirmation |

## Outputs

Under `.evidence/research/r1_3/`, mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-3/`:

| File | Holds |
| --- | --- |
| `r1_3_binding.json` | every digest verified before anything was read |
| `r1_3_turnover_reconciliation.json` | the definition, the independent rebuild, both conventions, the decomposition, the counterfactual, concentration, cost |
| `r1_3_membership_transitions.json` | transitions by cause, retention, the monthly survival curve, holding duration, size effects |
| `r1_3_rank_stability.json` | rank and score persistence on both populations |
| `r1_3_boundary_churn.json` | cutoffs, prior and current bands, rank-move distribution |
| `r1_3_holding_duration.json` | episode lengths and re-entry |
| `r1_3_turnover_lower_bound.json` | the floor, the critical question, threshold provenance |
| `r1_3_h1_forensic_evaluation.json` | mechanism, H1 final status, next dimension |
| `r1_3_summary.json`, `r1_3_complete.json` | the compact record |

`research_ledger.jsonl` records the phase as `FORENSIC_ANALYSIS` with zero
variants tried, zero sweeps and zero strategies simulated.
