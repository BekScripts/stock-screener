---
type: reference
title: Metrics
---

# Metrics

Every figure the scanner calculates, its formula, and the conditions under which
it is unavailable. Implemented in `packages/domain/src/domain/metrics.py`; all
functions are pure and take periods or bars in any order.

## Conventions

| Convention | Meaning |
| --- | --- |
| Decimal proportions | `0.35` is 35%. Nothing is stored pre-multiplied. |
| Percentage points | Differences between two rates are `pp`, not `%`. |
| `None` | The data cannot support the metric. Never rendered or stored as `0`. |
| `0.0` | The company reported zero. |

Input sequences are sorted oldest-first and de-duplicated by date before any
calculation. Where two records share a date, the later one in the input wins.

### Period matching

Quarters are matched by **date**, not list position. A function looking for the
year-ago quarter searches for a period ending within 45 days of 365 days before
the reference period. A company that skipped a filing therefore yields `None`
rather than a comparison against a fifteen-month-old quarter.

Trailing-twelve-month windows additionally require their four periods to span
228–318 days, which admits fiscal-calendar drift but rejects a window with a
quarter missing from the middle.

## Growth

| Metric | Formula | `None` when |
| --- | --- | --- |
| `revenue_growth_yoy` | `(revenue − revenue₋₄) / revenue₋₄` | Either quarter absent, or prior revenue ≤ 0 |
| `previous_revenue_growth_yoy` | Same, one quarter earlier | Either quarter of that pair absent |
| `revenue_growth_acceleration` | `revenue_growth_yoy − previous_revenue_growth_yoy` | Either input is `None` |
| `ttm_revenue` | Sum of the latest 4 quarters | Fewer than 4 consecutive quarters, or any lacks revenue |
| `ttm_revenue_growth` | Latest TTM vs the preceding TTM | Fewer than 8 consecutive quarters |
| `revenue_cagr_3y` | `(TTM / TTM₋₁₂)^(1/3) − 1` | Fewer than 16 quarters, or either TTM ≤ 0 |
| `gross_profit_growth_yoy` | `(gross_profit − gross_profit₋₄) / gross_profit₋₄` | Either quarter absent, or prior value ≤ 0 |
| `recent_revenue_growth_yoy` | Year-over-year growth of each of the latest four quarters, newest first | A quarter whose year-ago comparison cannot be made is **omitted**, so fewer than four values means a gap in the history — never a quarter that shrank |

Growth from a non-positive base is `None`: "up 300% from minus one million" is
arithmetic, not information.

Acceleration is a **subtraction**, never a ratio. `0.35` following `0.18` gives
`0.17`, meaning +17 percentage points.

### Three-year CAGR methodology

The latest trailing-twelve-month revenue against the trailing-twelve-month
revenue ending twelve quarters earlier, requiring sixteen quarters of history.
TTM is used rather than annual reports because ingestion stores quarterly
statements, and TTM avoids comparing a partial fiscal year with a complete one.

## Margins and balance sheet

Margins are taken from the **latest reported quarter**, not a trailing year, so
they move as soon as the business does.

### Where gross profit comes from

A filer that tags `GrossProfit` supplies it directly. Most do not, and XBRL has no
single cost-of-revenue concept — so it is derived as `revenue − cost of revenue`
from the first concept in this chain the filer used for that quarter:

| Concept | Means | Basis |
| --- | --- | --- |
| `GrossProfit` | Reported directly; never overwritten by a derived figure | as filed |
| `CostOfGoodsAndServicesSold` | Goods and services sold | includes D&A |
| `CostOfRevenue` | Total cost of revenue | includes D&A |
| `CostOfGoodsSold` | Goods only | includes D&A |
| `CostOfServices` | Services only — the service-company analogue of COGS | includes D&A |
| `DirectOperatingCosts` | Costs directly attributable to revenue; shipping, energy and media filers | includes D&A |
| `CostOf…ExcludingDepreciationDepletionAndAmortization` | The same cost with D&A stripped out | **excludes D&A** |

The chain exists because filers migrate between concepts: HF Sinclair's
`CostOfGoodsAndServicesSold` stops in 2024, Expedia's `CostOfRevenue` in 2019 and
Expand Energy's `GrossProfit` in 2011, while all three keep reporting revenue.
Without the later entries their gross margin is simply absent.

Concepts that are **not** cost of revenue are deliberately excluded, above all
`CostsAndExpenses` — total operating cost including SG&A. Revenue less that is
closer to operating income, and calling it a gross profit would be wrong by the
entire operating expense base.

`gross_profit_basis` records which concept produced the figure. It matters
because the excluding-D&A variants yield a **higher** gross profit than the GAAP
one, so two companies on different bases are not measuring the same thing — and
neither are two quarters of one company whose filer switched concepts mid-history.

| Metric | Formula | `None` when |
| --- | --- | --- |
| `gross_margin` | `gross_profit / revenue` | Either absent, or revenue is 0 |
| `operating_margin` | `operating_income / revenue` | Either absent, or revenue is 0 |
| `fcf_margin` | `free_cash_flow / revenue` | FCF unavailable, or revenue is 0 |
| `net_cash` | `cash − total_debt` | Either side absent |
| `share_count_growth_yoy` | `(shares − shares₋₄) / shares₋₄` | Either observation absent, or prior ≤ 0 |

### Margin trends and trailing cash flow

Scoring reads direction as well as level, so each margin has a year-over-year
change beside it. The comparison is against the same quarter a year earlier
rather than the previous quarter, because a margin moves with the seasons for
most businesses and a retailer's Q4 against its Q3 would read as a trend that is
really a calendar.

| Metric | Formula | `None` when |
| --- | --- | --- |
| `gross_margin_change` | `gross_margin − gross_margin₋₄` | Either quarter absent, or either margin unavailable |
| `operating_margin_change` | `operating_margin − operating_margin₋₄` | As above |
| `fcf_margin_change` | `fcf_margin − fcf_margin₋₄` | As above |
| `ttm_free_cash_flow` | Sum of free cash flow over the latest 4 quarters | Fewer than 4 consecutive quarters, or any quarter cannot produce an FCF figure |

Changes are **percentage points**: `0.03` is +3pp, not 3%.

### Two share counts, two purposes

| Field | Concept | Used for |
| --- | --- | --- |
| `shares_outstanding` | Weighted-average diluted shares, a period figure | Dilution |
| `common_shares_outstanding` | Cover-page common shares outstanding, a point-in-time figure | Market capitalisation |

They must never be substituted for one another. A weighted average was never the
number outstanding on any single day, so multiplying it by a price values the
company on a share base that did not exist; and a point-in-time count differenced
against itself measures issuance plus timing rather than dilution.

Cover-page counts stated on a **20-F or 40-F are not read at all**. They are in
ordinary shares while the U.S.-listed security is an American Depositary Share
representing some number of them — five for TSM, one for ASML, SAP and NVO — and
nothing in the XBRL says which. TSM's cover page calls the security "Common
Shares" and the ratio appears only in a prose footnote, so multiplying that count
by a U.S. price is right three times in four and 5x wrong the fourth, valuing the
company at $11tn against a real $2.2tn. Without the count there is no calculated
market cap, so `market_cap_source` falls to `PROVIDER` or `UNKNOWN` — which is
the honest outcome, because a company whose size is unknown is unscreenable and
`MISSING_REQUIRED_DATA` says so.

From EDGAR the second comes from `dei:EntityCommonStockSharesOutstanding`, the
count every 10-Q and 10-K states on its cover page. It is dated near the filing
rather than at the quarter end, so it is matched to the quarter it was filed with
— within 90 days after it — rather than to the balance-sheet date.

## Valuation

| Metric | Formula | `None` when |
| --- | --- | --- |
| `market_cap` | The provider's figure, else `price × common_shares_outstanding` | Neither is available |
| `calculated_market_cap` | `price × common_shares_outstanding`, whenever both exist | Either is missing, or the share count is over a year old |
| `market_cap_discrepancy` | `abs(provider − calculated) / provider` | Either figure is missing |
| `enterprise_value` | `market_cap + debt − cash` | Market cap, debt **or** cash is unavailable |

`market_cap_source` records which of the two `market_cap` is — `PROVIDER`,
`CALCULATED` or `UNKNOWN`. The calculated figure is what lets the screen run
across the whole market without a metered provider answering for every company;
it is blind to share classes the ticker does not represent, which is why the two
are compared rather than merged.

Enterprise value is computed, never fetched: all three inputs are already
normalised and stored. Absent debt is not read as zero, so a company whose
borrowing tag went unrecognised has no enterprise value rather than the
enterprise value of a debt-free company — see
[ADR-0005](../adr/0005-absent-debt-is-unknown-not-zero.md). The result may be
negative, which means the company is valued below its net cash.

Multiples built on it — EV/revenue, price-to-sales, FCF yield — are calculated
inside the scoring engine, where the choice between them is part of the rule.
See [CompounderScore v1](compounder-score.md).

**Which share count.** `shares_outstanding` holds **weighted-average diluted
shares** — `WeightedAverageNumberOfDilutedSharesOutstanding` from EDGAR,
`weightedAverageShsOutDil` from FMP. Dilution compares the field against itself
a year earlier, so the concept must stay consistent: comparing a weighted
average against a period-end count would manufacture a change that did not
happen. A new adapter must map the same concept.

A weighted average cannot be recovered by subtraction, so EDGAR yields no
fiscal-Q4 share count and dilution is unavailable for a company whose most
recent filing is its annual report.

**Debt.** Absence is never read as zero. A filing tags the instruments it has and
stops tagging them once they are repaid, so a debt-free company and one whose
borrowing tag is unrecognised look identical — and crediting the second as
debt-free would flatter exactly the companies a risk penalty exists to catch. An
explicit zero in a filing is a reported value and is kept. Where a vendor gives
no `totalDebt`, its short- and long-term components are summed only if **both**
are present. See [ADR-0005](../adr/0005-absent-debt-is-unknown-not-zero.md).

XBRL debt concepts come in two kinds, and each classification is read
accordingly. A **total** — `LongTermDebt`, `LongTermDebtCurrent` — rolls up every
borrowing of that classification, and the filer's own total is preferred because
it is stated net of issue costs. **Instruments** — `LineOfCredit`, `SecuredDebt`
and the rest — co-exist, so they are summed with one another and used only where
no total was tagged. Reading instruments as alternatives would report a revolver
and drop the term loan beside it; adding them to a stated total would count the
same borrowing twice. Where a filer states nil current maturities while carrying
its facilities under instrument concepts, the two tiers together are what stop
that nil being read as the whole of the debt.

`net_cash` is a subtraction, so a result of `0.0` is a real observation: cash
exactly offsets debt.

### Free cash flow

The reported figure is preferred. Otherwise:

```text
free_cash_flow = operating_cash_flow − abs(capital_expenditure)
```

`abs` is deliberate. Vendors disagree on the sign of capital expenditure, and
adding a negative capex would turn cash burn into cash generation. Adapters
normalise capex to a positive outflow before storage; the domain function
defends against it a second time.

The derivation is only as available as its inputs, so capital expenditure is
read from the industry-specific concepts as well as the general one: an
extractive filer tags its whole capital programme as
`PaymentsToAcquireOilAndGasProperty` and never tags
`PaymentsToAcquirePropertyPlantAndEquipment` at all. The general concept wins
where a filer tags both, which is common in the year one is retired for the
other. Free cash flow stays `None` when either input is genuinely absent — it is
never derived from one half.

## Price and liquidity

| Metric | Definition | `None` when |
| --- | --- | --- |
| `price` | Latest close | No price history |
| `average_dollar_volume_20d` | Mean of `close × volume` over the latest ≤20 sessions | No price history |
| `trading_days_used` | `min(20, sessions available)` | Never — `0` when there is no history |
| `return_6m` | `close / close_on_or_before(t − 182 days) − 1` | History does not reach back, or baseline close ≤ 0 |
| `return_12m` | Same over 365 days | As above |
| `high_52w` | Highest intraday high within 365 days | No price history |
| `low_52w` | Lowest intraday low within 365 days | No price history |
| `distance_from_52w_high` | `close / high_52w − 1` | No history, or high ≤ 0 |

### Average dollar volume, and what it represents

The figure is taken from a provider's **consolidated** average daily share
volume where one exists — FMP supplies this on the same profile request already
made — multiplied by the latest close. Only when no such average exists does it
fall back to the mean of `close × volume` over the most recent sessions, up to
twenty.

The distinction matters and travels with the number as `liquidity_basis`:

| Basis | Meaning | Threshold applied? |
| --- | --- | --- |
| `CONSOLIDATED` | Every U.S. venue | Yes |
| `PARTIAL` | One exchange, e.g. a free IEX-only feed | **No** — warning instead |
| `UNKNOWN` | No volume, or no statement of origin | **No** — warning instead |

A single-exchange feed carries roughly 2–4% of consolidated volume, so applying
a whole-market threshold to it would be about twenty-five times too strict. The
screen therefore reports `LIQUIDITY_UNVERIFIED` rather than excluding. See
[ADR-0004](../adr/0004-apply-the-liquidity-threshold-only-to-consolidated-volume.md).

The **insufficient-history** check is separate and still excludes: it requires
`trading_days_used >= MIN_TRADING_DAYS` (default 20) whatever the basis, so four
sessions of heavy turnover never pass as a twenty-day average.

### Return baselines

Returns use the last session **on or before** the calendar target rather than
assuming markets were open exactly 182 or 365 days ago. A target landing on a
holiday falls back to the previous trading day.

52-week high and low use intraday extremes, which is the conventional reading —
this is why a stock can sit below its 52-week high on a day it closed at a
record close.

## Eligibility

A security is eligible when every check passes. Thresholds come from
[configuration](configuration.md).

| Reason | Raised when |
| --- | --- |
| `PRICE_BELOW_MINIMUM` | `price < MIN_PRICE` |
| `MARKET_CAP_BELOW_MINIMUM` | `market_cap < MIN_MARKET_CAP` |
| `LOW_LIQUIDITY` | `trading_days_used < MIN_TRADING_DAYS`, or a **consolidated** ADV below `MIN_AVG_DOLLAR_VOLUME` |
| `INACTIVE` | The provider reports the security as not trading |
| `UNSUPPORTED_SECURITY_TYPE` | Not common stock on NASDAQ, NYSE or NYSE American |
| `UNSUPPORTED_CURRENCY` | The company files its statements in a currency other than USD, read from the XBRL unit key where the filings state one and from the provider's profile otherwise |
| `MISSING_REQUIRED_DATA` | Price or market cap unavailable |

A verdict may also carry warnings, which qualify it without excluding:

| Warning | Raised when |
| --- | --- |
| `LIQUIDITY_UNVERIFIED` | Only partial-market volume was available, so the dollar threshold was not applied |

A security can fail several checks at once, and all of them are reported. A
value exactly at a threshold passes: the minimums are inclusive.

`MISSING_REQUIRED_DATA` and a threshold breach are distinct. An absent market cap
raises the former only — "we do not know" is not "too small".

## Units and currency

Every monetary figure is stored in **whole units of the reporting currency** —
dollars, not thousands or millions. Providers that report in thousands would make
a company appear 1,000x smaller; no scaling is applied, so a new adapter must
convert to whole units before returning a model.

Market capitalisation is quoted by the market in USD. A company filing its
statements in another currency is therefore **excluded** with
`UNSUPPORTED_CURRENCY` rather than screened, because every ratio built from the
two would be wrong by an exchange rate. A provider that does not report a
currency is treated as USD, which is true for the overwhelming majority of
U.S.-listed common stock.

The size of that error is worth stating. Mixing TSM's TWD statements with its
USD market capitalisation produces an EV/Revenue of **0.38x** where the truth is
**24.07x** — a factor of sixty-three, and the difference between the most
expensive large cap on the board and the cheapest.

### Which side is converted

**The market side, never the statements.** A company's reported history stays in
the money it was filed in. Restating it would put exchange-rate movement into
revenue growth and margins, which are properties of the business and are already
currency-invariant when every period is compared in one currency — TSM's FY2024
revenue grew 33.9% in TWD and 25.0% in USD, and only the first is a fact about
the business. Just one number crosses:

```
market_cap_reporting_currency = market_cap_quote_currency x FX(quote -> reporting)
enterprise_value              = market_cap_reporting_currency + debt - cash
```

Every ratio of a financial figure to a market capitalisation reads
`CompanyMetrics.market_cap_for_ratios`, which is the converted figure for a
foreign issuer and the plain one for a domestic company. There are exactly four:
the quality component's `cash_vs_debt`, the valuation component's multiple and
`fcf_yield`, and the risk assessment's leverage. When a conversion was needed and
no rate was available it returns **None**, and those sub-scores are unavailable —
a missing sub-score is a gap, a mixed-currency one is a wrong answer wearing the
costume of a right one. `CompanyMetrics` refuses outright to hold an enterprise
value in that situation, so no path can build one quietly.

### Where a rate comes from

Rates are dated, stored and attributed. `fx_rates` keeps one row per
`(base, quote, rate_date, provider)`, so a stored score can be reproduced rather
than re-derived at whatever today's rate happens to be.

| Source | Covers | Used for |
| --- | --- | --- |
| European Central Bank daily reference rates | 30 currencies | Everything it publishes |
| A broad daily dataset | ~340 currencies | Only pairs the ECB does not publish — `TWD`, `ARS` |

The order is a statement about authority rather than a consensus: one source
answers and the row records which. The ECB is asked first and reports the
business day it actually used, so a Sunday score date resolves to Friday's fixing
rather than to a fixing that never happened.

Freshness is bounded by `FX_MAX_RATE_AGE_DAYS`, five days by default: enough for
a weekend with a holiday either side, and short enough that a rate from the far
side of a real gap in the series is refused. A rate dated *after* the score date
is refused outright — money from the future is not evidence about the past. A
company whose pair cannot be resolved carries the `FX_UNAVAILABLE` eligibility
warning and keeps every metric that does not need both sides.

**The currency comes from the filing, not the vendor.** A market-data provider's
`currency` field for an ADR is the currency the *share* trades in, which is USD
for every foreign issuer on a U.S. exchange. Trusting it would hold this gate
open on exactly the companies it exists to catch, so `CompanyMetrics`
carries the reporting currency read from the XBRL unit key and the screen
consults that first.

### Two taxonomies, one normalised period

EDGAR serves foreign private issuers' statements under `ifrs-full` rather than
`us-gaap`, and the taxonomy is detected per company from the facts document
rather than inferred. Nothing about a company predicts it: ASML and TSM both
file a 20-F, and ASML's statements arrive under `us-gaap` while TSM's arrive
under `ifrs-full`. A filer that changed taxonomy keeps the old block forever, so
the taxonomies are ranked by how many revenue facts each carries rather than by
which exists.

Both resolve through the same concept chains into the same `FinancialPeriod`.
Two fields need more than a renamed chain:

- **Borrowings.** Where a filer states an entity-wide `ifrs-full:Borrowings`,
  that is the whole answer and nothing is added to it — SAP's EUR 6,150.0m is
  exactly its non-current plus current, and its EUR 5,294.0m of `BondsIssued` is
  part of that total rather than a further debt. Where no total is stated,
  non-current bonds are summed with non-current borrowings, because TSM carries
  NT$926.6bn of bonds beside NT$31.8bn of bank loans and reading the loans alone
  reports 3% of its debt. Current bonds are **not** added to current maturities:
  TSM's NT$57.1bn of them sit inside the NT$59.9bn line its balance sheet shows.
- **Capital expenditure.** SAP states one concept covering property, intangibles,
  investment property and other non-current assets; TSM and NVO state property
  and intangibles separately. A stated combined figure wins, and the components
  are summed only where none exists. The two bases are close but not identical,
  which is a real comparability limit between filers rather than a mapping
  choice.

#### KNOWN_CAPEX_BASIS_LIMITATION

Capital expenditure is on a slightly different basis under the two taxonomies,
and this is recorded rather than fixed.

The IFRS chain sums property with intangibles, because that is how TSM and NVO
present their cash-flow statements and SAP states a single concept covering
both. The us-gaap chain reads property alone: `PaymentsToAcquireIntangibleAssets`
is not in it and has never been, so every domestic company has been scored
without intangible purchases since Phase 1. ASML files under us-gaap concept
names, so its capital expenditure — and therefore its free cash flow and FCF
margin — is on the narrower basis while NVO's is on the wider one.

Adding the concept would change the free cash flow of **every domestic
company**, and with it their scores. That is a cross-taxonomy normalisation
decision needing a representative audit of domestic filers first, and the
options are genuinely open: narrow IFRS to property alone to match current
domestic semantics, or broaden us-gaap to include intangible investment. Neither
is a change to make as a side effect of international coverage, so the
limitation is documented and left alone.

Short-term investments are not added to cash for an IFRS filer. No IFRS concept
is reliably the same measure — TSM's `ShorttermInvestmentsClassifiedAsCashEquivalents`
is already inside cash equivalents and would double-count — so cash is cash and
equivalents alone. That understates the liquid position of a company holding
marketable securities, which is the safe direction: it lowers net cash and the
quality sub-score built on it.

## Plausibility

Impossible observations are rejected at the boundary rather than stored:

- a price bar whose `high` is below its `low`;
- a negative price or a negative volume;
- a negative market capitalisation.

These surface as a `ProviderDataError` for one ticker, which ingestion logs and
counts, so a garbled response costs one company rather than corrupting a ranking.

Extreme-but-possible values are **not** rejected. A company really can grow
revenue 400% in a quarter, and refusing to record it would hide exactly the kind
of business this project exists to find. Scoring caps the *points* such a figure
can earn; the metric itself is stored and displayed unaltered.

## Universe rules

Accepted exchange codes: `NASDAQ`, `NYSE`, `AMEX` (Alpaca's code for NYSE
American), and the written-out spellings of the last. `ARCA`, where U.S. ETFs
list, is excluded.

A listing is rejected as non-common when its symbol carries a `W`, `WS`, `WT`,
`R`, `RT`, `U`, `UN`, `P` or `PR` suffix after a separator, or its registered
name contains a fund, warrant, rights, units, preferred or depositary term. A
class suffix such as `BRK.B` is not affected.

These heuristics are deliberately shallow. Wrongly excluding an obscure listing
costs one candidate; wrongly including a warrant puts a meaningless row near the
top of a ranking.
