"""Failures raised by provider adapters.

Callers catch `ProviderError` to keep a market-wide scan alive when one ticker
misbehaves. The subclasses exist so a caller that cares can tell a transient
outage apart from a misconfigured key — retrying an auth failure three thousand
times is how an API key gets suspended.

No exception message ever carries a credential.
"""

from __future__ import annotations


class ProviderError(Exception):
    """Base class for every provider failure."""


class ProviderAuthError(ProviderError):
    """Credentials are missing, wrong, or lack access to the endpoint.

    Never retried: repeating a rejected key does not make it valid.
    """


class ProviderRateLimitError(ProviderError):
    """The provider asked us to slow down.

    Retried with backoff, up to the configured attempt limit.
    """


class ProviderRequestError(ProviderError):
    """The request failed for a transport or server-side reason.

    Retried when the status code suggests it is transient.
    """


class ProviderPlanError(ProviderError):
    """The request is valid but the subscription does not permit it.

    Distinct from an auth failure (the key is fine) and from a rate limit (waiting
    will not help). It is raised out of the per-ticker loop rather than counted as
    one ticker's failure: if the plan forbids twenty quarters for one company it
    forbids them for all four thousand, and continuing would burn a daily quota
    to collect four thousand identical errors.
    """


class ProviderDataError(ProviderError):
    """The response arrived but could not be normalised.

    A schema change at the vendor usually surfaces here. It is not retried,
    because the same request will produce the same unparseable payload.
    """
