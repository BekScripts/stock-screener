---
type: reference
title: R1 backtest harness
---

# R1 backtest harness

`stock_screener.backtest_harness` is the first thing built on top of the frozen
`PIT_RESEARCH_DATASET_V2`. It takes a monthly decision from what was knowable
at the time, executes it at the next session's open, accounts for the cash and
the shares that result, and evaluates the outcome against the frozen labels.

It is a harness, not a strategy. The strategies it runs are preregistered and
deliberately small, and none of them is tuned.

## The stages, in the one order they may be taken

| Stage | Module | What it settles |
| --- | --- | --- |
| Contract verification | `binding` | Every frozen digest is re-proven, or the run refuses. No `--force`. |
| Decision calendar | `sessions` | The last benchmark session of each usable month, and the session after it. |
| Frozen split | `sessions` | Development, validation and holdout, written before any performance exists. |
| Research universe | `universe` | Who was in the month, read from frozen evidence and never recomputed. |
| Cross-section | `crosssection` | What the decision saw: the frozen score, tradability, and an execution price. |
| Portfolio | `portfolio` | Cash, shares, terminal legs, transaction costs. |
| Evaluation | `performance`, `diagnostics` | The fixed metric set; deciles and rank IC against frozen labels. |
| Record | `registry`, `audit`, `run` | Registry, preregistration, ledger, manifests, audits, completion marker. |

## Bindings

`verify_bindings` re-hashes the research database, the contract, and two frozen
point-in-time files, then loads `BACKTEST_DATA_CONTRACT_V1` through
`load_contract`, which refuses any binding that has moved. The two extra files
the contract does not name are:

| File | Holds |
| --- | --- |
| `a3_inputs_r1/pit_coverage_v2.jsonl` | The pre-A3 coverage of every universe month: the frozen classification verdict, whether the listing was observed, and the acceptance date where evidence already answered the month |
| `post_a3_pit/pit_month_outcomes.jsonl` | What the A3 expansion resolved: every month that had been open, and the acceptance date of the filing that closed it |

## The research universe of a month

A security is in month `M` when the frozen universe holds the identity-month
and either the Phase 9K build already classified it `ELIGIBLE`, or the
point-in-time materialisation records the month as `COVERED` — a common-stock
cover page, credited to that identity by the dated, occupancy-aware join.

One rule is **stronger** here than in the frozen files: coverage was computed
against the calendar month end and admits a filing accepted on it, while a
decision is taken at the close of the month's last *trading* session and an
acceptance date says nothing about the hour. So the establishing filing must
have been accepted **strictly before** the decision session, and what that
removes is counted as `EVIDENCE_NOT_YET_ACCEPTED`.

| Exclusion | Means |
| --- | --- |
| `NON_COMMON_OR_EXCLUDED` | The frozen verdict establishes a unit, warrant, right, note, preferred line or depositary share |
| `CLASS_UNESTABLISHED` | No cover page accepted by the month's end classifies the security — the residual B2.17 froze |
| `EVIDENCE_NOT_YET_ACCEPTED` | Covered, but only by a filing accepted on the decision session or later |
| `LISTING_UNOBSERVED` | The listing record never observed the security on a supported venue that month |

## The cross-section funnel

Every identity the universe admits becomes one row carrying the furthest stage
it reached and, where it stopped, the reason.

| Stage | Reached when |
| --- | --- |
| `UNIVERSE` | In the month's research universe |
| `FEATURE_READY` | A `MARKET_SIGNALS_V1` snapshot was captured on the session |
| `SCORE_READY` | The frozen `COMPOUNDER_V1_2` produced a number from what had been filed |
| `RANKABLE` | `domain.is_rank_eligible` — scored, on fundamentals current for its cadence |
| `TRADABLE` | The frozen signal layer proved a liquidity floor |
| `EXECUTABLE` | The next session has an opening price |

Exclusion reasons are `NO_SIGNAL_SNAPSHOT`, `SAME_DAY_HISTORICAL_FILING`,
`SCORE_<status>`, `STALE_FUNDAMENTALS`, `NOT_TRADABLE_<verdict>` and
`NO_EXECUTION_PRICE`. A missing execution price is recorded, never backfilled.

## Execution and accounting

`CLOSE_SIGNAL_NEXT_SESSION_OPEN`: a target set computed at the close of the
decision session executes at the open of the next, sells before buys, in
identity order. Fractional shares (`FRACTIONAL_SHARES_RESEARCH_V1`), long only,
no leverage, no shorting, nominal capital 100,000, and NAV is checked to equal
cash plus market value at every session.

Results are an **adjusted-basis total return**: the stored bars are the
vendor's fully adjusted series, so splits are exact and distributions are
inside the price rather than modelled as cash.

## `TERMINAL_LEG_POLICY_V1`

| The record says | The portfolio does |
| --- | --- |
| A source states the consideration | Liquidates at that value on the effective date |
| A reported wind-up | Liquidates at zero, on the report and never on a price |
| The tape stops and nothing states a value | Carries at the last observed close, flags it, and reports a floor NAV that values every such leg at nothing |
| A gap that later resumes | Carries at the last close, with no terminal claim |

## Baselines

| Version | Mode |
| --- | --- |
| `BASELINE_PASSIVE_V1` | The frozen benchmark, bought at the first execution open and held. A reference, not a strategy |
| `BASELINE_UNIVERSE_EQUAL_WEIGHT_V1` | Every executable name, equally weighted, rebalanced monthly |
| `COMPOUNDER_DECILES_V1` | A diagnostic: the executable cross-section cut into ten buckets by the frozen score, against the frozen labels |
| `COMPOUNDER_TOP_DECILE_V1` | The top bucket, equally weighted, rebalanced monthly |

`MARKET_SIGNALS_V1` is deferred: it emits multi-label setups, a three-state
tradability verdict and peer percentiles, and defines no canonical standalone
scalar ranking. Inventing one would be a new strategy rather than an evaluation
of a frozen component.

## Holdout discipline

The split is written before the first score is reconstructed. The
preregistration is written and hashed before any performance is evaluated, and
`first_holdout_access` refuses to record an access event unless the
preregistration on disk hashes to what the caller presents — so a retroactive
preregistration cannot be slipped in behind a result that has already been
seen.

## Outputs

Under `<control>/research/r1_0/`, with compact metadata mirrored to
`control/PIT_RESEARCH_DATASET_V2/research/r1-0/`:

| File | Holds |
| --- | --- |
| `research_split_v1.json` | The frozen split and every decision date |
| `baseline_registry.json` | The strategies, written before any of them ran |
| `r1_0_preregistration.json` | Definitions, parameters, execution, costs, metrics, boundaries |
| `holdout_access.json` | The first holdout evaluation and the hash that gated it |
| `cross_sections.jsonl` | One row per identity per decision date |
| `monthly_coverage.json` | The funnel, per decision date |
| `compounder_deciles.jsonl`, `signal_diagnostics_compounder_v1.json` | The decile statistics, the rank IC and the score distribution |
| `run_<STRATEGY>/` | `run_manifest.json`, `metrics.json`, `nav.jsonl`, and for portfolios `trades.jsonl` and `holdings.jsonl` |
| `research_ledger.jsonl` | Every run, attractive or not |
| `spot_audit.json`, `bias_audit.json` | A hash-chosen re-derivation, and the post-hoc composition audit |
| `r1_0_summary.json`, `r1_0_fingerprints.json`, `r1_0_complete.json` | The summary, the semantic digests and the completion marker |

## Limitations every result is read under

Sector history is unavailable and no result is attributed to one; listing
membership is partly inferred; market capitalisation is a lower bound where a
share count is a cover-page figure; ADV is single-venue and reported rather
than imposed; historical-security fundamentals carry a conservative filed-date
lag; there is no historical FX series, so currency-sensitive ratios are
unavailable for foreign filers at every historical decision date; dividends are
inside the adjusted basis rather than modelled as cash; spinoffs are
unsupported; an unknown terminal value is reported beside a floor; there is no
intraday model; and the risk-free rate is zero because the dataset holds no
risk-free series.

## The first live run (R1.0)

`backtest run-baselines --database stage9k.db --verify-fingerprints --runs 2
--workers 8 --mirror`, with all eleven `--expect-*` digests supplied. The
fourteen dataset projections were recomputed and matched the contract; the
contract accepted the run; the phase executed twice and every semantic
fingerprint was identical.

### The calendar and the split

| | |
| --- | --- |
| Decision dates | 51 — the last benchmark session of each month, 2022-01-31 … 2026-03-31 |
| Valuation window | 2022-02-01 … 2026-04-30, 1,065 sessions |
| Development | 2022-01 … 2023-12, 24 decisions |
| Validation | 2024-01 … 2024-12, 12 decisions |
| Holdout | 2025-01 … 2026-03, 15 decisions |
| Split digest | `b58283be55428b1a…` |
| Preregistration digest | `8a19f0fdc91f92fa…` |

The holdout ends at 2026-03 because point-in-time features were captured for
**both** identity classes only through that month; running to the universe's
last month (2026-07) would have dropped every historical security from the
tradable set half way through, which is survivorship arriving as a pipeline
horizon. The boundary was set from readiness, before any performance existed.

### The funnel

| Stage | Observations |
| --- | --- |
| Frozen universe rows in those 51 months | 250,537 |
| Admitted by the evidence | 164,193 (157,526 COMPANY, 6,667 HISTORICAL) |
| Feature-ready | 163,850 |
| Score-ready | 74,829 |
| Rankable | 74,193 |
| Tradable | 39,003 |
| Executable | 39,001 (38,609 COMPANY, 392 HISTORICAL) |

Universe exclusions: 80,791 `CLASS_UNESTABLISHED`, 2,685
`EVIDENCE_NOT_YET_ACCEPTED`, 1,890 `LISTING_UNOBSERVED`, 978
`NON_COMMON_OR_EXCLUDED`. Cross-section exclusions: 48,098 `SCORE_NOT_ELIGIBLE`,
32,850 `NOT_TRADABLE_UNVERIFIED`, 24,977 `SCORE_INSUFFICIENT_DATA`, 15,946
`SCORE_UNSUPPORTED_SECTOR`, 2,340 `NOT_TRADABLE_UNTRADABLE`, 636
`STALE_FUNDAMENTALS`, 259 `NO_SIGNAL_SNAPSHOT`, 84
`SAME_DAY_HISTORICAL_FILING`, 2 `NO_EXECUTION_PRICE`.

### Results, gross, over the whole window

| | Total | CAGR | Vol | Sharpe | Max DD | Turnover (annual) |
| --- | --- | --- | --- | --- | --- | --- |
| `BASELINE_PASSIVE_V1` | +68.2% | 13.10% | 17.5% | 0.79 | −22.1% | 0 |
| `BASELINE_UNIVERSE_EQUAL_WEIGHT_V1` | +43.5% | 8.94% | 20.7% | 0.52 | −24.0% | 1.38× |
| `COMPOUNDER_TOP_DECILE_V1` | +117.7% | 20.24% | 23.0% | 0.92 | −25.8% | 3.90× |

Under `SIMPLE_10BPS_PER_SIDE_V1` the equal-weight baseline returns +42.0%
(CAGR 8.66%) and the top-decile portfolio +110.9% (CAGR 19.34%).

### The decile diagnostic

39,001 observations over 51 monthly cross-sections, all of them cut. At the
preregistered 20-session horizon, mean realised forward return rises from
+0.56% in D1 to +1.31% in D10, with mean score rising monotonically 25.4 → 67.1.
`D10 − D1` is +0.75 percentage points pooled, and +0.84 pp as the mean of 51
monthly contrasts, standard error 0.51 pp, t = 1.66, bootstrap CI
[−0.15 pp, +1.79 pp], positive in 58.8% of months. Mean monthly rank IC is
+0.025 (t = 1.76, CI [−0.003, +0.053], positive in 62.7% of months), weakest in
the holdout at +0.011.

### What the accounting says

No accounting violation, no cash violation, no empty cross-section, and no
selected security without an execution price. 33 terminal legs were resolved by
identity — 30 settled at a stated consideration, 3 unknown. Valuing every
unknown leg at nothing moves the equal-weight total return by 0.19 pp and the
top-decile portfolio by nothing at all.
