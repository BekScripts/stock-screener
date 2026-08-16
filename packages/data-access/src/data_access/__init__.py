"""Persistence and caching for securities data.

This module is the package's public API. Consumers import from the package root
only::

    from data_access import CompanyRepository, session_scope

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package owns the seven tables, how they are written idempotently, and the
translation between stored rows and `domain` models. It does not know where the
data came from, nor how a score is calculated — only how to store one and how to
read a day of them back in ranking order. The same applies to a research report:
this package stores a validated one and will not accept anything else, but has
no opinion about what makes it valid.
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
    StoredResearchReport,
    WatchlistEntry,
)
from data_access.repositories import (
    FINAL,
    JOB_FAILED,
    JOB_RUNNING,
    JOB_SUCCEEDED,
    JOB_UNKNOWN,
    PRELIMINARY,
    BenchmarkPriceRepository,
    CompanyRepository,
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
    "FINAL",
    "JOB_FAILED",
    "JOB_RUNNING",
    "JOB_SUCCEEDED",
    "JOB_UNKNOWN",
    "PRELIMINARY",
    "Base",
    "BenchmarkPrice",
    "BenchmarkPriceRepository",
    "Company",
    "CompanyRepository",
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
