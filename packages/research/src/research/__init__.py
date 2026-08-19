"""The AI research contract: what the model is given, and what it may return.

This module is the package's public API. Consumers import from the package root
only::

    from research import ResearchBrief, validate_report

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package holds four things: the evidence a model is allowed to reason from
(`brief`), the shape of what comes back and what we accepted (`report`), the
rules that decide the difference (`validation`), and the instructions that ask
for it (`prompt`). None of them perform I/O and none of them call a model —
assembling a brief from the database, calling a provider and storing a report are
all application concerns.

Two invariants run through all of it. A report cannot restate the CompounderScore
as anything other than the number it was given, because `ResearchReport` has no
field to put a score in. And a report cannot introduce a figure the deterministic
layers did not produce, because every number in every claim is checked against
the brief before the claim is accepted.
"""

from research.brief import (
    CURRENT_CONTRACT_VERSION,
    EVIDENCE_PREFIXES,
    RESEARCH_V1,
    EnrichmentFacts,
    EvidenceKind,
    FilingReference,
    FilingText,
    MetricFact,
    Quantity,
    RankingState,
    ReportedPeriod,
    ResearchBrief,
    ScoreEvidence,
    ScoreItem,
    ScorePoint,
    SelectionReason,
    evidence_kind,
)
from research.prompt import (
    CURRENT_PROMPT_VERSION,
    RESEARCH_PROMPT_V1,
    RESEARCH_PROMPT_V2,
    build_system_prompt,
    render_brief,
)
from research.quantities import (
    Figure,
    Marker,
    find_advice,
    parse_figures,
    quoted_quantities,
    renderings,
    rounds_to,
)
from research.report import (
    MAX_CLAIM_CHARS,
    MAX_DRAFT_CLAIM_CHARS,
    Basis,
    Claim,
    ConfidenceLevel,
    DraftClaim,
    DraftReport,
    IssueCode,
    ReportSections,
    ResearchConfidence,
    ResearchReport,
    ResearchStatus,
    Section,
    ValidationIssue,
    confidence_rank,
)
from research.validation import (
    MIN_COVERAGE_FOR_HIGH,
    MIN_COVERAGE_FOR_MEDIUM,
    NO_EVIDENCE_TEXT,
    allowed_bases,
    confidence_ceiling,
    failed_report,
    unsupported_figures,
    validate_report,
)

__all__ = [
    "CURRENT_CONTRACT_VERSION",
    "CURRENT_PROMPT_VERSION",
    "EVIDENCE_PREFIXES",
    "MAX_CLAIM_CHARS",
    "MAX_DRAFT_CLAIM_CHARS",
    "MIN_COVERAGE_FOR_HIGH",
    "MIN_COVERAGE_FOR_MEDIUM",
    "NO_EVIDENCE_TEXT",
    "RESEARCH_PROMPT_V1",
    "RESEARCH_PROMPT_V2",
    "RESEARCH_V1",
    "Basis",
    "Claim",
    "ConfidenceLevel",
    "DraftClaim",
    "DraftReport",
    "EnrichmentFacts",
    "EvidenceKind",
    "Figure",
    "FilingReference",
    "FilingText",
    "IssueCode",
    "Marker",
    "MetricFact",
    "Quantity",
    "RankingState",
    "ReportSections",
    "ReportedPeriod",
    "ResearchBrief",
    "ResearchConfidence",
    "ResearchReport",
    "ResearchStatus",
    "ScoreEvidence",
    "ScoreItem",
    "ScorePoint",
    "Section",
    "SelectionReason",
    "ValidationIssue",
    "allowed_bases",
    "build_system_prompt",
    "confidence_ceiling",
    "confidence_rank",
    "evidence_kind",
    "failed_report",
    "find_advice",
    "parse_figures",
    "quoted_quantities",
    "render_brief",
    "renderings",
    "rounds_to",
    "unsupported_figures",
    "validate_report",
]

__version__ = "0.1.0"
