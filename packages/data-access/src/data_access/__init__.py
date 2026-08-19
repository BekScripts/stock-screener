"""Persistence and caching for securities data.

This module is the package's public API. Consumers import from the package root
only::

    from data_access import CompanyRepository, session_scope

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package owns the tables, how they are written idempotently, and the
translation between stored rows and `domain` models. It does not know where the
data came from, nor how a score is calculated — only how to store one and how to
read a day of them back in ranking order. The same applies to a research report:
this package stores a validated one and will not accept anything else, but has
no opinion about what makes it valid.

`deep_research_reports` is the one table that appends rather than upserting. A
deep report is a dated investigation, its history is the point, and
`DeepResearchReportRepository` has no update or delete path at all.
"""

from data_access.converters import (
    to_company_profile,
    to_filing,
    to_financial_period,
    to_price_bar,
)
from data_access.models import (
    Base,
    BenchmarkPrice,
    Company,
    FilingExcerptRecord,
    FilingRecord,
    FinancialSnapshot,
    JobRecord,
    PriceHistory,
    ScoreSnapshot,
    StoredDeepResearchReport,
    StoredResearchReport,
    WatchlistEntry,
)
from data_access.repositories import (
    EXTERNAL_DEGRADED,
    EXTERNAL_FRESH,
    EXTERNAL_REUSED,
    FINAL,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_UNKNOWN,
    MARKET_COVERAGE,
    PRELIMINARY,
    SINGLE_COVERAGE,
    BenchmarkPriceRepository,
    CompanyRepository,
    DeepResearchReportRepository,
    FilingExcerptRepository,
    FilingRepository,
    FinancialSnapshotRepository,
    JobRepository,
    PriceHistoryRepository,
    ResearchReportRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    UnsupportedDialectError,
    WatchlistRepository,
)
from data_access.session import (
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)

__all__ = [
    "EXTERNAL_DEGRADED",
    "EXTERNAL_FRESH",
    "EXTERNAL_REUSED",
    "FINAL",
    "JOB_FAILED",
    "JOB_RUNNING",
    "JOB_SUCCEEDED",
    "JOB_UNKNOWN",
    "MARKET_COVERAGE",
    "PRELIMINARY",
    "SINGLE_COVERAGE",
    "Base",
    "BenchmarkPrice",
    "BenchmarkPriceRepository",
    "Company",
    "CompanyRepository",
    "DeepResearchReportRepository",
    "FilingExcerptRecord",
    "FilingExcerptRepository",
    "FilingRecord",
    "FilingRepository",
    "FinancialSnapshot",
    "FinancialSnapshotRepository",
    "JobRecord",
    "JobRepository",
    "PriceHistory",
    "PriceHistoryRepository",
    "ResearchReportRepository",
    "ScoreRecord",
    "ScoreSnapshot",
    "ScoreSnapshotRepository",
    "StoredDeepResearchReport",
    "StoredResearchReport",
    "UnsupportedDialectError",
    "WatchlistEntry",
    "WatchlistRepository",
    "build_session_factory",
    "create_all",
    "create_engine_from_url",
    "session_scope",
    "to_company_profile",
    "to_filing",
    "to_financial_period",
    "to_price_bar",
]

__version__ = "0.1.0"
