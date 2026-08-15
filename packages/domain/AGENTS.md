# domain

Screening rules, scoring rules and the models they operate on, free of I/O.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** pure functions of already-fetched data — metric calculations,
eligibility rules, universe classification, CompounderScore and its risk
penalties, and the models all of them operate on.

**Not here:** anything that performs I/O or reads configuration. No HTTP, no
database, no filesystem, no `datetime.now()`, no `os.environ`. If a change needs
one of those, it belongs in `api-clients`, `data-access`, or the application.
Persisting a score, ranking a market and choosing a benchmark are all application
concerns; producing the score is this package's.

**Not here yet:** anything to do with AI research or the dashboard. Those are
Phases 3 and 4.

## Invariants

- A metric the data cannot support returns `None`, never `0.0`. Route new ratios
  through `_ratio` rather than dividing directly.
- Compare periods by date with `_period_near`, never by list index.
- Growth from a non-positive base is `None`.
- Capital expenditure is treated as an outflow regardless of its sign.
- Every public function takes sequences in any order; normalise with
  `_ordered_periods` / `_ordered_bars` at the top.
- A missing metric produces a `SubScore` with `points=None` — never zero and
  never full marks. Weight is redistributed inside its own component only.
- `shares_outstanding` is a weighted average and belongs to dilution;
  `common_shares_outstanding` is a point-in-time count and belongs to market
  capitalisation. Never substitute one for the other.
- A calculated market cap carries `MarketCapSource.CALCULATED`, and a derived
  gross profit carries `gross_profit_basis`. A figure whose provenance is lost is
  indistinguishable from a directly reported one, which is the failure these
  fields exist to prevent.
- A component's sub-score weights must sum to its maximum. `_build_component`
  raises `ScoringError` when they do not; do not silence it.
- Changing any curve, weight or coverage policy means a **new**
  `score_version` and a new `CURRENT_SCORE_VERSION`, never an edit to an
  existing one. `COMPOUNDER_V1_1` is current.

Any new metric needs a test asserting a hand-worked number, plus tests for its
missing, zero and malformed-input cases. Any new or changed scoring rule needs a
test at every reference point, between them, and outside both ends. Update
`docs/reference/metrics.md` and `docs/reference/compounder-score.md` in the
same change — the documentation and the code are the same formula stated twice.

## Public API

`src/domain/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

pydantic only. This package depends on no other workspace package, and nothing
may be added that performs I/O. It must not import from the root app.
