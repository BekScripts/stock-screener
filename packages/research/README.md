# research

The AI research contract: what the model is given, and what it is allowed to
return.

## Use

```python
from research import DraftReport, ResearchBrief, validate_report

report = validate_report(
    draft,
    brief,
    prompt_version="RESEARCH_PROMPT_V1",
    model_id="<provider model id>",
    generated_at=datetime.now(UTC),
)
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["research"]

[tool.uv.sources]
research = { workspace = true }
```

## What it contains

- `brief` — the evidence a report may rest on: `ResearchBrief` and the
  addressable pieces inside it (`ScoreEvidence`, `MetricFact`, `ReportedPeriod`,
  `FilingReference`, `EnrichmentFacts`), plus the fingerprint that decides
  whether a report is stale.
- `report` — what comes back: `Claim` and its `Basis`, the thirteen
  `ReportSections`, `DraftReport` for unvalidated output, and `ResearchReport`
  for what was accepted.
- `validation` — the rules between the two: citation resolution, per-section
  basis limits, the numeric whitelist, the advice check and the confidence
  ceiling.

## Boundaries

**Here:** the shape of the conversation with a model, and every check applied to
its answer. Pure functions of an already-assembled brief.

**Not here:** anything that performs I/O. No HTTP, no database, no filesystem, no
`datetime.now()`. Calling a model belongs in `api-clients` alongside the other
external providers; assembling a brief from stored data, choosing which companies
qualify, and persisting a report belong in the application.

**Not here yet:** prompt text, and filing-text extraction. `FilingReference`
carries metadata only for this contract version — `excerpt` is always `None`, and
sections that need filing text answer `UNKNOWN` until extraction exists.

## Rules worth knowing before changing anything

- `ResearchReport` has no score, rating or category field, and must never gain
  one. The CompounderScore is computed in `domain` and explained here, never
  revised here.
- Every number in a claim must already exist in the brief. New ratios,
  percentages, growth rates and multiples are rejected, not recalculated. A
  derived figure worth having belongs in Phase 1 or Phase 2, deterministically.
- A missing metric is `None` and renders as `unknown`. Never zero, in either
  direction.
- Changing which inputs are supplied, which sections are required, or how a claim
  must be evidenced means a new `CURRENT_CONTRACT_VERSION` — never an edit to an
  existing one.
