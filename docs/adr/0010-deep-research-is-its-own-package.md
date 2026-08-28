# 0010. Deep research is its own package, and appends its reports rather than replacing them

- Status: Accepted
- Date: 2026-08-17

## Context

Phase 6 adds on-demand deep research: a person types a ticker, and the system
investigates that one company against its current fundamentals, its filings and
what has since been published about it. Two questions had to be settled before
any of it could be built.

**Where does the contract live?** ADR 0009 put the Phase 3 research contract in
`packages/research` and froze it. Deep research needs a different report — a
sixth evidence namespace for external sources, seventeen sections instead of
thirteen, a fifth claim basis — while reusing the same score breakdown, metric
facts, reported periods and filing excerpts. It could extend the existing
package, live in the application, or be its own.

**How is a report stored?** `research_reports` carries a unique constraint on
`(company_id, score_version, brief_fingerprint, prompt_version)` and upserts into
it. Re-reading the same evidence under the same prompt produces the same report,
so a second row would be waste. It was not obvious that the same reasoning
applies to a deep report.

A third pressure shaped both answers. The requirement that AI research must never
revise the CompounderScore is met structurally in Phase 3 — the report has no
field to put a score in — and the external namespace is exactly the kind of
addition that erodes a structural guarantee into a convention.

## Decision

**We will keep the deep research contract in a new workspace package,
`packages/deep-research`,** containing four modules and no I/O: `evidence`
(the `W.` namespace), `brief`, `report` and `provenance`. It depends on `domain`,
`research` and pydantic.

It **imports** the Phase 3 deterministic primitives rather than copying them —
`ScoreEvidence`, `MetricFact`, `ReportedPeriod`, `FilingReference`, `FilingText`,
`EnrichmentFacts`, `ConfidenceLevel`, `ResearchStatus` — because the
deterministic half of a deep brief is a Phase 3 brief. It adds nothing to any
Phase 3 enum and subclasses nothing.

**`research.EvidenceKind` will not learn about `W.`.** The external namespace is
recognised only by `deep_research.is_external`, so `research.evidence_kind` on a
web id returns None and the Phase 3 validator cannot mistake a news article for
a metric. The absence is the mechanism.

**`deep_research_reports` will append, not upsert.** No unique constraint on the
cache key; an index serves the lookup, and the repository reads the newest
matching row. `DeepResearchReportRepository` has no update or delete path.

## Alternatives considered

**Add deep-research modules to `packages/research`.** No new pyproject, README,
AGENTS.md, CI matrix entry or ADR, and primitive reuse would be a local import.
Rejected because it puts a frozen contract and an actively changing one behind
one version number and one public `__init__`, and because the separation is the
product requirement: `ResearchReport` and `DeepResearchReport` must be obviously
different things. Two contracts exported from one module invites exactly the
confusion the phase was specified to avoid.

**Keep it in `src/stock_screener/deep_research/` until a second consumer
appears.** The repository's own rule is that one caller means `src/`. Rejected
because the second consumer is immediate rather than hypothetical: persisting a
report is `data-access`'s job, `DeepResearchReportRepository.save` must be typed
against the validated report, and a package may not import from the application.
The extraction would have happened in the same step.

**Add `WEB` to `research.EvidenceKind` and reuse `research.Basis`.** The obvious
move, and it would have avoided a parallel `DeepBasis`. Rejected because
`StrEnum` cannot be extended, so it would mean editing a frozen contract — and
because the edit would silently widen Phase 3: every `evidence_kind` call in the
Phase 3 validator would start resolving `W.` ids to a real kind. Leaving the enum
alone converts that risk into a guarantee for free.

**One fingerprint over the whole brief, as Phase 3 has.** Simpler, and adequate
as a cache key. Rejected because it cannot answer the question the refresh path
will actually ask: whether a report is stale because the fundamentals moved or
because somebody published something. Those have different costs and different
remedies, and a single hash conflates them permanently.

**Upsert deep reports on their cache key, matching Phase 3.** Consistent, and it
bounds the table. Rejected because a deep report is a dated investigation rather
than a derivation of a stored snapshot. Two runs a month apart over the same
company are two facts about what this system concluded, and the sequence — how a
thesis changed, and on what new evidence — is the most interesting thing the
table will ever hold. Overwriting is not idempotence here, it is deletion.

**Store external evidence in its own table.** Queryable across reports and
deduplicable across companies. Rejected because a report is always read whole,
which is the same argument that already keeps score breakdowns and Phase 3
reports as JSON. Embedding the cited items in the report document also makes a
stored report self-contained: a `W.` citation resolves to a title, publisher, URL
and date without a join, and cannot decay into a dangling id.

## Consequences

The workspace gains a fifth package, a fifth set of files to keep current, and a
fifth entry in the CI matrix. As with `research`, `domain`, `api-clients` and
`data-access`, the `[tool.uv.sources]` entries are load-bearing — though
`deep-research` was free on PyPI at the time of writing, so the shadowing risk is
lower here than for the others.

The root `pyproject.toml` declares `deep-research` before anything in `src/`
imports it, exactly as ADR 0009 did for `research`. The wiring is easy to forget
and expensive to debug, so it runs ahead of the code for one step.

`data-access` now depends on two contract packages. A change to either can
require a change there, and the dependency graph is one edge wider.

**The deep reports table grows without bound.** That is the point of the
decision, but it is a real cost: a company researched weekly accumulates rows
forever, each carrying a full report document and its cited sources. Nothing
prunes it today. If that becomes a problem the answer is a retention policy
applied deliberately — keep the newest N per company, or archive beyond a date —
not a unique constraint bolted back on, which would silently destroy the history
this decision exists to preserve.

`DeepBasis` and `research.Basis` will drift, because they are separate enums that
happen to share four members. A rule added to one does not reach the other, and
the Phase 3 validator and the future deep validator will need their own tests for
the same-sounding rule. That duplication is accepted as the price of not editing
a frozen contract.

Because external collection does not exist yet, every brief assembled today has
an empty external set, and any section resting on `W.` evidence will answer
`UNKNOWN`. That is the contract reporting a real limitation, not a defect.
