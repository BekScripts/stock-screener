"""The research feature: choosing companies, assembling evidence, storing reports.

The `research` workspace package says what a brief and a report are and which
reports are acceptable. This is the half that touches the database: it selects
candidates from the latest ranking, turns stored rows into briefs, and persists
what validation accepted.

Assembling a brief reads the database and only the database — a research run
must be able to work entirely from what the nightly scan already fetched, and
every input it reads is bounded by the date of the score it explains. The one
place a model is called is `runner`, and it treats every provider failure as a
`FAILED` report rather than an exception: scanning, scoring and ranking never
depend on a vendor being reachable.
"""

from stock_screener.research.brief import (
    DEFAULT_FILINGS,
    DEFAULT_HISTORY,
    DEFAULT_QUARTERS,
    DERIVED_SOURCE,
    SCORE_SOURCE,
    assemble_brief,
    assemble_briefs,
)
from stock_screener.research.candidates import (
    HIDDEN_GEM_LIMIT,
    MAX_CANDIDATES,
    MIN_DATA_COVERAGE,
    MIN_MOVER_SCORE,
    MIN_QUARTERS,
    MIN_SCORE_CHANGE_30D,
    SCORE_MOVER_LIMIT,
    TOP_RANKED_LIMIT,
    Candidate,
    select_candidates,
)
from stock_screener.research.report import (
    filing_evidence_ids,
    format_brief,
    format_candidates,
    format_research_run,
)
from stock_screener.research.runner import (
    RESERVED_PROMPT_TOKENS,
    ResearchOutcome,
    ResearchSkip,
    research_candidates,
    research_company,
)
from stock_screener.research.store import (
    USABLE_STATUSES,
    ResearchStorageError,
    find_cached_report,
    save_report,
)

__all__ = [
    "DEFAULT_FILINGS",
    "DEFAULT_HISTORY",
    "DEFAULT_QUARTERS",
    "DERIVED_SOURCE",
    "HIDDEN_GEM_LIMIT",
    "MAX_CANDIDATES",
    "MIN_DATA_COVERAGE",
    "MIN_MOVER_SCORE",
    "MIN_QUARTERS",
    "MIN_SCORE_CHANGE_30D",
    "RESERVED_PROMPT_TOKENS",
    "SCORE_MOVER_LIMIT",
    "SCORE_SOURCE",
    "TOP_RANKED_LIMIT",
    "USABLE_STATUSES",
    "Candidate",
    "ResearchOutcome",
    "ResearchSkip",
    "ResearchStorageError",
    "assemble_brief",
    "assemble_briefs",
    "filing_evidence_ids",
    "find_cached_report",
    "format_brief",
    "format_candidates",
    "format_research_run",
    "research_candidates",
    "research_company",
    "save_report",
    "select_candidates",
]
