---
type: reference
title: R1.4 H2 preregistration
---

# R1.4 H2 preregistration

`stock_screener.membership_hypothesis` defines one hypothesis,
`H2_MEMBERSHIP_STABILITY`, and one rule, `ONE_DECILE_HYSTERESIS_V1`, and freezes
both before anything is measured. It runs no experiment.

Run by `stock-screener backtest preregister-h2`. Reads only.

## Why it cannot compute a result

The experiment does not exist yet, so the package must not be able to produce
one. `binding.simulation_reachable` reads this package's own source and reports
every import of a name that could simulate or price — `simulate`, `PriceBook`,
`Rebalance`, `performance`, `benchmark_path`, `run_experiment`, `run_r1_0`,
`load_labels`. `bind_r1_3` calls it first and refuses when the list is
non-empty. `r1_4_complete.json` records `strategies_simulated`,
`return_calculations` and `turnover_calculations`, all zero.

## The rule

| Part | Value |
| --- | --- |
| entry | the ordinary top-decile target count — exactly R1.2's selection |
| retention | rank position inside **twice** that count |
| target size | the ordinary count, taken from R1.2 and never recomputed |
| over capacity | best-ranked retained incumbents kept, worst-ranked dropped |
| vacancy fill | highest-ranked eligible identities not already held |
| first rebalance | no incumbents exist, so it is exactly R1.2's top decile |
| everything else | R1.2's, unchanged |

Two choices are structural rather than selected. The **target count** is not
recomputed, so both strategies always hold the same number of names and a
turnover difference cannot come from holding fewer. The **band** is two
selection widths expressed in names, not a percentile: one width admits, a
second retains. It equals the top 20% by construction while inheriting the entry
rule's rounding instead of adding a second one, and that is what makes it one
decile of hysteresis rather than a tuned value near twenty percent.

R1.3 measured where entrants and exits sit relative to the cutoff and
deliberately chose no width from those distributions. Choosing one there would
have fitted the parameter to the result it must later be judged against.

## Selection order

1. Build the same executable ranked cross-section R1.2 builds.
2. Take the ordinary top-decile target count from the frozen R1.2 rule.
3. Remove incumbents the frozen contract no longer permits.
4. Keep remaining incumbents inside twice the count.
5. If kept incumbents exceed the count, drop the worst-ranked until it fits.
6. Fill any vacancy with the highest-ranked eligible identities not held.
7. The final count equals the ordinary count whenever enough names exist.
8. Equal-weight. 9. Execute at the next session's open.

Ranking is descending `COMPOUNDER_V1_2`, ties by `(kind, identity_id)`. Being
held is never a reason to keep a name that is ineligible, unscored, untradable
or terminal — those exits override the band.

`rule.Candidate` carries `kind`, `identity_id`, `final_score` and `eligible`,
and nothing else. There is no field a forward return could arrive in.

## The criteria, and the aggregate rule

| | Requirement |
| --- | --- |
| C1 | annual turnover strictly below the comparator's 2.253982668222645× |
| C2 | annualised entry/exit churn turnover strictly below 2.11411510465003× |
| C3 | same target-count rule, 0 intentional cash quarters, no hysteresis-caused exposure loss |
| C4 | 0 accounting violations, 0 identity violations, 0 future leakage, 0 same-close executions |

`SUPPORTED` requires **C1 AND C2 AND C3 AND C4**; anything else is
`NOT_SUPPORTED`. H1 stated five criteria and no way of combining them, which
R1.2 had to respect after the fact; H2 fixes the combination in advance so the
ambiguity cannot arise twice.

C2 compares an absolute annualised quantity rather than a share, because a share
can fall while the quantity rises. C3 exists so a turnover reduction cannot be
bought by holding fewer names or more cash.

A `SUPPORTED` verdict says the mechanism behaved as claimed. It says nothing
about investment merit: H2 makes no performance claim, and no performance
measurement takes part in the verdict.

### The legacy 1.95×

R1.3 classified H1's ceiling as an empirical anchor times an undeclared one
half. It is carried as `LEGACY_H1_TURNOVER_REFERENCE`, a continuity reading
only. Crossing it proves nothing about H2 and failing to cross it refutes
nothing. The binder refuses a specification that promotes it to a criterion or
moves its value.

## Historical evidence status

H2 was derived from R1.3's analysis of the whole frozen period — development,
validation and the already-read holdout alike. The record says so:

- the frozen dataset is `HYPOTHESIS_GENERATION_DATA_FOR_H2`;
- a later historical run may provide `EXPLORATORY_HISTORICAL_EVIDENCE` and
  `MECHANISM_EVIDENCE`, and must be classified `EXPLORATORY_HISTORICAL_TEST`;
- it may **not** provide `INDEPENDENT_CONFIRMATION`,
  `CLEAN_HOLDOUT_CONFIRMATION`, `OUT_OF_SAMPLE_CONFIRMATION` or
  `PROSPECTIVE_CONFIRMATION`;
- `clean_h2_holdout_remains` is false and no subperiod is relabelled;
- `CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST` is true.

No prospective duration is chosen, because no research policy defines one.

## The binder R1.5 must pass

`verify_h2` is written here rather than in R1.5, because the pressure a
preregistration exists to resist arrives at execution time. It refuses when:

| Condition | Why |
| --- | --- |
| the record or specification digest moved | it is not the frozen preregistration |
| the specification binds to a different record | the pair must match |
| the specification is executed, or permits more than one run | once, then never |
| any of eight singular fields carries more than one value | that is a sweep |
| the stored rule, or the executor's rules, differ in any entry | drift |
| the criteria or the aggregate rule changed | the verdict was rewritten |
| 1.95× became a criterion, or its value moved | it is a diagnostic |
| the comparator is not `COMPOUNDER_QUARTERLY_TOP_DECILE_V1` | wrong comparison |
| a historical run claims to be out-of-sample, or a holdout is reset | contamination denied |

There is no `force` parameter.

## Outputs

Under `.evidence/research/r1_4/`, mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-4/`:

| File | Holds |
| --- | --- |
| `r1_4_binding.json` | every digest verified, and R1.3's measurements re-read |
| `h2_membership_stability.json` | the hypothesis, rule, criteria and contamination record |
| `r1_5_experiment_spec.json` | the specification, `PREREGISTERED_NOT_EXECUTED` |
| `r1_4_fingerprints.json`, `r1_4_complete.json` | the compact record |

`research_ledger.jsonl` records the phase as `HYPOTHESIS_PREREGISTRATION` with
zero variants, zero sweeps, zero return calculations and zero holdout
evaluations.
