---
type: reference
---

# Phase 7 — international coverage

Foreign private issuers now flow through the **same** pipeline as every other
company: the same ingestion, the same metric engine, the same CompounderScore
V1.1, the same rankings. There is no international score, no second fundamentals
engine and no special case for any ticker.

Everything Phase 7A found lived in the input layer, and that is where all of it
was fixed. **No curve, weight, threshold, coverage rule or penalty changed.**

## What 7B delivered

### The reporting currency is read, never assumed

`SecEdgarFundamentals` hardcoded `USD` in two places: the profile's `currency`
and every period's `reported_currency`. Both now come from the unit key the
filer actually tagged, picked as the money unit carrying the most revenue facts.

That last detail matters. TSM tags 26 revenue facts in TWD and 9 in USD, the USD
column being a courtesy restatement at one year-end spot rate. Reading it would
mix two exchange rates into every growth figure — TSM's FY2024 revenue grew
33.9% in TWD and 25.0% in USD, and neither number is wrong, they are answers to
different questions. The currency the company reports in wins on fact count.

This is the guard, not the feature, and it shipped first for that reason. With
it in place `UNSUPPORTED_CURRENCY` fires correctly for the first time.

### The unit is threaded through every read

Every concept was read from the `USD` unit key, so a filer reporting in euros
produced nothing at all. ASML is the worked example: its statements are tagged
with `us-gaap` concept names that **all twelve** existing chains already match,
and it still yielded zero periods because the facts are in `EUR`.

### `ifrs-full` is a second concept table, not a second code path

The reading machinery already took a facts block and a unit as arguments, so
supporting IFRS needed a table rather than a resolver class. `_ConceptSet` is a
record of constants; `_detect_concept_set` picks one per company.

Detection ranks taxonomies by revenue facts rather than taking the first that
exists, because a filer that changed taxonomy keeps the old block forever —
Telus carries one us-gaap concept with four facts from 2018 beside 269 ifrs-full
concepts with eight thousand.

### Borrowings and capital expenditure

Both needed real logic, and both were verified against the filed balance sheets
rather than reasoned about:

| Filer | Total debt | How it is reached |
| --- | --- | --- |
| TSM FY2024 | NT$1,018,286.8m | No stated total. Non-current bonds + non-current borrowings + current maturities |
| SAP FY2025 | EUR 6,150.0m | `Borrowings` stated; its own `BondsIssued` of EUR 5,294.0m is part of it |
| NVO FY2025 | DKK 130,958.0m | `Borrowings` stated; `BondsIssued` of DKK 121,074.0m is part of it |

Two mistakes are guarded explicitly. Reading `LongtermBorrowings` alone for TSM
returns 3% of its debt and a nearly debt-free company. Adding its current bonds
to its current maturities double-counts NT$57.1bn, because the balance sheet
shows one current line that already contains them.

Capital expenditure is the same shape: SAP states one combined concept, TSM and
NVO state property and intangibles separately, and a stated total is never added
to its own components.

### Calculated market capitalisation is withheld from foreign issuers

Cover-page share counts on a 20-F or 40-F are in ordinary shares while the
listed security is an ADS. TSM's ratio is five; ASML's, SAP's and NVO's are one.
**Nothing in the XBRL says which** — TSM's cover page calls the security "Common
Shares" and the ratio appears once, in a prose footnote — so the naive
calculation is right three times in four and values TSM at $11tn against a real
$2.2tn the fourth.

Those counts are therefore not read, `market_cap_source` falls to `PROVIDER` or
`UNKNOWN`, and a company whose size is unknown is excluded with
`MISSING_REQUIRED_DATA` rather than entering a ranking five times too large.

### An ineligible company now says why

`ExclusionReason` was computed on every scan and thrown away. Migration `0016`
adds `score_snapshots.exclusion_reasons`, and `/api/companies/{ticker}` serves
it, so a screen can distinguish a company reporting in TWD from a fund, a
delisted shell and a stock that trades too thinly — all four of which arrived as
`NOT_ELIGIBLE`.

No new statuses were added. `UNSUPPORTED_CURRENCY`, `INSUFFICIENT_DATA` and
`MISSING_REQUIRED_DATA` already said everything; only the persistence was
missing.

### 20-F text, and no 6-K text

`extract_sections` reads a 20-F: business is **Item 4**, the MD&A equivalent is
**Item 5**, and risk factors are a titled subsection of Item 3 matched on their
own line, because the phrase recurs throughout the prose as a cross-reference.

`6-K` is **not** indexed at all, which is a deliberate reversal of what 7A
recommended. It is not a foreign 8-K: an 8-K reports a material event under a
numbered item, while a 6-K is an untyped envelope for anything a company
publishes at home. TSM files fifty to ninety a year against one 20-F, so
indexing them made the eight most recent filings all 6-Ks and left the annual
report — the only filing with anything quotable — unreachable for TSM and NVO.

## Two rules govern this layer

- **The filing's unit is authoritative, and never the vendor's.** A market-data
  provider's `currency` for an ADR is the currency the *share* trades in, which
  is USD for every foreign issuer on a U.S. exchange. `CompanyMetrics` carries
  the currency read from XBRL, and every ratio against a market capitalisation
  reads `market_cap_for_ratios` — converted where a rate exists, None where one
  does not. The 63x error cannot reach a ranking by any path.
- **Coverage is never bought with a fabricated period.** Four annual periods are
  not a trailing year, six-month figures are never halved into quarters, and
  `recent_revenue_growth` returns empty for a non-quarterly history rather than
  reporting four years of growth as "4 of 4 comparable quarters".

## What this does and does not recover

Of the 1,056 active companies with no fundamentals, 780 have readable XBRL —
418 under `ifrs-full` and 362 under `us-gaap`. But only **15.4%** of them
publish quarterly durations; 51.5% are annual-only and 30.8% file half-yearly.

So the realistic gain is **roughly 50 to 105 rankable companies**, not 780. The
rest are limited by reporting cadence, not by taxonomy or currency, and they now
fail visibly with a reason attached instead of silently.

**TSM, ASML, SAP and NVO are all annual-only and remain unrankable.** They gain a
correct reporting currency, a correct exclusion reason, an indexed 20-F and
extractable filing text. They do not gain a score, and forcing one on a company
with one financial period a year would be exactly the failure Phase 7A existed
to prevent.

## Phase 7C — currency-safe valuation

7B made the currency *visible* and refused to mix. 7C makes it *convertible*, so
a foreign issuer's valuation can be computed rather than only withheld.

### Reporting currency and quote currency are now two fields

`companies.currency` held one answer to two questions and whichever provider ran
last won. Migration `0017` splits it: `reporting_currency` is what the balance
sheet is in, `quote_currency` is what the market capitalisation is in. FMP writes
the second — its profile currency is the currency the *share* trades in — and
EDGAR writes the first, from the XBRL unit key.

### Only the market side is converted

The statements stay in the money they were filed in, always. Restating a history
would put exchange-rate movement into revenue growth and margins, which are
properties of the business — TSM's FY2024 revenue grew 33.9% in TWD and 25.0% in
USD, and only the first says anything about the company. One number crosses:

```
market_cap_reporting_currency = market_cap x FX(quote -> reporting)
enterprise_value              = market_cap_reporting_currency + debt - cash
```

Four sub-scores divide a financial figure by a market capitalisation — quality's
`cash_vs_debt`, valuation's multiple and `fcf_yield`, and risk's leverage. All
four now read `market_cap_for_ratios`, which is the converted figure for a
foreign issuer, the plain one for a domestic company, and **None** when a
conversion was needed and no rate was found. `CompanyMetrics` refuses outright to
hold an enterprise value in that last case, so no path can build one quietly.

### The proof

TSM's FY2024 figures as filed, against its real USD market capitalisation:

| | EV | EV / Revenue |
| --- | --- | --- |
| Before — USD market cap into a TWD balance sheet | 1,102,602.3m *(no currency)* | **0.38x** |
| After — market cap converted at USD/TWD 31.99226 | NT$69,657,480.7m | **24.07x** |
| After — no rate available | None | None |

A factor of **63.2**, and the difference between the most expensive large cap on
the board and the cheapest. It is now either correct or absent.

### Where rates come from

The ECB's daily reference rates are asked first: an official fixing, free,
keyless, and it reports the business day it actually used, so a Sunday score date
resolves to Friday's rate rather than to a fixing that never happened. It covers
thirty currencies and **not TWD**, so a broad dataset answers only for pairs the
ECB does not publish. Every stored rate records which source produced it — a
documented fallback order, not a consensus.

`fx_rates` keeps one row per `(base, quote, rate_date, provider)`. A stored rate
is the answer rather than a cache to revalidate, because a past day's fixing is
final; that is what makes a historical score reproduce exactly. Rates are
resolved once per pair per run, so four hundred euro filers cost one request.

Freshness is bounded at five days by default — a weekend with a holiday either
side. A rate dated after the score date is refused outright.

### Degradation

An FX outage costs foreign companies their currency-sensitive sub-scores and
nothing else. Domestic companies make no request at all, a stored rate still
serves during an outage, and growth and margins are computed in one currency each
and remain valid. `FX_UNAVAILABLE` is an eligibility **warning**, not an
exclusion: the company is screened and usually settles on `INSUFFICIENT_DATA`,
which is the honest account of what is known about it.

### What 7C does not change

TSM, ASML, SAP and NVO are still annual-only and still unrankable. Currency was
never their blocker — cadence is, and that is Phase 7D. Brookfield turned out not
to be the counter-example hoped for either: its nine "quarterly" periods are all
Q2 of successive years, so no trailing year can be built from them, and it
reports in USD so it needed no rate in the first place.

## Not built, and not to be started without being asked

- **FX conversion of fundamentals.** Applying a rate to a historical income
  statement is a methodology choice — which rate, on which date, for a flow
  versus a balance — and needs an ADR before any code.
- **Annual and semiannual period semantics.** Annual 3Y CAGR, an annual-basis
  TTM, and half-year periods. This is where the remaining ~400 companies live,
  and it must not land before FX: today `ttm_revenue` returning `None` for an
  annual filer is the last thing preventing a mixed-currency EV/Revenue, and
  building period support first would remove that guard.
- **ADR ratio lookup.** The ratio lives in the deposit agreement, not in XBRL.
  Nothing may infer, hardcode or guess it.
- **Reading the FY2025 figures companyfacts is missing for TSM.** Its 20-F filed
  2026-04-16 carries 339 fully tagged IFRS reports and contributed exactly one
  fact — the cover-page share count — to the aggregated API. Parsing filing
  documents directly to work around that is a separate capability.

## Phase 7F — rollout, admission and current rankings

The layers above made foreign issuers *scoreable*. 7F ran the whole recoverable
cohort through the pipeline and fixed what that exposed. Every stage ran against
a **copy** of the database.

### What the rollout found

Three defects, each caught by running real companies rather than by reasoning:

- **The composite provider replaced the EDGAR profile wholesale**, so a vendor
  that knew TSM's market capitalisation erased the reporting currency only EDGAR
  knew. The profile is now merged field by field.
- **The enrichment pass re-scored without an FX resolver**, turning a complete
  `PRELIMINARY` score into a `FINAL` one missing every currency-sensitive metric
  — and, because valuation's multiple is required, dropping the company out of
  the ranking entirely. Verification made the score worse for exactly the
  companies it verified.
- **A deposit-funded bank scored 74.78 and ranked twenty-first.** Both available
  classifications called Kaspi.kz a technology company. `domain.statements` now
  reads the statements instead — see
  [CompounderScore](../compounder-score.md#when-the-label-is-wrong).

### Liquidity moved to the consolidated tape

The $1M dollar-volume gate could not be applied to stored volume: across 259
companies, the IEX share of consolidated volume ranged from **0.006% to 12%**, so
there is no factor to scale by. A free Alpaca plan does serve SIP historically —
the restriction is recency, not the tape — so `update-eligibility-volume` reads
it into `consolidated_avg_volume`. Running it before the metered pass took
provider requests for the 762-company cohort from 762 to **353**.

### Validated Stage C results

| | |
| --- | --- |
| Foreign cohort | **762** |
| Fundamentals recovered | 289 |
| Numerical scores | 206 |
| **Current-rank eligible** | **191** |
| Stale scored but excluded | 15 |
| Insufficient | 82 |
| Not eligible | 461 |
| Unsupported sector | 13 |
| Provider unavailable | **0** |

Current-rank-eligible by cadence: 191 ANNUAL, 0 SEMIANNUAL, 0 QUARTERLY. By
taxonomy: 147 IFRS, 44 us-gaap.

Foreign entrants to the current ranking: **GFI (#11) and ERO (#19)** in the Top
20; **DLO, ARIS, CYD, KGC, IAG** joining them in the Top 50; **TBBB, B, AU, MTA**
in the Top 100. Every Top-20 and Top-50 entrant was reconciled against its filed
statements.

**CGAU keeps its 73.48 and leaves the ranking.** Its newest statement is from
2023, so ranking it meant 2023 revenue against a 2026 market capitalisation — a
mixed-vintage valuation. See
[what changed in V1.2](../compounder-score.md#what-changed-in-v12).

## Known limitations, documented rather than fixed

### 6-K interim statements

Some foreign quarterly reporters publish current interim financial statements
**only** through 6-K. A 6-K is an untyped envelope: most carry no financial
statements, and the ones that do are indistinguishable from the ones that do not
without opening them — hundreds of megabytes per filer to find out. It is
therefore excluded from the structured-period fallback, and that exclusion is
deliberate.

The consequence is honest degradation rather than a wrong number:

- the company keeps its valid historical scores
- freshness becomes `STALE`
- V1.2 excludes it from current rankings
- stock detail, Research and Deep Research remain available

This is why all 191 current-rank-eligible foreign companies are annual filers.
The quarterly ones are not missing — they are not *current*, and the system says
so. 6-K exhibit discovery is not to be implemented without being asked.

### us-gaap capital expenditure basis

`KNOWN_CAPEX_BASIS_LIMITATION` stands. Measured across the rollout: **7 affected
names, none in the Top 100**, maximum score sensitivity **0.04 points**. Full
measurement in [Metrics](../metrics.md#known_capex_basis_limitation).
Normalisation is unchanged.

### Filing-instance bandwidth

The fallback is valuable and it is not cheap. Across Stage C: **75 triggers, 74
successes, 1 failure** — and 458.7 MB of the 613.2 MB total, 75% of all traffic,
with a median instance of 5.45 MB and a maximum of 49.6 MB.

It is what makes GFI and CYD current. Where it fails it fails safely: CGAU's
40-F instance parsed to a single `dei` concept because Canadian filers put
financial statements in a separate exhibit, and the company kept the 2023 history
it already had rather than losing it.

## Primary database status

**The primary database has not received the foreign cohort.**

The whole of the Phase 7 validation and rollout ran against copies.
`compounder_radar.db` is byte-identical to its pre-rollout state, verified by row
counts across every table and by checksum before and after each stage.

Code completion and data promotion are separate operations. Promoting the cohort
is a deliberate, separate step: it means running the ordinary passes —
`update-eligibility-volume`, `enrich`, `update-fundamentals`, `score` — against
the real database, with the migrations applied first.
