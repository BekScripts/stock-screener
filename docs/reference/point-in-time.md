---
type: reference
title: Point-in-time metrics
---

# Point-in-time metrics

Fundamental metrics as they stood on a past date, built from what had actually
been filed by then.

## Why the operational path cannot answer this

`financial_snapshots` holds one row per company per period and is overwritten
when a filing restates it. That is correct for a screener — the newest figure is
the best estimate of the truth — and it makes the table unable to answer a
historical question, because the figure a past decision saw has been replaced by
the figure that came later.

`fundamental_observations` appends instead. Every filing's version of every
figure survives, carrying the date it became public.

## `as_of` semantics

```text
as_of  (a date, meaning the close of that trading day)
  |  observations accepted at or before 16:00 America/New_York on as_of
  |  provider normalisation, unchanged             (shared with the live path)
  |  FinancialPeriod
  |  domain.build_company_metrics, unchanged       (frozen arithmetic)
  v  CompanyMetrics
```

**`as_of` is a date, and it denotes a decision taken at the close of that
trading day — 16:00 in America/New_York.** That is a choice, made for three
reasons: the capture pass already runs after the close and stamps a UTC calendar
date, so a close-of-day reading keeps these dates in step with `snapshot_date`
and `score_date`; a daily trader decides on closing information and acts at the
next open; and an open-of-day reading would discard the whole session's
information for nothing, since nothing here acts intraday.

### Why a date comparison cannot express it

A filing date says nothing about the hour. Measured across 2,546 XBRL-bearing
filings from 45 companies, of those accepted on their stated filing date:

| Accepted | Share |
| --- | --- |
| Before the open | 26.4% |
| During the session | 27.9% |
| **After the 16:00 close** | **45.7%** |

So `filed_date <= as_of` shows a close-of-day decision roughly 46% of that day's
filings before they were public, and `filed_date < as_of` hides the 54% that
genuinely were. **Neither operator is a correct availability test.** Swapping one
for the other trades a look-ahead error for an omission error.

The correct test compares instants: a filing is available if and only if the SEC
accepted it at or before the close of `as_of`. `filing_acceptance` stores that
instant per accession, and every observation carries an accession, so the two
join exactly.

### The rules

- **Acceptance time is the test.** `accepted_at <= close_of(as_of)`.
- **`filed_date` is a fallback, not the test.** Where no acceptance time is
  stored, the filing must predate `as_of` outright — treated as though it
  arrived after that day's close. Conservative: it can withhold a filing for a
  day, and can never reveal one early.
- **`period_end` is never a proxy for availability.** No fallback, at any level.
- **An observation with no `filed_date` is never selected.**
- **The close follows daylight saving.** 20:00 UTC in summer, 21:00 in winter. A
  fixed offset would be an hour wrong for two-thirds of the year.
- **Price bars carry the same date bound**, via `list_for_company(until=as_of)`.

### What is not modelled

There is **no market calendar**. A date that was not a trading day still yields
an instant, so asking about a Sunday gets Sunday's close — harmless for an
availability test, wrong for anything claiming to describe a session. Holidays
and early closes (13:00 the day after Thanksgiving, for instance) are not
represented, so an early-close day is treated as a 16:00 day and a filing
accepted between the two would be admitted about three hours early.

## Restatement behaviour

Given revenue reported twice for the same quarter:

| Filed | Value |
| --- | --- |
| 2026-02-15 | 100M |
| 2026-04-30 | 104M |

| Query | Sees |
| --- | --- |
| `as_of 2026-01-15` | nothing — not yet filed |
| `as_of 2026-03-01` | **100M** — the figure that stood then |
| `as_of 2026-05-01` | **104M** — the restatement is now public |

Neither observation is destroyed and neither is preferred in general; each date
gets the latest figure available *to it*.

## Facts with different filing dates

A metric usually needs several concepts, and they rarely arrive together. Each
concept is resolved independently against the same cutoff, then normalised.

The consequence is deliberate: **a metric waits for its slowest input.** If
revenue was filed in February and gross profit in April, a March query has
revenue and no gross margin — because in March there was no gross margin to
compute. Substituting a later gross profit would be look-ahead in its purest
form.

Restating one concept moves only that concept. A revenue revision leaves the
gross profit filed beside it untouched.

## Relationship to the operational metrics

Both use **the same definitions**. Gross margin is gross profit over revenue in
both, computed by the same `build_company_metrics`, from periods assembled by the
same normalisation. Only the source and its temporal bound differ.

There is therefore **no separate metric version**. A version records that a
calculation changed; nothing here changed one. See
[ADR-0012](../adr/0012-point-in-time-metrics-reuse-the-provider-normalisation.md).

A point-in-time metric for a past date will often differ from what the
operational path reports for that date today. That is the point: one is built
from what was known then, the other from what is known now.

## Relationship to Phases 8B and 8C

| Phase | Provides | This phase uses |
| --- | --- | --- |
| 8B capture | `fundamental_observations`, append-only with `filed_date` | The source of every historical fundamental |
| 8B capture | `pit_feature_snapshots`, immutable daily market state | Unchanged — market features were already point-in-time |
| 8C signals | `pit_signal_snapshots`, derived market state | Unchanged — signals read features, never fundamentals |

Phase 8C is untouched by this work. Market signals describe price behaviour and
have no fundamental input; nothing here reaches them.

## What this does **not** enable

**A point-in-time metric engine does not create a backtest.**

Being able to ask what the fundamentals looked like on a date is one input to a
historical study. It is not the study, and several other things still block one:

| Still missing | Consequence |
| --- | --- |
| **Acceptance coverage** | A filing with no stored acceptance time falls back to the conservative date rule and is withheld a day longer than necessary. `update-acceptance` resolves them. |
| **Historical universe membership** | `companies` holds what is investable today. Delistings are now discoverable and stored, but they are not company rows and no snapshot before 2026 was taken over the full historical set. |
| **Consolidated liquidity** | A proven floor or unknown. No historical series. |
| **Score history** | The capture record holds one score date. Market features and signals backfill; scores do not. |

The honest summary: the repository can now construct historical fundamental
metrics correctly, value them at a past date, and measure what followed. It
still cannot construct a survivorship-free backtest, because the score half of
the record has one day in it and the universe it ran over excludes everything
that has since ceased to exist.

## Market capitalisation on a past date

Solved, on the split axis. `domain.pricing` recovers the price a security
actually traded at from the stored close and the split factors since, and
`build_metrics_as_of` withholds the profile's market capitalisation entirely —
it is a current quote, and using it would value a 2019 snapshot at today's
price.

Two subtleties carry the correctness:

**The anchor is the share count's date, not the valuation's.** AAPL split
four-for-one on 2020-08-31 and did not file again until 2020-10-30. For those
two months the newest available count is 4.28bn pre-split shares and the price
has already quartered. Anchoring on the valuation date applies no factor —
the split is in the past — and values Apple at $557bn. Anchoring on the count's
own cover date applies the four across the whole window, which is what every
market participant did at the time.

**Distributions are not undone.** `adjustment=all` removes them too, and this
layer leaves that removal in place, so a recovered price is low by the
cumulative distribution yield since the date: about 3% for AAPL over six years,
more for a high-yield security. A recovered price is a **lower bound**, stated
here rather than hidden. Removing it needs a raw price series, which the
provider serves and this project does not store.

Verified against staging:

| Date | Stored close | Point-in-time market cap |
| --- | --- | --- |
| 2020-08-28 | $120.98 | $2,069bn |
| 2020-09-01 | $130.31 | $2,229bn |
| 2021-01-04 | $125.80 | $2,139bn |
| 2023-06-30 | $191.07 | $3,005bn |

## Forward returns

`forward_returns` stores what a security did at 1, 5, 20, 60 and 120 sessions
from an observation date.

**The entry is the next session's open.** A snapshot for date D is computable
only once D has closed, so a decision taken on it cannot be filled at D's close;
the first price a holder of that snapshot could transact at is the open of D+1.
Close-to-close is the more common convention and is not available here, because
it would pay the entry at a price the decision depended on.

**The horizon is counted on the market's calendar**, taken from the benchmark
series. Counting a security's own bars would let one that stopped trading for a
month appear to reach its horizon on time.

**A ratio of adjusted prices is a total return.** The basis that makes a stored
close useless for a market capitalisation is what makes it right here:
distributions are reflected on both ends and a split inside the horizon is a
non-event. The two modules want opposite things from the same adjustment.

Four statuses, and three of them carry no number on purpose:

| Status | Means |
| --- | --- |
| `REALISED` | Both ends priced. A measurement. |
| `TRUNCATED` | The horizon has not elapsed. Will realise on a later run. |
| `INTERRUPTED` | The market kept trading and the security did not. No price series can say whether that was an acquisition or a failure. |
| `UNPRICED` | The security was still trading and we have no bar. A gap in our record, not an event in the market — kept apart so a count of delistings never counts our missing rows. |
| `NO_ENTRY` | No first price at all. |

## The survivorship measurement

Running the pass over staging produced the finding that governs everything else:

> Across 4,599 companies and the 120 sessions after 2024-01-02, **not one
> security stopped trading.**

A universe that size loses roughly a hundred names to acquisitions and failures
over six months. The zero is a property of the `companies` table, which holds
what is investable today and therefore cannot contain anything that stopped
being so. No amount of care in the point-in-time layers repairs it, because the
rows were never there to bound.

## Delistings

`delistings` holds what `companies` structurally cannot. Keyed by ticker rather
than `company_id`: giving each delisted security a company row would fix the
join by putting thousands of untradeable tickers into the table the scanner, the
rankings and the dashboard all read as the investable set.

**Discovery is by date range, not by symbol.** Asking a provider about a symbol
can only confirm the fate of one already known, which is no help when the
missing securities are exactly those the database never had. The
corporate-action endpoint also answers without a symbol filter, and that is what
makes enumeration possible.

The provider's own inactive-asset list does not: of twenty well-known large-cap
delistings, **eighteen appear in neither the active nor the inactive list** —
Twitter, Activision, VMware, Seagen, Splunk, Pioneer, Hess, Ansys, Juniper among
them. The corporate-action record has all of them, with terminal values.

Discovered over 2020–2026 against staging: 5,796 delistings, 5,448 distinct
tickers, of which 2,199 carry a cash value and 436 are worthless removals.

| Ticker | Kind | Effective | Terminal value |
| --- | --- | --- | --- |
| ATVI | Cash merger | 2023-10-13 | $95.00 |
| SGEN | Cash merger | 2023-12-14 | $229.00 |
| SPLK | Cash merger | 2024-03-18 | $157.00 |
| PXD | Stock merger | 2024-05-03 | 2.3234 XOM |
| HES | Stock merger | 2024-09-30 | 1.025 CVX |
| ANSS | Stock and cash | 2025-07-18 | $199.91 + 0.3399 SNPS |

**What it does not cover: receivership.** SVB and First Republic ended in FDIC
receivership rather than a merger or removal, and neither appears under any
type. Bank failures are a category this source does not report, and the gap is
stated rather than filled.

**Ticker reuse is real.** 138 delisted tickers are live symbols today belonging
to different companies, so a study joining on ticker across time will attribute
one company's history to another unless it resolves through this table.

## Usage

```python
from stock_screener.pit import build_metrics_as_of, periods_as_of

# Periods as they would have been assembled on that date.
periods = periods_as_of(session, company_id, date(2026, 3, 1))

# Full metrics, with fundamentals and price bars bounded by the same date.
result = build_metrics_as_of(session, company_id, date(2026, 3, 1))
result.metrics  # CompanyMetrics, or None if nothing was filed
result.observations_available  # how many observations that date could see
```

`build_metrics_as_of` performs no I/O beyond reading the database, calls no
provider, and never touches `financial_snapshots`.
