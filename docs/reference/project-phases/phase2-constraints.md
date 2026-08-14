---
type: reference
title: Phase 2 constraints
---

# Phase 2 constraints

Decisions taken during the Phase 1 review that bind the scoring implementation.
None of them are implemented here — Phase 1 contains no scoring code — but each
one is a commitment Phase 2 inherits, recorded so it is not re-litigated or
quietly broken.

## Provider boundary

Scoring reads **normalised fields only**:

```text
revenue  gross_profit  operating_income  free_cash_flow
cash     debt          shares            price          market_cap
```

It must never know, or branch on, whether a value came from Alpaca, FMP or
EDGAR. The three providers are deliberate and complementary:

| Provider | Supplies |
| --- | --- |
| Alpaca | Price history, OHLCV, returns, 52-week range, and the benchmark series Phase 2 adds |
| FMP | Market capitalisation, sector, industry, consolidated average volume — the structured fields available under the current plan |
| EDGAR | Reported statements from XBRL, the fallback wherever FMP's plan does not reach, and later the filings themselves |

The composite policy stays small: a field reliably available from FMP is
normalised and used; a field FMP cannot serve comes from EDGAR. Where the two
materially disagree on an important field, prefer the primary filing or log the
discrepancy — **do not average them**, and do not build a reconciliation engine.

## Missing values are never favourable

The rule that runs through Phase 1 extends into scoring. A metric that is `None`
must not become `0`, and must not earn a company either the best or the worst
outcome by default.

| Field | May be `None` because |
| --- | --- |
| `share_count_growth_yoy` | Weighted averages cannot be differenced, so EDGAR yields no fiscal-Q4 share count |
| `total_debt`, `net_cash` | Absence is unknown, not zero — see [ADR-0005](../../adr/0005-absent-debt-is-unknown-not-zero.md) |
| `average_dollar_volume_20d` | May be partial-market — see [ADR-0004](../../adr/0004-apply-the-liquidity-threshold-only-to-consolidated-volume.md) |
| `revenue_cagr_3y`, `ttm_revenue_growth` | Insufficient history |

Unavailable dilution in particular must not read as 0% and must not hand the
company the full dilution-risk benefit. Score it as not assessed.

`EligibilityWarning.LIQUIDITY_UNVERIFIED` is a statement about the data, not
about the company, and is not a scoring input.

## Valuation adapts to the company

Not every multiple must exist before valuation can score. A negative P/E on an
unprofitable growth company is **not a defect** and must not penalise it.

| Company shape | Prefer |
| --- | --- |
| Unprofitable growth | EV/Revenue, Price/Sales, FCF yield where meaningful, growth-adjusted valuation |
| Profitable | Additionally P/E, EV/EBITDA, FCF yield |

EBITDA is not a mandatory input if sourcing it reliably would add
disproportionate complexity. Select whichever metrics are economically
meaningful for the company and do not penalise a company for a multiple that
does not apply to it.

## Enterprise value is computed, not fetched

```text
enterprise_value = market_cap + debt - cash
```

All three are already normalised and stored, so there is no need to depend on a
vendor endpoint that may be unavailable under the current plan.

EV must be `None` when debt or cash is unknown. Substituting zero for missing
debt to make EV calculable would reintroduce the failure ADR-0005 exists to
prevent.

## Financial companies are out of scope for v1

Banks and similar institutions have accounting economics that make EV/Revenue,
net debt and FCF margin misleading. Mark them rather than forcing the general
model onto them:

```text
SCORING_STATUS = UNSUPPORTED_SECTOR
```

They stay in the company universe. Do not build a bank-specific model yet.

## Debt risk is contextual, not absolute

Ford carries roughly $132bn of net debt because of its financing arm — that is
the business model, not distress. A penalty keyed to an absolute dollar figure
would rank companies by size rather than by risk. Keep the MVP logic simple, but
scale debt against something (revenue, market cap, EBITDA) rather than comparing
raw dollars across companies.

## Benchmark data

Add one broad-market benchmark price series through Alpaca and use it
consistently, so relative strength is:

```text
6-month stock return  −  6-month benchmark return
12-month stock return −  12-month benchmark return
```

## Score persistence

Scores belong in a new `score_snapshots` table, one row per company per day.
**Do not add scoring columns to `financial_snapshots`** — that table holds
reported facts, and mixing derived opinion into it would make a restatement
indistinguishable from a re-score. The snapshot table is what makes the 7-day and
30-day score changes, and the Improving Fast view, possible.

## Do not optimise the scanner

Two queries per company, roughly eleven seconds for four thousand companies, is
acceptable for a nightly personal tool. Do not add batching, caching or other
infrastructure for it unless profiling later shows a real problem.

## Related

- [Phase 1 brief](phase1.md)
- [Metrics](../metrics.md) — every formula and its missing-data rule
- [ADR-0003](../../adr/0003-normalise-provider-data-at-the-boundary.md) — the provider boundary
