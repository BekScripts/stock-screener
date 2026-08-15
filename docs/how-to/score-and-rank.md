---
type: how-to
title: Score and rank the market
---

# Score and rank the market

Get from stored data to a shortlist, and from a shortlist to an explanation of
why one company is on it.

## Score for the first time

Scoring reads only what is already in the database, so ingest first — including
the benchmark, which is what relative strength is measured against.

```bash
uv run stock-screener update-universe
uv run stock-screener update-market
uv run stock-screener update-benchmark
uv run stock-screener update-fundamentals
uv run stock-screener score
```

None of that needs a metered provider. With `FUNDAMENTALS_PROVIDER=edgar` the
whole market is scanned, scored and ranked on free data: market capitalisation is
calculated from the cover-page share count and the latest close, and the SIC
description keeps banks out of the ranking.

`score` prints a status count and the top ten:

```text
scoring: processed=3841 scored=1204 insufficient_data=2103 not_eligible=498 unsupported_sector=36 persisted=3841
benchmark SPY: 6m 4.8%   12m 11.2%
```

Or run the whole thing in one command:

```bash
uv run stock-screener run-daily
```

If every company comes back `insufficient_data`, the benchmark series is missing.
Run `update-benchmark` and score again.

## Verify the top candidates

The broad scan cannot know consolidated volume, and its market caps are
calculated rather than quoted. `enrich` spends one metered request per top-ranked
candidate to replace both, then re-scores them with the same formula:

```bash
uv run stock-screener enrich                 # FMP_ENRICHMENT_LIMIT candidates
uv run stock-screener enrich --limit 50      # spend less
```

```text
enrichment: status=PARTIAL candidates=200 attempted=38 succeeded=37 uncovered=0
rate_limited=1 failed=0 skipped=162 rescored=37
```

A quota that runs out stops the pass. The 37 companies already verified are
`FINAL`; the rest stay `PRELIMINARY`, which is visible in the `ranking_state`
column, and the ranking still works. Companies whose consolidated volume turns
out to be below the threshold drop out here — which is exactly what the second
pass is for.

## Read the rankings

```bash
uv run stock-screener rankings                    # top 50
uv run stock-screener rankings --limit 20
uv run stock-screener rankings --min-score 70
```

The other three views are filters over the same snapshots:

```bash
uv run stock-screener hidden-gems     # small, fast, already scoring well
uv run stock-screener wrong-price     # strong business, poor valuation score
uv run stock-screener improving       # biggest score gain over 30 days
```

`improving` is empty until there is score history to compare against, and fills
in over the first month of nightly runs.

## Export for manual review

```bash
uv run stock-screener rankings --limit 50 --output rankings.csv
```

Every component score, the risk penalty, data coverage, the valuation basis and
both score changes are columns. Missing values are empty cells, not zeros, so a
spreadsheet average skips them.

## Ask why a company ranked where it did

```bash
uv run stock-screener explain NVDA
```

Each component prints with the points every metric earned and the value it earned
them on, followed by the risk penalties. Metrics that could not be calculated are
shown as unavailable rather than as zero — which is also the answer when a
company is missing from the ranking entirely:

```bash
uv run stock-screener explain SOFI
# Status: UNSUPPORTED_SECTOR
```

## Try a formula change safely

`--dry-run` calculates without writing, so the score history is untouched:

```bash
uv run stock-screener score --dry-run --preview 20
```

If you keep the change, give it a new `score_version` in `domain/scoring.py`
before scoring for real. Scores from two versions are never compared, so the
score-change views restart from that day — see
[ADR-0006](../adr/0006-score-snapshots-are-versioned-and-immutable.md).

## Score one company while iterating

```bash
uv run stock-screener score --ticker NVDA --dry-run
uv run stock-screener score --limit 100
```

## Schedule the nightly run

```cron
0 22 * * 1-5 cd /path/to/stock-screener && uv run stock-screener run-daily >> radar.log 2>&1
```

Every write is an upsert, so a run that repeats or overlaps updates rows rather
than duplicating them — including the day's scores.

## Related

- [CompounderScore v1](../reference/compounder-score.md) — every rule the score applies
- [Run a scan](run-a-scan.md) — ingestion and the eligibility screen
- [Commands](../reference/commands.md) — every command and option
