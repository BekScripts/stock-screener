---
type: reference
title: CompounderScore
---

# CompounderScore

The production scoring rules, in full. This page and
`packages/domain/src/domain/scoring.py` describe the same formula; a change to
one without the other is a bug.

**Current version: `COMPOUNDER_V1_2`.** Every formula, curve, weight and
threshold is V1.1's — see [what changed in V1.2](#what-changed-in-v12) — which
in turn is V1's plus three guards, see
[what changed in V1.1](#what-changed-in-v11). Snapshots scored under an earlier
version remain in the database and are never recomputed; no comparison crosses
versions.

```text
Growth                35  ┐
Financial Quality     25  │  raw score, 0-100
Valuation             25  │
Market Confirmation   15  ┘
                          → risk penalties, 0 to -25
                          → final score, clamped 0-100
```

Every rule is a deterministic function of the metrics in
[Metrics](metrics.md). There is no model, no fitting, no peer percentile and no
hidden normalisation: a company that scores 82 can be handed the breakdown and
told which metrics bought which points.

## Conventions

| Convention | Meaning |
| --- | --- |
| Decimal proportions | `0.35` is 35%. Nothing is stored pre-multiplied. |
| Percentage points | A difference between two rates. `0.15` is +15pp. |
| Interpolation | Between two reference points, points move linearly. Outside the ends, the curve is **clamped**. |
| Bands | A step rule, used only where the specification states ranges. |
| Points | Rounded to two decimals when stored. |

Clamping is what caps a 5,000% grower at the same points as a 75% one. The raw
metric is never altered — only its contribution to the score.

## Growth — 35 points

Two guards apply to this component before it is assembled; both are described
under [what changed in V1.1](#what-changed-in-v11).

| Metric | Points | Curve |
| --- | --- | --- |
| `revenue_growth_yoy` | 12 | ≤0% → 0, 10% → 3, 20% → 6, 30% → 8, 50% → 10, ≥75% → 12 |
| `revenue_growth_acceleration` | 8 | ≤-20pp → 0, -10pp → 1, 0pp → 3, +10pp → 5, +20pp → 7, ≥+30pp → 8 |
| `revenue_cagr_3y` | 6 | ≤0% → 0, 5% → 1, 10% → 2, 15% → 3, 25% → 5, ≥35% → 6 |
| `gross_profit_growth_yoy` | 5 | ≤0% → 0, 10% → 1, 20% → 2, 30% → 3, 50% → 4, ≥75% → 5 |
| Growth persistence | 4 | One point per positive quarter among the latest four year-over-year observations |

**Required:** `revenue_growth_yoy`. **Minimum coverage:** 50%.

Persistence needs four comparable observations; with fewer it is unavailable
rather than counted as negative. A quarter of exactly 0% growth is not positive.

## Financial Quality — 25 points

| Metric | Points | Rule |
| --- | --- | --- |
| Gross margin level and trend | 7 | Level as bands, plus a trend adjustment of -1 to +1, clamped to 0-7 |
| FCF margin | 7 | ≤-30% → 0, -20% → 1, -10% → 2, 0% → 3, 5% → 4, 10% → 5, 15% → 6, ≥25% → 7 |
| Cash against debt | 6 | `net_cash / market_cap`: ≤-75% → 0, -50% → 1, -25% → 2, -10% → 3, 0% → 4, +10% → 5, ≥+20% → 6 |
| Operating margin improvement | 5 | ≤-10pp → 0, -5pp → 1, 0pp → 2, +3pp → 3, +5pp → 4, ≥+10pp → 5 |

**Required:** none. **Minimum coverage:** 50%.

Gross margin bands:

| Gross margin | Base points |
| --- | --- |
| < 10% | 0 |
| 10-20% | 1 |
| 20-30% | 2 |
| 30-40% | 3 |
| 40-50% | 4 |
| 50-65% | 5 |
| ≥ 65% | 6 |

The trend adjustment interpolates `gross_margin_change` from -3pp → -1 through
0pp → 0 to +3pp → +1. When no comparable year-ago margin exists, the level scores
alone and no adjustment is applied — absence moves the score neither way.

A company with a negative FCF margin whose margin improved by at least 5pp
year-over-year earns +1, capped at 7. A profitable company gets no such bonus;
its level already reflects the outcome.

Cash against debt is scaled by market capitalisation, never measured in dollars.
Ford's $132bn of net debt is its financing arm, not distress, and an absolute
threshold would rank companies by size.

## Valuation — 25 points

| Sub-score | Points | Rule |
| --- | --- | --- |
| Primary multiple | 15 | ≤1x → 15, 2x → 13, 3x → 11, 5x → 8, 8x → 5, 12x → 2, ≥20x → 0 |
| Growth-adjusted valuation | 7 | The table below |
| FCF yield | 3 | ≤0% → 0, 2% → 1, 5% → 2, ≥8% → 3 |

**Required:** the primary multiple. **Minimum coverage:** 50%.

### Which multiple

```text
enterprise_value = market_cap + debt - cash
```

Market capitalisation itself has two possible sources, and the one used travels
with the score as `market_cap_source`:

| Source | Meaning |
| --- | --- |
| `PROVIDER` | Supplied by the fundamentals provider. |
| `CALCULATED` | Latest close × cover-page common shares outstanding from the most recent filing within a year. |
| `UNKNOWN` | Neither — the company cannot be screened. |

A calculated figure is screened and scored on exactly like a provider's. It is
blind to share classes the ticker does not represent and to issuance since the
last filing, so where both exist they are compared: a gap of more than 25% raises
`MARKET_CAP_DISCREPANCY`. Neither figure is adjusted towards the other and they
are never averaged.

| Condition | Basis used |
| --- | --- |
| Market cap, cash and debt all known, TTM revenue > 0 | `EV_TO_REVENUE` |
| Debt or cash unknown, market cap known, TTM revenue > 0 | `PRICE_TO_SALES` |
| TTM revenue unknown or ≤ 0 | `NOT_AVAILABLE` — the component cannot be scored |

Absent debt is never read as zero, so enterprise value is `None` whenever debt or
cash is. The basis travels with the score and is exposed in the breakdown, the
CSV and the API: price-to-sales and EV/revenue are not the same measure.

A negative enterprise value — a company valued below its net cash — falls in the
cheapest band and scores 15.

### Growth-adjusted valuation

A table, not a ratio. A ratio of multiple to growth divides by a number that
approaches zero and produces a figure nobody can interpret.

| Revenue growth ⟍ multiple | ≥20x | 10-20x | 5-10x | 2-5x | <2x |
| --- | --- | --- | --- | --- | --- |
| ≥ 40% | 1 | 3 | 5 | 7 | 7 |
| 25-40% | 1 | 2 | 4 | 6 | 7 |
| 10-25% | 0 | 1 | 3 | 5 | 6 |
| < 10% | 0 | 0 | 1 | 3 | 5 |

Growth is `revenue_growth_yoy`; the multiple is whichever basis was selected.
100% growth at 25x sales earns 1 point of 7, not 7.

### FCF yield

`ttm_free_cash_flow / market_cap`. A negative trailing free cash flow scores 0 —
that is a known fact about the company. An **unavailable** free cash flow scores
nothing at all and has its weight redistributed.

## Market Confirmation — 15 points

| Sub-score | Points | Curve |
| --- | --- | --- |
| 6-month relative strength | 6 | ≤-30pp → 0, -15pp → 1, 0pp → 3, +15pp → 5, ≥+30pp → 6 |
| 12-month relative strength | 6 | The same curve |
| 52-week position | 3 | ≤-50% → 0, -35% → 1, -20% → 2, -10% → 2.5, ≥-5% → 3 |

**Required:** none. **Minimum coverage:** 50%.

```text
relative_strength_6m  = company return_6m  - benchmark return_6m
relative_strength_12m = company return_12m - benchmark return_12m
```

The benchmark is [`BENCHMARK_SYMBOL`](configuration.md), `SPY` by default, stored
in its own table and fetched through the same market-data provider as every other
price. Without a benchmark series, relative strength cannot be calculated and
every company is `INSUFFICIENT_DATA`.

Momentum is 15 of 100 deliberately. Strong fundamentals with a weak price may be
the opportunity rather than the warning.

## Missing data

Three rules, in order of application.

**A missing metric is neither zero nor full marks.** It produces a sub-score with
`points = None`, visible in the breakdown and named in `missing_metrics`.

**Weight is redistributed inside the component only.** The available metrics
carry the component's full weight in proportion to their own:

```text
component score = points earned × (component maximum / available weight)
```

A growth component missing its 6-point CAGR scores what it earned out of 29 and
scales by 35/29 = 1.207 — above the cap, so 1.15 is used instead. A component
missing 13% of its weight or less is still fully compensated. Absence therefore
neither rewards nor punishes; beyond the cap it leaves a mark, and
`data_coverage` says why. Unavailable valuation weight is never carried by
growth, and each component's maximum is fixed whatever was available.

**A component that knows too little does not score.** If a required metric is
missing, or less than 50% of the component's weight is available, the component
is `INSUFFICIENT_DATA` — and so is the company, which does not enter the ranking.

| Field | When missing |
| --- | --- |
| `debt` or `cash` | Enterprise value is `None`; valuation falls back to price-to-sales. Cash-against-debt and the leverage penalty are unassessed. |
| `revenue_cagr_3y` | Growth scores on its other metrics, rescaled. Young companies are not penalised for having no three-year history. |
| `gross_profit_growth_yoy` | Its 5 points are carried by the other growth metrics. Never derived from anything else. |
| `share_count_growth_yoy` | No dilution penalty and no dilution credit. `DILUTION_NOT_ASSESSED`, and risk coverage falls. |
| `ttm_free_cash_flow` | FCF yield unavailable; cash runway unassessed. |
| Benchmark returns | Market confirmation cannot reach coverage, so the company is `INSUFFICIENT_DATA`. |

## Data coverage

```text
data_coverage = available sub-score weight / 100
```

Reported beside the score, never folded into it: incomplete data should be
visible, not silently punished. A final score of 82 at 91% coverage is a
different claim from 82 at 100%, and the reader is the one who should decide what
to do about it.

`risk_coverage` is the equivalent for the three assessable risks.

## Risk penalties

Applied after the raw score, never inside a component.

| Penalty | Range | Rule |
| --- | --- | --- |
| Dilution | 0 to -10 | `share_count_growth_yoy`: ≤2% → 0, 5% → -1, 10% → -3, 20% → -6, ≥35% → -10 |
| Cash runway | 0 to -10 | Months of cash: >24 → 0, 24 → -1, 18 → -3, 12 → -6, ≤6 → -10 |
| Balance sheet | 0 to -5 | `net_debt / market_cap`: ≤25% → 0, 50% → -2, 75% → -4, ≥100% → -5 |
| Liquidity | 0 | Not priced in v1 — see below |

```text
cash_runway_months = cash / (-ttm_free_cash_flow / 12)
total_penalty      = max(sum of penalties, -25)
```

Runway applies only to companies burning cash. A company with positive trailing
free cash flow has no finite runway, so its penalty is 0 and the risk counts as
assessed — not as unknown.

Only severe leverage is penalised here, because ordinary balance-sheet quality is
already scored in the quality component; charging for it twice would rank
capital-intensive industries by their industry.

Unverified liquidity carries `LIQUIDITY_UNVERIFIED` as a warning and no penalty.
It is a statement about the data feed, not about the company — see
[ADR-0004](../adr/0004-apply-the-liquidity-threshold-only-to-consolidated-volume.md).

### Risk level

| Total penalty | Level |
| --- | --- |
| 0 to -3 | `LOW` |
| -3.01 to -8 | `MEDIUM` |
| -8.01 to -15 | `HIGH` |
| below -15 | `VERY_HIGH` |

## Final score

```text
raw_score   = growth + quality + valuation + momentum      (0-100)
final_score = clamp(raw_score + total_penalty, 0, 100)
```

Both are stored. The final number is never shown without the raw score and the
penalty beside it.

| Final score | Category |
| --- | --- |
| 85-100 | `EXCEPTIONAL_RESEARCH_CANDIDATE` |
| 75-84.99 | `STRONG_RESEARCH_CANDIDATE` |
| 65-74.99 | `WORTH_WATCHING` |
| 50-64.99 | `MIXED` |
| below 50 | `LOW_PRIORITY` |

These rank research priority. They are not buy or sell recommendations.

## Scoring status

| Status | Means |
| --- | --- |
| `SCORED` | Every component scored; `final_score` is a number. |
| `INSUFFICIENT_DATA` | A component fell below its coverage minimum. |
| `UNSUPPORTED_SECTOR` | The model's economics do not apply — see below. |
| `NOT_ELIGIBLE` | The security failed the Phase 1 screen; nothing was calculated. |
| `ERROR` | Scoring raised. Recorded so one broken company is visible without ending the run. |

Only `SCORED` companies enter a ranking. Every other status still gets a stored
row, which is what makes "why is this not in the ranking?" answerable.

## Unsupported sectors

Banks, insurers, reinsurers, lenders, mortgage businesses, capital markets firms,
asset managers and shells are marked `UNSUPPORTED_SECTOR`. For a bank, deposits
are the business rather than leverage, and EV/revenue is not a multiple the
sector is priced on.

A company in the financial sector whose industry is unknown is also excluded:
within financials the industry is the only thing separating a payments company
from a lender.

### When the label is wrong

The rule above reads the provider's label. A label can be wrong, and it is wrong
in the direction that matters: Kaspi.kz is a deposit-funded bank that one vendor
classifies as `Software - Infrastructure` and the SEC's own SIC list as
`Business Services`. On those two opinions it scored 74.78 and ranked
twenty-first — gross margin computed without interest expense, and no leverage
penalty at all, because a bank's funding carries no `Borrowings` tag and its debt
therefore read as unknown.

So a second gate reads the **statements** rather than the label. A filer is
`UNSUPPORTED_SECTOR` when its XBRL concepts show deposit funding *and* at least
two of: a loan book, banking interest revenue, central-bank balances, or a
loan-loss allowance specific to lending. Both taxonomies are covered — IFRS
`DepositsFromCustomers` and US-GAAP `Deposits` reach the same conclusion.

Two signals rather than one, because deposit funding alone is not decisive: a
lithium miner tags customer prepayments as `DepositsFromCustomers`, and a
pharmaceutical company tags the cash it holds at banks as `DepositsFromBanks`.
Neither is a bank, and neither clears two. The threshold was measured against
eighteen banks and seventy-eight operating companies; see
`domain.statements` for the tags this deliberately refuses to use and why.

This is a **supplement**, not a replacement. A bank whose label is right is still
caught by its label, and two banks in the reference set tag too sparsely to be
classified from their statements at all. Missing one that the label gate catches
costs nothing; classifying a miner as a bank costs a candidate.

Neither gate changes any score. The scoring formula is unchanged — every company
that scores under it scores exactly what it scored before. What changed is which
companies are admitted, and admitting a bank was never the policy.

They stay in the universe and keep their metrics, their reporting currency and
their market capitalisation, so deep research can still read them. No
bank-specific model exists yet.

## What changed in V1.1

Three guards, no change to any weight, curve, threshold or risk penalty. Each was
added because a measurement over 1,005 real companies showed V1 rewarding
something it did not intend.

### A1 — redistribution cap

```text
scale factor = min(component maximum / available weight, 1.15)
```

*Why.* A fifth of scored companies gained more than five points from
redistribution, the average gain inside the top fifty was 5.6 points, and
removing redistribution entirely would have replaced sixteen of those fifty.
Companies missing the same two metrics — a filer that tags no cost of revenue
loses both `gross_margin` and `gross_profit_growth` — were systematically lifted.

### B3 — cyclical rebound guard

```text
if revenue_growth_yoy >= 30% and revenue_cagr_3y <= 5%:
    acceleration points = min(acceleration points, 3.0)
```

*Why.* Companies with fast year-over-year growth on a flat or negative three-year
trend were 4.1% of the population and 22% of the Top 50 — a 5.4× concentration —
and outscored genuinely durable growers by 2.9 points on average. The growth
*level* is real and keeps its points; the acceleration bonus is withdrawn,
because for a recovery from a trough that acceleration is an artifact of the
depressed base. The cap is the curve's own zero-acceleration reference, so a
rebound scores as though its growth rate were steady.

**An unavailable CAGR does not trigger it.** An unknown trend is not a flat one.

*Known limitation.* This leaves a blind spot: a company with fewer than sixteen
quarters of history has no three-year CAGR, so its growth cannot be tested for
durability at all. 70 of 1,005 scored companies are in that position and six of
them sit in the Top 50 — ECG at #20 is the clearest example, earning full credit
for 33.7% growth with no trend to check it against. Deliberately left alone: the
alternatives are to penalise a young company for being young, or to invent a
durability signal from data that does not exist.

### C2 — lumpy growth guard

```text
if persistence <= 2/4 and (revenue_growth + acceleration) > 12:
    scale both proportionally so their sum is 12
```

*Why.* Revenue growth and acceleration are both read from a single quarter
against a single quarter a year earlier, so a milestone payment can earn 20 of
the 35 growth points on one lumpy quarter while persistence — the only sub-score
that can see the lumpiness — carries 4. Companies growing 75%+ with two or fewer
positive quarters out of four scored **11 points higher** on average than
companies growing just as fast every quarter.

**An unavailable persistence does not trigger it.**

### Stacking

The three stack naturally, with **no combined cap**. Only 38 of 1,005 companies
meet more than one, and six meet all three; a cap at 15 points would have changed
exactly one company's score and moved nobody in or out of the Top 50 or Top 100.
A company that is both under-covered and a cyclical rebound has two independent
defects, and discounting the second because the first already applied would be a
concession, not a correction.

Order matters and is fixed: the rebound guard runs first and only touches
acceleration; the lumpy guard then applies to whatever the pair is worth
together; the redistribution cap applies last, when the component is assembled.

## What changed in V1.2

**No number moved.** V1.2 preserves all V1.1 scoring formulas and curves. It
excludes scores based on `STALE` fundamentals from current rankings while
retaining those scores for stock detail, research and historical analysis.

A company whose fundamentals are current scores exactly what it scored under
V1.1, component for component. That is asserted rather than claimed: the golden
value in `test_a_current_company_scores_exactly_what_v1_1_scored` was measured by
running one company through the last V1.1 commit and through V1.2 and comparing.

### Why a version, if nothing moved

Because it changes what a ranking *is*, so rankings from before and after are not
comparable — which is exactly what a version identifier exists to record.

Centerra Gold ranked twenty-eighth on revenue from 2023 measured against a market
capitalisation from 2026. Every figure in that score was correct; the score was
not an answer to "what looks interesting now". A mixed-vintage valuation is wrong
in the same way a mixed-currency one is, and the fix is the same: refuse the
comparison rather than present it.

### What stale does and does not do

| | Stale score |
| --- | --- |
| Numerical CompounderScore | **kept** |
| Stock detail page | **shown**, with a notice saying why it is not ranked |
| Research and Deep Research | **available** |
| Historical snapshots | **untouched** |
| Current rankings — all four views | **excluded** |

Freshness is recorded on the snapshot as `CURRENT` or `STALE`, not derived by
whoever reads it. The staleness bound scales with reporting cadence — an annual
filer is not stale eight months after its year end — so a reader comparing a date
against a fixed window would disagree with the screen that produced the row, and
two readers would disagree with each other. `domain.is_rank_eligible` is the
single place the question is answered.

Rows written before V1.2 have no freshness recorded. They read as `CURRENT`,
which is what they meant: they were ranked under rules where freshness had no
bearing on ranking, and reading them as stale now would rewrite history.

## Score version

Every snapshot records the version it was scored under — `COMPOUNDER_V1_2`
today, `COMPOUNDER_V1_1` for rows written before the staleness policy, and
`COMPOUNDER_V1` for rows written before the guards. Changing a curve, a weight or
a policy means a new version identifier, not an edit to this one — and score
changes are only ever calculated between snapshots of the same version. See
[ADR-0006](../adr/0006-score-snapshots-are-versioned-and-immutable.md).

A **data-quality fix is not a policy change**: identifying a company an existing
policy already excluded, or correcting an input that was read wrongly, changes no
company's number and needs no new version. The test is simply whether any score
moves.

## Two passes, one formula

A ranking is produced in two passes, because the data behind it comes at two
very different prices.

| Pass | Data | Cost | Produces |
| --- | --- | --- | --- |
| Broad scan | Alpaca prices, EDGAR filings | Free, whole market | `PRELIMINARY` scores for every company |
| Candidate enrichment | The metered provider, top N only | One request per candidate | `FINAL` scores for those companies |

The formula is identical in both. Enrichment replaces **inputs** — a calculated
market cap with the vendor's, single-exchange volume with consolidated volume, a
SIC description with a sector — and the same code path scores them again. No
points are awarded for a provider having answered.

`ranking_state` on every snapshot says which pass a row came from:

| State | Means |
| --- | --- |
| `PRELIMINARY` | A **valid ranking** computed from Alpaca prices and EDGAR filings. Market capitalisation may be calculated rather than quoted, and liquidity is unverified because single-exchange volume cannot be compared with a whole-market threshold. |
| `FINAL` | Enrichment succeeded for this company: a vendor market cap was obtained and cross-checked, and eligibility was re-evaluated against **consolidated** average dollar volume. |

**Enrichment is optional.** The broad scan does not need it, and a metered
provider that is unavailable, out of quota, or missing its key must never stop
the market being scanned, scored or ranked. A `429` ends the enrichment pass
only: companies already enriched keep `FINAL`, the rest stay `PRELIMINARY` and
say so, and the ranking is served either way. A `PRELIMINARY` ranking is the
normal output of this system, not a degraded one.

A company can leave the ranking at the second pass — that is the point of it. A
stock that looked liquid on single-exchange volume, and whose consolidated
average dollar volume turns out to be below the threshold, becomes `NOT_ELIGIBLE`
when re-scored.

## Ranking views

All four are queries over the same snapshots.

| View | Filter |
| --- | --- |
| Top Opportunities | `SCORED`, ordered by final score. Default 50. |
| Hidden Gems | Market cap < $5bn, final score ≥ 70, revenue growth ≥ 20%, risk ≠ `VERY_HIGH` |
| Great Company, Wrong Price | Growth ≥ 28/35, quality ≥ 20/25, valuation ≤ 10/25 |
| Improving Fast | Positive change in final score over the window, largest first |

Ordering is `final_score`, then `raw_score`, then `growth_score`, then
`market_cap`, then ticker — fully deterministic, so two runs over unchanged data
produce the same ranking.

Score changes compare today's snapshot with the **nearest snapshot at or before**
the target date, of the same version. A company with no earlier snapshot has a
change of `None`, not zero. When the only earlier snapshot is older than the
window, it is still the one used: the change is "since the last time we saw it",
which for a young score history is the most that can honestly be said.
