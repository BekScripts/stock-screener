---
type: reference
title: Reference
---

# Reference

Descriptions of what things are. Complete, accurate, and dry — reference pages
describe the machinery and nothing else. No tutorials, no advice, no worked
examples that will drift out of date.

If you find yourself writing "you should" or "it's usually best to" on a
reference page, that sentence belongs in
[explanation](../explanation/index.md) or a [how-to](../how-to/index.md).

- [Project state](PROJECT_STATE.md) — the current checkpoint: phase, frozen decisions, schema, limits
- [Configuration](configuration.md) — every setting the app reads
- [Storage layout](storage-layout.md) — artifact contract, local control plane and B2.4 failure semantics
- [A3 planning inputs](a3-planning-inputs.md) — the planner's input contract, the dated rules behind it, the frozen V2 package, the forensic review, the corrected package and the authoritative Plan A3 V2
- [Backtest data contract](backtest-contract.md) — `PIT_RESEARCH_DATASET_V2` under `BACKTEST_DATA_CONTRACT_V1`: what a historical decision may see, the identity and time semantics, the dataset fingerprints, the readiness matrix and the first safe research class
- [R1 backtest harness](backtest-harness.md) — the leakage-safe monthly harness over that dataset: the decision calendar, the frozen split, the cross-section funnel, `TERMINAL_LEG_POLICY_V1`, the preregistered baselines and what they are read under
- [R1.1 signal review](signal-review.md) — interpreting that baseline: the two contrasts and why a U-shaped decile profile makes them differ, the concentration and robustness forensics, the analytic cost break-even, the holdout-contamination record, the classification rubric and the one experiment it licenses
- [R1.2 quarterly experiment](quarterly-experiment.md) — executing the one preregistered experiment: how the executor is held to the frozen rules, why the quarterly cross-sections are sliced from R1.0 rather than recomputed, the five criteria and why no aggregate rule is invented
- [R1.3 turnover forensics](turnover-forensics.md) — explaining the one criterion that failed: the turnover formula the project actually uses, the two annualisation conventions, how membership change is attributed to cause rather than to the score, the floor that shows the ceiling was unreachable, and why no replacement rule is chosen here
- [R1.4 H2 preregistration](membership-hypothesis.md) — the next hypothesis, frozen before it is tested: one membership-hysteresis rule whose band is structural rather than chosen, four criteria and the aggregate rule that combines them, why no part of the frozen history can confirm it, and the binder R1.5 has to pass
- [R1.5 H2 experiment](hysteresis-experiment.md) — spending that preregistration once: the fixed order that puts the mechanism verdict before any return, what is shared with the comparator and what is not, the four criteria and the aggregate rule, the durable one-run marker, and why the result is exploratory whatever it says
- [R1.6 H2 candidate and shadow protocol](prospective-protocol.md) — freezing what history established and closing what it can no longer offer: the candidate and why it is frozen on the mechanism rather than the returns, the bridge contract to a live stream, the four-transition window whose first date is a rule rather than a date, and the promotion ladder that stops at shadow
- [R1.7 prospective shadow runner](prospective-runner.md) — the machinery that waits: the frozen session calendar checked against every historical session, activation that cannot be backdated, arming that derives the first decision rather than accepting one, the semantic fingerprint that halts capture when the decision path changes, and why decision and execution packets are separate records
- [R1.9 prospective source plane](prospective-source.md) — the data plane R1.8 was lost for want of: the two timestamps that must never be conflated, the four adapters and how each proves availability, why five of six is BLOCKED, the capture-timing constraint a current-state source imposes, and the governance audit that settles the missed quarter
- [Commands](commands.md) — every `make` target and CLI command
- [Metrics](metrics.md) — every calculation, its formula, and when it is unavailable
- [CompounderScore v1](compounder-score.md) — every scoring rule, penalty and status
- [Market signals](market-signals.md) — the descriptive market-state layer, and what it deliberately is not
- [Point-in-time metrics](point-in-time.md) — fundamentals as they stood on a past date, and what that still does not enable
- [Project brief](project-intro.md) — the full Compounder Radar MVP specification
- [Phase 1 brief](project-phases/phase1.md) — the data and screening scope
- [Phase 2 brief](project-phases/phase2.md) — the scoring and ranking scope
- [Phase 2 constraints](project-phases/phase2-constraints.md) — decisions the scoring work inherits
- [Phase 3 brief](project-phases/phase3.md) — the grounded AI research scope and its limits
- [Phase 4 brief](project-phases/phase4.md) — the dashboard and watchlist scope and its limits
- [Phase 5 brief](project-phases/phase5.md) — running the pipeline from the dashboard
- [Phase 6 brief](project-phases/phase6.md) — on-demand deep research and its boundaries
- [Phase 7 brief](project-phases/phase7.md) — international coverage, currency safety and its limits
