"""Outbound HTTP clients for market data providers.

This module is the package's public API. Consumers import from the package root
only::

    from api_clients import AlpacaMarketData, MarketDataProvider

Everything re-exported here is a supported contract. Anything not listed in
`__all__` is internal and may change without a version bump.

The package's job is translation. Vendor payloads come in; `domain` models go
out. No vendor field name, status code or pagination token crosses this boundary,
so the rest of the system can be tested — and a vendor replaced — without knowing
which one is in use.
"""

from api_clients._http import RateLimiter, RetryPolicy
from api_clients.alpaca import AlpacaMarketData
from api_clients.base import FundamentalsProvider, MarketDataProvider
from api_clients.composite import CompositeFundamentals
from api_clients.edgar import SecEdgarFundamentals
from api_clients.errors import (
    ProviderAuthError,
    ProviderDataError,
    ProviderError,
    ProviderPlanError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from api_clients.fmp import FmpFundamentals
from api_clients.mock import MockFundamentals, MockMarketData, load_fixture_providers

__all__ = [
    "AlpacaMarketData",
    "CompositeFundamentals",
    "FmpFundamentals",
    "FundamentalsProvider",
    "MarketDataProvider",
    "MockFundamentals",
    "MockMarketData",
    "ProviderAuthError",
    "ProviderDataError",
    "ProviderError",
    "ProviderPlanError",
    "ProviderRateLimitError",
    "ProviderRequestError",
    "RateLimiter",
    "RetryPolicy",
    "SecEdgarFundamentals",
    "load_fixture_providers",
]

__version__ = "0.1.0"
