# 0008. Scan the market on free data; spend metered requests on candidates only

- Status: Accepted
- Date: 2026-08-15

## Context

The first full-market run of the scoring engine ranked nothing at all.

The pipeline worked: 5,752 companies were loaded from Alpaca, 999 of them had a
year of price history, the benchmark was in place, and the scoring engine
processed every company in under four seconds. Every one of them came back
`NOT_ELIGIBLE`.

The cause was one field. Market capitalisation arrived only from the fundamentals
provider's profile endpoint, and that provider's free tier answers a few hundred
requests a day. About thirty-five companies got a profile before the account
began returning `429 Limit Reach` for every subsequent request — including, when
tested afterwards, every other endpoint on the plan. Without a market cap the
eligibility screen raises `MISSING_REQUIRED_DATA`, correctly: an unknown size is
not a small one. So the metered provider was a gate on the entire universe, and
at that quota a single full refresh of a 5,700-company universe would take weeks.

The screener already had two providers that cover the whole market for nothing:
Alpaca for prices, and SEC EDGAR for filings. What they did not obviously supply
was a share count that can be multiplied by a price. The one EDGAR field already
in use — `WeightedAverageNumberOfDilutedSharesOutstanding` — is an
income-statement average over a period and was never the number of shares
outstanding on any single day.

## Decision

The broad scan runs on Alpaca and EDGAR alone, and the metered provider becomes
candidate enrichment.

**Market capitalisation gets a second source.** EDGAR's
`dei:EntityCommonStockSharesOutstanding` — the cover-page count on every 10-Q and
10-K, stated at a point in time — multiplied by the latest close. The provider's
figure is preferred when one exists; otherwise the calculated one is used; the
source is recorded as `PROVIDER`, `CALCULATED` or `UNKNOWN` and travels with the
score into the CSV, the API and the explanation. Where both exist they are
compared, and a gap over 25% raises a warning. Neither is adjusted towards the
other, and they are never averaged.

**Sector classification gets a second source.** The SEC's submissions document
carries an SIC description — "State Commercial Banks", "Fire, Marine & Casualty
Insurance" — which is what keeps the `UNSUPPORTED_SECTOR` rule working when no
vendor sector is available.

**Scoring happens in two passes.** The broad scan produces `PRELIMINARY` scores
for every company. The top `FMP_ENRICHMENT_LIMIT` candidates then get one request
each, and the **same** formula is run again over the replaced inputs, producing
`FINAL` scores. A quota exhausted mid-pass stops the pass: companies already
enriched keep their verified state, the rest stay preliminary and say so, and the
run reports attempted, succeeded, rate-limited and skipped counts.

Liquidity keeps its Phase 1 treatment throughout. Single-exchange volume is never
compared with a whole-market threshold; a preliminary row carries
`LIQUIDITY_UNVERIFIED`, and enrichment is where consolidated volume finally
applies the threshold — which is allowed to remove the company from the ranking.

## Alternatives considered

**Multiply the weighted-average diluted count by the price.** It is already
stored, so this needed no new extraction. Rejected: the two concepts differ by
about 1% for Apple and by far more for a company that issued shares mid-quarter,
and the field's whole purpose is measuring that issuance. Reusing it for market
cap would have quietly coupled the dilution penalty to the valuation component.

**Use a bulk endpoint from the metered provider.** Investigated first, as the
cleanest fix if one existed: a batch quote or profile-bulk endpoint would cut
5,700 requests to a handful. Every endpoint tested — `profile`, `quote`,
`batch-quote`, `profile-bulk`, `company-screener` — returned the same
`429 Limit Reach`, so the plan's availability could not be established, and a
paid plan is out of scope. Worth re-testing when the quota resets; the
architecture here does not preclude using one.

**Rank without market capitalisation.** Drop it as a required field and screen on
price and liquidity alone. Rejected because market cap is not decoration: it is
the denominator of EV/Revenue, of the net-cash and leverage ratios, and of the
FCF yield. A ranking without it would be four sub-scores short and would silently
compare companies scored on different subsets.

**Cache the provider's market cap and refresh it slowly, a few hundred companies
a day.** A rolling refresh would eventually cover the universe. Rejected: a share
price moves daily, so a market cap refreshed every three weeks is wrong by
whatever the stock did in between — and the error is largest exactly for the
volatile small caps the tool exists to find.

**Estimate the missing companies from a peer median.** Rejected on the same
grounds as every other imputation in this codebase: an estimate that cannot be
distinguished from a measurement downstream.

## Consequences

Most of the market is now scored on a calculated market capitalisation, and that
figure has two known blind spots: it misses share classes the ticker does not
represent (a dual-class company is understated by the classes it does not price),
and it is stale by any issuance since the last filing. Both are visible —
`market_cap_source` on every row, a discrepancy warning where a provider figure
exists to compare — and neither is silently corrected.

Preliminary and final rows sit in the same ranking. That is deliberate: a ranking
that waited for verification would show nothing on a day the quota was already
spent. It does mean a reader must look at `ranking_state` before treating a row
as checked, and that the top of a preliminary ranking can contain a company whose
consolidated volume will later disqualify it.

EDGAR now costs two requests per company rather than one, because the submissions
document is fetched for the SIC description. The SEC's limit is ten requests a
second and the data is free, so this is a real but cheap cost.

The metered provider is no longer load-bearing. If its quota is exhausted, or its
key is missing, or the vendor disappears entirely, the tool still produces a
ranking — labelled as unverified.
