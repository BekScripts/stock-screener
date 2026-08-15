---
type: explanation
title: Architecture
---

# Architecture

Compounder Radar reduces thousands of listings to a small group worth
researching. Almost every structural decision here follows from one observation
about that job: **the ranking is the product, and the ranking is only as good as
the arithmetic underneath it.**

That makes the metric engine the part worth protecting. Everything else —
providers, persistence, the CLI, the API — exists to feed it or to display what
it produced, and is arranged so that none of it can make the arithmetic harder to
verify.

## Shape

```text
src/stock_screener/   the application — config, logging, ingestion, scanner, scoring, CLI, API
  config.py           the only place that reads the environment
  logging.py          structlog setup
  providers.py        selects an adapter from settings
  scanning/           the feature: ingestion, scanner, report
  scoring/            the feature: scoring run, rankings, report

packages/domain/      metric engine, eligibility rules and CompounderScore — pure, no I/O
packages/api-clients/ provider protocols and adapters
packages/data-access/ tables, idempotent writes, row↔model translation
migrations/           alembic revisions
```

Both feature directories are organised by feature rather than by technical layer.
Ingestion, screening and reporting are one workflow; scoring, ranking and
explaining are another. Splitting either into parallel `services/`, `schemas/`
and `handlers/` trees would mean every change touched three directories to
accomplish one thing.

## Dependency direction

```text
src/stock_screener  →  packages/*  →  packages/domain
```

Strictly one-way. `domain` depends on nothing but pydantic, `api-clients` and
`data-access` depend on `domain` for vocabulary, and the application composes all
three. A package importing from `src/stock_screener` is always wrong.

uv cannot enforce this — Python will import anything on the path. It is held up
by review and by the `python-package` skill.

## Why the metric engine is a package with no I/O

`domain` could have been a module inside `src/`. Making it a package with an
empty dependency list buys one thing: it is impossible to accidentally reach for
a database or an HTTP client from inside a calculation, because neither is
installed as far as that package is concerned.

The payoff is that every metric can be tested against a hand-worked example.
`test_metrics.py` asserts that 135 against 100 four quarters ago is `0.35`, and
that is a claim about arithmetic a reader can check in their head. A screener
whose numbers are only verifiable by running it is a screener nobody can trust
enough to act on.

The same logic explains why the engine takes sequences of models rather than a
session: a function that queries for its own inputs cannot be given inputs.

## Missing is not zero

This is the invariant that runs through every layer, and the one most likely to
be broken by a well-meaning change.

A company with three quarters of history has no trailing-twelve-month revenue.
Returning `0` there would be a lie that survives every subsequent step: it would
be summed into an average, sorted into a ranking, and eventually shown to someone
as a fact. So `ttm_revenue` returns `None`, the column is nullable, the CSV cell
is empty, and the API serialises `null`.

The enforcement points are deliberate and repeated:

- `_ratio` in the metric engine returns `None` for any absent or zero
  denominator, so no individual formula has to remember.
- Every financial column is nullable. A `NOT NULL DEFAULT 0` would reintroduce
  the lie at the storage layer regardless of what the code does.
- Adapters return `None` for a field a vendor omitted, including when the value
  is present but unparseable.
- The CSV writes an empty cell, because a spreadsheet average skips a blank and
  counts a zero.

The mirror case matters just as much: a company that reported exactly zero gross
profit did report something, and that is `0.0`. `net_cash` of zero means cash
offsets debt precisely. The two must never be conflated in either direction.

## The provider boundary

Vendors disagree about field names, sign conventions, pagination and coverage.
`api-clients` absorbs all of that and returns `domain` models, so no vendor
vocabulary reaches the metric engine — see
[ADR-0003](../adr/0003-normalise-provider-data-at-the-boundary.md) for the
decision and the alternatives rejected.

One consequence worth stating plainly: the mock providers are not a testing
crutch. They return the same domain models the real adapters do, so a clone with
no credentials runs the entire pipeline, and the integration tests exercise real
ingestion code rather than a parallel implementation.

## Idempotency lives in the database

The daily job re-fetches overlapping data on purpose: a restated quarter must
replace the old one, and recent bars get corrected. That makes "insert if new,
update if seen" the only write pattern that leaves the database correct after a
second run.

Rather than select-then-write — which has a race and costs two round trips per
row — the repositories use dialect-aware `INSERT ... ON CONFLICT DO UPDATE`
against the unique constraints on `(company_id, period_end)` and
`(company_id, date)`. Those constraints are load-bearing. Removing one would not
fail a test immediately; it would slowly accumulate duplicate quarters that
quietly double a trailing-twelve-month total.

## Failure is expected, not exceptional

A market-wide scan touches thousands of symbols through a metered API. Some
requests will fail. The ingestion loop therefore treats a per-ticker failure as
data, not as an emergency: it is logged with its symbol, counted in an
`IngestionReport`, and the loop continues.

The distinction the report draws is between **failed** and **skipped**. A
provider that has no coverage for a symbol is a gap, not an error, and lumping
the two together would make a coverage hole look like an outage every night.

Retry policy follows the same reasoning. Rate limits and server errors are
transient and get exponential backoff; rejected credentials are not, and
retrying them three thousand times is how an API key gets suspended.

## Boundaries that matter

- **Configuration has one door.** `config.py` is the only reader of the
  environment; everything else receives a `Settings` object. The eligibility
  thresholds live there rather than as constants in the screening code, because
  tuning them is the main thing this project does — that should be an
  environment variable, not a commit.
- **A package's public API is its `__init__.py`.** Reaching into a submodule
  from outside turns an internal detail into something that cannot change.
- **Entry points stay thin.** `__main__.py` delegates to `cli.run()`. A branch on
  business state there is a branch in the wrong place.
- **Dependencies are passed in.** Sessions and providers are arguments, which is
  why the whole pipeline can be tested against SQLite and a fake transport with
  no patching.

## Cheap data broadly, expensive data narrowly

The three providers are not interchangeable, and the difference that matters is
not quality but **quota**. Alpaca serves the whole universe in a few dozen
batched requests; EDGAR serves any filer's entire history in two, for free, at
ten requests a second. A commercial fundamentals plan answers a few hundred
requests a *day*.

Building the scan on the third would mean a market-wide refresh measured in
weeks — and, when its quota ran out mid-run, a ranking of nothing at all. That is
not hypothetical: it is what the first full-market run produced, because market
capitalisation came only from that provider and a company without one is
correctly excluded as unscreenable.

So the pipeline is shaped by cost. The broad scan uses only what is free and
complete, including a market capitalisation multiplied out from the cover-page
share count in the filings. Only once there is a preliminary ranking is the
metered provider asked anything, and only about the few hundred companies a
reader will actually see — where it buys the two things filings cannot give: a
quoted market cap to cross-check the calculated one, and consolidated volume,
which is the only figure the liquidity threshold may be applied to.

Every figure carries where it came from. `market_cap_source` distinguishes a
quote from a calculation, `volume_basis` distinguishes whole-market volume from
one exchange's share, and `ranking_state` says whether a row has been through the
second pass. The formula is identical in both passes: enrichment replaces inputs,
never rules. See
[ADR-0008](../adr/0008-broad-scan-on-free-data-metered-enrichment-last.md).

## Why the score is a pure function too

CompounderScore lives in `domain` beside the metric engine, for the same reason:
it takes calculated metrics and returns a score, touching nothing else. Every
curve in it can be tested against the reference points printed in
[CompounderScore v1](../reference/compounder-score.md) — "20% growth earns 6
of 12 points" is a claim a reader can check without a database.

The application layer does the rest, and the split is where the I/O is:
`scoring/engine.py` reads stored rows, hands each company to the formula, and
writes a snapshot; `scoring/rankings.py` reads those snapshots back as the four
views. Neither knows a curve.

Scores are stored in their own table rather than as columns on
`financial_snapshots`. That table holds what a company reported; a score is an
opinion derived from it under a named set of rules. Mixed together, a restatement
and a re-score would be indistinguishable — and the daily history that makes
"improving fast" possible would have nowhere to live.

The version identifier on every row is the other half of that. A formula that
changes without a new version turns a change in the rules into an apparent change
in the business, which is exactly what the score-change views would surface
first. See [ADR-0006](../adr/0006-score-snapshots-are-versioned-and-immutable.md).

## What is deliberately absent

Not every company gets a number. A bank, an ineligible security and a company
with two quarters of history each get a stored row carrying the status that says
why — `UNSUPPORTED_SECTOR`, `NOT_ELIGIBLE`, `INSUFFICIENT_DATA` — and no score.
Forcing a number onto all three would put meaningless values into a ranking that
sorts on exactly that field.

There is no AI research layer and no dashboard. Both are later phases, and both
depend on the ranking being worth reading first.

## Decisions

Significant, hard-to-reverse choices are recorded in [ADRs](../adr/index.md).
