"""The on-demand deep research contract: evidence in, grounded report out.

This module is the package's public API. Consumers import from the package root
only::

    from deep_research import DeepResearchBrief, DeepResearchReport

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

Deep research is a **separate layer over** Compounder Radar, not a replacement
for any part of it. Phase 3's `research` package answers "what do our metrics and
the SEC evidence we supplied say about this company?" and is frozen. This package
answers "somebody typed a ticker — investigate the company against its current
fundamentals, its filings and what has since been published about it".

`ResearchReport` and `DeepResearchReport` are different types with different
sections, different bases and different tables, and neither is built from the
other. What they share, they share by importing the same primitives from
`research`: the score breakdown, metric facts, reported periods, filing
references and excerpts are the same models here as there, because the
deterministic half of a deep brief is the deterministic half of a Phase 3 brief.

Three invariants run through it.

**The `W.` namespace never reaches the score.** External evidence is a namespace
of its own, `research.EvidenceKind` is left alone so a web id cannot resolve as
deterministic evidence, and the sections that explain the CompounderScore accept
`DETERMINISTIC` claims only.

**The report issues no instructions.** No score, rating, category,
recommendation or price target field exists on `DeepResearchReport`. The
conclusion describes how strong the evidence is and how interesting the company
is to research further, which is a statement about the research rather than
about what anyone should do.

**Provenance survives storage.** Every claim carries its basis and its citation
ids, and a stored report carries the external sources those ids name — so a
`W.` citation still resolves to a title, a publisher, a URL and a date long after
the brief that produced it is gone.

Nothing here performs I/O and nothing here calls a model. Refreshing a company,
collecting external evidence, calling a provider and storing a report are all
application concerns.
"""

from deep_research.brief import (
    CURRENT_DEEP_CONTRACT_VERSION,
    DEEP_RESEARCH_V1,
    DataFreshness,
    DeepResearchBrief,
    MarketRanking,
)
from deep_research.evidence import (
    EXTERNAL_PREFIX,
    MAX_EXCERPT_CHARS,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    external_evidence_id,
    is_external,
)
from deep_research.prompt import (
    CURRENT_DEEP_PROMPT_VERSION,
    DEEP_RESEARCH_PROMPT_V1,
    DEEP_RESEARCH_PROMPT_V2,
    build_deep_system_prompt,
    render_deep_brief,
)
from deep_research.provenance import (
    allowed_bases,
    allowed_kinds,
    allows_external,
    cites_filing_text,
    claim_problems,
    requires_evidence,
    supports,
    unsupported_citations,
)
from deep_research.report import (
    MAX_CLAIM_CHARS,
    MAX_DRAFT_CLAIM_CHARS,
    DeepBasis,
    DeepClaim,
    DeepConfidence,
    DeepDraftClaim,
    DeepIssueCode,
    DeepResearchDraft,
    DeepResearchReport,
    DeepSection,
    DeepSections,
    DeepValidationIssue,
    UnknownReason,
)
from deep_research.validation import (
    MIN_COVERAGE_FOR_HIGH,
    MIN_COVERAGE_FOR_MEDIUM,
    MIN_GROUNDED_FOR_HIGH,
    NO_EVIDENCE_TEXT,
    NO_VALID_CLAIMS_TEXT,
    deep_confidence,
    find_forecast,
    find_rating,
    unsupported_figures,
    validate_deep_report,
)

__all__ = [
    "CURRENT_DEEP_CONTRACT_VERSION",
    "CURRENT_DEEP_PROMPT_VERSION",
    "DEEP_RESEARCH_PROMPT_V1",
    "DEEP_RESEARCH_PROMPT_V2",
    "DEEP_RESEARCH_V1",
    "EXTERNAL_PREFIX",
    "MAX_CLAIM_CHARS",
    "MAX_DRAFT_CLAIM_CHARS",
    "MAX_EXCERPT_CHARS",
    "MIN_COVERAGE_FOR_HIGH",
    "MIN_COVERAGE_FOR_MEDIUM",
    "MIN_GROUNDED_FOR_HIGH",
    "NO_EVIDENCE_TEXT",
    "NO_VALID_CLAIMS_TEXT",
    "DataFreshness",
    "DeepBasis",
    "DeepClaim",
    "DeepConfidence",
    "DeepDraftClaim",
    "DeepIssueCode",
    "DeepResearchBrief",
    "DeepResearchDraft",
    "DeepResearchReport",
    "DeepSection",
    "DeepSections",
    "DeepValidationIssue",
    "ExternalEvidence",
    "ExternalSourceType",
    "MarketRanking",
    "SourceTier",
    "UnknownReason",
    "allowed_bases",
    "allowed_kinds",
    "allows_external",
    "build_deep_system_prompt",
    "cites_filing_text",
    "claim_problems",
    "deep_confidence",
    "external_evidence_id",
    "find_forecast",
    "find_rating",
    "is_external",
    "render_deep_brief",
    "requires_evidence",
    "supports",
    "unsupported_citations",
    "unsupported_figures",
    "validate_deep_report",
]

__version__ = "0.1.0"
