---
type: reference
title: Phase 2 brief
---

# Compounder Radar — Phase 2 Implementation Prompt: Scoring & Rankings

You are implementing **Phase 2 of Compounder Radar**.

Phase 1 is complete and frozen at commit:

```text
c7f5ecc
```

Do not redesign or rewrite Phase 1 unless Phase 2 exposes a genuine correctness defect.

Before making any changes:

1. Read the master MVP specification.
2. Read all Phase 1 documentation and ADRs.
3. Read `phase2-constraints.md`.
4. Inspect the current repository.
5. Understand the existing domain models, provider boundaries, metrics, CLI, database, and tests.
6. Confirm the working branch starts from the completed Phase 1 commit.

The purpose of Phase 2 is to turn the normalized metrics from Phase 1 into a **simple, transparent stock-ranking engine**.

The core question is:

> Which companies deserve deeper research based on growth, financial quality, valuation, market confirmation, and identifiable risks?

Do **not** implement AI research or the frontend yet.

---

# 1. Phase 2 Scope

Implement:

```text
Growth Score              35 points

Financial Quality Score   25 points

Valuation Score           25 points

Market Confirmation       15 points

────────────────────────────────

Raw Compounder Score     100 points

↓

Risk Penalties

↓

Final Compounder Score
```

Also implement:

* score snapshots;
* ranking generation;
* benchmark-relative strength;
* risk penalties;
* score explanations;
* scoring eligibility/status;
* Top Opportunities;
* Hidden Gems;
* Great Company, Wrong Price;
* Improving Fast;
* CLI commands;
* minimal API additions;
* comprehensive scoring tests;
* ranking validation tooling.

Do not implement:

```text
AI research
LLM analysis
news analysis
SEC narrative analysis
portfolio construction
trade execution
position sizing
Next.js dashboard
complex backtesting
machine-learning ranking
```

---

# 2. Keep the Existing Multi-Source Architecture

Phase 1 intentionally uses:

```text
Alpaca
+
FMP
+
SEC EDGAR
```

The scoring engine must consume **normalized internal metrics only**.

It must not know whether:

```text
revenue
cash
debt
shares
```

came from FMP or EDGAR.

Bad:

```python
if fmp.revenue_growth > ...
```

Correct:

```python
metrics.revenue_growth_yoy
```

Provider-specific logic stays outside scoring.

---

# 3. Existing Source Responsibilities

Preserve approximately:

```text
ALPACA

price
OHLCV
returns
52-week range
benchmark prices


FMP

market cap
sector
industry
consolidated volume
security/profile metadata
available fundamentals


SEC EDGAR

reported revenue
gross profit
operating income
cash flow
capex
cash
debt when available
shares
financial history
```

Do not make Phase 2 dependent on paid FMP statement endpoints.

---

# 4. Scoring Philosophy

The scoring system must remain understandable.

If a company scores:

```text
82 / 100
```

I should be able to determine exactly why.

Avoid:

* black-box formulas;
* ML;
* hidden normalization;
* large statistical models;
* excessive percentile systems;
* hundreds of factors.

Version 1 should use explicit piecewise deterministic rules.

---

# 5. Score Structure

Implement exactly four positive components:

```text
Growth                  35

Financial Quality       25

Valuation               25

Market Confirmation     15
```

Maximum raw score:

```text
100
```

Then apply risk penalties separately.

Do not hide risk inside these four component scores.

Store and expose:

```text
growth_score
quality_score
valuation_score
momentum_score

raw_score

risk_penalty

final_score
```

---

# 6. Score Bounds

Every component must remain inside its intended range.

Examples:

```text
0 <= growth_score <= 35

0 <= quality_score <= 25

0 <= valuation_score <= 25

0 <= momentum_score <= 15

0 <= raw_score <= 100
```

Risk penalty:

```text
0 to negative value
```

Final score should generally be clamped:

```text
0–100
```

unless there is a strong reason not to.

---

# 7. Missing Data Is NOT Zero

This is a critical Phase 2 rule.

Do not interpret:

```text
debt = None
```

as:

```text
debt = 0
```

Do not interpret:

```text
dilution = None
```

as:

```text
dilution = 0%
```

Do not automatically assign zero points to every unavailable optional metric.

Similarly, unavailable information must not automatically receive full points.

Implement an explicit scoring-data policy.

---

# 8. Recommended Missing-Data Policy

Each score component should define:

```text
required metrics

optional metrics

minimum data coverage
```

A company may only receive a production component score if minimum coverage is satisfied.

If minimum coverage is not satisfied:

```text
component_status = INSUFFICIENT_DATA
```

If an entire essential component cannot be scored:

```text
scoring_status = INSUFFICIENT_DATA
```

and the company should not appear in normal Top Opportunities rankings.

Do not create fake completeness by replacing unavailable values.

---

# 9. Optional Metric Weight Redistribution

When minimum component coverage is satisfied but an optional metric is unavailable:

redistribute that metric's weight **only among closely related available metrics inside that same component**.

Do not redistribute:

```text
missing valuation points
```

into:

```text
growth points
```

The component maximum remains unchanged.

The score explanation must record that redistribution occurred.

Example:

```text
Gross Profit Growth unavailable

Growth component available inputs:
Revenue Growth
Acceleration
CAGR
Persistence

Its 5 points may be redistributed proportionally
inside Growth only.
```

If this becomes unnecessarily complicated, prefer a simpler deterministic rule and document it.

Correctness and explainability are more important than theoretical elegance.

---

# 10. Data Confidence

Add a lightweight:

```text
data_confidence
```

or:

```text
score_coverage
```

field.

This should be separate from Compounder Score.

Example:

```text
Final Score:
82

Data Coverage:
91%
```

Do not heavily penalize the investment score simply because optional data is unavailable.

But users must be able to see whether 82 is based on complete or incomplete information.

---

# 11. Unsupported Sectors

Do not apply CompounderScore v1 blindly to companies where the model is economically inappropriate.

At minimum, identify unsupported financial businesses such as:

```text
banks
credit institutions
insurance companies
certain diversified financials
```

Use sector/industry information already available.

Return:

```text
scoring_status = UNSUPPORTED_SECTOR
```

These companies stay in the database and scanner universe.

They simply do not enter the standard ranking.

Do not build a bank-specific model in Phase 2.

---

# 12. Growth Score — 35 Points

Implement:

```text
Current Revenue Growth          12

Growth Acceleration              8

3-Year Revenue CAGR              6

Gross Profit Growth              5

Growth Persistence               4

──────────────────────────────────

Total                           35
```

Use the existing Phase 1 metrics.

Do not independently recalculate financial data inside scoring.

---

# 13. Current Revenue Growth — 12 Points

Use deterministic piecewise interpolation.

Reference points:

```text
<= 0%       → 0

10%         → 3

20%         → 6

30%         → 8

50%         → 10

>= 75%      → 12
```

Interpolate linearly between reference points.

Example:

```text
25%
```

should score between:

```text
6 and 8
```

Do not use abrupt bucket jumps where interpolation is straightforward.

Cap scoring contribution at 12.

Raw revenue growth remains uncapped in displayed metrics.

---

# 14. Growth Acceleration — 8 Points

Growth acceleration is expressed in decimal percentage-point form.

Example:

```text
current growth = 0.35
previous growth = 0.18

acceleration = 0.17
```

meaning:

```text
+17 percentage points
```

Reference scoring:

```text
<= -20pp     → 0

-10pp        → 1

0pp          → 3

+10pp        → 5

+20pp        → 7

>= +30pp     → 8
```

Interpolate between points.

---

# 15. 3-Year Revenue CAGR — 6 Points

Reference:

```text
<= 0%        → 0

5%           → 1

10%          → 2

15%          → 3

25%          → 5

>= 35%       → 6
```

Interpolate.

Do not penalize young companies purely because 3-year history is unavailable if Growth component minimum coverage can otherwise be met.

Use the missing-data policy.

---

# 16. Gross Profit Growth — 5 Points

Reference:

```text
<= 0%        → 0

10%          → 1

20%          → 2

30%          → 3

50%          → 4

>= 75%       → 5
```

Interpolate.

Do not derive fake gross-profit growth if gross-profit data is unavailable.

---

# 17. Growth Persistence — 4 Points

Use the existing recent YoY growth observations.

For the latest four comparable YoY quarters:

```text
0 positive → 0

1 positive → 1

2 positive → 2

3 positive → 3

4 positive → 4
```

If fewer than four observations exist, handle according to component data-coverage policy.

Do not assume missing observations are negative.

---

# 18. Growth Score Explanation

For every company expose something equivalent to:

```text
Growth Score:
30.4 / 35

Revenue Growth:
9.1 / 12
Observed: 38.2%

Acceleration:
6.1 / 8
Observed: +15.4pp

3-Year CAGR:
5.0 / 6
Observed: 25.1%

Gross Profit Growth:
4.2 / 5
Observed: 55%

Persistence:
4 / 4
Observed: 4/4 positive quarters
```

This structure is important.

---

# 19. Financial Quality Score — 25 Points

Implement:

```text
Gross Margin / Trend            7

FCF Margin / Trend              7

Cash vs Debt                    6

Operating Margin Improvement    5

────────────────────────────────

Total                          25
```

Use simple, deterministic rules.

---

# 20. Gross Margin — 7 Points

Because margin expectations differ by industry, do not make this entirely absolute.

For MVP combine:

```text
current gross margin
+
gross margin direction
```

Suggested base levels:

```text
< 10%        → 0

10–20%       → 1

20–30%       → 2

30–40%       → 3

40–50%       → 4

50–65%       → 5

>= 65%       → 6
```

Then trend adjustment:

```text
material deterioration     → up to -1

stable                     → 0

meaningful improvement     → up to +1
```

Final:

```text
0–7
```

Use available comparable periods to calculate margin change.

Do not force a trend if historical margin data is unavailable.

---

# 21. FCF Margin / Trend — 7 Points

Reference base:

```text
<= -20%       → 0

-20%          → 1

-10%          → 2

0%            → 3

5%            → 4

10%           → 5

15%           → 6

>= 25%        → 7
```

Use interpolation.

If current FCF margin is negative but substantially improving, allow a modest trend adjustment if necessary, while keeping total at 7 maximum.

Keep the implementation understandable.

---

# 22. Cash vs Debt — 6 Points

Do not use absolute debt dollars.

Use normalized balance-sheet context.

At minimum calculate when inputs exist:

```text
net_cash = cash - debt
```

Potential rule:

```text
Debt unknown
→ this subcomponent unavailable

Net debt materially large relative to company scale
→ 0–2

Near balanced
→ 3

Net cash
→ 4–6
```

A reasonable scale-aware ratio is:

```text
net_cash / market_cap
```

or:

```text
net_debt / market_cap
```

Do not penalize Ford and a $500M software company using the same absolute dollar threshold.

Keep this rule simple.

Example reference approach:

```text
net cash / market cap >= +20%     → 6

+10%                              → 5

0%                                → 4

-10%                              → 3

-25%                              → 2

-50%                              → 1

<= -75%                           → 0
```

Validate whether this behaves reasonably on real examples.

If it produces nonsense for certain supported industries, revise minimally and document.

---

# 23. Operating Margin Improvement — 5 Points

Use change in operating margin over an appropriate comparable period.

Reference:

```text
deterioration <= -10pp     → 0

-5pp                       → 1

0pp                        → 2

+3pp                       → 3

+5pp                       → 4

>= +10pp                   → 5
```

Interpolate.

This gives credit to companies becoming more efficient even if still unprofitable.

---

# 24. Valuation Score — 25 Points

Keep valuation deliberately simple.

Do not build a full DCF.

Do not require all valuation metrics.

The scoring method should adapt to company profitability.

Primary goal:

> Reward reasonable valuation relative to growth and current economics.

---

# 25. Enterprise Value

Calculate internally when possible:

```text
Enterprise Value =
Market Cap
+ Debt
- Cash
```

Only calculate if:

```text
market_cap is known
cash is known
debt is known
```

If debt or cash is unknown:

```text
enterprise_value = None
```

Do not substitute zero.

---

# 26. Primary Valuation Metrics

For unprofitable/high-growth companies prioritize:

```text
EV / Revenue

Price / Sales

Growth-adjusted EV / Revenue
```

For profitable companies later allow:

```text
P/E

EV / EBITDA

FCF Yield
```

Do not make P/E or EBITDA mandatory.

---

# 27. Add Required Financial Fields Carefully

Phase 2 may add:

```text
net_income

EBITDA
```

only if they can be obtained reliably from the existing FMP/EDGAR architecture without making the system dependent on inaccessible FMP endpoints.

Prefer EDGAR where practical.

If EBITDA extraction becomes messy or unreliable:

do not block Phase 2 on it.

The MVP can operate without EV/EBITDA.

Likewise P/E is optional.

---

# 28. Valuation Score Structure

Recommended Version 1:

```text
Primary Valuation Multiple     15

Growth-Adjusted Valuation       7

FCF / Profitability Context     3

─────────────────────────────────

Total                          25
```

Do not add peer modeling yet unless the required data already exists cleanly.

---

# 29. Primary Valuation Multiple — 15 Points

For companies with valid EV:

use:

```text
EV / TTM Revenue
```

as the initial common valuation multiple.

Suggested broad reference points:

```text
<= 1x        → 15

2x           → 13

3x           → 11

5x           → 8

8x           → 5

12x          → 2

>= 20x       → 0
```

Interpolate.

This is intentionally broad.

It will later be balanced by growth-adjusted valuation.

---

# 30. Price/Sales Fallback

If EV cannot be calculated because debt/cash is unknown:

use:

```text
Price / Sales =
Market Cap / TTM Revenue
```

as a fallback.

Do not pretend P/S and EV/Revenue are identical.

Record:

```text
valuation_basis = PRICE_TO_SALES
```

instead of:

```text
EV_TO_REVENUE
```

The score explanation must expose which metric was used.

---

# 31. Growth-Adjusted Valuation — 7 Points

Create a simple heuristic based on:

```text
valuation multiple
relative to revenue growth
```

One possible metric:

```text
growth_adjusted_sales_multiple =
EV/Revenue / max(revenue_growth * 100, floor)
```

However, do not implement a strange ratio simply because this prompt suggests one.

Before coding, design a simple deterministic approach that has intuitive behavior:

```text
high growth + low multiple
→ high points

high growth + high multiple
→ moderate points

low growth + high multiple
→ low points
```

Prefer a small two-dimensional scoring matrix or piecewise rule over an opaque formula.

Document the final rule explicitly and test it heavily.

The maximum is 7 points.

---

# 32. Example Growth-Adjusted Behavior

The algorithm should intuitively prefer:

```text
Company A

Revenue Growth:
50%

EV/Sales:
3x
```

over:

```text
Company B

Revenue Growth:
15%

EV/Sales:
12x
```

without automatically concluding A is a buy.

Also test:

```text
100% growth at 25x sales
```

It should not automatically receive maximum valuation points.

---

# 33. FCF / Profitability Valuation Context — 3 Points

Use whichever is meaningful and available.

Examples:

```text
FCF Yield

P/E

EV/EBITDA
```

For the MVP, FCF Yield is preferred because FCF already exists.

Calculate when:

```text
TTM FCF > 0
market_cap > 0
```

```text
FCF Yield =
TTM FCF / Market Cap
```

Potential:

```text
<= 0%      → 0

2%         → 1

5%         → 2

>= 8%      → 3
```

If FCF is negative:

0 points for this subcomponent is reasonable because the fact is known.

If FCF data itself is unavailable:

use the missing-data policy.

---

# 34. Validate Valuation With Real Stocks

Before declaring valuation complete, inspect examples such as:

```text
AAPL
NVDA
PLTR
AMPX
RKLB
```

and several additional eligible companies.

The values should at least be economically plausible.

Do not tune them to force a desired ranking.

---

# 35. Market Confirmation Score — 15 Points

Implement:

```text
6-Month Relative Strength       6

12-Month Relative Strength      6

52-Week Position                3

────────────────────────────────

Total                          15
```

Use market performance only as secondary confirmation.

---

# 36. Benchmark Series

Use Alpaca to maintain a simple broad-market benchmark.

Use:

```text
SPY
```

unless the existing project architecture strongly favors another broad U.S. benchmark.

Store/update its daily price history using the same market-data infrastructure.

Do not create a separate market-data system.

---

# 37. Relative Strength

Calculate:

```text
relative_strength_6m =
company_return_6m - benchmark_return_6m
```

and:

```text
relative_strength_12m =
company_return_12m - benchmark_return_12m
```

These are percentage-point differences.

Example:

```text
Company:
+30%

SPY:
+10%

Relative Strength:
+20pp
```

---

# 38. 6-Month Relative Strength — 6 Points

Reference:

```text
<= -30pp      → 0

-15pp         → 1

0pp           → 3

+15pp         → 5

>= +30pp      → 6
```

Interpolate.

---

# 39. 12-Month Relative Strength — 6 Points

Use the same or very similar normalization.

Do not over-weight momentum.

---

# 40. 52-Week Position — 3 Points

Use:

```text
distance_from_52w_high
```

Remember the existing sign convention.

Example:

```text
current = 80
high = 100

distance = -20%
```

Potential score:

```text
at high / within 5%        → 3

-10%                       → 2.5

-20%                       → 2

-35%                       → 1

<= -50%                    → 0
```

Use interpolation.

This is confirmation only.

---

# 41. Risk Penalties

Risk is separate from raw score.

Implement Version 1 risk penalties for:

```text
Dilution

Cash Runway

Balance-Sheet Risk

Liquidity Confidence
```

Potential maximum total penalty:

```text
-25
```

Avoid double-counting the same risk heavily across Quality and Risk.

---

# 42. Dilution Penalty

Use:

```text
share_count_growth_yoy
```

when known.

Reference:

```text
<= 2%        → 0

5%           → -1

10%          → -3

20%          → -6

>= 35%       → -10
```

Interpolate.

If dilution is:

```text
None
```

do not treat it as 0%.

Instead:

```text
penalty unavailable
```

and lower risk-data coverage.

Do not automatically apply a penalty merely because it is unknown.

---

# 43. Cash Runway

For cash-burning companies only.

Use a reasonable TTM or recent normalized burn measure.

Conceptually:

```text
cash_runway_months =
cash / average_monthly_cash_burn
```

Do not use positive FCF companies as though they have finite runway.

If FCF is positive:

```text
runway penalty = 0
```

If cash or FCF is unknown:

```text
runway = None
```

---

# 44. Cash Runway Penalty

Reference:

```text
> 24 months       → 0

18–24             → -1

12–18             → -3

6–12              → -6

< 6               → -10
```

Interpolate if useful.

Document whether burn uses:

```text
TTM FCF / 12
```

or another available measure.

Keep it consistent.

---

# 45. Balance-Sheet Risk Penalty

Because Quality Score already evaluates cash/debt, do not heavily duplicate it.

Use only severe leverage conditions.

For example:

```text
net debt / market cap
```

Potential penalty:

```text
<= 25%      → 0

50%         → -2

75%         → -4

>= 100%     → -5
```

Only apply when debt and cash are known.

Keep maximum small.

---

# 46. Liquidity Risk

Phase 1 now exposes:

```text
VolumeBasis

CONSOLIDATED
PARTIAL
UNKNOWN
```

and:

```text
LIQUIDITY_UNVERIFIED
```

Do not pretend partial IEX volume is consolidated volume.

If reliable consolidated ADV is known and stock already passed the eligibility threshold:

no penalty is necessary unless you intentionally distinguish marginal liquidity.

If liquidity is unverified:

prefer:

```text
risk warning
+
lower risk-data confidence
```

rather than a large arbitrary score penalty.

Maximum penalty should be small if used.

---

# 47. Risk Output

Expose:

```text
dilution_penalty

runway_penalty

balance_sheet_penalty

liquidity_penalty

total_risk_penalty
```

and a simple risk label:

```text
LOW
MEDIUM
HIGH
VERY_HIGH
```

Define the label deterministically from penalties and known severe risks.

Example:

```text
0 to -3       LOW

-4 to -8      MEDIUM

-9 to -15     HIGH

< -15         VERY_HIGH
```

Adjust if necessary after inspection.

---

# 48. Final Score

Calculate:

```text
raw_score =
growth
+ quality
+ valuation
+ momentum
```

Then:

```text
final_score =
raw_score + risk_penalty
```

Clamp:

```text
0–100
```

Store both.

Never show only the final number.

---

# 49. Scoring Categories

Use:

```text
85–100
EXCEPTIONAL_RESEARCH_CANDIDATE

75–84.99
STRONG_RESEARCH_CANDIDATE

65–74.99
WORTH_WATCHING

50–64.99
MIXED

< 50
LOW_PRIORITY
```

These labels are research priority categories.

They are not buy/sell recommendations.

---

# 50. Scoring Status

Add explicit status such as:

```text
SCORED

INSUFFICIENT_DATA

UNSUPPORTED_SECTOR

NOT_ELIGIBLE

ERROR
```

Do not force every company to have a numeric score.

---

# 51. Score Snapshot Table

Add a migration and model for:

```text
score_snapshots
```

Minimum fields:

```text
id

company_id

score_date / calculated_at

growth_score

quality_score

valuation_score

momentum_score

raw_score

risk_penalty

final_score

risk_level

scoring_status

data_coverage

score_version
```

Also store sufficient breakdown/explanation either:

* in structured columns;
* or a limited JSON field.

Do not create dozens of score-component tables for the MVP.

A reasonable `breakdown_json` is acceptable here.

---

# 52. Score Version

Hard-code or configure an explicit version:

```text
COMPOUNDER_V1
```

Every snapshot must record it.

Future formula changes can become:

```text
COMPOUNDER_V2
```

Do not overwrite historical score snapshots merely because the algorithm changes.

---

# 53. Snapshot Idempotency

Running the scoring command twice on the same day with identical version/input state should not create uncontrolled duplicates.

Choose a clean rule such as uniqueness by:

```text
company_id
score_date
score_version
```

with an upsert.

Document it.

---

# 54. Rankings

Build a ranking service that returns companies with:

```text
scoring_status = SCORED
```

sorted primarily:

```text
final_score DESC
```

Tie-breakers may include:

```text
raw_score
growth_score
market_cap
```

Keep tie-breaking deterministic.

---

# 55. Top Opportunities

Implement:

```text
Top Opportunities
```

as the normal final-score ranking.

Default:

```text
Top 50
```

This is the primary Phase 2 output.

---

# 56. Hidden Gems

Implement as a query/filter over the standard score.

Do not create a separate Hidden Gems score.

Suggested:

```text
Market Cap < $5B

Final Score >= 70

Revenue Growth >= 20%

Risk != VERY_HIGH

Eligible
```

If these thresholds need small adjustments after validation, document them.

---

# 57. Great Company, Wrong Price

Implement as a query over component scores.

Initial criteria:

```text
Growth Score >= 28 / 35

Quality Score >= 20 / 25

Valuation Score <= 10 / 25
```

Label:

```text
GREAT_COMPANY_WRONG_PRICE
```

This should help identify companies worth monitoring for a better entry.

---

# 58. Improving Fast

Use score snapshots to calculate:

```text
score_change_7d

score_change_30d
```

Compare against the nearest available prior snapshot at or before the target date.

Do not require an exact calendar-date match.

Example:

```text
today score:
81

30-day comparison:
69

change:
+12
```

Implement:

```text
Improving Fast
```

sorted by meaningful positive 30-day change.

Require current company to still be scored and eligible.

---

# 59. Score Change Must Be Comparable

Only compare snapshots using:

```text
same score_version
```

Do not calculate:

```text
V2 score - V1 score
```

as though it represented business improvement.

---

# 60. Score Explanation Model

Create a clean structured response such as:

```text
ScoreBreakdown

growth
quality
valuation
momentum
risk

raw_score
final_score

observed_metrics

points_awarded

missing_metrics

warnings
```

The API and future dashboard should consume this.

Do not make future UI parse strings.

---

# 61. CLI Commands

Add commands similar to the existing CLI style.

At minimum:

```text
stock-screener score
```

Calculates scores and persists snapshots.

```text
stock-screener rankings
```

Displays Top Opportunities.

Useful options:

```text
--limit 50

--output rankings.csv

--min-score 70
```

Also support:

```text
stock-screener hidden-gems

stock-screener improving
```

if consistent with the existing CLI design.

Do not overbuild the command system.

---

# 62. Ranking Console Output

Example:

```text
Rank Ticker Score Raw Risk Growth Quality Valuation Momentum Rev Growth Mkt Cap
--------------------------------------------------------------------------------
1    XYZ    88.4  93  -4.6 32.1   22.4    22.7      15.0     51%       $1.2B
2    ABC    84.7  87  -2.3 30.5   23.0    19.2      14.3     37%       $3.1B
```

Make the output easy to inspect.

---

# 63. CSV Output

Ranking CSV should include:

```text
rank

ticker

company

final_score

raw_score

growth_score

quality_score

valuation_score

momentum_score

risk_penalty

risk_level

data_coverage

market_cap

revenue_growth

growth_acceleration

enterprise_value

valuation_basis

score_change_30d
```

Include warnings where useful.

Missing remains blank, not zero.

---

# 64. Minimal API Additions

Add only what Phase 2 needs.

Potential endpoints:

```text
GET /api/rankings

GET /api/rankings/hidden-gems

GET /api/rankings/improving

GET /api/companies/{ticker}/score
```

Do not build dashboard-specific complexity yet.

---

# 65. Tests — Piecewise Scoring

Every scoring curve must have tests for:

* every reference point;
* values between reference points;
* below minimum;
* above maximum.

For example Revenue Growth:

```text
0% → 0

10% → 3

20% → 6

30% → 8

50% → 10

75% → 12
```

and at least:

```text
15%
25%
40%
60%
```

Verify interpolation.

---

# 66. Missing-Data Tests

Required examples:

```text
debt None
→ no assumption of zero

dilution None
→ no zero-dilution reward

CAGR None
→ Growth component follows configured missing policy

gross-profit growth None
→ no fabricated value

cash None
→ EV None

debt None
→ EV None
```

These are critical.

---

# 67. Risk Tests

Create synthetic examples.

### Healthy Growth Company

```text
strong growth
good FCF
net cash
low dilution
```

Expected:

```text
small/no penalty
```

### Dilutive Company

```text
70% revenue growth
40% share growth
```

Expected:

```text
strong Growth Score
large dilution penalty
```

This is important.

Do not reduce Growth Score because of dilution.

---

# 68. Cash-Burning Company Test

Example:

```text
Cash:
$30M

Annualized burn:
$60M
```

Expected:

```text
~6 months runway
```

and substantial penalty.

Verify signs carefully.

---

# 69. Positive FCF Company

Positive FCF should not receive a finite cash-runway penalty.

Test explicitly.

---

# 70. Valuation Tests

Construct companies such as:

### Company A

```text
Growth:
50%

EV/Sales:
3x
```

### Company B

```text
Growth:
15%

EV/Sales:
12x
```

A should receive materially higher valuation points.

Also:

### Company C

```text
Growth:
100%

EV/Sales:
25x
```

should **not** receive maximum valuation points merely because growth is extreme.

---

# 71. Enterprise Value Tests

Test:

```text
Market Cap = 1B
Debt = 200M
Cash = 100M

EV = 1.1B
```

Also:

```text
Debt = None
→ EV None
```

```text
Cash = None
→ EV None
```

---

# 72. Momentum Tests

Example:

```text
Company 6M:
+30%

SPY 6M:
+10%

Relative Strength:
+20pp
```

Verify scoring.

Also test weak-relative-strength companies.

---

# 73. Snapshot Tests

Verify:

* correct persistence;
* no uncontrolled duplicate daily snapshots;
* score version saved;
* prior score lookup;
* 7-day change;
* 30-day change;
* missing historical comparison returns `None`.

---

# 74. Ranking Tests

Create synthetic companies where expected ordering is obvious.

Example:

```text
A:
excellent everything

B:
strong growth but expensive

C:
mediocre

D:
strong raw score but huge dilution
```

Verify ranking.

This should protect the core product behavior.

---

# 75. Unsupported Sector Tests

A bank/financial company should return:

```text
UNSUPPORTED_SECTOR
```

and not appear in Top Opportunities.

Do not delete it from the company database.

---

# 76. Real-Data Validation

After automated tests pass, run Phase 2 against the real Phase 1 dataset.

Generate at least:

```text
Top 50 Opportunities

Top Hidden Gems

Top Improving Fast
```

where sufficient history exists.

Do not immediately modify formulas based on whether familiar companies appear near the top.

First inspect why.

---

# 77. Manual Ranking Review

For the Top 50 inspect:

```text
Are obvious junk companies ranking too high?

Are extremely dilutive companies ranking too high?

Are companies with terrible cash runway surviving?

Are valuations behaving sensibly?

Are mega-caps crowding out smaller companies?

Are tiny-base 500% growers getting absurd growth scores?

Are missing data companies receiving unfair advantages?

Are high-quality profitable companies being punished too much?

Does growth acceleration actually affect ranking meaningfully?
```

Document observations.

---

# 78. Do Not Curve-Fit

Do not change scoring thresholds simply to make:

```text
AAPL
NVDA
AMPX
PLTR
RKLB
```

appear where expected.

These companies are sanity checks, not targets.

If the algorithm produces an unexpected result, inspect whether:

* the input data is wrong;
* the formula is wrong;
* or the unexpected result is logically valid.

Avoid hindsight fitting.

---

# 79. Outlier Handling

Do not let:

```text
Revenue Growth = 5,000%
```

produce unlimited points.

Piecewise scoring already caps its effect.

Maintain raw metrics for display.

Do not silently alter source financial data.

---

# 80. Performance

Phase 2 is a nightly personal scanner.

Do not optimize prematurely.

Do not add:

```text
Redis
Celery
Kafka
parallel scoring clusters
```

Pure deterministic scoring for a few thousand companies should be cheap.

---

# 81. Documentation

Add/update documentation explaining:

```text
CompounderScore v1

component weights

each scoring curve

risk penalties

missing-data behavior

unsupported sectors

valuation fallback

benchmark choice

score snapshots

ranking views
```

A developer should understand the entire formula without reading every source file.

---

# 82. Create a Score Reference Document

Add something like:

```text
docs/reference/compounder-score-v1.md
```

containing the exact production rules.

This document should become the source of truth for the V1 formula.

Code and documentation must agree.

---

# 83. Do Not Start Phase 3

Do not implement:

```text
OpenAI
Anthropic
LLM prompts
filing summaries
news analysis
Bull/Base/Bear AI reports
```

Phase 3 begins only after we manually inspect and approve the ranking quality.

---

# 84. Final Self-Audit

Before declaring Phase 2 complete, independently review:

```text
piecewise scoring boundaries

percentage representations

percentage-point acceleration

missing-data behavior

optional-weight redistribution

enterprise-value missingness

risk double-counting

cash-runway signs

FCF signs

relative-strength calculations

score snapshot versioning

ranking tie-breakers

unsupported sectors

CSV/API consistency
```

Fix any genuine issues.

---

# 85. Final Response Format

When complete, provide:

## A. Overall Result

Choose:

```text
PASS — Phase 2 implementation complete

PASS WITH ISSUES

FAIL
```

---

## B. What Was Built

Summarize:

```text
scoring engine
risk engine
benchmark support
score persistence
rankings
queries/views
CLI/API
```

---

## C. Exact CompounderScore v1 Formula

Show final production weights and rules.

If you made a justified adjustment from this prompt, identify it explicitly.

---

## D. Missing-Data Behavior

Explain exactly what happens when:

```text
debt missing

cash missing

CAGR missing

gross profit missing

dilution missing

momentum history missing
```

---

## E. Valuation Behavior

Explain:

```text
EV calculation

EV/Revenue

P/S fallback

growth-adjusted valuation

FCF Yield

what happens for unprofitable companies
```

---

## F. Risk Penalties

Show each implemented penalty and maximum.

---

## G. Database Changes

List migrations and `score_snapshots`.

---

## H. Ranking Output

Provide the current real-data:

```text
Top 20 Opportunities
```

with at least:

```text
Rank
Ticker
Final Score
Raw Score
Risk
Growth
Quality
Valuation
Momentum
Revenue Growth
Market Cap
```

Do not include hundreds of companies in the report.

---

## I. Interesting Ranking Observations

Point out:

* unexpected high-ranking companies;
* unexpected low-ranking companies;
* high growth/high risk names;
* Great Company, Wrong Price examples;
* obvious data-quality concerns.

Do not automatically fix them.

---

## J. Tests

Provide:

```text
passed
failed
skipped
coverage
```

and summarize major scoring/risk regression tests.

---

## K. Remaining Limitations

Only genuine Phase 2 limitations.

Do not list Phase 3 AI features as defects.

---

## L. Phase 3 Readiness

Answer:

> Is the ranking engine reliable enough for us to manually inspect the Top 50 before adding AI research?

Do **not** start Phase 3.

---

# Final Principle

Phase 2 is successful if Compounder Radar can take thousands of companies and produce a shortlist where every ranking can be explained.

The objective is not:

> mathematically sophisticated scoring.

The objective is:

> **a simple ranking system that consistently surfaces companies worth researching while making growth, valuation, and risk obvious.**

Build the smallest reliable version of that system.
