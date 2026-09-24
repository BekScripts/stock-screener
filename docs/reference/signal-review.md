---
type: reference
title: R1.1 signal review
---

# R1.1 signal review

`stock_screener.signal_review` interprets the R1.0 record. It computes no
strategy, searches no parameter and changes no frozen system; it takes the
baseline result apart, states what the result describes and what it is limited
by, and produces at most one thing that points forward — a frozen
specification for a single experiment.

Run by `stock-screener backtest review-signal`. Reads only.

## What it binds to

R1.0's outputs are inputs here, so they are re-derived rather than trusted.
`bind_r1_0` reproduces each artifact's fingerprint exactly as R1.0 produced it
and compares against `r1_0_fingerprints.json`:

| Shape | Fingerprint is | Files |
| --- | --- | --- |
| document | the digest of the file's own bytes (sorted keys, two-space indent) | the split, registry, preregistration, coverage, diagnostics, both audits, the summary |
| ledger | the digest of `{"rows": [...]}` over the parsed rows | trades, holdings, NAV, selection, deciles |
| cross-section | the digest of the seven-field projection R1.0 fingerprinted | `cross_sections.jsonl` |

A mismatch ends the command. There is no override.

## The two contrasts

R1.0 recorded `D10 − D1`, the long-short contrast. R1.1 reports it unchanged
and adds `D10 − cross-section mean` beside it, together with the full
ten-decile profile for every block, because the two answer different questions:

- **`D10 − D1`** is what a long-short book would earn. It is only a fair
  summary of a profile that rises monotonically.
- **`D10 − cross-section mean`** is what a long-only top-decile portfolio is
  exposed to, because such a portfolio never holds the bottom decile.

Where the profile is **U-shaped** — both tails above the middle — the first
collapses while the second does not. `subset_diagnostics` classifies each
block's profile as `RISING`, `U_SHAPED` or `FLAT` so neither number can be read
on its own, and `label_versus_path` in the review document reconciles the
cross-sectional view against the realised portfolio path block by block.

The second measure was added during R1.1, after the profile shape was observed.
The classification rubric was **not** changed to use it.

## Diagnostics

| Area | What is computed |
| --- | --- |
| Splits and years | Decile profile, both contrasts, rank IC, each summarised across months |
| Monthly series | The full 51-month contrast and IC series with its distribution |
| Leave-one-out | Every month and every year removed in turn; the sign's survival |
| Persistence | Month-to-month rank correlation, top-decile retention at one and three months |
| Horizons | Both contrasts and the IC at 1, 5, 20, 60 and 120 sessions, each with how many later monthly decisions its window overlaps |
| Cutoff stability | Where the top-decile score threshold sat each month |
| Cross-section size | Rank correlation of month size against that month's signal |
| Liquidity | Both measures across equal-count buckets of the frozen single-venue ADV field |
| Concentration | Profit by identity and by month, cumulative at 1, 5, 10, 25 and 50 |
| Holdings | Realised weights, top-5 and top-10 weight, effective holdings per rebalance |
| Turnover | Traded notional split into entrants, exits, resizes and terminal events |
| Cost | The analytic per-side break-even against the benchmark |
| Path | Relative drawdowns, rolling twelve-month figures, drawdown episodes, monthly return distributions |
| Attrition | The funnel by stage, reason and identity class |

## The cost break-even

Analytic, from the realised ledger rather than from more backtests. A cost `c`
per side on a trade of notional `N` taken when the portfolio is worth `V`
multiplies the path by `(1 − c·N/V)`, so the path is multiplied by roughly
`exp(−c·S)` with `S = Σ N/V`. Setting the costed total return equal to the
benchmark's and solving gives the break-even, and the model is validated
against the one cost level R1.0 actually ran.

**It is not a market model.** Spread, market impact, capacity, latency and
financing are not in it.

## What is deliberately not computed

| Section | Status | Why |
| --- | --- | --- |
| Score change vs level | `SKIPPED` | The frozen system defines no canonical notion of a score change, and inventing one is a construct this phase may not create |
| Market regime | `REGIME_DIAGNOSTIC_NOT_AVAILABLE` | No frozen point-in-time market state exists; `MARKET_SIGNALS_V1` carries a per-security trend direction, not a market one |
| Sector attribution | forbidden | Sector history is `BLOCKED`; a test scans every module for a sector reference |
| Market-cap size control | not computed | The contract records market capitalisation as a lower bound |
| `MARKET_SIGNALS_V1` | documented only | Its output semantics are described and future questions listed; no scalarisation, no combination, no performance test |

## Holdout status

R1.0 evaluated its four preregistered strategies against 2025-01 … 2026-03
once, legitimately, under a hashed preregistration. That interval is therefore
**not** an untouched holdout for anything derived afterwards, and every R1.1
artifact records it:

```text
R1_0_HOLDOUT_STATUS:    VALID_FOR_R1_0_PREREGISTERED_STRATEGIES
NEW_DERIVED_HYPOTHESES: HOLDOUT_ALREADY_OBSERVED
```

No untouched historical holdout exists in the frozen dataset, and none is
manufactured by re-cutting the split. Genuine confirmation must come from
observations that did not exist when a hypothesis was frozen, from a paper
period run under exactly its rules, or from an independently frozen future
extension.

## The rubric

`review.RUBRIC` holds eight conditions, written as code before the diagnostics
that feed most of them were computed:

| Condition | Passes when |
| --- | --- |
| `direction` | both headline means are positive in all three blocks |
| `breadth` | at least 55% of months have a positive rank IC |
| `month_robustness` | removing any single month leaves both means positive |
| `year_robustness` | removing any single year leaves both means positive |
| `name_concentration` | the five largest contributors are under half the profit |
| `month_concentration` | the best single month is under a quarter of the profit |
| `persistence` | top-decile membership survives one month at least half the time |
| `cost_headroom` | the break-even is at least twice the 10 bps sensitivity |

All eight passing gives `STRONG_ENOUGH_FOR_HYPOTHESIS_TEST`; a positive
direction with any failure gives `PROMISING_BUT_FRAGILE`; a negative block
gives `INCONCLUSIVE`; a negative overall result gives `NO_MEANINGFUL_SIGNAL`.

Statistical significance is deliberately **not** a condition. The question is
whether one further preregistered experiment is warranted, not whether the
effect is established — and the uncertainty is reported either way.

## The hypothesis and the one experiment

`H1_MEDIUM_HORIZON_SELECTION`: the frozen score looks like a persistent
medium-horizon selection signal rather than a short-horizon timing signal, so a
lower-frequency top-decile portfolio may keep much of the selection effect
while cutting turnover. Five premises are scored `SUPPORTED`,
`WEAKLY_SUPPORTED` or `NOT_SUPPORTED` from the diagnostics, and any
`NOT_SUPPORTED` rejects the hypothesis.

If it survives, `r1_2_experiment_spec.json` freezes
`COMPOUNDER_QUARTERLY_TOP_DECILE_V1` — quarter-end signal, top 10%, equal
weight, next-session open, one cost level, unchanged terminal policy — with its
falsification criteria fixed in advance. `experiment_spec` raises without an
accepted hypothesis, so a specification cannot exist as a strategy someone
simply wanted to try, and it is never executed here.

## Outputs

Under `<control>/research/r1_1/`, mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-1/`: `holdout_status.json`,
`compounder_signal_review.json`, `compounder_split_diagnostics.json`,
`compounder_year_diagnostics.json`, `compounder_persistence.json`,
`compounder_concentration.json`, `compounder_turnover_analysis.json`,
`compounder_cost_break_even.json`, `sample_attrition.json`,
`compounder_monthly_signal.jsonl`, `r1_1_hypothesis.json`,
`r1_2_experiment_spec.json`, `r1_1_summary.json`, `r1_1_fingerprints.json` and
`r1_1_complete.json`.
