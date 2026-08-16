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
    PriceHistory,
    ScoreSnapshot,
    StoredResearchReport,
)
from data_access.repositories import (
    FINAL,
    PRELIMINARY,
    BenchmarkPriceRepository,
    CompanyRepository,
    FilingExcerptRepository,
    FilingRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    ResearchReportRepository,
    ScoreRecord,
    ScoreSnapshotRepository,
    UnsupportedDialectError,
)
from data_access.session import (
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)

__all__ = [
    "FINAL",
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
    "PriceHistory",
    "PriceHistoryRepository",
    "ResearchReportRepository",
    "ScoreRecord",
    "ScoreSnapshot",
    "ScoreSnapshotRepository",
    "StoredResearchReport",
    "UnsupportedDialectError",
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
