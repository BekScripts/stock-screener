# data-access

Persistence and caching for securities data.

## Use

```python
from data_access import CompanyRepository, build_session_factory, create_engine_from_url

factory = build_session_factory(create_engine_from_url(settings.database_url))
with session_scope(factory) as session:
    CompanyRepository(session).upsert_profile(profile)
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["data-access"]

[tool.uv.sources]
data-access = { workspace = true }
```

## What it contains

- `models` — the five tables: `companies`, `financial_snapshots`,
  `price_history`, `benchmark_prices` and `score_snapshots`.
- `session` — engine and session construction. No module-level engine.
- `repositories` — reads and idempotent upserts for each table.
- `converters` — translation between stored rows and `domain` models.

PostgreSQL is the intended production database; SQLite is supported so a clone
runs with no setup. The only place that difference is visible is the dialect-aware
upsert.

## Boundaries

**Belongs here:** table definitions, queries, write logic, and row↔model
translation.

**Does not belong here:** HTTP calls, metric calculations, eligibility rules, or
reading configuration. A repository takes a `Session`; it never builds one, and
it never reads `DATABASE_URL`.

## Invariants

**The unique constraints are load-bearing.** `(company_id, period_end)`,
`(company_id, date)` and `(company_id, score_date, score_version)` are what make
a re-run of the daily job an update rather than a duplicate. Removing one would
not fail a test immediately — it would slowly accumulate duplicate quarters that
quietly double a trailing-twelve-month total, or a second score for the same day.

**A score is not a reported fact.** Scores live in `score_snapshots`, never as
columns on `financial_snapshots`. Mixing derived opinion into a table of reported
figures would make a restatement indistinguishable from a re-score.

**Score reads take a version.** Every query and every prior-snapshot lookup
filters on `score_version`; comparing across versions measures the formula rather
than the business.

**Every financial column is nullable.** A `NOT NULL DEFAULT 0` would convert "not
reported" into "reported as zero" at the storage layer, defeating the care taken
everywhere else.

**Upserts do not overwrite with `None`.** Alpaca knows a company's exchange and
FMP knows its sector; whichever provider runs second must not blank out what the
other contributed.

Schema changes need an Alembic revision in the same change.
`tests/integration/test_migrations.py` asserts the migrated schema matches the
models, so a model edited without a revision fails there.
