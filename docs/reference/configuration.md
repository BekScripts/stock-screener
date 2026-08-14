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
| `ALPACA_FEED` | `iex` \| `sip` | `iex` | Which tape to read. See the warning below. |

### Feed choice distorts liquidity

`sip` is the consolidated tape — every U.S. exchange — and is correct for both
price and volume. It requires a paid Alpaca data plan; a free account requesting
it gets `403 subscription does not permit querying recent SIP data`.

`iex` is a single exchange and is what a free account can read. Its **prices are
sound**, but it reports only IEX's share of volume, measured at roughly **2–4%**
of consolidated. Since `MIN_AVG_DOLLAR_VOLUME` is calibrated against
consolidated volume, on this feed the liquidity screen behaves like a threshold
twenty-five times higher and excludes genuinely liquid companies.

Building the provider logs a warning when the feed is `iex`, because the symptom
is silent: a shorter list of eligible companies, with nothing marked wrong.
| `FUNDAMENTALS_API_KEY` | secret | unset | Required when the fundamentals provider is `fmp`. |
| `FUNDAMENTALS_BASE_URL` | str | `https://financialmodelingprep.com` | FMP host. |
| `FUNDAMENTALS_API_ROOT` | str | `/stable` | Path prefix before each FMP endpoint. `/api/v3` is retired for keys issued today. |

Selecting a real provider without its credentials raises `ConfigurationError` at
startup, naming the variables to set.

### Choosing a fundamentals provider

| Value | Statements | Profile | Cost |
| --- | --- | --- | --- |
| `mock` | fixture file | fixture file | free, no network |
| `edgar` | SEC XBRL, every U.S. filer, full history | **none** — no market cap, so the screen excludes everything | free |
| `edgar+fmp` | SEC XBRL | FMP profile | free tier sufficient |
| `fmp` | FMP | FMP | needs a plan covering the symbols being scanned |

`edgar+fmp` is the combination that works on free plans. EDGAR is the source the
commercial vendors resell, so its statements are authoritative and complete down
to micro caps, but it publishes no market data at all. FMP's profile endpoint
supplies market cap and sector for every symbol even where its statement
endpoints are gated.

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
