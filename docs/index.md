---
type: explanation
title: Overview
---

# stock-screener — Compounder Radar

> Scans U.S. equities and produces a short list worth researching.

**Phase 1 is complete**: universe and data ingestion, the derived-metric engine,
eligibility filtering, a CLI with CSV export, and a minimal API. There is
deliberately no Compounder Score yet — that is Phase 2.

## Getting started

```bash
make setup     # install dependencies, link agent skills, create .env
make migrate   # create the database schema
make scan      # run the pipeline against the sample fixture
make check     # lint, types, tests
```

No credentials are needed for that: both providers default to `mock`. See
[Run a scan](how-to/run-a-scan.md) to switch to live data.

## How these docs are organised

Documentation follows [Diátaxis](https://diataxis.fr): four types, split by what
the reader is trying to do. Every page declares its `type` in frontmatter, and a
page belongs to exactly one.

| Section | The reader is… | Written as |
| --- | --- | --- |
| [Tutorials](tutorials/index.md) | learning by doing | a lesson that always works |
| [How-to](how-to/index.md) | achieving a specific goal | a recipe for someone who knows the basics |
| [Reference](reference/index.md) | looking something up | dry, complete, no opinions |
| [Explanation](explanation/index.md) | trying to understand | discussion of the why |
| [Decisions](adr/index.md) | asking why it's built this way | numbered, immutable ADRs |

The split exists because these modes conflict. A tutorial that stops to explain
trade-offs loses the beginner; a reference page with a worked example goes stale.
See the `documentation` skill before adding a page.

## Where the code lives

| Path | Contains |
| --- | --- |
| `src/stock_screener/` | the application — config, ingestion, scanner, CLI, API |
| `packages/domain/` | the metric engine and eligibility rules, free of I/O |
| `packages/api-clients/` | provider protocols and the Alpaca, FMP and mock adapters |
| `packages/data-access/` | the three tables, idempotent writes, row↔model translation |
| `migrations/` | Alembic revisions |
| `tests/` | tests for `src/` |
| `.agents/skills/` | the procedures AI agents follow in this repo |
