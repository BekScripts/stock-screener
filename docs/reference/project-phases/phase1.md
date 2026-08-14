---
type: reference
title: Phase 1 brief
---

# Claude Code Implementation Prompt — Compounder Radar Phase 1

You are implementing **Phase 1 of Compounder Radar**, a small personal stock-discovery tool.

Your job is to build a clean, working foundation that:

1. loads a universe of U.S. stocks;
2. collects market and fundamental data;
3. stores that data locally;
4. calculates the core financial metrics needed later;
5. filters stocks for basic eligibility;
6. produces a usable table of eligible companies.

Do **not** implement scoring, AI research, portfolio management, trading, or the frontend yet.

---

# Project Goal

Compounder Radar is designed to answer:

> Which stocks deserve deeper research because they show strong or improving growth, reasonable financial quality, and enough liquidity to be investable?

The final application will eventually:

```text
Market Data
   ↓
Scanner
   ↓
Compounder Score
   ↓
Top Candidates
   ↓
AI Research
   ↓
Dashboard
```

For this phase, implement only:

```text
Market Data
   ↓
Scanner
   ↓
Financial Metrics
   ↓
Eligible Company Dataset
```

---

# Phase 1 Scope

Build:

* project structure;
* configuration;
* database;
* stock universe loader;
* market-data provider abstraction;
* fundamentals provider abstraction;
* provider implementations where credentials/configuration exist;
* data normalization;
* eligibility filters;
* financial metric calculations;
* command-line workflows;
* tests.

At completion, I should be able to run a command such as:

```bash
python -m radar run-scan
```

and receive a ranked-neutral table of eligible stocks with raw financial metrics.

There should be **no investment score yet**.

---

# Technology

Use:

```text
Python
FastAPI
SQLAlchemy
Pydantic
PostgreSQL
pytest
```

The backend should be structured so FastAPI can later expose the data, but Phase 1 does not need a large API surface.

For development, allow SQLite only if necessary for fast tests/local setup, but PostgreSQL should be the intended production database.

Do not introduce:

```text
Celery
Redis
Kafka
Kubernetes
Microservices
LangChain
Vector databases
```

They are unnecessary.

---

# Architecture Philosophy

Keep the codebase small and understandable.

Prefer:

```text
simple modules
explicit interfaces
deterministic calculations
strong tests
```

over:

```text
deep abstractions
generic frameworks
premature scalability
```

Do not create architecture merely because it may theoretically be useful later.

---

# Suggested Project Structure

Use something close to:

```text
compounder-radar/
│
├── app/
│   ├── __init__.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   └── database.py
│   │
│   ├── models/
│   │   ├── company.py
│   │   ├── financial_snapshot.py
│   │   └── price.py
│   │
│   ├── providers/
│   │   ├── base.py
│   │   ├── market_data.py
│   │   └── fundamentals.py
│   │
│   ├── services/
│   │   ├── universe.py
│   │   ├── ingestion.py
│   │   ├── metrics.py
│   │   └── scanner.py
│   │
│   ├── schemas/
│   │
│   └── api/
│
├── tests/
│
├── scripts/
│
├── migrations/
│
├── pyproject.toml
├── .env.example
└── README.md
```

You may improve the structure if there is a clear reason, but do not overengineer it.

---

# Database

Create the minimum useful schema.

## `companies`

Fields:

```text
id

ticker

name

exchange

sector

industry

market_cap

is_active

created_at

updated_at
```

Ticker must be unique for the MVP.

We do not need the historical ticker architecture from an institutional-grade platform.

---

# `financial_snapshots`

Store one financial snapshot for each company/reporting date.

Fields should support:

```text
company_id

period_end

revenue

gross_profit

operating_income

free_cash_flow

cash

total_debt

shares_outstanding

source

created_at
```

Add fields if clearly necessary.

Do not store vendor-specific response objects as the primary schema.

Normalize incoming data.

---

# `price_history`

Daily bars.

Fields:

```text
company_id

date

open

high

low

close

volume
```

Unique constraint:

```text
company_id + date
```

---

# Data Provider Abstractions

Define small interfaces.

Example:

```python
class MarketDataProvider:
    def get_stock_universe(self): ...
    def get_daily_prices(self, ticker, start_date, end_date): ...
```

and:

```python
class FundamentalsProvider:
    def get_company_profile(self, ticker): ...
    def get_financial_statements(self, ticker): ...
```

Do not leak provider-specific objects into the rest of the application.

Normalize provider responses into internal Pydantic/domain objects.

---

# Market Data

Use Alpaca as the initial market-data implementation if the project already has usable Alpaca credentials/configuration.

Use it primarily for:

```text
daily OHLCV
active-stock metadata if practical
```

The rest of Compounder Radar must depend on the `MarketDataProvider` interface rather than directly calling Alpaca everywhere.

If credentials are unavailable during implementation:

* implement the adapter;
* support mocks/fixtures;
* ensure the codebase and tests still run.

Do not fabricate live responses.

---

# Fundamentals Provider

Implement a provider abstraction suitable for a fundamentals service such as FMP or another configured provider.

The application needs enough historical data to calculate:

```text
Revenue
Gross Profit
Operating Income
Free Cash Flow
Cash
Debt
Shares Outstanding
```

Prefer quarterly financial statements where available.

Do not tightly couple the rest of the application to the provider.

If no API credential is available, create:

```text
MockFundamentalsProvider
```

and sample fixtures for tests.

---

# Universe

Initial universe:

```text
NASDAQ
NYSE
NYSE American
```

Only common stocks should pass when security-type information is available.

Exclude obvious:

```text
ETFs
ETNs
funds
preferred shares
warrants
rights
OTC securities
inactive securities
```

Do not spend excessive time solving every unusual security structure.

---

# Basic Eligibility Filters

A stock is eligible for the MVP scanner if:

```text
Price >= $2

Market Cap >= $100,000,000

20-Day Average Dollar Volume >= $1,000,000

Actively Trading
```

These values must live in configuration.

Example:

```python
MIN_PRICE = 2.0
MIN_MARKET_CAP = 100_000_000
MIN_AVG_DOLLAR_VOLUME = 1_000_000
```

Prefer a typed configuration object rather than global constants scattered across files.

---

# Average Dollar Volume

Calculate:

```text
Average Dollar Volume =
mean(close × volume)
over the latest 20 trading sessions
```

If fewer than 20 sessions exist, calculate using available history but expose:

```text
trading_days_used
```

and do not treat a newly listed stock as having full data confidence.

For the eligibility check, requiring 20 sessions is acceptable if that simplifies behavior.

Choose one approach and document it.

---

# Core Metric Engine

Implement deterministic functions for the following.

---

## 1. YoY Revenue Growth

Using comparable quarterly periods:

```text
(Current Revenue - Prior-Year Revenue)
/
Prior-Year Revenue
```

Return `None` when the prior value is:

```text
missing
zero
invalid
```

Do not fabricate growth.

---

## 2. Previous YoY Revenue Growth

Needed later for growth acceleration.

For example:

```text
Current:
Q2 2026 vs Q2 2025

Previous:
Q1 2026 vs Q1 2025
```

---

## 3. Growth Acceleration

```text
Current YoY Revenue Growth
-
Previous YoY Revenue Growth
```

Return the result as a decimal.

Example:

```text
current = 0.35
previous = 0.18

acceleration = 0.17
```

This means:

```text
+17 percentage points
```

Do not divide one growth rate by the other.

---

## 4. TTM Revenue

Sum the latest four quarterly revenue observations.

If four valid quarters do not exist:

```text
TTM Revenue = None
```

Do not silently sum three quarters.

---

## 5. TTM Revenue Growth

Compare latest TTM revenue against the preceding four-quarter TTM period.

Require eight quarterly observations.

If insufficient history exists:

return `None`.

---

## 6. 3-Year Revenue CAGR

Formula:

```text
(Ending Revenue / Beginning Revenue)^(1/3) - 1
```

Prefer annual data if available.

Otherwise, use comparable TTM values approximately three years apart.

Document the methodology.

Return `None` when the calculation is invalid.

---

## 7. Gross Margin

```text
Gross Profit / Revenue
```

---

## 8. Gross Profit Growth

Quarterly YoY:

```text
(Current Gross Profit - Prior-Year Gross Profit)
/
Prior-Year Gross Profit
```

---

## 9. Operating Margin

```text
Operating Income / Revenue
```

---

## 10. Free Cash Flow Margin

```text
Free Cash Flow / Revenue
```

If the provider does not directly provide FCF but does provide:

```text
Operating Cash Flow
Capital Expenditures
```

calculate:

```text
FCF = Operating Cash Flow - Capital Expenditures
```

Be careful about capex sign conventions.

Normalize it before calculation.

---

## 11. Net Cash

```text
Cash - Total Debt
```

---

## 12. Share Count Growth / Dilution

Using comparable share-count observations:

```text
(Current Shares - Prior Shares)
/
Prior Shares
```

Use roughly one-year comparable observations.

Return `None` when unavailable.

---

## 13. Six-Month Return

Using daily adjusted/close price where appropriate:

```text
Current Price / Price ~6 months ago - 1
```

Use trading dates rather than assuming exactly 180 calendar days.

---

## 14. Twelve-Month Return

Same concept over approximately one year.

---

## 15. 52-Week High / Low

Calculate from daily history.

Expose:

```text
52_week_high
52_week_low
distance_from_52_week_high
```

Where:

```text
distance =
current_price / high - 1
```

---

# Metric Output Model

Create a normalized object similar to:

```python
class CompanyMetrics(BaseModel):
    ticker: str

    price: float | None
    market_cap: float | None
    average_dollar_volume_20d: float | None

    revenue_growth_yoy: float | None
    previous_revenue_growth_yoy: float | None
    revenue_growth_acceleration: float | None
    ttm_revenue: float | None
    ttm_revenue_growth: float | None
    revenue_cagr_3y: float | None

    gross_margin: float | None
    gross_profit_growth_yoy: float | None
    operating_margin: float | None
    fcf_margin: float | None

    cash: float | None
    debt: float | None
    net_cash: float | None

    share_count_growth_yoy: float | None

    return_6m: float | None
    return_12m: float | None
    high_52w: float | None
    low_52w: float | None
    distance_from_52w_high: float | None
```

Names may change if a better domain convention is chosen.

Keep it readable.

---

# Missing Data Behavior

This is important.

Never do this:

```python
revenue_growth = 0
```

because revenue is unavailable.

Missing means:

```python
None
```

or an equivalent explicit missing value.

Zero means the real observed value is zero.

Do not confuse them.

---

# Eligibility Result

Create something similar to:

```python
class EligibilityResult(BaseModel):
    ticker: str
    eligible: bool
    reasons: list[str]
```

Possible failure reasons:

```text
PRICE_BELOW_MINIMUM

MARKET_CAP_BELOW_MINIMUM

LOW_LIQUIDITY

INACTIVE

UNSUPPORTED_SECURITY_TYPE

MISSING_REQUIRED_DATA
```

A stock can have multiple reasons.

---

# Scanner

Implement:

```python
scan_market()
```

conceptually performing:

```text
Load stock universe

↓

Load latest company/market data

↓

Calculate metrics

↓

Apply eligibility filters

↓

Return eligible companies
```

The result should be sortable/exportable.

Do not calculate Compounder Score.

---

# CLI

Implement a simple command interface.

At minimum:

```bash
python -m radar update-universe
```

```bash
python -m radar update-market
```

```bash
python -m radar update-fundamentals
```

```bash
python -m radar scan
```

and ideally:

```bash
python -m radar run-scan
```

which performs the required sequence.

A simple CLI library is acceptable, but do not overbuild it.

---

# Scanner Output

Console output should be useful.

Example:

```text
Ticker   Market Cap   Rev Growth   Acceleration   Gross Margin   FCF Margin   Net Cash   ADV
------------------------------------------------------------------------------------------------
XYZ      $1.2B        42.5%        +13.0pp        38.2%          6.5%         $82M       $12.4M
ABC      $540M        71.0%        +20.4pp        27.1%         -8.2%         $44M       $4.1M
```

Also support export to:

```text
CSV
```

so results can be inspected manually.

Example:

```bash
python -m radar scan --output eligible_stocks.csv
```

---

# FastAPI

Create a minimal FastAPI application so the project has a clean API foundation.

Only a couple of endpoints are necessary.

For example:

```text
GET /health
```

and:

```text
GET /api/companies
```

or:

```text
GET /api/scan
```

Do not build the complete dashboard API yet.

---

# Configuration

Use environment variables and `.env.example`.

Likely settings:

```text
DATABASE_URL

ALPACA_API_KEY

ALPACA_SECRET_KEY

FUNDAMENTALS_API_KEY

MIN_PRICE

MIN_MARKET_CAP

MIN_AVG_DOLLAR_VOLUME
```

Never commit real credentials.

---

# Logging

Add straightforward logging around:

```text
provider requests

companies processed

failed tickers

missing fundamentals

database writes

scanner summary
```

Do not create a complex observability stack.

---

# Error Handling

One failed ticker should not terminate a full-market scan.

Example:

```text
Process AAPL
success

Process XYZ
provider error

log error
continue
```

At completion report:

```text
Processed: 3,842

Successful: 3,761

Failed: 81

Eligible: 2,418
```

---

# API Rate Limits

Provider calls should be made responsibly.

Implement:

* batching where the provider supports it;
* retry for transient failures;
* basic rate-limit handling;
* configurable delays if necessary.

Do not aggressively parallelize requests in a way likely to violate API limits.

---

# Caching / Incremental Updates

Avoid downloading years of unchanged fundamentals during every scan.

At minimum:

```text
if latest stored financial period matches provider latest period:
    do not duplicate it
```

Price updates should fetch only missing dates when practical.

Keep this simple.

---

# Idempotency

Running:

```bash
python -m radar run-scan
```

twice should not create duplicate financial periods or duplicate daily price rows.

Database uniqueness constraints and proper upsert logic should enforce this.

---

# Tests

Use pytest.

Tests must not require live provider APIs.

Use fixtures and mocks.

---

# Required Metric Tests

Implement tests for:

```text
YoY revenue growth

Growth acceleration

TTM revenue

TTM growth

3-year CAGR

Gross margin

Gross profit growth

Operating margin

FCF margin

Net cash

Share-count growth

20-day average dollar volume

6-month return

12-month return

52-week high/low
```

---

# Required Edge-Case Tests

Test:

```text
missing prior revenue

zero prior revenue

missing quarter

negative FCF

zero revenue

missing share history

fewer than required price bars

duplicate provider records
```

Functions should fail predictably rather than silently producing bad data.

---

# Eligibility Tests

Test:

```text
price = $1.50
→ excluded
```

```text
market cap = $90M
→ excluded
```

```text
ADV = $500K
→ excluded
```

and:

```text
all requirements pass
→ eligible
```

Also verify that multiple exclusion reasons can coexist.

---

# Provider Tests

Mock provider responses and verify normalization.

Provider-specific field names should never leak into metric calculations.

For example, the Metrics Engine should see:

```text
revenue
```

not:

```text
vendorField123RevenueTTM
```

---

# Documentation

Update README with:

```text
What Compounder Radar is

Phase 1 scope

Architecture

Local setup

Environment variables

Database setup

How to run migrations

How to load data

How to run scanner

How to run tests
```

Include one example scan output.

---

# Code Quality

Use:

* type hints;
* clear naming;
* small functions;
* meaningful tests.

Avoid:

* giant service classes;
* unnecessary factories;
* excessive inheritance;
* magic constants;
* duplicated calculations.

A future developer should be able to understand the metric engine quickly.

---

# Important Boundary

Do **not** implement Phase 2.

Specifically do not build:

```text
Growth Score

Financial Quality Score

Valuation Score

Momentum Score

Risk Penalty

Compounder Score

Hidden Gems

Improving Fast

AI Research

Next.js Dashboard
```

You may create interfaces/placeholders only when genuinely needed to prevent architectural dead ends.

Do not create fake implementations of future functionality.

---

# Implementation Workflow

Work in this order.

## Step 1

Inspect the existing repository completely before changing anything.

Understand:

```text
existing architecture

dependencies

configuration

database

provider code

tests
```

Do not duplicate functionality that already exists.

---

## Step 2

Write a short implementation plan.

Identify:

```text
files to create

files to modify

schema changes

provider interfaces

metric functions

tests
```

Then implement.

---

## Step 3

Build data models and database migrations.

Verify migrations work from a clean database.

---

## Step 4

Build provider interfaces and mocks.

---

## Step 5

Implement ingestion.

---

## Step 6

Implement metric calculations as pure functions where practical.

---

## Step 7

Implement eligibility scanner.

---

## Step 8

Implement CLI.

---

## Step 9

Add minimal API.

---

## Step 10

Complete tests and documentation.

---

# Required Final Review

Before declaring the phase complete, review the entire implementation yourself.

Specifically inspect for:

```text
incorrect financial formulas

sign errors in FCF

percentage vs percentage-point confusion

missing-data treated as zero

duplicate database records

provider leakage

hard-coded API credentials

hard-coded eligibility thresholds

unhandled failed provider calls

incorrect date ordering

tests that accidentally use live APIs
```

Fix issues before presenting the result.

---

# Final Response Format

When implementation is complete, provide:

## 1. What Was Built

Short explanation.

## 2. Architecture

Important modules and responsibilities.

## 3. Database Changes

Tables/migrations added.

## 4. Metrics Implemented

List all calculations.

## 5. Provider Integrations

What is real vs mocked.

## 6. CLI Commands

Exact commands I can run.

## 7. Tests

How many passed and what they cover.

## 8. Known Limitations

Anything intentionally deferred.

## 9. Phase 2 Readiness

Explain whether the data produced is sufficient to implement scoring next.

Do not begin Phase 2.
