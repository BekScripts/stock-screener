"""Outbound HTTP clients for market data providers.

This module is the package's public API. Consumers import from the package root
only::

    from api_clients import AlpacaMarketData, MarketDataProvider

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package's job is translation. Vendor payloads come in; `domain` and
`research` models go out. No vendor field name, status code or pagination token
crosses this boundary,
so the rest of the system can be tested — and a vendor replaced — without knowing
which one is in use.
"""

from api_clients._http import RateLimiter, RequestMeter, RetryPolicy
from api_clients.alpaca import AlpacaMarketData
from api_clients.base import (
    ExternalResearchProvider,
    FundamentalsProvider,
    FxProvider,
    MarketDataProvider,
)
from api_clients.composite import CompositeFundamentals
from api_clients.deep_research import (
    AnthropicDeepResearch,
    DeepResearchCompletion,
    DeepResearchProvider,
    MockDeepResearch,
)
from api_clients.edgar import (
    SecEdgarFundamentals,
    facts_document_from_observations,
    filings_from_submissions,
    periods_from_facts_document,
    periods_from_observations,
)
from api_clients.errors import (
    ProviderAuthError,
    ProviderBudgetError,
    ProviderConfigurationError,
    ProviderDataError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from api_clients.filing_text import (
    MAX_SECTION_CHARS,
    MIN_SECTION_CHARS,
    ExtractedSection,
    clean_filing_text,
    extract_sections,
)
from api_clients.fmp import FmpFundamentals
from api_clients.fx import (
    BroadFxRates,
    CompositeFxRates,
    EcbFxRates,
)
from api_clients.mock import (
    MockFundamentals,
    MockFxRates,
    MockMarketData,
    load_fixture_providers,
)
from api_clients.research import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_RESEARCH_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    AnthropicResearch,
    MockResearch,
    ResearchCompletion,
    ResearchProvider,
    estimate_cost_usd,
    parse_draft,
)
from api_clients.sec_cover import CoverPage, parse_cover_page
from api_clients.sec_documents import (
    FetchedDocument,
    SecDocuments,
    document_url,
    submissions_url,
)
from api_clients.sec_ftd import (
    FTD_INDEX_URL,
    FtdArchive,
    FtdFileContent,
    FtdRow,
    SecFailsToDeliver,
    parse_ftd_archive,
    parse_ftd_member,
)
from api_clients.websearch import MockExternalResearch, TavilySearch

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_RESEARCH_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "FTD_INDEX_URL",
    "MAX_SECTION_CHARS",
    "MIN_SECTION_CHARS",
    "AlpacaMarketData",
    "AnthropicDeepResearch",
    "AnthropicResearch",
    "BroadFxRates",
    "CompositeFundamentals",
    "CompositeFxRates",
    "CoverPage",
    "DeepResearchCompletion",
    "DeepResearchProvider",
    "EcbFxRates",
    "ExternalResearchProvider",
    "ExtractedSection",
    "FetchedDocument",
    "FmpFundamentals",
    "FtdArchive",
    "FtdFileContent",
    "FtdRow",
    "FundamentalsProvider",
    "FxProvider",
    "MarketDataProvider",
    "MockDeepResearch",
    "MockExternalResearch",
    "MockFundamentals",
    "MockFxRates",
    "MockMarketData",
    "MockResearch",
    "ProviderAuthError",
    "ProviderBudgetError",
    "ProviderConfigurationError",
    "ProviderDataError",
    "ProviderError",
    "ProviderInvalidRequestError",
    "ProviderPlanError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "RateLimiter",
    "RequestMeter",
    "ResearchCompletion",
    "ResearchProvider",
    "RetryPolicy",
    "SecDocuments",
    "SecEdgarFundamentals",
    "SecFailsToDeliver",
    "TavilySearch",
    "clean_filing_text",
    "document_url",
    "estimate_cost_usd",
    "extract_sections",
    "facts_document_from_observations",
    "filings_from_submissions",
    "load_fixture_providers",
    "parse_cover_page",
    "parse_draft",
    "parse_ftd_archive",
    "parse_ftd_member",
    "periods_from_facts_document",
    "periods_from_observations",
    "submissions_url",
]

__version__ = "0.1.0"
