---
type: reference
title: Backtest readiness
---

# Backtest readiness

What has to be true before a strategy may be studied over history, which of
those things are built, and which only accumulate with time. This page is a
**state record**, checkable against the code and the tests named in it. It does
not describe a strategy, and nothing here calibrates a threshold or admits a
signal into a decision — those are later phases and are not started.

The single entry point is the harness in `stock_screener.backtest`. For one
security on one date it answers the seven questions a survivorship-aware study
must have answered before its first bar, and — the point of it — names every
place the record cannot answer, so a study reads a stated absence rather than a
confident wrong number.

## The seven questions

`security_readiness(session, settings, as_of, ticker)` returns a
`SecurityReadiness` whose fields answer, in order:

| # | Question | Answered by | Field |
| --- | --- | --- | --- |
| 1 | Was it in the universe on the date? | `reconstruct_universe` | `in_universe`, `membership_kind` |
| 2 | What fundamentals were filed by the date? | `build_metrics_as_of` | `fundamentals_known`, `periods_used` |
| 3 | What market cap was knowable on the date? | point-in-time recovery | `market_cap` |
| 4 | What CompounderScore existed on the date? | `reconstruct_score` | `score_status`, `final_score`, `score_is_reconstructed`, `score_was_captured` |
| 5 | What signals were derived for the date? | `pit_signal_snapshots` | `signals_present` |
| 6 | What did it do over 1/5/20/60 sessions? | `domain.forward_returns` | `forward_outcomes` |
| 7 | If it left the market, how? | `delistings` + `classify_delisting` | `delisting` |

Every unanswerable part appends to `gaps`; `answerable` is true only when `gaps`
is empty. A delisted security is still answerable — its fate is a recorded fact
— which is exactly the row a study handling survivorship wants.

## What is built

### Delisting reasons are first-class (blocker 1)

`domain.DelistingReason` names the fate a study reasons about, separate from the
settlement mechanism a provider reports:

- **`ACQUIRED`** — any merger, cash or shares. The holder received consideration.
- **`BANKRUPT`** — a worthless removal. The extinguished equity is the reported
  fact, so -100% is a measurement, not an assumption.
- **`DELISTED`** — removed from the exchange while the company continued. The
  shares may have kept value the tape stopped recording, so no return is implied.
- **`RECEIVERSHIP`** — seized by a regulator. No action feed files this (SVB and
  First Republic appear under none), so it is **only** enterable by asserted
  evidence a caller supplies, never inferred.
- **`UNKNOWN`** — the security left and the source does not establish why. The
  honest default, never collapsed into a neighbour to tidy a total.

`classify_delisting` derives the reason from the stored corporate-action kind.
It is derived on read rather than persisted: the reason is a pure function of a
column the `delistings` table already holds, and a second stored copy could only
disagree with it.

### Historical universe membership (blocker 2)

`reconstruct_universe(session, as_of)` rebuilds the investable set for a past
date from the append-only `universe_events` record and the delisting registry.
The rule, in `domain.universe_members_as_of`, is one sentence: **the latest
event dated on or before `as_of` decides a company's state, and a company whose
only record begins after `as_of` is *undetermined* rather than assumed present.**

Each member carries how its presence is known:

- **`OBSERVED`** — a transition the capture pass witnessed on or before the date.
- **`BASELINE`** — the state as first seen, present but with no claim about how
  long. A universe made entirely of baselines is one with no membership history
  yet, and `assess_universe_readiness` flags it as `BASELINE_ONLY_UNIVERSE`.

A security delisted before the date is excluded even when its company row and its
active baseline event both survive — proven by
`test_a_security_delisted_before_the_date_is_not_a_member`. The same security is
a member on a date before it left — `test_the_same_security_is_a_member_before_it_left`.

### Historical CompounderScore (blocker 3)

`reconstruct_score(session, settings, company_id, as_of)` rebuilds the score the
frozen formula **would have** produced on a past date, from point-in-time inputs
only. It is possible because `domain.score_company` is a pure function of one
company's own inputs — profile, metrics, and the broad-market return shared by
every company that day. **There is no cross-sectional term**: no universe
percentile, no peer rank. A single company's past score needs a single company's
past data, and the whole market need not be reconstructed first.

Reconstruction changes no number and mints no version. The metrics come from
`build_metrics_as_of` (the provider's normalisation and the frozen arithmetic,
unchanged); eligibility from the frozen `evaluate_eligibility`; the score from
the frozen `score_company`. This module only bounds each input to the date.

What a reconstruction needs, and has:

1. **Fundamentals** filed by the date — the `fundamental_observations` record,
   selected by `filed_date`.
2. **A point-in-time market cap** — the filed share count times the price
   recovered from the stored close with the split factors since. Proven by
   `test_a_point_in_time_market_cap_is_recovered_from_shares_and_price`.
3. **A point-in-time benchmark return** — `benchmark_returns_as_of` drops the
   bars after the date before measuring 6- and 12-month returns.

The one honest gap: sector, activity flag and statement profile are read from the
company's current row, because the project keeps no history of them. A
reclassification since the date is therefore applied retroactively. The result is
labelled a *reconstruction*, and `score_was_captured` distinguishes it from a
score that was actually recorded on the day by a capture run.

**Two paths, both valid.** `pit_score_snapshots` records what the system *said*
on a day and grows forward one capture at a time; reconstruction computes what it
*would have said* and reaches back as far as the observation record does. A date
before capture began has only the second.

### Market signals history (blocker 4)

Signals are backfillable from stored prices, with no provider and no look-ahead.
`capture_day(session, settings, snapshot_date=D)` computes features from price
history bounded to `D`, and `derive_market_signals(session, snapshot_date=D)`
reads that day's features and writes the signals. Both accept an arbitrary past
date. Proven by `test_signals_backfill_for_a_past_date_from_stored_prices`, which
also asserts the feature watermark never reaches past `D`.

For a truly past date `capture_day` writes **features only** — it refuses to copy
a current operational score onto a past date, so a backfill cannot smuggle
today's score into yesterday.

**No threshold is calibrated here.** The signal thresholds remain provisional in
`domain.signals.SignalThresholds`; this phase makes history *exist*, not decide.

## Identity: a ticker is not a security

Three of the four tables this harness joins are keyed by ticker, because that is
all a delisting carries and `companies` holds no CIK. A ticker is not an
identity, and two places treated it as one.

`CZR` is the worked example. Caesars Entertainment Corporation left the market
on 2020-07-20 in a cash merger with Eldorado Resorts, and the survivor took the
symbol. `CZR` prints bars every session since. Joined on ticker alone, the 2020
exit attaches to the company trading today, and:

- `reconstruct_universe` removed it from **every** universe after 2020-07-20 —
  along with 126 other live companies whose symbols had been reused.
- `security_readiness` reported it as `ACQUIRED on 2020-07-20`, a live company
  described as having been bought four years ago.

**The price record adjudicates.** A company still printing bars after a
delisting's effective date demonstrably did not stop trading then, so the exit
belongs to a different issuer and is not applied to it. A company with no bars
cannot refute the delisting and stays excluded, which is the conservative
direction: a name wrongly dropped costs coverage, and a dead name wrongly kept is
survivorship bias.

This is a data-quality fix in the audit layer, not a policy change. No score, no
signal and no version moved; `reconstruct_universe` and `security_readiness` are
the only callers.

## Building the record over past sessions

`stock_screener.backtest.backfill` runs the *same* daily passes over sessions
that have already happened. `capture_day`, `derive_market_signals` and
`measure_forward_returns` are called unchanged, with the arguments a daily run
would pass; the module is the loop around them and the four properties that loop
has to have.

| Property | How |
| --- | --- |
| **Idempotent** | Every underlying pass already refuses to rewrite a day. The runner adds nothing but a decision not to treat "already done" as an error. |
| **Resumable** | Each session is its own transaction. A run killed halfway leaves every completed day intact and the day in flight absent; the record is the cursor. |
| **Observable** | Each date reports what it wrote as it lands, through a progress callback. |
| **Safe to rerun** | Nothing updates or deletes. A second run over the same range writes zero rows and says so. |

Two commands drive it: `backfill-signals --start … --cadence daily|weekly|monthly`
and `backfill-returns`. Both report the sessions that still hold no rows, because
a backfill that claims to have finished while leaving dates empty has not.

### Why a price cache, and why it changes nothing

The naive loop asks the database for a company's bars once per company per date:
six and a half million queries over the same six million rows to describe 1,150
sessions. `CachedPriceSource` reads the record once and slices it by binary
search. The bars are the same bars, under the same `as_of` bound — proven
directly rather than argued: 2023-06-30 was captured through the repository path,
recomputed through the cache, and compared field by field across **223,146 values
over 4,554 securities with zero differences**. The equivalence is also held by
`test_the_cached_source_and_the_repository_agree_bar_for_bar`.

The measured cost: about 34 seconds to load 6.8 million bars into roughly 3.7 GB,
then **≈10 s per session** for features and signals over 5,769 companies, and
**≈2.9 s per decision date** for forward returns at five horizons.

### What a backfilled capture deliberately does not write

**No membership history.** `companies` holds today's roster, so a universe event
written for a past date would assert that today's survivors were present then —
the exact survivorship bias the point-in-time record exists to remove. A capture
for a past date writes features and nothing else, because the state it would
record was already first seen on a later day. Held by
`test_a_backfilled_capture_invents_no_membership_history`, and confirmed on
staging: 1,170 backfilled captures wrote **zero** universe events.

**No score.** For a genuinely past date `capture_day` refuses to copy a current
operational score onto it, so a backfill cannot smuggle today's score into
yesterday. The past score comes from `reconstruct_score` instead, and is labelled
a reconstruction.

## The daily-accumulation process

The point-in-time record grows by running these passes each day, in order. They
are separate jobs, not folded into `run-daily`, because each writes an immutable
record and must not be entangled with the operational re-scoring:

```
update-observations   # append the day's filings to the observation record
update-acceptance     # resolve each filing's acceptance instant
update-actions        # ingest splits and distributions
capture               # write the day's immutable score and feature snapshots
derive-signals        # derive the day's market signals from the features
measure-returns       # measure forward returns that have now elapsed
discover-delistings    # enumerate securities that left the market
```

Scheduling is out of scope and deliberately not built. To backfill history
rather than accumulate it forward, use `backfill-signals` and `backfill-returns`
over a date range — see *Building the record over past sessions* above.

## What only time and data can close

These are not engineering gaps. The mechanisms exist and are tested; what they
lack is history, and history accrues only by running the passes above day after
day.

| Gap | Why it is a time/data gap |
| --- | --- |
| **Universe membership does not reach back at all.** Every company's membership record begins on the day capture first ran, so `universe_members_as_of` returns *undetermined* — not baseline — for every earlier date. The historical universe is empty, and no amount of backfilling changes that, because the only honest source for a past roster is an observation made at the time. | The record forbids inventing pre-baseline history. Only forward observation fills it, one day at a time from the first capture onward. |
| **A delisted ticker cannot be resolved to a filer.** EDGAR's current `company_tickers.json` maps 177 of 5,448 delisted symbols, and almost all 177 are symbols since reissued to a *different* filer. Without a CIK there is no identity, and without an identity a delisted name cannot be ingested. | A source limitation. The SEC publishes no historical ticker-to-CIK map. Form 25/25-NSE delisting notices name the issuer and recover a minority of them. |
| **Delisted securities have no stored price history** unless reconstructed into `historical_securities`, so their forward returns are `INTERRUPTED` rather than measured from a recovered path. | The provider serves the bars; the project does not store them for names without a company row. Closing this needs a data operation, not a code change. |
| **Receivership is uncovered by the action feed.** | A source limitation. `RECEIVERSHIP` exists to be asserted from evidence, but no feed supplies it. |

## The measured state of the record

Everything here was produced on `stage9.db`, a throwaway copy. The primary
database was not opened; its SHA-256 is unchanged.

| Layer | Coverage |
| --- | --- |
| Forward returns | 1,064 daily decision dates, 2022-01-03 → 2026-03-31, **30,669,800 measurements** over 5,765 securities. 0 failed, 0 missing. |
| Market signals | 56 month-end sessions, 2022-01-31 → 2026-08-28, **282,998 rows**. 0 failed, 0 missing. The daily pass over 1,168 sessions runs at ≈10 s a session. |
| Historical securities | **402 ingested**, 400 with fundamentals, 402 with prices, 291 with a stated terminal value. 0 failures. |
| Usable securities | **51 consecutive month-ends**, median **1,698** per date (min 1,533, max 1,793). |
| Universe membership | **0 members on every date before 2026-08-26.** Not baseline — undetermined. |

### Look-ahead, checked rather than assumed

Across all 30.67 M measurements: **0** rows with an entry on or before the
decision date, **0** with an exit on or before the entry, **0** carrying a
number without `REALISED`, **0** marked `REALISED` without both prices. A
200,000-row sample confirms every horizon spans exactly *N* benchmark sessions.
The last decision date with all five horizons realised is **2026-03-06**.

### Where the securities go

On 2024-06-28, of 5,769 companies: 4,720 listed, 4,520 with a trading year of
bars, 4,512 with fundamentals filed by the date, 3,529 with a point-in-time
market capitalisation, and **1,688 that the frozen formula scores**. The
remainder are `INSUFFICIENT_DATA` (1,833), `NOT_ELIGIBLE` (1,881) and
`UNSUPPORTED_SECTOR` (367) — the policy working, not a defect. The intersection
with signals and realised returns is **1,682 fully usable**, 29.2% of the roster.

### What the census says about survivorship

3,801 securities left the market over the window. The record holds **22** of
those exits as observable events. The delisted names' prices are recoverable —
the free provider serves them by ticker, verified on ATRS, SWIR, SIVB and
BBBY — but `price_history` needs a `companies` row and
`historical_securities.cik` is `NOT NULL`, while the free record establishes a
CIK for only **12.5%** of delisted tickers.

**638 of 5,765 companies (11.1%)** have a price series with a discontinuity
longer than 120 sessions; 34 have a delisting recorded inside it. `companies`
row 2147 is the proven case — Bed Bath & Beyond's collapse and an unrelated
company from 2025-07-14, under one ticker and one `company_id`.

## Regression coverage

| Test | Protects |
| --- | --- |
| `packages/domain/tests/test_survivorship.py` | the reason taxonomy, the as-of membership rule, and the readiness gaps |
| `tests/integration/test_backtest_readiness.py::test_a_security_delisted_before_the_date_is_not_a_member` | survivorship: a delisted name leaves the past universe |
| `tests/integration/test_backtest_readiness.py::test_a_filing_becomes_visible_only_after_it_was_filed` | look-ahead: the filing date is the only boundary |
| `tests/integration/test_backtest_readiness.py::test_a_delisted_only_security_is_answerable_through_its_fate` | a security with no company row never vanishes |
| `tests/integration/test_backtest_readiness.py::test_a_security_that_stops_trading_has_an_interrupted_outcome` | a fabricated zero is never returned for a name that stopped trading |
| `tests/integration/test_backtest_readiness.py::test_signals_backfill_for_a_past_date_from_stored_prices` | signals are backfillable, point-in-time |
| `tests/integration/test_backtest_readiness.py::test_a_reused_ticker_does_not_retire_the_company_that_inherited_it` | identity: one issuer's exit is not another's |
| `tests/integration/test_backtest_readiness.py::test_a_company_that_stopped_trading_at_its_delisting_is_still_retired` | the price guard does not rescue a genuinely dead name |
| `tests/integration/test_backtest_readiness.py::test_a_company_with_no_price_record_cannot_refute_its_delisting` | silence is not a refutation |
| `tests/integration/test_backtest_readiness.py::test_a_live_company_does_not_inherit_the_fate_of_a_previous_symbol_holder` | the same guard on the readiness answer |
| `tests/integration/test_backtest_readiness.py::test_an_acquired_company_is_a_member_before_and_absent_after` | an acquisition inside the period is not continuous life |
| `tests/integration/test_backfill.py` | the backfill's four properties, and the cached price source's equivalence to the repository |
| `tests/integration/test_backfill.py::test_a_backfilled_capture_invents_no_membership_history` | a backfill never asserts today's roster into the past |
| `tests/integration/test_research_coverage.py` | the coverage funnel: membership reported not assumed, exits counted over the whole registry |
