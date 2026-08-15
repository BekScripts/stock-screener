# api-clients

Outbound HTTP clients for market data providers.

## Use

```python
from api_clients import AlpacaMarketData, MarketDataProvider

provider: MarketDataProvider = AlpacaMarketData(api_key=..., secret_key=...)
bars = provider.get_daily_prices("AAPL", start, end)  # list[domain.PriceBar]
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["api-clients"]

[tool.uv.sources]
api-clients = { workspace = true }
```

## What it contains

- `base` — the `MarketDataProvider` and `FundamentalsProvider` protocols.
- `alpaca` — the universe from `/v2/assets` and daily OHLCV from
  `/v2/stocks/bars`, batched and paginated.
- `edgar` — quarterly statements from the SEC's XBRL company facts, the
  cover-page share count that turns a price into a market capitalisation, and the
  filer's SIC description. Free, no key, every U.S. filer, full history. No
  market cap and no consolidated volume of its own. Gross profit is taken as
  filed where a filer tags it and otherwise derived from a cost-of-revenue
  concept, with `gross_profit_basis` recording which one — the concepts differ in
  whether they include depreciation.
- `fmp` — quarterly statements and a company profile. The profile is the part
  worth having on a free plan; its statement endpoints are usually gated, and the
  plan's daily allowance is small enough that it belongs in the enrichment pass
  rather than in a market-wide scan.
- `composite` — statements from one source, profile from another. `edgar+fmp`
  is the pairing that works without a paid plan.
- `mock` — fixture-backed providers, used when no credentials are configured and
  by every test.
- `_http` — shared retry, backoff and rate limiting.

## Status of each adapter

| Adapter | Verified against the live API |
| --- | --- |
| `AlpacaMarketData` | Yes, 2026-08-13. Note the free data plan is IEX-only; `sip` returns 403, and IEX volume is ~2-4% of consolidated. |
| `SecEdgarFundamentals` | Yes, 2026-08-13. Cross-checked against FMP: revenue, cash flow, capex, cash and margins agree to the dollar for AAPL. |
| `FmpFundamentals` | Yes, 2026-08-13, on the `/stable` API. The retired `/api/v3` paths return 403 for keys issued today. |
| `CompositeFundamentals` | N/A — delegates to two others. |
| `MockMarketData` / `MockFundamentals` | N/A — no network. |

## Boundaries

**Belongs here:** anything that knows how a specific vendor formats a response,
authenticates a request, paginates, or signals a rate limit.

**Does not belong here:** database access, business rules, and any decision about
what the data means. An adapter fetches and translates; it does not filter a
universe or calculate a margin.

## The rule that matters

**No vendor vocabulary crosses this boundary.** Every method returns `domain`
models — `CompanyProfile`, `PriceBar`, `FinancialPeriod` — never a dict, never a
vendor payload. Alpaca's `"c"` becomes `PriceBar.close` here or the metric engine
inherits Alpaca's compression scheme. See
[ADR-0003](../../docs/adr/0003-normalise-provider-data-at-the-boundary.md).

Sign conventions are normalised here too: capital expenditure is stored as a
positive outflow whatever the vendor sent.

A field the vendor omitted, or sent unparseably, becomes `None` — never `0.0`.
