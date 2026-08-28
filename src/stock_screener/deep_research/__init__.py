"""Single-stock deep research preparation: refresh one company, then describe it.

The application layer over the `deep_research` contract package. It holds the
two halves Phase 3 established and Phase 6 keeps:

- `preparation` performs every network call, by running the **existing**
  ingestion and scoring passes for one ticker. No pipeline logic is
  reimplemented here and no second score exists — `prepare_company` calls
  `score_market`, which persists an ordinary snapshot.
- `brief` reads the result back out of the database and assembles a
  `DeepResearchBrief`, touching no provider at all.

Keeping them apart is what makes a brief reproducible: assembling twice over
unchanged data yields the same fingerprints, so the cache can work.

- `collection` searches for current external material and normalises what
  survives into `W.` evidence. It is the only other network stage, and it runs
  between the two: prepare, collect, assemble.
- `sources` holds the tier policy — who is worth citing, and which two links
  are the same article.

Nothing in this module calls a model or writes a `DeepResearchReport`.
External evidence is optional throughout: deterministic preparation never
depends on it, and a brief with `external=()` is a legal brief.
"""

from stock_screener.deep_research.brief import assemble_deep_brief
from stock_screener.deep_research.collection import (
    MAX_MARKET_REACTION_ITEMS,
    MIN_EXCERPT_CHARS,
    CollectionReport,
    EventClass,
    RejectedResult,
    Rejection,
    build_queries,
    classify_event,
    collect_external_evidence,
    evidence_from,
    search_anchor,
)
from stock_screener.deep_research.preparation import (
    BENCHMARK_STALENESS_DAYS,
    PreparationError,
    PreparationResult,
    Stage,
    StageOutcome,
    StageState,
    prepare_company,
    stored_freshness_dates,
)
from stock_screener.deep_research.report import (
    format_collection,
    format_preparation,
    format_run,
)
from stock_screener.deep_research.runner import (
    DeepResearchRun,
    RunStatus,
    run_deep_research,
)
from stock_screener.deep_research.sources import (
    DENIED_DOMAINS,
    TIER_1_DOMAINS,
    TIER_2_DOMAINS,
    TIER_3_DOMAINS,
    canonical_url,
    is_denied,
    registrable_domain,
    source_type_for,
    tier_for,
)

__all__ = [
    "BENCHMARK_STALENESS_DAYS",
    "DENIED_DOMAINS",
    "MAX_MARKET_REACTION_ITEMS",
    "MIN_EXCERPT_CHARS",
    "TIER_1_DOMAINS",
    "TIER_2_DOMAINS",
    "TIER_3_DOMAINS",
    "CollectionReport",
    "DeepResearchRun",
    "EventClass",
    "PreparationError",
    "PreparationResult",
    "RejectedResult",
    "Rejection",
    "RunStatus",
    "Stage",
    "StageOutcome",
    "StageState",
    "assemble_deep_brief",
    "build_queries",
    "canonical_url",
    "classify_event",
    "collect_external_evidence",
    "evidence_from",
    "format_collection",
    "format_preparation",
    "format_run",
    "is_denied",
    "prepare_company",
    "registrable_domain",
    "run_deep_research",
    "search_anchor",
    "source_type_for",
    "stored_freshness_dates",
    "tier_for",
]
