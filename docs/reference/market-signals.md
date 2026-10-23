---
type: reference
title: Market signals
---

# Market signals

A descriptive layer over the point-in-time feature record. It answers *what is
this security doing* — how much it moves, whether it trends, where it sits
against its peers, and whether it can be traded at all.

## What it is not

Stating this first because every one of these was considered and rejected on
evidence, not taste.

| It is not | Why |
| --- | --- |
| CompounderScore | That answers which *businesses* look attractive. This answers what the *market* is doing. The two are near-orthogonal — Phase 8A measured Spearman 0.067 between the score and average true range. |
| An OpportunityScore | Its dimensions are anti-correlated. Volatility against distance-from-52-week-high measured Spearman −0.58, with two names shared between their top hundreds. An average of them ranks highest whatever is mediocre at everything. |
| A blend of the two | A 50/50 percentile blend retained one name of the fundamental top twenty. Blending does not combine the signals; it destroys both. |
| A ranking | There is no "best" security here. `for_date` returns rows by ticker for exactly that reason. |
| A trading strategy | No entry, no stop, no size, no target. Those belong to a strategy layer that does not exist yet. |
| A claim of profitability | Nothing here has been fitted to returns, and nothing could be — see [Provisional thresholds](#provisional-thresholds). |

## What it produces

Three kinds of output per company per day, stored in `pit_signal_snapshots`.

### Derived facts

| Field | Meaning | Unit |
| --- | --- | --- |
| `compression_ratio` | 20-session average true range over the 60-session one. Below 1 means the recent range is tighter than the quarter's baseline. | ratio |
| `volatility_expansion_ratio` | 20-session realised volatility over the 60-session one, carried from the features. | ratio |
| `distance_from_52w_high` | Close against the trailing-year intraday high. Negative below it. | fraction |
| `distance_from_52w_low` | Close against the trailing-year intraday low. Derived here from two stored values. | fraction |
| `stretch_20d` | The 20-session return in standard deviations of the security's own 20-session volatility. −1 is a one-sigma fall for *this* security. | sigma |
| `trend_direction` | Sign of the fitted log-price slope: `UP`, `DOWN`, `FLAT`, `UNKNOWN`. | — |
| `trend_r2` | Quality of that fit. Near zero is directionless noise. | 0–1 |

### Peer percentiles

Because a 3% average true range means different things in different parts of the
market.

**The peer population is every company with a feature snapshot on that date
under that feature version.** Not a sector — sector is missing for 89% of
companies, so a sector-relative percentile would describe coverage rather than
the market. Not the rank-eligible set either, because eligibility is a
fundamental judgement and this layer is independent of it.

- Cross-sectional: one date, never pooled across days.
- Mid-rank convention — the fraction strictly below plus half the fraction
  equal — so ties share a percentile and the result does not depend on input
  order.
- A missing value has no percentile. It is never imputed and never read as zero.
- Below **30** peers with a non-null value, the percentile is `None`. A
  percentile over eight names describes the sample, not the security.
- `peer_population` is stored on every row, because a percentile without its
  population is not a percentile.

Ranked features: `atr_pct_14`, `rvol_60_pct`, `avg_abs_ret_252_pct`,
`ret_20d_pct`, `ret_60d_pct`, `rel_strength_60_pct`, `trend_r2`,
`distance_from_52w_high`, `venue_dollar_volume_median`.

### Classifications

**Setups are multi-label.** A security can be compressing inside an uptrend, and
collapsing that to one winner discards the more informative half. `NONE` is
valid and common — most securities are doing nothing describable on most days.

| Setup | Fires when |
| --- | --- |
| `MOMENTUM` | 60-day return positive and ahead of the benchmark, trend R² ≥ 0.50, ATR ≥ 1.5%, above the 20-day mean on ≥ 60% of the last 60 sessions, within 10% of the 52-week high |
| `MEAN_REVERSION` | 20-day return ≤ −1 sigma of its own volatility, while the 252-day return is still positive |
| `COMPRESSION` | 20-session ATR ≤ 0.70 × the 60-session ATR |
| `NONE` | none of the above |

A missing input never counts as a pass. An unknown trend fit is not a good one.

## Tradability

Three states, never a boolean.

| Verdict | Means |
| --- | --- |
| `TRADABLE` | Positive evidence: activity is real and a liquidity floor is proven. |
| `UNTRADABLE` | Positive evidence the other way — a tape that is substantially dead. |
| `UNVERIFIED` | Not enough evidence either way. **Never a synonym for bad.** |

Evidence of a dead tape outranks everything else, because the alternative was
falsified. HBB carried an unremarkable average true range of 3.91% while
printing `high == low` on **59.9%** of sessions — the range was assembled almost
entirely from overnight gaps. So the structural activity measures are
first-class and are checked first:

- `flat_bar_share` ≥ 20%
- `zero_move_share` ≥ 20%
- `median_range_pct` ≤ 0.10%
- `closing_band_pct` ≤ 5%

### The liquidity rule

Stored volume comes from a **single venue** and is a strict subset of the
consolidated tape — verified across 546 paired companies with zero violations,
at ratios between 0.0% and 17.15%.

That asymmetry is the whole rule:

- Clearing the floor **proves** at least that much trades. Positive evidence.
- Failing the floor **proves nothing**. The consolidated figure could be far
  higher. It is therefore never grounds for `UNTRADABLE`; the verdict stays
  `UNVERIFIED`.

No IEX-to-consolidated ratio is assumed anywhere, because Phase 8A measured that
ratio varying 17–36× across the deciles where the boundary sits.

## Provisional thresholds

**Every threshold in this layer is provisional and none has been fitted to
returns.**

They come from the Phase 8A exploratory buckets, which that report labelled
"ANALYTICAL BUCKETS, NOT PRODUCTION RULES" and which were chosen to be
conventional rather than optimised. They exist to make a market state
*nameable*, not to assert it is profitable.

Calibration is impossible today: the point-in-time record holds a single day.
Fitting against it would produce numbers that look validated and are not — Phase
8A measured what that costs, with point-in-time recomputation cutting one
feature's apparent information coefficient from +0.362 to −0.009.

Thresholds therefore live in `SignalThresholds`, a parameter object, so a future
phase with real history calibrates by constructing a different instance under a
new version — with no classification code changing.

## Versioning

`MARKET_SIGNALS_V1` is current. Every stored row carries it, **and** the
`feature_version` it was derived from, because a signal is only meaningful
against the features it read.

Changing a threshold, a rule, the peer policy or the percentile method means a
**new version**, never an edit. Re-deriving a day under new rules appends beside
the old row; neither restates the other. See
[ADR-0012](../adr/0012-market-signals-are-stored-not-recomputed.md).

## Point-in-time guarantees

The derivation reads `pit_feature_snapshots` **and nothing else**. The
operational tables that get rewritten as better information arrives —
`financial_snapshots`, `score_snapshots`, `companies` — are not consulted, so a
signal for a past date cannot drift when they change. That is structural rather
than careful, and there is a test that rewrites the score for a captured day and
asserts the stored signal is unmoved.

`feature_as_of` travels onto the signal row, so a security whose newest bar is
months old is visibly stale rather than silently current.

## Known limitations

- **One day of history.** No calibration, no backtest, no predictive claim.
- **No consolidated volume series.** Liquidity is a proven floor or unknown.
- **No intraday data, spreads, depth or execution data.** Execution quality is
  not measurable here at all.
- **No sector-relative percentiles.** Sector is 89% missing and will not be
  manufactured.
- **Some Phase 8A rules could not be reproduced exactly.** The stored feature
  set carries no moving-average stack, no 60-day Donchian channel and no
  5-session ATR, so three conditions use documented stand-ins. See the Phase 8C
  report for the substitutions.

## Commands

| Command | Does |
| --- | --- |
| `derive-signals` | Derives and stores one day's signals from the captured features. `--date` selects the day; it defaults to the newest day with features. Re-running the same day and version writes nothing. |
