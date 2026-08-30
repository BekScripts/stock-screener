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
| **Point-in-time market capitalisation** | Read from the mutable company profile; nothing records what it was on a past date. Any ratio against it — valuation multiples, enterprise value — is point-in-time on its fundamental side only. |
| **Corporate actions** | The table exists and is empty. Adjusted closes are still rewritten retroactively, so a past price series cannot be rebuilt as it appeared. |
| **Historical universe membership** | Recorded forward from 2026-08-29 only. Anything earlier is survivorship-biased. |
| **Consolidated liquidity** | A proven floor or unknown. No historical series. |
| **Forward returns** | No outcome or label exists on any table. A decision cannot yet be joined to what happened next. |
| **History** | The point-in-time record holds one day. Nothing calibrated against it is meaningful. |

The honest summary: the repository can now construct historical fundamental
metrics correctly **once the observation record is populated**. It still cannot
construct an honest backtest.

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
