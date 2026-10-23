---
type: reference
title: R1.2 quarterly experiment
---

# R1.2 quarterly experiment

`stock_screener.quarterly_experiment` executes one preregistered experiment —
`COMPOUNDER_QUARTERLY_TOP_DECILE_V1` — and judges it against criteria it is not
allowed to touch. It tests a rebalance frequency, not a score, and it searches
nothing.

Run by `stock-screener backtest run-quarterly`. Reads only.

## What holds it to the specification

R1.1 froze the rules in `r1_2_experiment_spec.json`. The executor does not
restate them: `binding.EXPECTED_RULES` lists exactly what it is about to apply,
and `bind_experiment` refuses to run unless every entry matches the stored
document verbatim. It also refuses when:

| Condition | Why |
| --- | --- |
| the hypothesis is not `ACCEPTED_FOR_ONE_EXPERIMENT` | an experiment without a surviving hypothesis is a strategy someone wanted to try |
| the spec is not marked unexecuted | it has already been run |
| any of `frequencies_tested`, `selection_fractions_tested`, `weightings_tested`, `execution_rules_tested`, `cost_levels_tested`, `variants_tried` is not 1 | more than one of anything is a search |
| `parameter_sweeps` is non-empty | likewise |
| the criteria do not number five, or do not name the turnover reference | the criteria are frozen and are not guessed at |

The full R1.0 record is re-derived underneath through
`signal_review.bind_r1_0`, so 18 further fingerprints must match before
anything is computed.

## Why the comparison is apples-to-apples

A quarter's last benchmark session is also that month's last session, so **every
quarterly decision date is one of R1.0's monthly decision dates**. The module
proves this rather than assuming it, and the consequence is what makes R1.2
meaningful:

- the quarterly cross-sections are **sliced from R1.0's frozen ledger**, not
  recomputed — identical identities, scores, tradability verdicts and execution
  prices;
- the monthly comparator, `R1_0_MONTHLY_TOP_DECILE_MATCHED_WINDOW`, is R1.0's
  own NAV and trade ledgers restricted to the quarterly strategy's sessions. No
  trade is altered and nothing is re-simulated;
- the benchmark uses exactly those sessions too.

Only the rebalance frequency differs, which is the one thing H1 is about.

## The five frozen criteria

R1.1 stated each as a falsification condition. The executor inverts each into a
predicate and reports the measurement beside the verdict.

| # | Survives when |
| --- | --- |
| 1 | mean quarterly `D10 − D1` at 60 sessions is positive over the full window |
| 2 | the decile rank association at 60 sessions is positive |
| 3 | annual turnover falls by at least half against 3.90×, i.e. to ≤ 1.95× |
| 4 | benchmark-relative total return is positive in at least two of the three blocks |
| 5 | the five largest identity contributors are under half of the profit |

**The specification states no rule for combining them.** R1.2 therefore applies
none: `interpret` reduces the five verdicts to `SUPPORTED_BY_R1_2` (all met),
`NOT_SUPPORTED_BY_R1_2` (none met) or `PARTIALLY_SUPPORTED_BY_R1_2`
(a mixture), records that this is the unavoidable all/none/mixed summary rather
than a preregistered rule, and applies no weights and treats no criterion as
decisive.

## Research status, carried everywhere

```text
experiment_type: HYPOTHESIS_DRIVEN_HISTORICAL_EXPERIMENT
independent_out_of_sample_confirmation: false
r1_0_holdout_block: REPORTABLE_BUT_CONTAMINATED_FOR_NEW_HYPOTHESIS_CONFIRMATION
```

H1 was formed after R1.0 had read 2025-01 … 2026-03. That block is reported in
full and is never described as confirmation, and no replacement holdout is
manufactured by re-cutting the split. True confirmation needs data that did not
exist when the specification was frozen, or a prospective shadow period under
exactly these rules.

## Outputs

Under `<control>/research/r1_2/`, with the compact metadata mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-2/`:
`r1_2_experiment_binding.json`, `quarterly_decision_calendar.jsonl`,
`quarterly_cross_sections.jsonl`, `quarterly_selection.jsonl`,
`quarterly_trade_ledger.jsonl`, `quarterly_holdings_ledger.jsonl`,
`quarterly_nav.jsonl`, `quarterly_metrics.json`,
`quarterly_decile_diagnostics.json`,
`monthly_matched_window_comparison.json`, `quarterly_sample_attrition.json`,
`r1_2_hypothesis_evaluation.json`, `research_ledger.jsonl`,
`r1_2_run_manifest.json`, `r1_2_summary.json`, `r1_2_fingerprints.json` and
`r1_2_complete.json`.

## What it will not do

No second frequency, no other selection fraction, no score change, no
`MARKET_SIGNALS_V1` combination, no sector logic, no threshold moved after a
result, and no claim of alpha — benchmark-relative return is called
benchmark-relative return. Tests scan every module in the package for sector
references and for market-signal imports, and assert that the comparator
contains no simulation call.
