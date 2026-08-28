# deep-research

The on-demand deep research contract: deterministic evidence, current external
evidence, and the report they support.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** what a deep research model is given, what shape its answer takes, and
which evidence may support which kind of claim. Pure functions over an
already-assembled brief.

**Not here:** anything that performs I/O or reads configuration. No HTTP, no
database, no filesystem, no `datetime.now()`, no `os.environ`. Web search and an
LLM client are external providers and belong in `api-clients`; refreshing a
company, assembling a brief from stored rows, and persisting a report are
application concerns.

**Not here yet, and not to be started without being asked:** the deep validator
(dropping claims, composing confidence, building a `DeepResearchReport`), the
deep prompt, the numeric whitelist, external collection, and any staleness
policy beyond `ExternalEvidence.age_in_days`. `provenance` states the rules; the
thing that applies them is a later step.

## Its relationship to `research`

`research` is Phase 3, and it is **frozen**. This package imports from it and
changes nothing in it. Specifically:

- Never add a member to `research.EvidenceKind`, `research.Basis`,
  `research.Section` or `research.IssueCode`. `W.` is absent from
  `EvidenceKind` on purpose — that absence is what makes a web id unresolvable
  to the Phase 3 validator.
- Never rename, subclass or repurpose a Phase 3 class. `ResearchReport` and
  `DeepResearchReport` are unrelated types.
- Do reuse the deterministic primitives outright: `ScoreEvidence`, `ScoreItem`,
  `ScorePoint`, `MetricFact`, `ReportedPeriod`, `FilingReference`, `FilingText`,
  `EnrichmentFacts`, `RankingState`, `ConfidenceLevel`, `ResearchStatus`,
  `confidence_rank`. Copying one of these into this package would be the wrong
  fix for any problem.

There is no `SelectionReason` here. A Phase 3 brief records why the screener
picked a company; a deep brief exists because somebody typed a ticker, and
inventing a selection reason to fill the field would put a fiction into the
fingerprint.

## Invariants

- `DeepResearchReport` carries **no** score, rating, category, recommendation or
  price-target field. The model cannot override the CompounderScore because
  there is nowhere to put one. Adding such a field is the one change to this
  package that is always wrong.
- **Web evidence never changes the score**, enforced three ways: the brief's
  `score` is a read-only `research.ScoreEvidence`; `CURRENT_SNAPSHOT` and
  `WHY_THE_ALGORITHM_LIKES_IT` accept `DETERMINISTIC` and `UNKNOWN` only; and
  the report has no score field. Weakening any one of the three needs all three
  reconsidered.
- `D.` proves a filing exists, `X.` quotes what it says, and only `X.` supports a
  claim about contents.
- `UNKNOWN` cites nothing and is legal in every section.
- A missing value is `None` and renders as `unknown`. Never `0.0`, in either
  direction.
- No tier exists below `TIER_3_SUPPORTING`, so social media, forums and blogs
  are unrepresentable in V1. Adding a tier for them is a product decision, not a
  tidy-up.
- **Fingerprints hash what the evidence is, never how it was obtained.**
  Excluded: `assembled_at`, each external item's `retrieved_at`, and
  `DataFreshness`'s `refreshed_at`, `refreshed`, `reused` and `stale`. Included:
  the freshness dates, because `price_as_of` moving is a real change. Adding a
  process field to `DataFreshness` means adding it to the exclusion set in
  `deterministic_fingerprint` in the same commit, or the cache silently stops
  working. External items are sorted by id before hashing, so collection order
  is not evidence.
- Changing which inputs are supplied, which sections exist, or how a claim must
  be evidenced means a new `CURRENT_DEEP_CONTRACT_VERSION`, never an edit to an
  existing one.

A new provenance rule needs tests for the case it catches, the case it must not
catch, and the boundary between them.

## Public API

`src/deep_research/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

`domain`, `research` and pydantic. This package must not import `data-access`,
`api-clients` or the root app — a contract that depended on persistence could
not be tested against a hand-written brief, which is the whole point of keeping
it separate.
