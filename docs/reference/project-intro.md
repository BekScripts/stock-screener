---
type: reference
title: Compounder Radar MVP brief
---

# Compounder Radar — Practical MVP Specification

## 1. Goal

Build a personal stock discovery tool that scans U.S. equities and helps identify companies that may have strong long-term upside because they combine:

* strong or accelerating growth;
* improving financial quality;
* reasonable valuation;
* acceptable risk;
* positive market confirmation.

The tool should not predict exact stock prices or automatically trade.

Its purpose is:

> Find interesting stocks early, rank them, explain why they are interesting, and help the user decide which ones deserve deeper research.

---

# 2. Core Workflow

```text
Market Universe
      ↓
Fetch Market + Fundamental Data
      ↓
Basic Eligibility Filters
      ↓
Calculate Financial Metrics
      ↓
Calculate Compounder Score
      ↓
Apply Risk Penalties
      ↓
Rank Stocks
      ↓
Select Top Candidates
      ↓
AI Research
      ↓
Dashboard / Watchlist
```

The first version should prioritize simplicity over architectural perfection.

---

# 3. Initial Stock Universe

Focus on U.S.-listed common stocks.

Initial exchanges:

```text
NASDAQ
NYSE
NYSE American
```

Basic eligibility:

```text
Share Price >= $2

Market Cap >= $100M

20-Day Average Dollar Volume >= $1M

Common Stock

Actively Trading
```

ETFs, warrants, preferred shares, OTC securities, shell companies, and similar instruments should be excluded.

The thresholds should be configuration values rather than hard-coded throughout the project.

---

# 4. Required Stock Data

For each company, collect as much of the following as reasonably available.

## Market Data

```text
Ticker
Company Name
Sector
Industry

Current Price
Market Cap
Enterprise Value

Daily Volume
20-Day Average Volume
20-Day Average Dollar Volume

6-Month Price Return
12-Month Price Return
52-Week High
52-Week Low
```

## Growth Data

```text
Quarterly Revenue
TTM Revenue

Latest YoY Revenue Growth
Previous YoY Revenue Growth

3-Year Revenue CAGR

Quarterly Gross Profit
Gross Profit Growth
```

## Financial Quality

```text
Gross Margin

Operating Margin

Free Cash Flow
FCF Margin

Cash
Total Debt

Net Cash / Net Debt
```

## Shareholder Quality

```text
Shares Outstanding
YoY Share Count Change
```

## Valuation

When applicable:

```text
EV / Revenue
Price / Sales
P / E
EV / EBITDA
FCF Yield
```

Not every metric must exist for every company.

Missing values should remain missing rather than becoming zero.

---

# 5. Derived Metrics

The backend should calculate metrics itself instead of trusting precomputed vendor scores.

## Revenue Growth

```text
Revenue Growth =
(Current Quarter Revenue - Prior-Year Quarter Revenue)
/
Prior-Year Quarter Revenue
```

## Growth Acceleration

```text
Growth Acceleration =
Current YoY Revenue Growth
-
Previous YoY Revenue Growth
```

Example:

```text
Previous Growth = 18%
Current Growth = 35%

Acceleration = +17 percentage points
```

## Gross Margin

```text
Gross Margin =
Gross Profit / Revenue
```

## Free Cash Flow Margin

```text
FCF Margin =
Free Cash Flow / Revenue
```

## Net Cash

```text
Net Cash =
Cash - Total Debt
```

## Dilution

```text
Share Count Growth =
(Current Shares - Prior Shares)
/
Prior Shares
```

## Average Dollar Volume

```text
Average Dollar Volume =
Average Daily Volume × Average Price
```

---

# 6. Compounder Score

Every eligible company receives a score from:

```text
0–100
```

Version 1:

```text
Growth                  35 points
Financial Quality       25 points
Valuation               25 points
Market Confirmation     15 points
────────────────────────────────
Maximum                 100 points
```

Risk should not be mixed invisibly into these components.

Risk penalties are applied afterward and displayed separately.

---

# 7. Growth Score — 35 Points

Growth is the largest component.

Suggested breakdown:

```text
Current Revenue Growth          12
Growth Acceleration              8
3-Year Revenue CAGR              6
Gross Profit Growth              5
Growth Persistence               4
──────────────────────────────────
Total                           35
```

## Current Revenue Growth

Example normalization:

```text
<= 0%       → 0 points
10%         → 3
20%         → 6
30%         → 8
50%         → 10
>= 75%      → 12
```

Interpolate between ranges if desired.

Very extreme growth should be capped for scoring purposes.

Raw growth should still be displayed.

---

# 8. Growth Acceleration

Example:

```text
Acceleration <= -20pp   → 0
-10pp                    → 1
0pp                      → 3
+10pp                    → 5
+20pp                    → 7
>= +30pp                 → 8
```

This allows Compounder Radar to find companies whose businesses are improving quickly rather than merely companies that are already large growers.

---

# 9. Revenue CAGR

Example:

```text
< 0%      → 0
5%        → 1
10%       → 2
15%       → 3
25%       → 5
>= 35%    → 6
```

If insufficient history exists, mark the metric unavailable.

Do not automatically give zero.

---

# 10. Gross Profit Growth

Example:

```text
<= 0%      → 0
10%        → 1
20%        → 2
30%        → 3
50%        → 4
>= 75%     → 5
```

Gross profit growing faster than revenue is an especially positive sign.

---

# 11. Growth Persistence

Look at the last four available YoY revenue-growth observations.

Example:

```text
0 positive quarters → 0
1 positive quarter  → 1
2 positive quarters → 2
3 positive quarters → 3
4 positive quarters → 4
```

This prevents one strong quarter from completely dominating the score.

---

# 12. Financial Quality Score — 25 Points

Suggested breakdown:

```text
Gross Margin / Trend            7
FCF Margin / Trend              7
Cash vs Debt                    6
Operating Margin Improvement    5
────────────────────────────────
Total                          25
```

This component asks:

> Is the company growing in a financially healthy direction?

---

# 13. Gross Margin

Gross margin should consider both level and direction.

Example:

```text
Low + deteriorating       → 0–1

Average + stable          → 3–4

Strong + stable           → 5–6

Strong + improving        → 7
```

Sector differences should eventually matter.

For MVP, use broad ranges and margin trend.

---

# 14. FCF Quality

Profitable cash generation should receive strong credit.

Example:

```text
FCF Margin < -20%         → 0
-20% to -10%              → 1
-10% to 0%                → 2
0% to 5%                  → 4
5% to 15%                 → 5–6
> 15%                     → 7
```

A company with negative FCF but rapidly improving burn may receive partial credit.

---

# 15. Balance Sheet

Example:

```text
Large Net Debt            → 0–1

Moderate Debt             → 2–3

Roughly Neutral           → 4

Net Cash                  → 5

Large Net Cash Position   → 6
```

Exact interpretation may later depend on business size.

---

# 16. Operating Margin Improvement

Companies approaching profitability should receive credit.

Example:

```text
Margin deteriorating materially  → 0

Stable                           → 2

Improved 3–5 percentage points   → 3

Improved 5–10 points             → 4

Improved >10 points              → 5
```

A company does not need to already be profitable.

---

# 17. Valuation Score — 25 Points

Valuation must be interpreted relative to business maturity.

For the MVP, use a small set of rules.

For unprofitable growth companies:

```text
EV / Revenue
Price / Sales
```

For profitable companies:

```text
P / E
EV / EBITDA
FCF Yield
```

The scoring engine should select metrics based on which ones are economically meaningful.

---

# 18. Growth-Adjusted EV / Revenue

For high-growth companies, one useful heuristic is:

```text
EV / Revenue
relative to
Revenue Growth
```

Example:

```text
Revenue Growth = 50%
EV / Revenue = 3x

Potentially attractive
```

versus:

```text
Revenue Growth = 15%
EV / Revenue = 12x

Potentially expensive
```

This should be a ranking heuristic rather than a formal valuation model.

---

# 19. Valuation Score Example

A simplified 25-point model could use:

```text
Primary Valuation Multiple     15
Growth-Adjusted Valuation       6
Historical / Peer Context       4
─────────────────────────────────
Total                          25
```

If peer data is unavailable initially, reallocate those four points to the first two measures.

The system should avoid penalizing companies simply because one valuation metric is not applicable.

---

# 20. Market Confirmation — 15 Points

The goal is not to create a momentum trading strategy.

Momentum acts only as secondary confirmation.

Suggested:

```text
6-Month Relative Strength      6
12-Month Relative Strength     6
Distance From 52-Week High     3
────────────────────────────────
Total                         15
```

Compare returns with a broad market benchmark where practical.

---

# 21. Momentum Interpretation

Positive:

```text
Strong fundamentals
+
strong relative stock performance
```

can indicate the market is beginning to recognize improvement.

But:

```text
Strong fundamentals
+
weak price
```

should not automatically remove a company.

That may represent a valuation opportunity.

Therefore, market confirmation only receives 15% of the score.

---

# 22. Risk Penalties

After the 100-point score is calculated, risk penalties are applied.

Suggested initial penalties:

```text
High Dilution                   0 to -10

Short Cash Runway              0 to -10

Very High Debt                 0 to -10

Very Low Liquidity             0 to -5

Severe Revenue Deterioration   0 to -5
```

Total penalty may be capped, for example:

```text
-25
```

The UI should display:

```text
Raw Compounder Score

Risk Penalty

Adjusted Score
```

Example:

```text
Raw Score       86

Risk Penalty    -9

Final Score     77
```

This is much more transparent than hiding risk inside the original score.

---

# 23. Dilution Penalty

Example:

```text
Shares YoY Change

<= 2%      → 0

5%         → -1

10%        → -3

20%        → -6

>= 35%     → -10
```

The threshold should be adjustable.

Dilution is particularly important for small, unprofitable growth companies.

---

# 24. Cash Runway

For companies burning cash:

```text
Quarterly Cash Burn =
max(0, -Quarterly FCF)
```

Approximate:

```text
Cash Runway =
Cash / Average Quarterly Burn × 3 months
```

Possible penalty:

```text
> 24 months     → 0

18–24           → -1

12–18           → -3

6–12            → -6

< 6             → -10
```

This is approximate and should be labeled accordingly.

---

# 25. Candidate Categories

Based on the final score:

```text
85–100
Exceptional Research Candidate

75–84
Strong Research Candidate

65–74
Worth Watching

50–64
Mixed

<50
Low Priority
```

Risk should also be shown separately:

```text
LOW
MEDIUM
HIGH
VERY HIGH
```

A stock can therefore appear as:

```text
Compounder Score: 88

Adjusted Score: 76

Risk: HIGH
```

This is useful information rather than a contradiction.

---

# 26. Ranking Engine

After scoring, sort companies primarily by:

```text
Adjusted Score DESC
```

Allow alternative sorting:

```text
Raw Compounder Score

Growth Score

Valuation Score

Risk

Revenue Growth

Market Cap
```

Default page:

```text
Top 50 Opportunities
```

---

# 27. Hidden Gems Filter

Add one special view:

```text
Hidden Gems
```

Suggested initial criteria:

```text
Market Cap < $5B

Adjusted Score >= 70

Revenue Growth >= 20%

Average Dollar Volume >= $1M

Risk != VERY HIGH
```

This should simply be a query over the normal rankings.

Do not build a separate Hidden Gems scoring system.

---

# 28. Great Company, Wrong Price

Another useful query:

```text
Raw Quality/Growth strong

Valuation Score weak
```

Example:

```text
Growth Score >= 28 / 35

Financial Quality >= 20 / 25

Valuation <= 10 / 25
```

Label:

```text
GREAT COMPANY — WRONG PRICE
```

This allows the user to create a watchlist and wait for a better entry.

---

# 29. Improving Fast

One of the most useful views should be:

```text
Improving Fast
```

Track changes in scores over time.

Save one ranking snapshot per day.

Calculate:

```text
Score Change 7D

Score Change 30D
```

Show companies with large positive improvement.

This makes Compounder Radar more useful than a static screener.

---

# 30. Daily Ranking History

A simple table can store:

```text
Date
Ticker
Raw Score
Risk Penalty
Final Score
Growth Score
Quality Score
Valuation Score
Momentum Score
```

This allows:

```text
Score today:
81

30 days ago:
69

Change:
+12
```

No complex point-in-time research architecture is required for the MVP.

---

# 31. AI Research Layer

AI runs only on:

```text
Top ranked companies

New high-scoring companies

Large score movers

Watchlist companies
```

Do not analyze the entire market with AI.

---

# 32. AI Research Questions

For each selected company, provide structured financial metrics plus recent filings/news.

Ask the AI to answer:

```text
1. What does the company actually do?

2. Why is revenue growing?

3. Is growth accelerating or slowing?

4. Are margins improving?

5. What are the biggest future growth drivers?

6. What recent catalysts matter?

7. What are the biggest risks?

8. Is dilution a concern?

9. What could break the thesis?

10. What should I watch next quarter?

11. Give a Bull Case.

12. Give a Bear Case.
```

The AI must not calculate the official score.

---

# 33. AI Output

Save a concise structured report:

```text
Summary

Why It Ranked High

Growth Drivers

Catalysts

Risks

Bull Case

Bear Case

Next Things to Watch
```

The user should be able to read the entire analysis in a few minutes.

---

# 34. AI Guardrails

The AI should receive explicit instructions:

```text
Do not invent financial figures.

Use the provided metrics as authoritative.

Clearly distinguish reported facts from interpretation.

If information is unavailable, say unknown.

Do not output buy/sell orders.
```

This is enough for the MVP.

---

# 35. Dashboard

The main dashboard should have four primary areas.

## Top Opportunities

Ranked stocks by final score.

## Improving Fast

Stocks with the biggest recent score improvement.

## Hidden Gems

Strong smaller companies.

## Watchlist

Companies manually tracked by the user.

---

# 36. Main Ranking Table

Recommended columns:

```text
Rank

Ticker

Company

Final Score

Growth

Quality

Valuation

Momentum

Risk

Market Cap

Revenue Growth

Score Change 30D
```

Clicking a row opens the company page.

---

# 37. Filters

Useful filters:

```text
Market Cap

Sector

Industry

Minimum Score

Minimum Revenue Growth

Risk Level

Profitable / Unprofitable

Net Cash Only

Growth Accelerating

Hidden Gems
```

Keep filters practical.

---

# 38. Company Page

The company page should contain:

## Header

```text
Ticker
Company
Price
Market Cap
Sector
Final Score
Risk
```

## Score Breakdown

```text
Growth             30 / 35

Financial Quality  20 / 25

Valuation           18 / 25

Momentum            11 / 15

Raw Score           79

Risk Penalty        -5

Final Score         74
```

## Financial Trends

Charts for:

```text
Revenue

Revenue Growth

Gross Margin

FCF

Shares Outstanding
```

## Valuation

Display available multiples.

## Price Chart

1-year price history.

## AI Research

Show latest structured summary.

---

# 39. Watchlist

Allow:

```text
Add to Watchlist

Remove from Watchlist

Add Notes
```

Watchlist companies should receive AI refreshes even if they fall outside the current Top 50.

---

# 40. Alerts

Keep alerts simple.

Potential initial alerts:

```text
Score crossed above 75

Score increased >10 points

Revenue growth accelerated materially

Large dilution detected

Company entered Hidden Gems

Watchlist company dropped sharply in score
```

Email/Discord/Telegram can be added later.

The MVP can simply display alerts inside the application.

---

# 41. Backend

Recommended backend:

```text
Python
FastAPI
```

Main modules:

```text
app/

    data/
    metrics/
    scoring/
    research/
    rankings/
    api/
```

Keep the structure small.

---

# 42. Database

Use:

```text
PostgreSQL
```

If fastest possible prototyping is preferred:

```text
SQLite
```

can work locally first.

PostgreSQL is preferable once regular scheduled ingestion begins.

---

# 43. Minimum Database Tables

The MVP does not need dozens of tables.

Start with approximately:

```text
companies

financial_snapshots

price_history

score_snapshots

ai_reports

watchlist

alerts
```

That is enough.

---

# 44. Companies Table

Basic fields:

```text
id

ticker

name

sector

industry

market_cap

exchange

is_active
```

---

# 45. Financial Snapshots

One row per company per reporting snapshot.

Fields may include:

```text
company_id

date

revenue

revenue_growth

gross_profit

gross_margin

operating_margin

free_cash_flow

fcf_margin

cash

debt

shares_outstanding

source
```

Keep original source data where useful.

---

# 46. Price History

```text
company_id

date

open

high

low

close

volume
```

Daily bars are sufficient.

Intraday data is unnecessary.

---

# 47. Score Snapshots

```text
company_id

date

growth_score

quality_score

valuation_score

momentum_score

raw_score

risk_penalty

final_score
```

This table enables score-change views.

---

# 48. AI Reports

```text
company_id

created_at

summary

growth_drivers

catalysts

risks

bull_case

bear_case

watch_next
```

Optionally save raw structured JSON as well.

---

# 49. Data Providers

Use provider adapters so one API can later be replaced.

Interface examples:

```python
class FundamentalsProvider:
    get_companies()
    get_financials(ticker)
```

```python
class MarketDataProvider:
    get_daily_prices(ticker)
```

The first implementation only needs one provider for each role.

---

# 50. Suggested Initial Providers

Use:

```text
Alpaca
```

for daily market prices if already available.

Use one fundamentals provider for:

```text
income statements
balance sheets
cash flow
market cap
enterprise value
```

The exact provider can be selected based on available subscription/API access.

Do not block development trying to perfectly reconcile multiple vendors.

---

# 51. Scheduler

The MVP can use a simple scheduled command.

For example:

```text
Every evening:

1. Update price data.
2. Update companies with new fundamentals.
3. Recalculate metrics.
4. Recalculate scores.
5. Save ranking snapshot.
6. Identify top candidates.
7. Run AI research where necessary.
```

No complex worker infrastructure is necessary initially.

---

# 52. CLI Commands

Useful commands:

```text
python -m radar update-market

python -m radar update-fundamentals

python -m radar score

python -m radar research

python -m radar run-daily
```

This makes development and debugging much easier.

---

# 53. Logging

Log:

```text
companies processed

provider failures

missing data

score calculation errors

AI failures
```

Do not overbuild observability.

A normal application log is enough initially.

---

# 54. Testing

Focus tests on the important algorithms.

Required:

```text
Revenue growth calculation

Growth acceleration

Gross margin

FCF margin

Dilution

Cash runway

Each score component

Risk penalties

Final score
```

Also test missing-data behavior.

---

# 55. Scoring Tests

Create synthetic companies.

Example:

```text
Company A

Revenue Growth:
60%

Acceleration:
+20pp

Strong Margins

Net Cash

Cheap Valuation

Expected:
High score
```

Company B:

```text
Revenue Growth:
70%

Heavy dilution

Six months runway

Very expensive

Expected:
Strong raw growth
but much lower final score
```

This protects the ranking logic.

---

# 56. Four Implementation Phases

The whole MVP should be built in four phases.

---

# Phase 1 — Data + Scanner

Build:

```text
Project structure

Database

Company universe loader

Market-data loader

Fundamentals loader

Eligibility filters

Derived financial metrics
```

Output:

A script that can produce a table of eligible companies and their financial metrics.

Do not build AI or a polished dashboard yet.

---

# Phase 2 — Scoring + Rankings

Build:

```text
Growth Score

Financial Quality Score

Valuation Score

Momentum Score

Risk Penalties

Final Score

Daily Score Snapshot

Top Opportunities

Hidden Gems

Improving Fast
```

Output:

Command such as:

```text
python -m radar score
```

that prints or exports the ranked market.

This is the core of the entire project.

---

# Phase 3 — AI Research

Build:

```text
AI provider abstraction

Candidate selection

Recent-document/news collection

Structured research prompt

AI report persistence
```

Only analyze:

```text
Top 20–50
+
watchlist
+
major score movers
```

Output:

A useful research report for each interesting stock.

---

# Phase 4 — Dashboard

Build:

```text
Next.js frontend

Top Opportunities page

Hidden Gems

Improving Fast

Company page

Watchlist

Basic alerts
```

Connect everything to the FastAPI backend.

At that point, Compounder Radar is a complete useful product.

---

# 57. Explicitly Out of Scope for MVP

Do NOT build:

```text
Automatic trading

Broker order execution

Portfolio optimization

Options analytics

Intraday strategy

Hundreds of AI agents

Kafka

Kubernetes

Microservices

Vector database

Complex point-in-time filing warehouse

Institutional-grade backtesting infrastructure

Social network

Mobile application

Complex authentication
```

These can be added later only if the product proves useful.

---

# 58. Development Priority

The single most important part of the project is:

```text
SCORING ALGORITHM
```

Not:

```text
AI
Dashboard
Infrastructure
```

The ranking must be useful first.

Therefore the development sequence is:

```text
Data
→
Metrics
→
Scores
→
Validate Rankings
→
AI
→
UI
```

---

# 59. First Validation

Before building the dashboard, run the scanner against several hundred or thousand stocks.

Inspect:

```text
Top 50
```

manually.

Ask:

```text
Are these actually interesting companies?

Are obvious junk companies ranking too high?

Are strong companies ranking too low?

Is dilution being punished enough?

Is valuation being punished enough?

Is growth acceleration useful?
```

Then adjust the scoring system.

This manual review is more important than adding features.

---

# 60. Scoring Calibration

Do not immediately optimize the algorithm using historical stock returns.

First calibrate it using economic reasoning.

Then compare:

```text
high-score companies

medium-score companies

low-score companies
```

historically.

Later, a lightweight backtest can be added.

Avoid curve-fitting the scoring formula to historical winners.

---

# 61. MVP Success Definition

Compounder Radar is successful if it can regularly answer:

```text
What stocks should I research today?

Which smaller companies are growing unusually fast?

Which companies are improving quickly?

Which strong companies are becoming cheaper?

Which companies look exciting but carry major financial risk?

Why did this company rank highly?
```

If it does those things well, the MVP has achieved its purpose.

---

# 62. Final Product Principle

Compounder Radar should stay simple.

The system does not need to understand every company perfectly.

It needs to reduce:

```text
thousands of stocks
```

into:

```text
a small group worth investigating.
```

The essential loop is:

```text
SCAN

RANK

EXPLAIN

RESEARCH

WATCH
```

Everything else is optional.

The guiding principle for future development should be:

> If a feature does not materially improve stock discovery, ranking quality, research efficiency, or risk awareness, it does not belong in the MVP.
