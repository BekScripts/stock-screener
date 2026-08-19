# deep-research

The on-demand deep research contract: deterministic evidence, current external
evidence, and the report they support.

## Use

```python
from deep_research import DeepResearchBrief, ExternalEvidence, external_evidence_id

brief = DeepResearchBrief(
    ticker="ACME",
    name="Acme Corp",
    as_of=score_date,
    assembled_at=datetime.now(UTC),
    score=score_evidence,  # research.ScoreEvidence, unchanged
    external=(article,),  # W.* items
)

cache_key = brief.evidence_fingerprint()
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["deep-research"]

[tool.uv.sources]
deep-research = { workspace = true }
```

## What it contains

- `evidence` — the `W.` namespace: `ExternalEvidence`, `ExternalSourceType`,
  `SourceTier`, and `external_evidence_id`, which derives a stable id from a URL
  so re-collecting the same article does not read as new evidence.
- `brief` — `DeepResearchBrief`, plus `DataFreshness` and `MarketRanking`. Three
  fingerprints: deterministic, external, and the combined `evidence_fingerprint`
  that is the cache key.
- `report` — `DeepBasis` (five bases), the seventeen `DeepSections`,
  `DeepResearchDraft` for unvalidated output and `DeepResearchReport` for what
  was accepted.
- `provenance` — which basis may cite which namespace, and which bases each
  section accepts.

## How it relates to `research`

`research` is Phase 3 and is frozen. It answers *"what do our deterministic
metrics and the SEC evidence we supplied say about this company?"* over a
selected candidate.

This package answers *"somebody typed a ticker — investigate it against current
fundamentals, filings and what has since been published"*. It **imports**
`research` for the deterministic primitives — `ScoreEvidence`, `MetricFact`,
`ReportedPeriod`, `FilingReference`, `FilingText`, `EnrichmentFacts`,
`ConfidenceLevel`, `ResearchStatus` — because the deterministic half of a deep
brief is identical to a Phase 3 brief and a second copy would be a second thing
to keep correct.

It does not subclass, rename, wrap or modify anything in `research`.
`ResearchReport` and `DeepResearchReport` are unrelated types stored in
different tables.

## Rules worth knowing before changing anything

- **`DeepResearchReport` has no score, rating, category, recommendation or
  target field, and must never gain one.** The CompounderScore is computed in
  `domain`, explained here, and revised nowhere.
- **`W.` never reaches the score.** `research.EvidenceKind` is deliberately not
  extended, so a web id resolves to nothing in the Phase 3 validator. The
  sections explaining the score accept `DETERMINISTIC` and `UNKNOWN` only.
- **`D.` proves a filing exists; `X.` quotes what it says.** Phase 3's rule,
  unchanged.
- **`UNKNOWN` cites nothing**, and is legal in every section.
- **Process metadata is never hashed.** `assembled_at`, each external item's
  `retrieved_at`, and `DataFreshness`'s `refreshed_at` / `refreshed` / `reused` /
  `stale` all record how the evidence was obtained rather than what it is.
  Hashing them would hand an unchanged company a new cache key every time an
  optional provider had a bad afternoon. The freshness *dates* are hashed —
  `price_as_of` moving is a real change in the evidence.
- Changing which inputs are supplied, which sections exist, or how a claim must
  be evidenced means a new `CURRENT_DEEP_CONTRACT_VERSION` — never an edit to an
  existing one, and never a change to `research.CURRENT_CONTRACT_VERSION`.

## What it does not do

No I/O of any kind: no HTTP, no database, no filesystem, no `datetime.now()`, no
configuration. In particular it does **not**:

- search the web or fetch a page — collection belongs in `api-clients`
- call a model — an LLM provider is an outbound HTTP dependency like any other
- refresh a company's data, recompute metrics or recompute a score — that is
  `domain` plus the application
- persist anything — `data-access` stores a validated report
- **validate a draft.** The provenance rules are here as pure functions; the
  validator that applies them, drops claims and composes confidence is a later
  step and is not to be started here without being asked.
