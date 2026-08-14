"""Persistence and caching for securities data.

This module is the package's public API. Consumers import from the package root
only::

    from data_access import CompanyRepository, session_scope

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package owns the three Phase 1 tables, how they are written idempotently, and
the translation between stored rows and `domain` models. It does not know where
the data came from or what will be calculated from it.
"""

from data_access.converters import to_company_profile, to_financial_period, to_price_bar
from data_access.models import Base, Company, FinancialSnapshot, PriceHistory
from data_access.repositories import (
    CompanyRepository,
    FinancialSnapshotRepository,
    PriceHistoryRepository,
    UnsupportedDialectError,
)
from data_access.session import (
    build_session_factory,
    create_all,
    create_engine_from_url,
    session_scope,
)

__all__ = [
    "Base",
    "Company",
    "CompanyRepository",
    "FinancialSnapshot",
    "FinancialSnapshotRepository",
    "PriceHistory",
    "PriceHistoryRepository",
    "UnsupportedDialectError",
    "build_session_factory",
    "create_all",
    "create_engine_from_url",
    "session_scope",
    "to_company_profile",
    "to_financial_period",
    "to_price_bar",
]

__version__ = "0.1.0"
