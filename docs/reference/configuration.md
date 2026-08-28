---
type: reference
title: Configuration
---

# Configuration

Every value the application reads from the environment. Defined as typed fields
on `Settings` in `src/stock_screener/config.py`; nothing else in the codebase
reads the environment.

Values are read from environment variables, falling back to a `.env` file in the
working directory. Field names map to upper-case variables: `log_level` reads
`LOG_LEVEL`.

## Application

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `ENVIRONMENT` | `local` \| `test` \| `staging` \| `production` | `local` | Deployment environment. |
| `LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` | `INFO` | Minimum level emitted. |
| `LOG_JSON` | bool | `false` | Emit JSON logs instead of console-formatted. |

Credentials carried in a URL query string are redacted before any handler
writes them. `httpx` logs every request at `INFO`, URL included, so a provider
that authenticates with a query parameter — FMP's `apikey` — would otherwise
put its key in the log. The value is replaced with `REDACTED`; the rest of the
URL is left readable. `SecretStr` does not cover this: by that point the value
is part of a URL a third-party library formatted.

## Jobs

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `JOBS_ENABLED` | bool | `true` | Whether the API may start pipeline commands. |
| `JOB_LOG_DIR` | str | `.jobs` | Directory a spawned command's output is written to, one file per job. |

The job endpoints let a caller start an hour-long ingest and spend money on
research, and there is no authentication in front of them. That is only
reasonable because uvicorn binds `127.0.0.1`. Set `JOBS_ENABLED=false` before
binding the API anywhere else; every job endpoint then answers `404`, so the
feature looks absent rather than guarded.

## Database

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `DATABASE_URL` | str | `sqlite:///./compounder_radar.db` | SQLAlchemy URL. SQLite is the default so a clone runs with no setup; PostgreSQL is the intended production database. |

Migrations read this same value, so `alembic` and the application can never
target different databases.

## Providers

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `MARKET_DATA_PROVIDER` | `alpaca` \| `mock` | `mock` | Source of the universe and daily bars. |
| `FUNDAMENTALS_PROVIDER` | `edgar` \| `edgar+fmp` \| `fmp` \| `mock` | `mock` | Source of profiles and statements. See below. |
| `SEC_USER_AGENT` | str | unset | Contact string sent to the SEC, e.g. `Compounder Radar you@example.com`. Required whenever the provider involves EDGAR; the SEC blocks anonymous requests. |
| `FIXTURE_PATH` | str | `fixtures/sample_universe.json` | Fixture file the mock providers read. |
| `ALPACA_API_KEY` | secret | unset | Required when the market-data provider is `alpaca`. |
| `ALPACA_SECRET_KEY` | secret | unset | Required when the market-data provider is `alpaca`. |
| `ALPACA_TRADING_BASE_URL` | str | `https://api.alpaca.markets` | Host serving `/v2/assets`. **Paper keys (beginning `PK`) need `https://paper-api.alpaca.markets`** or every request returns 401. |
| `ALPACA_DATA_BASE_URL` | str | `https://data.alpaca.markets` | Host serving `/v2/stocks/bars`. |
| `ALPACA_FEED` | `iex` \| `sip` | `iex` | Which tape price history is read from. See the warning below. |
| `ELIGIBILITY_VOLUME_ENABLED` | bool | `true` | Whether `update-eligibility-volume` may read the consolidated tape for the liquidity screen. Needs no paid plan. |
| `ELIGIBILITY_VOLUME_DELAY_MINUTES` | int ≥ 15 | `15` | How far in the past a consolidated query must end. Below the floor the request is refused as too recent and fetches nothing. |

### Feed choice distorts liquidity

`iex` is a single exchange and is what a free account reads live. Its **prices
are sound**, but it reports only IEX's share of volume. Measured across 259
companies that also carry a vendor's consolidated figure, that share ranged from
**0.006% to 12%** — a spread of two thousand times. `MIN_AVG_DOLLAR_VOLUME` is
calibrated against consolidated volume, so on this feed the liquidity screen is
both far too strict and inconsistently so; it cannot be corrected by scaling,
because there is no constant to scale by.

The screen therefore refuses to apply the threshold to a `PARTIAL` figure at all,
warning `LIQUIDITY_UNVERIFIED` instead.

**Consolidated volume without a paid plan.** `ELIGIBILITY_VOLUME_ENABLED` lets
`update-eligibility-volume` read consolidated daily bars directly. The
subscription limit is about *recency*, not the tape: a query ending at least
`ELIGIBILITY_VOLUME_DELAY_MINUTES` in the past is served, and one inside that
window returns `403 subscription does not permit querying recent SIP data`. An
eligibility screen asks what average volume *has been*, so the delay costs it
nothing. The `end` parameter must be a timestamp — a bare date is read as
end-of-day and refused however old the rest of the range is.

That figure is stored separately from the price feed's, in
`consolidated_avg_volume` with `volume_source` naming the tape, so one column
never holds two bases of volume.
| `FUNDAMENTALS_API_KEY` | secret | unset | Required when the fundamentals provider is `fmp`. |
| `FUNDAMENTALS_BASE_URL` | str | `https://financialmodelingprep.com` | FMP host. |
| `FUNDAMENTALS_API_ROOT` | str | `/stable` | Path prefix before each FMP endpoint. `/api/v3` is retired for keys issued today. |

Selecting a real provider without its credentials raises `ConfigurationError` at
startup, naming the variables to set.

### Choosing a fundamentals provider

| Value | Statements | Profile | Cost |
| --- | --- | --- | --- |
| `mock` | fixture file | fixture file | free, no network |
| `edgar` | SEC XBRL, every U.S. filer, full history | identity, SIC industry, and a **calculated** market cap | free |
| `edgar+fmp` | SEC XBRL | FMP profile | free tier sufficient |
| `fmp` | FMP | FMP | needs a plan covering the symbols being scanned |

`edgar` alone is enough for a broad scan. EDGAR is the source the commercial
vendors resell, so its statements are authoritative and complete down to micro
caps; market capitalisation is calculated from the cover-page share count and the
latest close, and the SIC description drives the unsupported-sector rule.

`edgar+fmp` adds the second opinion: a vendor market cap to cross-check the
calculated one, and consolidated average volume, which is the only figure the
liquidity threshold may be applied to. On a metered plan that is a few hundred
requests a day, so it belongs in the enrichment pass rather than in the scan —
see [ADR-0008](../adr/0008-broad-scan-on-free-data-metered-enrichment-last.md).

## Provider behaviour

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `PROVIDER_MAX_ATTEMPTS` | int ≥ 1 | `3` | Tries per request, including the first. |
| `PROVIDER_RETRY_BACKOFF_SECONDS` | float ≥ 0 | `1.0` | Base delay, doubled after each failed attempt. |
| `PROVIDER_MIN_REQUEST_INTERVAL_SECONDS` | float ≥ 0 | `0.2` | Minimum gap between requests. `0` disables limiting. |
| `PROVIDER_BATCH_SIZE` | int ≥ 1 | `100` | Symbols per batched market-data request. |
| `PROVIDER_TIMEOUT_SECONDS` | float > 0 | `30.0` | Per-request timeout. |

Rate limits and server errors are retried; rejected credentials are not.

## Ingestion

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `PRICE_HISTORY_DAYS` | int ≥ 1 | `400` | Calendar days of price history to fetch. Covers a 52-week high and a twelve-month return with room for holidays. |
| `FUNDAMENTALS_QUARTERS` | int ≥ 1 | `20` | Quarters requested per company. See the depth table below. |

### History depth and what it buys

A metered plan caps how many quarters it will return and rejects a larger
request outright rather than truncating it, so this setting must match the
subscription. FMP's free tier allows five.

| Quarters | Unlocks |
| --- | --- |
| 1 | Margins, net cash |
| 4 | TTM revenue |
| 5 | YoY revenue growth, gross profit growth, dilution |
| 6 | Previous YoY growth, and therefore **growth acceleration** |
| 8 | TTM revenue growth |
| 16 | Three-year revenue CAGR |

Below the requirement a metric is `None`, not wrong — but acceleration is one of
the signals the project exists to surface, so five quarters is a real constraint
rather than a cosmetic one.

## Scoring

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `BENCHMARK_SYMBOL` | str | `SPY` | The broad-market series relative strength is measured against. Fetched through the market-data provider and stored in `benchmark_prices`. |
| `FMP_ENRICHMENT_LIMIT` | int ≥ 0 | `200` | How many top-ranked candidates the enrichment pass may spend metered requests on. |

Changing the benchmark changes what every momentum score means. Scores computed
against two different benchmarks are not comparable, so re-score the market after
changing it rather than reading across the two.

Without a stored benchmark series, relative strength cannot be calculated and
every company ends the run as `INSUFFICIENT_DATA`. Run `update-benchmark` before
`score`, or use `run-daily`, which does both.

`FMP_ENRICHMENT_LIMIT` bounds the second pass, not the scan. The broad scan runs
entirely on Alpaca and EDGAR; enrichment then spends one request per candidate to
replace a calculated market cap with the vendor's and unverified volume with
consolidated volume. Set it to what the plan's daily allowance can serve — a free
FMP tier is a few hundred requests a day, and a pass that runs out stops rather
than retrying into the wall.

## Eligibility thresholds

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `MIN_PRICE` | float > 0 | `2.0` | Minimum latest close. |
| `MIN_MARKET_CAP` | float > 0 | `100000000` | Minimum market capitalisation. |
| `MIN_AVG_DOLLAR_VOLUME` | float > 0 | `1000000` | Minimum 20-day average dollar volume. |
| `MIN_TRADING_DAYS` | int > 0 | `20` | Sessions of price history required before the liquidity figure counts. |

Thresholds are inclusive: `$2.00`, `$100,000,000` and `$1,000,000` all pass;
`$1.99`, `$99,999,999` and `$999,999` all fail.

`Settings.eligibility_thresholds` converts these four into the
`EligibilityThresholds` the `domain` package takes, which is how the screening
rules stay free of configuration.

## Behaviour

`Settings` is frozen after construction and `extra="forbid"` rejects unknown
keyword arguments. Undeclared environment variables are ignored rather than
rejected — a typo'd variable name is silently unused.

A field without a default is required: the process fails at startup if the
variable is absent.

`get_settings()` caches a single instance for the process lifetime. Tests call
`get_settings.cache_clear()` to reset it.

## Secrets

Credential fields are `SecretStr`. Their values do not appear in `repr()`, in a
logged settings object, or in a traceback — `settings.alpaca_api_key` renders as
`SecretStr('**********')`, and the value is reached with `.get_secret_value()`.

`.env` is gitignored. `.env.example` holds variable names with placeholder
values only, and is the documentation of what the application needs to run.

## Related

- [Add a configuration setting](../how-to/add-a-setting.md)
- [Run a scan](../how-to/run-a-scan.md)
