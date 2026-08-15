# domain

Screening rules, scoring rules and filter models, free of I/O.

## Use

```python
from domain import build_company_metrics, evaluate_eligibility, score_company

metrics = build_company_metrics(profile, periods, bars)
verdict = evaluate_eligibility(profile, metrics, thresholds)
score = score_company(profile, metrics, benchmark, eligible=verdict.eligible)
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["domain"]

[tool.uv.sources]
domain = { workspace = true }
```

## What it contains

- `models` — the normalised vocabulary every other layer speaks:
  `PriceBar`, `FinancialPeriod`, `CompanyProfile`, `CompanyMetrics`,
  `EligibilityResult`, `EligibilityThresholds`.
- `metrics` — the deterministic metric engine. Growth, margins, cash, dilution,
  returns, 52-week range, enterprise value, and the assembly function that
  produces a full `CompanyMetrics`.
- `eligibility` and `universe` — the basic screen and the rules for which
  listings belong in the scannable universe.
- `scoring`, `curves`, `sectors` — CompounderScore v1: the four components, the
  risk penalties, the piecewise curves they are built from, and the businesses
  the model declines to score.
- `scores` — the value objects a score is expressed in: `CompanyScore`,
  `ComponentScore`, `SubScore`, `RiskAssessment` and their statuses.

Every formula, and the exact conditions under which each returns `None`, is
documented in [docs/reference/metrics.md](../../docs/reference/metrics.md).
Every scoring rule is in
[docs/reference/compounder-score.md](../../docs/reference/compounder-score.md).

## Boundaries

**Belongs here:** anything that is a pure function of already-fetched data.
Calculations, classification rules, thresholds as data, and the models those
operate on.

**Does not belong here:** anything that performs I/O or knows where data came
from. No HTTP, no database, no filesystem, no reading the clock or the
environment. The package's dependency list is pydantic and nothing else, and it
should stay that way — an empty dependency list is what makes it impossible to
accidentally query a database from inside a calculation.

Also not here: persisting a score, ranking a market, choosing a benchmark symbol,
or rendering a table. Producing the score is this package's job; everything
around it belongs to the application.

## Two rules that must not be relaxed

**Missing is not zero.** A metric the data cannot support returns `None`. `0.0`
means the company reported zero. Every ratio goes through `_ratio`, which is the
single chokepoint enforcing this — bypassing it is how a fabricated zero gets
into a ranking.

**Periods are matched by date, not list position.** A company that skipped a
filing must yield a missing metric, never a comparison against the wrong quarter.
