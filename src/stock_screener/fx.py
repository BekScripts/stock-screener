"""Resolving the exchange rates one scoring run needs.

The domain package performs no I/O, so it cannot go and find a rate; it takes
one and uses it. This is the piece that finds them, and it exists to answer one
question well: for this score date, what rate turns a dollar market
capitalisation into the currency each company files in?

Three properties matter, and all three are about not doing the obvious thing:

**Ask once per pair, not once per company.** Forty companies reporting in euros
need one USD/EUR rate between them, not forty identical requests. Rates are
resolved for the distinct pairs a run actually needs and handed out from a map.

**Look in the store before the network.** A rate for a past date is final —
nothing about last Tuesday's fixing changes — so a stored observation is not a
cache to be revalidated, it is the answer. This also makes a re-run of a
historical score reproduce it exactly rather than approximately.

**Never reach forward, and never reach far back.** A rate published after the
score date did not exist when the score was computed. A rate from three weeks
before it is not evidence about the date in question. Both are refused, and the
company loses its currency-sensitive metrics rather than gaining a wrong one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from api_clients import (
    BroadFxRates,
    CompositeFxRates,
    EcbFxRates,
    MockFxRates,
    ProviderError,
)
from data_access import FxRateRepository
from domain import FxConversion, normalise_currency

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import date

    from sqlalchemy.orm import Session

    from api_clients import FxProvider
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)


class FxRateResolver:
    """Supplies dated rates to a scoring run, fetching each pair at most once.

    Args:
        session: Open database session, used to read and record observations.
        provider: Source of rates. None disables fetching entirely, which leaves
            the resolver serving whatever is already stored — the right
            behaviour for an offline run and for a deployment that has not
            configured a source.
        max_age_days: How many days before the requested date a rate may be
            dated and still be used.
    """

    def __init__(
        self,
        session: Session,
        provider: FxProvider | None,
        *,
        max_age_days: int,
    ) -> None:
        self._repository = FxRateRepository(session)
        self._provider = provider
        self._max_age_days = max_age_days
        self._resolved: dict[tuple[str, str], FxConversion | None] = {}

    def rate_for(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Return the rate converting `base` into `quote` for a score date.

        Args:
            base: Currency to convert from — what a market cap is quoted in.
            quote: Currency to convert to — what a company files in.
            as_of: The score date.

        Returns:
            The conversion, or None when no acceptable rate exists. None is an
            ordinary outcome and callers must treat it as a missing metric, not
            as a reason to fall back to an unconverted figure.
        """
        base, quote = normalise_currency(base), normalise_currency(quote)
        if base == quote:
            return None

        key = (base, quote)
        if key not in self._resolved:
            self._resolved[key] = self._resolve(base, quote, as_of)
        return self._resolved[key]

    def warm(self, pairs: Iterable[tuple[str, str]], as_of: date) -> int:
        """Resolve several pairs up front, before any company is scored.

        Not required — `rate_for` resolves lazily — but it makes a run's network
        traffic happen in one identifiable place rather than scattered through
        the scoring loop, and it means a rate failure is logged before five
        thousand companies rather than in the middle of them.

        Args:
            pairs: The `(base, quote)` pairs the run will need.
            as_of: The score date.

        Returns:
            How many pairs resolved to a usable rate.
        """
        found = 0
        for base, quote in pairs:
            if self.rate_for(base, quote, as_of) is not None:
                found += 1
        return found

    def _resolve(self, base: str, quote: str, as_of: date) -> FxConversion | None:
        """Find a rate in the store, then from the provider, then give up."""
        stored = self._repository.latest_on_or_before(
            base, quote, as_of, max_age_days=self._max_age_days
        )
        if stored is not None:
            return FxConversion(
                base=stored.base_currency,
                quote=stored.quote_currency,
                rate=stored.rate,
                rate_date=stored.rate_date,
                provider=stored.provider,
                retrieved_at=stored.retrieved_at,
            )

        if self._provider is None:
            return None

        try:
            fetched = self._provider.get_rate(base, quote, as_of)
        except ProviderError as exc:
            # An FX outage costs foreign companies their valuation sub-scores.
            # It must never end a run: the domestic market needs no rate at all,
            # and growth and margins are computed in one currency each.
            log.warning("fx lookup failed", base=base, quote=quote, error=str(exc))
            return None

        if fetched is None:
            log.info("no fx rate published for pair", base=base, quote=quote)
            return None

        if not self._within_bounds(fetched, as_of):
            log.warning(
                "fx rate outside the accepted window, refusing it",
                base=base,
                quote=quote,
                requested=str(as_of),
                returned=str(fetched.rate_date),
                max_age_days=self._max_age_days,
            )
            return None

        self._repository.save(fetched)
        return fetched

    def _within_bounds(self, conversion: FxConversion, as_of: date) -> bool:
        """Whether a fetched rate is close enough to the date asked for.

        A source may legitimately answer a Sunday with Friday's fixing. It may
        not answer with a rate from after the date — that money did not exist
        yet — nor with one from a month before, where the gap is larger than the
        valuation differences this system exists to detect.
        """
        age = (as_of - conversion.rate_date).days
        return 0 <= age <= self._max_age_days


def pairs_needed(
    profiles: Iterable[tuple[str | None, str | None]],
) -> set[tuple[str, str]]:
    """Return the distinct currency pairs a set of companies needs converting.

    Args:
        profiles: `(quote_currency, reporting_currency)` for each company.

    Returns:
        One entry per pair that genuinely differs. A domestic company
        contributes nothing, which is what keeps the FX layer entirely off the
        path that scores the overwhelming majority of the universe.
    """
    pairs: set[tuple[str, str]] = set()
    for quote_currency, reporting_currency in profiles:
        base = normalise_currency(quote_currency)
        quote = normalise_currency(reporting_currency)
        if base != quote:
            pairs.add((base, quote))
    return pairs


def build_fx_provider(settings: Settings) -> FxProvider | None:
    """Build the exchange-rate source, or None when rates are not to be fetched.

    Only foreign issuers ever reach it. A company that files in the currency its
    shares trade in — which is nearly the whole universe — needs no rate, so a
    misconfigured or unreachable source cannot affect domestic scoring.

    Args:
        settings: Application settings.

    Returns:
        The provider, or None when configured off.
    """
    if settings.fx_provider == "none":
        return None
    if settings.fx_provider == "mock":
        log.debug("building fx provider", provider="mock")
        return MockFxRates()
    if settings.fx_provider == "ecb":
        log.debug("building fx provider", provider="ecb")
        return EcbFxRates()

    # The ECB publishes an official fixing for thirty currencies and nothing for
    # the rest. The broad dataset answers only where the ECB does not, and every
    # stored rate records which source produced it, so authority is visible
    # rather than assumed.
    log.debug("building fx provider", provider="ecb+fallback")
    return CompositeFxRates((EcbFxRates(), BroadFxRates()))
