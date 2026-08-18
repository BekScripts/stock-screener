---
type: how-to
title: Run a scan
---

# Run a scan

Get from a clean checkout to a table of eligible companies, then to a CSV you can
review by hand.

## First run, no credentials

Both providers default to `mock`, which reads `fixtures/sample_universe.json`.

```bash
make setup
make migrate
make scan
```

`make scan` runs `stock-screener run-scan`: universe, prices, fundamentals, then
the screen.

## Run the stages separately

Useful when debugging one stage, or when the fundamentals provider is metered and
you only need fresh prices.

```bash
uv run stock-screener update-universe
uv run stock-screener update-market
uv run stock-screener update-fundamentals
uv run stock-screener scan
```

Restrict any stage to specific symbols:

```bash
uv run stock-screener update-market --ticker NVDA --ticker AMD
```

## Export for manual review

```bash
uv run stock-screener scan --output eligible_stocks.csv
```

The CSV contains every company scanned — including exclusions and the reasons —
regardless of what the console table showed. Missing metrics are empty cells, so
a spreadsheet average skips them instead of counting a zero.

To see exclusions in the terminal too:

```bash
uv run stock-screener scan --all --limit 50
```

## Switch to live Alpaca data

Put the keys in `.env` (never in `.env.example`, never in a commit):

```bash
MARKET_DATA_PROVIDER=alpaca
ALPACA_API_KEY=your-key
ALPACA_SECRET_KEY=your-secret
# Paper keys begin `PK` and need the paper host, or /v2/assets returns 401.
ALPACA_TRADING_BASE_URL=https://paper-api.alpaca.markets
# A free data plan can only read the IEX feed; `sip` returns 403.
ALPACA_FEED=iex
```

Two things to know about a free Alpaca data plan:

- **`feed=sip` is rejected.** The consolidated tape needs a paid plan. Leaving
  the feed unset makes Alpaca default to SIP, which is why the setting is
  explicit.
- **IEX volume is not consolidated volume.** Prices are sound, but IEX carries
  ~2–4% of total volume, so the ADV liquidity filter will exclude companies that
  really do trade above the threshold. Either lower `MIN_AVG_DOLLAR_VOLUME` to
  compensate crudely, or upgrade for SIP.

Then re-run. The first `update-market` pulls `PRICE_HISTORY_DAYS` of history for
every company and takes a while; later runs resume from the last stored session.

Fundamentals stay on `mock` until an FMP key exists. Without real fundamentals
every company reports a missing market cap, so the scan will exclude them all
with `MISSING_REQUIRED_DATA` — that is the screen working, not a bug.

## Switch to live fundamentals

The recommended configuration takes statements from SEC EDGAR and the profile
from FMP:

```bash
FUNDAMENTALS_PROVIDER=edgar+fmp
SEC_USER_AGENT=Compounder Radar you@example.com
FUNDAMENTALS_API_KEY=your-fmp-key
FUNDAMENTALS_QUARTERS=20
```

EDGAR is free, needs no key, covers every U.S. filer including micro caps, and
carries full filing history — which is what makes growth acceleration and the
three-year CAGR computable. It publishes no market capitalisation, which is why
FMP's profile endpoint is paired with it; that endpoint works for every symbol
even on a free FMP plan.

The SEC asks for no more than ten requests a second and blocks callers who do
not identify themselves, so `SEC_USER_AGENT` is required and requests are spaced
automatically. One request per company returns its entire filing history.

### Using FMP alone

FMP alone is viable only on a plan whose statement endpoints cover the symbols
you scan; a free plan blocks most of them and caps history at five quarters,
which leaves growth acceleration and the three-year CAGR uncomputable.

**Re-check the mapping whenever a provider changes.** Field names, units and
sign conventions are assumptions until a live response confirms them:

```bash
export FUNDAMENTALS_API_KEY=your-key
uv run scripts/verify_fundamentals.py --ticker AAPL --expected-market-cap 3.4e12
```

The script prints every field the adapter reads beside its live value, reports
the capex sign convention, checks the units, and exits non-zero if anything is
missing or implausible. If the endpoints 404, the API version has moved — try
`--base-path /stable` and change the paths in
`packages/api-clients/src/api_clients/fmp.py` to match.

Once it passes:

```bash
FUNDAMENTALS_PROVIDER=fmp
FUNDAMENTALS_API_KEY=your-key
```

**Bound the first run.** The adapter costs four requests per company — profile
plus three statements — so a 4,000-name universe is 16,000 requests, which will
exhaust a metered plan. Ingest a slice, inspect it, then widen:

```bash
uv run stock-screener update-fundamentals --limit 50
uv run stock-screener scan --all --output first-fifty.csv
```

`--limit` takes companies in ticker order, so the same ones are chosen on every
run and a partial ingest stays reproducible. Later runs are far cheaper: a
company whose newest stored quarter matches the provider's is skipped without a
request.

## Tune the thresholds

The thresholds are configuration precisely so this does not need a code change:

```bash
MIN_MARKET_CAP=500000000 MIN_PRICE=5 uv run stock-screener scan
```

Re-running `scan` alone is cheap — it reads stored data and recalculates. There
is no need to re-ingest to try a different threshold.

## Use PostgreSQL instead of SQLite

```bash
createdb compounder_radar
export DATABASE_URL="postgresql+psycopg://user:pass@localhost:5432/compounder_radar"
make migrate
make scan
```

## Schedule it

Nothing in this project schedules itself — there is no worker, no scheduler
dependency and no timer. `run-daily` is named for the cadence it is designed
for, not for something that fires on its own. A cron entry is enough:

```cron
0 22 * * 1-5 cd /path/to/stock-screener && uv run stock-screener run-daily >> daily.log 2>&1
```

Weekdays after the close. Use `run-daily` rather than `scan`: **`scan` writes no
score snapshots**, so a schedule built on it leaves the rankings, the score
history and the dashboard permanently empty. `run-daily` ingests, scores, enriches
when a profile provider is configured, and prints the ranking.

Both are idempotent, so a run that overlaps a previous one updates rows rather
than duplicating them. Score snapshots are unique on
`(company_id, score_date, score_version)` — one row per company per day per
formula version — which is what makes a second run in the same day an update.

That uniqueness is also why the cadence matters: the `improving` view ranks on
the score change over 30 days, so skipped days leave gaps in the history it
reads.

## Related

- [Metrics](../reference/metrics.md) — every formula and its missing-data rule
- [Configuration](../reference/configuration.md) — every environment variable
