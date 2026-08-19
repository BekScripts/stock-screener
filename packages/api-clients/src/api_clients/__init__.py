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

from api_clients._http import RateLimiter, RetryPolicy
from api_clients.alpaca import AlpacaMarketData
from api_clients.base import (
    ExternalResearchProvider,
    FundamentalsProvider,
    MarketDataProvider,
)
from api_clients.composite import CompositeFundamentals
from api_clients.edgar import SecEdgarFundamentals
from api_clients.errors import (
    ProviderAuthError,
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
from api_clients.mock import MockFundamentals, MockMarketData, load_fixture_providers
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
from api_clients.websearch import MockExternalResearch, TavilySearch

__all__ = [
    "DEFAULT_MAX_OUTPUT_TOKENS",
    "DEFAULT_RESEARCH_MODEL",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_SECTION_CHARS",
    "MIN_SECTION_CHARS",
    "AlpacaMarketData",
    "AnthropicResearch",
    "CompositeFundamentals",
    "ExternalResearchProvider",
    "ExtractedSection",
    "FmpFundamentals",
    "FundamentalsProvider",
    "MarketDataProvider",
    "MockExternalResearch",
    "MockFundamentals",
    "MockMarketData",
    "MockResearch",
    "ProviderAuthError",
    "ProviderConfigurationError",
    "ProviderDataError",
    "ProviderError",
    "ProviderInvalidRequestError",
    "ProviderPlanError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "RateLimiter",
    "ResearchCompletion",
    "ResearchProvider",
    "RetryPolicy",
    "SecEdgarFundamentals",
    "TavilySearch",
    "clean_filing_text",
    "estimate_cost_usd",
    "extract_sections",
    "load_fixture_providers",
    "parse_draft",
]

__version__ = "0.1.0"
