"""SEC EDGAR adapter: quarterly fundamentals from XBRL company facts.

EDGAR is the source the commercial vendors resell. It is free, needs no key, and
covers every U.S. filer down to the smallest micro cap — which is the coverage a
screener looking for small companies actually needs.

The cost is that XBRL is filings data, not a tidy financial API, and three of its
properties will produce wrong numbers if ignored:

**Tags vary between filers, and over time.** Apple reports revenue under
`RevenueFromContractWithCustomerExcludingAssessedTax`; another company uses
`Revenues` or `SalesRevenueNet`. Taxonomies also retire tags, so one company's
history is often split across two of them. Each concept therefore has a fallback
chain and the whole chain is **merged**, with the more specific tag winning for
any period both cover.

**The same period is reported many times.** A quarter appears in its own 10-Q,
again in the next year's comparatives, and again in each 10-K — Apple has four
facts for one 2018 quarter. Later filings supersede earlier ones, so facts are
de-duplicated by period keeping the most recently *filed* value, which is also
what makes restatements land correctly.

**Cash-flow figures are cumulative, and Q4 is never filed at all.** Within a
fiscal year a filer reports three months, then six, then nine, then the 10-K's
twelve — every fact sharing the year's start date. Consecutive rungs of that
ladder are differenced to recover discrete quarters, and the same operation
produces the fourth quarter that no filer states directly. Skip this and three
quarters in four have no cash flow, while every trailing-twelve-month figure —
which needs four *consecutive* quarters — comes back `None`.

Differencing only applies to concepts that accumulate. A weighted-average share
count is an average, not a sum, and subtracting one from another yields negative
share counts.

Balance-sheet concepts are *instant* facts — a value at a date, with no start —
while income and cash-flow concepts are *durations*. They are collected
separately and joined on the period end.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import httpx

from api_clients._http import RateLimiter, RetryPolicy, request_json
from api_clients.errors import ProviderDataError
from domain import CompanyProfile, FinancialPeriod, normalise_ticker

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

PROVIDER_NAME = "sec-edgar"

DEFAULT_DATA_URL = "https://data.sec.gov"
DEFAULT_WWW_URL = "https://www.sec.gov"

#: SEC asks for no more than ten requests a second, and blocks clients that
#: ignore it. One tenth of a second between requests keeps us inside that.
DEFAULT_MIN_INTERVAL_SECONDS = 0.11

#: A duration this long is one quarter. Fiscal quarters run 13 weeks and drift a
#: few days either way; the window excludes six-month and nine-month cumulative
#: figures, which filers report alongside the discrete ones.
_QUARTER_MIN_DAYS = 80
_QUARTER_MAX_DAYS = 100

_YEAR_MIN_DAYS = 340
_YEAR_MAX_DAYS = 380

#: Quarters that must already be known inside a fiscal year before its final
#: quarter can be derived from the annual figure.
_QUARTERS_BEFORE_YEAR_END = 3

#: Concept fallback chains, **most specific first**. Every tag in a chain is
#: merged, with earlier entries overriding later ones for periods both cover, so
#: ordering decides precedence rather than which tag is consulted at all.
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
_GROSS_PROFIT_TAGS = ("GrossProfit",)
_COST_OF_REVENUE_TAGS = (
    "CostOfGoodsAndServicesSold",
    "CostOfRevenue",
    "CostOfGoodsSold",
)
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_OPERATING_CASH_FLOW_TAGS = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
_CAPEX_TAGS = (
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
)
_SHARES_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)

_CASH_TAGS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
_SHORT_TERM_INVESTMENTS_TAGS = (
    "ShortTermInvestments",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
    "MarketableSecuritiesCurrent",
)
#: Interest-bearing borrowings and finance leases. Operating-lease liabilities
#: are deliberately absent: they are not borrowings, and the schedules filers tag
#: (`LesseeOperatingLeaseLiabilityPaymentsDue*`) are future minimum payments
#: rather than a balance-sheet amount.
_LONG_TERM_DEBT_TAGS = (
    "LongTermDebtNoncurrent",
    "LongTermDebt",
    "ConvertibleDebtNoncurrent",
    "ConvertibleNotesPayable",
    "NotesPayableRelatedPartiesNoncurrent",
    "FinanceLeaseLiabilityNoncurrent",
)
_SHORT_TERM_DEBT_TAGS = (
    "LongTermDebtCurrent",
    "DebtCurrent",
    "ShortTermBorrowings",
    "ConvertibleDebtCurrent",
    "ConvertibleNotesPayableCurrent",
    "NotesPayableCurrent",
    "FinanceLeaseLiabilityCurrent",
)

_USD = "USD"
_SHARES = "shares"


class SecEdgarFundamentals:
    """Quarterly fundamentals from the SEC's XBRL company-facts API.

    One request per company returns every fact the filer has ever tagged, which
    is why this needs no per-statement calls the way a commercial API does.

    Profiles carry identity only. EDGAR has no market capitalisation and no
    sector, so those stay `None` — pair this with a provider that supplies them,
    or the eligibility screen will exclude everything for missing market cap.

    Args:
        user_agent: Contact string sent on every request. **The SEC requires
            this** and blocks requests without a real one; it should name the
            application and an email address.
        data_base_url: Host serving the XBRL API.
        www_base_url: Host serving the ticker-to-CIK map.
        timeout_seconds: Per-request timeout. Company-facts documents run to
            several megabytes, so this is generous.
        limiter: Request spacing. Defaults to the SEC's ten-per-second ceiling.
        retry: Retry policy. Defaults apply if omitted.
        client: HTTP client, injected by tests.
    """

    def __init__(
        self,
        user_agent: str,
        *,
        data_base_url: str = DEFAULT_DATA_URL,
        www_base_url: str = DEFAULT_WWW_URL,
        timeout_seconds: float = 60.0,
        limiter: RateLimiter | None = None,
        retry: RetryPolicy | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not user_agent.strip():
            raise ValueError("the SEC requires a User-Agent naming the app and a contact email")

        self._data_base_url = data_base_url.rstrip("/")
        self._www_base_url = www_base_url.rstrip("/")
        self._client = client or httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout_seconds,
            follow_redirects=True,
        )
        self._limiter = limiter or RateLimiter(DEFAULT_MIN_INTERVAL_SECONDS)
        self._retry = retry or RetryPolicy()
        self._cik_by_ticker: dict[str, int] | None = None
        self._facts_cache: tuple[str, dict[str, Any]] | None = None

    def close(self) -> None:
        """Release the underlying HTTP connection."""
        self._client.close()

    def __enter__(self) -> SecEdgarFundamentals:
        """Return self so the adapter can be used as a context manager."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the client on exit."""
        self.close()

    # -- FundamentalsProvider ---------------------------------------------

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        """Return the registrant's name for one ticker.

        EDGAR publishes filings, not market data, so there is no market
        capitalisation, sector or industry here. All three stay `None` for a
        provider that has them to fill in.

        Args:
            ticker: The symbol to look up.

        Returns:
            A profile carrying identity only, or None when the SEC does not map
            this ticker to a filer.

        Raises:
            ProviderError: If a request failed.
        """
        symbol = normalise_ticker(ticker)
        cik = self._cik_for(symbol)
        if cik is None:
            return None

        facts = self._company_facts(cik)
        return CompanyProfile(
            ticker=symbol,
            name=str(facts.get("entityName") or symbol),
            # Filings are in USD unless a filer says otherwise; the units key on
            # each fact is checked when the figures themselves are read.
            currency=_USD,
        )

    def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
        """Return normalised quarterly financials, oldest period first.

        Args:
            ticker: The symbol to look up.
            limit: Maximum quarters to return, most recent kept.

        Returns:
            One period per fiscal quarter the filer has reported, including
            fourth quarters derived from the annual figure. Empty when the SEC
            does not cover the symbol.

        Raises:
            ProviderError: If a request failed or the payload was unusable.
        """
        symbol = normalise_ticker(ticker)
        cik = self._cik_for(symbol)
        if cik is None:
            return []

        gaap = self._company_facts(cik).get("facts", {}).get("us-gaap", {})
        if not gaap:
            return []

        flows = {
            "revenue": _quarterly_series(gaap, _REVENUE_TAGS),
            "gross_profit": _quarterly_series(gaap, _GROSS_PROFIT_TAGS),
            "cost_of_revenue": _quarterly_series(gaap, _COST_OF_REVENUE_TAGS),
            "operating_income": _quarterly_series(gaap, _OPERATING_INCOME_TAGS),
            "operating_cash_flow": _quarterly_series(gaap, _OPERATING_CASH_FLOW_TAGS),
            "capital_expenditure": _quarterly_series(gaap, _CAPEX_TAGS),
            # Weighted averages are not additive, so no ladder differencing:
            # a fiscal Q4 share count simply stays missing rather than being
            # invented by subtraction.
            "shares_outstanding": _quarterly_series(
                gaap, _SHARES_TAGS, unit=_SHARES, additive=False
            ),
        }
        instants = {
            "cash": _instant_series(gaap, _CASH_TAGS),
            "short_term_investments": _instant_series(gaap, _SHORT_TERM_INVESTMENTS_TAGS),
            "long_term_debt": _instant_series(gaap, _LONG_TERM_DEBT_TAGS),
            "short_term_debt": _instant_series(gaap, _SHORT_TERM_DEBT_TAGS),
        }
        period_ends = sorted({end for series in flows.values() for end in series})
        periods = [_build_period(period_end, flows, instants) for period_end in period_ends]
        return periods[-limit:] if limit > 0 else periods

    # -- internals ---------------------------------------------------------

    def _cik_for(self, symbol: str) -> int | None:
        """Return the SEC filer number for a ticker, loading the map once."""
        if self._cik_by_ticker is None:
            self._cik_by_ticker = self._load_cik_map()
        return self._cik_by_ticker.get(symbol)

    def _load_cik_map(self) -> dict[str, int]:
        """Fetch and index the SEC's ticker-to-CIK file.

        Raises:
            ProviderDataError: If the file is not in the expected shape.
        """
        payload = request_json(
            self._client,
            "GET",
            f"{self._www_base_url}/files/company_tickers.json",
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            raise ProviderDataError("sec returned an unexpected ticker map")

        mapping: dict[str, int] = {}
        for entry in payload.values():
            if not isinstance(entry, dict):
                continue
            ticker = entry.get("ticker")
            cik = entry.get("cik_str")
            if isinstance(ticker, str) and isinstance(cik, int):
                mapping[normalise_ticker(ticker)] = cik
        if not mapping:
            raise ProviderDataError("sec ticker map contained no usable entries")
        return mapping

    def _company_facts(self, cik: int) -> dict[str, Any]:
        """Fetch every XBRL fact for one filer.

        The document is several megabytes and both public methods need it, so the
        most recent one is held. A single-entry cache is enough: ingestion works
        through one company at a time.
        """
        key = f"{cik:010d}"
        if self._facts_cache is not None and self._facts_cache[0] == key:
            return self._facts_cache[1]

        payload = request_json(
            self._client,
            "GET",
            f"{self._data_base_url}/api/xbrl/companyfacts/CIK{key}.json",
            provider=PROVIDER_NAME,
            limiter=self._limiter,
            retry=self._retry,
        )
        if not isinstance(payload, dict):
            raise ProviderDataError(f"sec returned an unexpected facts payload for CIK {key}")

        self._facts_cache = (key, payload)
        return payload


def _latest_filed(facts: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """Return the most recently filed fact from a group covering one period.

    The same quarter is filed repeatedly — in its own 10-Q, in next year's
    comparatives, and in each 10-K. The newest filing is the one that reflects
    any restatement, so it wins.
    """
    dated = [fact for fact in facts if isinstance(fact.get("filed"), str)]
    if not dated:
        return next(iter(facts), None)
    return max(dated, key=lambda fact: str(fact["filed"]))


def _facts_for_tag(gaap: Mapping[str, Any], tag: str, unit: str) -> list[Mapping[str, Any]]:
    """Return the raw facts filed under one concept tag."""
    entries = gaap.get(tag, {}).get("units", {}).get(unit)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _merge_chain(
    build: Callable[[list[Mapping[str, Any]]], dict[date, float]],
    gaap: Mapping[str, Any],
    tags: Sequence[str],
    unit: str,
) -> dict[date, float]:
    """Combine every tag in a fallback chain, most specific winning per period.

    Taking only the first tag that has *any* facts is not enough: accounting
    taxonomies retire tags, so a filer's history is often split across two of
    them. Apple still carries five `AvailableForSaleSecuritiesDebtSecuritiesCurrent`
    facts from 2010 while reporting sixty-two current ones under
    `MarketableSecuritiesCurrent` — stopping at the first non-empty tag returned
    a decade-old number and nothing recent.

    Chains are therefore merged. Tags are applied least-specific first so that a
    more specific tag overwrites for any period both cover, which also gives a
    filer that switched tags mid-history one continuous series.

    Args:
        build: Turns one tag's raw facts into a period-to-value mapping.
        gaap: The `us-gaap` block of a company-facts document.
        tags: Concept fallback chain, most specific first.
        unit: Unit key to read.

    Returns:
        The merged series.
    """
    merged: dict[date, float] = {}
    for tag in reversed(tags):
        merged.update(build(_facts_for_tag(gaap, tag, unit)))
    return merged


def _quarterly_series(
    gaap: Mapping[str, Any],
    tags: Sequence[str],
    unit: str = _USD,
    *,
    additive: bool = True,
) -> dict[date, float]:
    """Return one value per fiscal quarter end for a concept.

    Two shapes have to be handled, and most filers use both for different
    statements. Income-statement concepts are usually filed as discrete
    three-month durations. Cash-flow concepts are almost always filed
    **cumulatively**: every fact in a fiscal year shares that year's start date,
    so a 10-Q reports three months, then six, then nine, and the 10-K reports
    twelve. Read at face value, only the first quarter of each year would have a
    cash-flow figure — which is exactly what happens if the ladder is ignored.

    Consecutive rungs of such a ladder are therefore differenced: nine months
    less six months is the third quarter. The same operation yields the fourth
    quarter, which no filer reports directly, as the year less the nine months
    before it.

    Args:
        gaap: The `us-gaap` block of a company-facts document.
        tags: Concept fallback chain.
        unit: Unit key to read, `USD` for money and `shares` for counts.
        additive: Whether the concept accumulates over the year. True for flows
            like revenue and cash generated. **False for weighted averages such
            as diluted share count**, where the annual figure is the average of
            its quarters rather than their sum — differencing those produces
            negative share counts, which is how this was caught.

    Returns:
        A mapping of period end to value, discrete quarters only.
    """

    def build(facts: list[Mapping[str, Any]]) -> dict[date, float]:
        return _quarters_from_facts(facts, additive=additive)

    return _merge_chain(build, gaap, tags, unit)


def _quarters_from_facts(facts: list[Mapping[str, Any]], *, additive: bool) -> dict[date, float]:
    """Turn one tag's duration facts into discrete quarters."""
    if not facts:
        return {}

    direct: dict[date, list[Mapping[str, Any]]] = {}
    ladders: dict[date, dict[date, list[Mapping[str, Any]]]] = {}

    for fact in facts:
        start = _as_date(fact.get("start"))
        end = _as_date(fact.get("end"))
        if start is None or end is None:
            continue
        span = (end - start).days
        if _QUARTER_MIN_DAYS <= span <= _QUARTER_MAX_DAYS:
            direct.setdefault(end, []).append(fact)
        # Deliberately not `elif`. A three-month fact beginning at the fiscal
        # year start is both a discrete quarter *and* the first rung of that
        # year's cumulative ladder. Excluding it from the ladder leaves the
        # second quarter with nothing to subtract from, which is precisely how
        # every Q2 cash flow went missing.
        if span <= _YEAR_MAX_DAYS:
            ladders.setdefault(start, {}).setdefault(end, []).append(fact)

    series = _resolve(direct)
    if additive:
        resolved_ladders = {start: _resolve(rungs) for start, rungs in ladders.items()}
        for start, rungs in resolved_ladders.items():
            _difference_ladder(series, start, rungs)
        # A filer reporting discrete quarters files each with its own start, so
        # its annual figure shares a start with nothing and the ladder above
        # cannot reach the fourth quarter. Subtracting the three known quarters
        # from the year is the only route to it for those filers.
        for start, rungs in resolved_ladders.items():
            _derive_year_end_quarter(series, start, rungs)
    return series


def _resolve(grouped: Mapping[date, list[Mapping[str, Any]]]) -> dict[date, float]:
    """Collapse each period's competing facts to the most recently filed value."""
    resolved: dict[date, float] = {}
    for period_end, group in grouped.items():
        chosen = _latest_filed(group)
        value = _as_float(chosen.get("val")) if chosen else None
        if value is not None:
            resolved[period_end] = value
    return resolved


def _derive_year_end_quarter(
    series: dict[date, float], start: date, rungs: Mapping[date, float]
) -> None:
    """Fill a fiscal year's final quarter from the annual figure.

    Used where the cumulative ladder cannot help: a filer that reports each
    quarter discretely gives every fact its own start date, so the annual figure
    has no six- or nine-month rung to subtract. The three quarters inside the
    year serve instead.

    Refuses unless exactly three quarters sit inside the year and they leave one
    quarter's gap to its end — otherwise the subtraction would manufacture a
    "quarter" out of a partially covered year, which is worse than a missing one.

    Args:
        series: Discrete quarters so far, updated in place.
        start: The fiscal-year start shared by these facts.
        rungs: Cumulative value by period end, including the annual figure.
    """
    for end, total in rungs.items():
        span = (end - start).days
        if not (_YEAR_MIN_DAYS <= span <= _YEAR_MAX_DAYS) or end in series:
            continue

        inside = sorted(known for known in series if start <= known < end)
        if len(inside) != _QUARTERS_BEFORE_YEAR_END:
            continue

        gap = (end - inside[-1]).days
        if not (_QUARTER_MIN_DAYS <= gap <= _QUARTER_MAX_DAYS):
            continue

        series[end] = total - sum(series[known] for known in inside)


def _difference_ladder(series: dict[date, float], start: date, rungs: Mapping[date, float]) -> None:
    """Turn one fiscal year's cumulative figures into discrete quarters.

    Gaps only: a discrete quarter the filer reported directly always wins over
    one reconstructed by subtraction.

    Args:
        series: Discrete quarters so far, updated in place.
        start: The fiscal-year start every rung shares.
        rungs: Cumulative value by period end.
    """
    previous_end, previous_value = start, 0.0
    for end in sorted(rungs):
        gap = (end - previous_end).days
        if _QUARTER_MIN_DAYS <= gap <= _QUARTER_MAX_DAYS and end not in series:
            series[end] = rungs[end] - previous_value
        previous_end, previous_value = end, rungs[end]


def _instant_series(gaap: Mapping[str, Any], tags: Sequence[str]) -> dict[date, float]:
    """Return one balance-sheet value per reporting date for a concept.

    Instant facts carry an `end` and no `start`, which is how they are told apart
    from the duration facts on the income and cash-flow statements.
    """
    return _merge_chain(_instants_from_facts, gaap, tags, _USD)


def _instants_from_facts(facts: list[Mapping[str, Any]]) -> dict[date, float]:
    """Turn one tag's instant facts into a value per reporting date."""
    grouped: dict[date, list[Mapping[str, Any]]] = {}

    for fact in facts:
        if fact.get("start") is not None:
            continue
        value_end = _as_date(fact.get("end"))
        if value_end is not None:
            grouped.setdefault(value_end, []).append(fact)

    series: dict[date, float] = {}
    for period_end, group in grouped.items():
        chosen = _latest_filed(group)
        value = _as_float(chosen.get("val")) if chosen else None
        if value is not None:
            series[period_end] = value
    return series


def _nearest_instant(series: Mapping[date, float], period_end: date) -> float | None:
    """Return a balance-sheet value at or very near a period end.

    A balance sheet is dated to the same day as the quarter that closes it, but
    a filer occasionally dates it a day or two out. Anything further away is a
    different quarter and is not substituted.
    """
    if period_end in series:
        return series[period_end]
    for offset in (1, -1, 2, -2, 3, -3):
        candidate = period_end + timedelta(days=offset)
        if candidate in series:
            return series[candidate]
    return None


def _build_period(
    period_end: date,
    flows: Mapping[str, Mapping[date, float]],
    instants: Mapping[str, Mapping[date, float]],
) -> FinancialPeriod:
    """Assemble one quarter from the collected series.

    Args:
        period_end: The quarter being built.
        flows: Duration-based series by field name.
        instants: Balance-sheet series by field name.
    """
    revenue = flows["revenue"].get(period_end)
    gross_profit = flows["gross_profit"].get(period_end)
    if gross_profit is None:
        # Not every filer tags gross profit; those that don't usually tag the
        # cost of revenue, and the subtraction is exact rather than an estimate.
        cost = flows["cost_of_revenue"].get(period_end)
        if revenue is not None and cost is not None:
            gross_profit = revenue - cost

    capex = flows["capital_expenditure"].get(period_end)
    cash = _nearest_instant(instants["cash"], period_end)
    short_term_investments = _nearest_instant(instants["short_term_investments"], period_end)
    if cash is not None and short_term_investments is not None:
        cash += short_term_investments

    return FinancialPeriod(
        period_end=period_end,
        revenue=revenue,
        gross_profit=gross_profit,
        operating_income=flows["operating_income"].get(period_end),
        operating_cash_flow=flows["operating_cash_flow"].get(period_end),
        # XBRL reports capital expenditure as a positive payment, but the domain
        # model's contract is an outflow either way.
        capital_expenditure=None if capex is None else abs(capex),
        cash=cash,
        total_debt=_total_debt(instants, period_end),
        shares_outstanding=flows["shares_outstanding"].get(period_end),
        reported_currency=_USD,
        source=PROVIDER_NAME,
    )


def _total_debt(instants: Mapping[str, Mapping[date, float]], period_end: date) -> float | None:
    """Return short- plus long-term debt for a period, or None if none is tagged.

    XBRL has no single `totalDebt`; there are only the individual instruments,
    and a filer with only one kind tags only that one. Summing what is present
    is therefore correct rather than partial — unlike a vendor's `totalDebt`
    fallback, where a missing component means the vendor omitted it.

    **Absence is not zero.** A filer tags a line item only while it exists, so a
    company that repaid its borrowings stops tagging them and looks identical to
    one whose borrowing tag this chain does not recognise. Reading absence as
    zero would let the second case pass as debt-free, and a risky company
    appearing financially strong is a far worse error than a debt-free one
    losing its net-cash figure. An explicit zero in the filing is still a
    recognised value and is returned as `0.0`.

    Args:
        instants: Balance-sheet series by field name.
        period_end: The quarter being built.

    Returns:
        Total debt, `0.0` where the filing states zero, or None where no
        borrowing was tagged at all.
    """
    long_term = _nearest_instant(instants["long_term_debt"], period_end)
    short_term = _nearest_instant(instants["short_term_debt"], period_end)
    if long_term is None and short_term is None:
        return None
    return (long_term or 0.0) + (short_term or 0.0)


def _as_date(value: object) -> date | None:
    """Parse an ISO date, returning None when absent or malformed."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _as_float(value: object) -> float | None:
    """Return a float, or None when the value is absent or not numeric."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def load_cik_map_from_file(path: str) -> dict[str, int]:
    """Load a ticker-to-CIK map from a saved copy of the SEC's file.

    Useful for tests and for running against a pinned snapshot rather than
    re-fetching the map.

    Args:
        path: Location of a `company_tickers.json` copy.

    Returns:
        A mapping of normalised ticker to filer number.

    Raises:
        ProviderDataError: If the file cannot be read or parsed.
    """
    try:
        with open(path, encoding="utf-8") as handle:  # noqa: PTH123 — plain read
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise ProviderDataError(f"could not read the CIK map: {exc}") from exc

    return {
        normalise_ticker(entry["ticker"]): entry["cik_str"]
        for entry in payload.values()
        if isinstance(entry, dict) and isinstance(entry.get("ticker"), str)
    }
