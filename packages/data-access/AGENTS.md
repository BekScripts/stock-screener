# data-access

Persistence and caching for securities data.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** table definitions, engine and session construction, queries, upserts,
and translation between rows and `domain` models.

**Not here:** HTTP calls, metric calculations, eligibility rules, or reading the
environment. Repositories take a `Session` and never create one; the URL comes
from the caller.

## Invariants

- Never remove or weaken the unique constraints on
  `(company_id, period_end)` and `(company_id, date)`. They are what makes the
  daily job idempotent.
- Every financial column stays nullable. NULL means "not reported" and must read
  back as `None`.
- Upserts must not write `None` over an existing value — different providers
  populate different fields.
- De-duplicate rows within a single upsert call. PostgreSQL rejects an
  `ON CONFLICT` statement that touches the same row twice.
- Both PostgreSQL and SQLite must work. Use the dialect-aware `_insert_for`
  helper rather than a raw `insert()`.

## Migrations

A schema change needs an Alembic revision in the same change.
`tests/integration/test_migrations.py` compares the migrated schema against
`Base.metadata` and fails if they drift.

## Public API

`src/data_access/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

SQLAlchemy, psycopg, and the `domain` workspace package. It must not depend on
`api-clients` or import from the root app.
