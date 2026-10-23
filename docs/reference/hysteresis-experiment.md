---
type: reference
title: R1.5 H2 experiment
---

# R1.5 H2 experiment

`stock_screener.hysteresis_experiment` executes the one experiment R1.4 froze —
`ONE_DECILE_HYSTERESIS_V1` under `H2_MEMBERSHIP_STABILITY` — exactly once, and
judges it by four criteria fixed before anything was measured.

Run by `stock-screener backtest run-h2`. Reads only.

## The order, and why it is the design

| Step | What happens |
| --- | --- |
| 1 | bind — `verify_h2` plus the whole upstream record; no price read yet |
| 2 | build the decisions from the comparator's own frozen cross-sections |
| 3 | freeze the target memberships and the displacement ledger to disk |
| 4 | simulate, gross and at 10 bps per side |
| 5 | gather accounting, identity and execution findings |
| 6 | rebuild turnover under the frozen definition and decompose it |
| 7 | **fix the mechanism verdict and write it** |
| 8 | only now, compute performance |

`evaluate` takes turnover, exposure and safety and has **no parameter a return
could arrive in**, so the mechanism verdict is mathematically independent of
performance rather than merely intended to be. A test asserts that signature.

## What is shared with the comparator

Everything except the selection rule. The calendar, the executable
cross-sections, the scores and the target counts are read from R1.2's frozen
ledgers rather than rebuilt, so a difference between the two strategies can only
come from which rows each one selects.

Two guards live in `selection.py` rather than in a test:

- the **target count** is asserted against R1.2's selection ledger at every
  decision — a silently different decile count would make C1 a comparison
  between two portfolios rather than two rules;
- the **first decision** is asserted to reproduce R1.2's selection exactly,
  which it must: with no incumbents the hysteresis rule reduces to the ordinary
  one, so a difference there would mean the ranking itself had drifted.

## The four criteria

| | Requirement | Basis |
| --- | --- | --- |
| C1 | annual turnover strictly below the comparator's | the frozen R1.2 definition, unchanged |
| C2 | annualised entry/exit churn turnover strictly below the comparator's | an absolute quantity, never a share |
| C3 | same target-count rule, 0 intentional cash quarters, no hysteresis-caused exposure loss | selection ledger, cash flags, invested weight |
| C4 | 0 accounting, identity, leakage and same-close violations | harness invariants, gathered not asserted |

`SUPPORTED` requires **C1 AND C2 AND C3 AND C4**. There is no partial label:
H2 has none and `interpret` invents none. `interpret` refuses a criterion set
that is not exactly four.

### The C2 threshold basis

The frozen threshold came from R1.3's ranking-churn *share*, which is a share of
all traded notional including terminal legs. This phase measures churn as a
share of the turnover metric's own numerator, which excludes them. Rebuilding
the comparator on the phase's basis gives a marginally **higher** figure, so the
frozen threshold is the stricter of the two and was the one used.
`r1_5_turnover.json` records both.

### The legacy 1.95×

Carried as a continuity reading only. `is_an_h2_criterion` is false,
`used_in_this_verdict` is false, and no criterion threshold equals it.

## Execution state

A preregistered specification is spent once. `ExecutionLock` writes
`r1_5_execution_state.json` beside the outputs before the run begins; a second
attempt finds it and stops. A run that crashes releases the marker, because a
specification is spent by a finished experiment rather than by a failure.

**The preregistered specification is never rewritten.** A document that gains a
result afterwards is no longer the document that was frozen, so the marker and
the results live in their own files and `r1_5_experiment_spec.json` keeps its
original digest.

## Historical status

Mandatory and recorded on every artifact: `EXPLORATORY_HISTORICAL_TEST`, with
`independent_confirmation: NOT_PROVIDED_BY_R1_5`. R1.3 generated this hypothesis
from the whole frozen period, so nothing here is out-of-sample. The legacy
research blocks are renamed `LEGACY_DEV_BLOCK`, `LEGACY_VAL_BLOCK` and
`LEGACY_HOLDOUT_BLOCK`, each marked `DESCRIPTIVE_ONLY_FOR_H2`, and the last
additionally `CONTAMINATED_FOR_H2_CONFIRMATION`.

## Outputs

Under `.evidence/research/r1_5/`, the compact ones mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-5/`:

| File | Holds |
| --- | --- |
| `r1_5_binding.json` | every digest verified before a price was read |
| `r1_5_selection_ledger.jsonl` | per decision: target count, what the rule did, overlap with the ordinary set |
| `r1_5_displacement_ledger.jsonl` | each retained incumbent and the higher-ranked name it kept out |
| `r1_5_trade_ledger.jsonl`, `r1_5_holdings_ledger.jsonl`, `r1_5_nav.jsonl` | the executed path |
| `r1_5_turnover.json` | the rebuild, the decomposition, the comparator and the attribution |
| `r1_5_membership_diagnostics.json` | retention, holding duration, re-entry, displacement |
| `r1_5_h2_evaluation.json` | the four criteria and the aggregate verdict |
| `r1_5_performance.json` | computed after the verdict; decides nothing |
| `r1_5_execution_state.json` | the durable one-run marker |
