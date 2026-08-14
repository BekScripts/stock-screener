# domain

Screening rules and filter models, free of I/O.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** pure functions of already-fetched data — metric calculations,
eligibility rules, universe classification, and the models they operate on.

**Not here:** anything that performs I/O or reads configuration. No HTTP, no
database, no filesystem, no `datetime.now()`, no `os.environ`. If a change needs
one of those, it belongs in `api-clients`, `data-access`, or the application.

**Not here yet:** the Compounder Score, risk penalties, and any ranking. Those
are Phase 2. Do not add a placeholder — a score field returning zero is worse
than no score field.

## Invariants

- A metric the data cannot support returns `None`, never `0.0`. Route new ratios
  through `_ratio` rather than dividing directly.
- Compare periods by date with `_period_near`, never by list index.
- Growth from a non-positive base is `None`.
- Capital expenditure is treated as an outflow regardless of its sign.
- Every public function takes sequences in any order; normalise with
  `_ordered_periods` / `_ordered_bars` at the top.

Any new metric needs a test asserting a hand-worked number, plus tests for its
missing, zero and malformed-input cases. Update
`docs/reference/metrics.md` in the same change.

## Public API

`src/domain/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

pydantic only. This package depends on no other workspace package, and nothing
may be added that performs I/O. It must not import from the root app.
