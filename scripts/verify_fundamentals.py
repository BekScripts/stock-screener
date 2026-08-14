#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx>=0.28"]
# ///
"""Check a fundamentals provider's real responses against what the adapter expects.

The FMP adapter in `packages/api-clients` was written from documented endpoint
shapes and has never seen a live response. Field names, units and sign
conventions are therefore assumptions. This script turns each assumption into a
pass or fail against one real ticker, before anything is ingested.

It is read-only: it issues GETs and prints. Nothing is written to the database.

Usage:
    export FUNDAMENTALS_API_KEY=your-key
    uv run scripts/verify_fundamentals.py --ticker AAPL
    uv run scripts/verify_fundamentals.py --ticker AAPL --expected-market-cap 3.4e12
    uv run scripts/verify_fundamentals.py --ticker AAPL --raw

Exit codes:
    0  every expected field was present and plausible
    1  one or more fields were missing, or a plausibility check failed
    2  the provider could not be reached, or the credentials were rejected
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

import httpx

DEFAULT_BASE_URL: Final = "https://financialmodelingprep.com"

#: FMP's current API. The older `/api/v3` paths return 403 "Legacy Endpoint" for
#: keys issued today. The stable API also takes the symbol as a query parameter
#: rather than a path segment, which is why `_get` builds the request that way.
DEFAULT_BASE_PATH: Final = "/stable"

#: Every field the adapter reads, by endpoint. Keep this in step with
#: `FmpFundamentals._merge` — a field added there and not here is unverified.
EXPECTED_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "profile": (
        "symbol",
        "companyName",
        "exchange",
        "sector",
        "industry",
        "marketCap",
        "currency",
        "isActivelyTrading",
        "isEtf",
        "isFund",
    ),
    "income-statement": (
        "date",
        "revenue",
        "grossProfit",
        "operatingIncome",
        "weightedAverageShsOutDil",
        "reportedCurrency",
    ),
    "balance-sheet-statement": (
        "date",
        "cashAndShortTermInvestments",
        "totalDebt",
    ),
    "cash-flow-statement": (
        "date",
        "operatingCashFlow",
        "capitalExpenditure",
        "freeCashFlow",
    ),
}

#: A large-cap's quarterly revenue is billions, not thousands. A provider
#: reporting in thousands would land far below this and make every company look
#: 1,000x smaller — the unit error that is invisible to a structural check.
_MIN_PLAUSIBLE_LARGE_CAP_REVENUE: Final = 1e8

_MARKET_CAP_TOLERANCE: Final = 0.25


class VerificationError(Exception):
    """A check failed in a way that should stop the run."""


def _key_from_env_file(path: Path, name: str) -> str:
    """Read one variable out of a `.env` file without importing a dependency.

    The application loads `.env` through pydantic-settings, but this script is
    standalone and stdlib-only. Reading the key here means it never has to be
    exported into a shell, echoed on a command line, or pasted as an argument.

    Args:
        path: The `.env` file to read. Missing is not an error.
        name: The variable to look for.

    Returns:
        The value with surrounding quotes stripped, or an empty string when the
        file or the variable is absent.
    """
    if not path.is_file():
        return ""

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def _get(client: httpx.Client, path: str, api_key: str, **params: Any) -> Any:
    """Issue one GET and return the decoded body.

    Raises:
        VerificationError: If the request failed or the body was not JSON.
    """
    query = {**params, "apikey": api_key}
    try:
        response = client.get(path, params=query)
    except httpx.HTTPError as exc:
        raise VerificationError(f"request to {path} failed: {type(exc).__name__}") from exc

    if response.status_code in (401, 403):
        raise VerificationError(f"credentials rejected (HTTP {response.status_code}) for {path}")
    if response.status_code == 404:
        raise VerificationError(
            f"{path} returned 404 — the endpoint path may have moved. "
            "Try --base-path /api/v3 or whatever FMP documents now"
        )
    if response.status_code >= 400:
        raise VerificationError(f"{path} returned HTTP {response.status_code}")

    try:
        return response.json()
    except ValueError as exc:
        raise VerificationError(f"{path} did not return JSON") from exc


def _first_row(payload: Any, endpoint: str) -> dict[str, Any]:
    """Return the first record of a list payload.

    Raises:
        VerificationError: If the payload is empty or not a list of objects.
    """
    if not isinstance(payload, list) or not payload:
        raise VerificationError(
            f"{endpoint} returned no rows — is the ticker covered by this plan?"
        )
    row = payload[0]
    if not isinstance(row, dict):
        raise VerificationError(f"{endpoint} returned {type(row).__name__}, expected an object")
    return row


def _report_fields(endpoint: str, row: dict[str, Any], expected: tuple[str, ...]) -> list[str]:
    """Print each expected field beside its live value; return the missing ones."""
    print(f"\n  {endpoint}")
    missing: list[str] = []

    for field in expected:
        if field not in row:
            missing.append(field)
            print(f"    {field:<32} MISSING FROM RESPONSE")
        elif row[field] is None:
            print(f"    {field:<32} null")
        else:
            value = row[field]
            rendered = f"{value:,}" if isinstance(value, int | float) else str(value)
            print(f"    {field:<32} {rendered[:60]}")

    unused = sorted(set(row) - set(expected))
    if unused:
        print(f"    ({len(unused)} further fields the adapter ignores)")
    return missing


def _check_capex_sign(cash_flow: dict[str, Any]) -> list[str]:
    """Report the capex sign convention and whether the adapter handles it."""
    capex = cash_flow.get("capitalExpenditure")
    ocf = cash_flow.get("operatingCashFlow")
    problems: list[str] = []

    print("\n  capex sign convention")
    if not isinstance(capex, int | float):
        problems.append("capitalExpenditure is not numeric — free cash flow cannot be derived")
        print("    capitalExpenditure is not a number; cannot check")
        return problems

    convention = "negative (outflow)" if capex < 0 else "positive"
    print(f"    capitalExpenditure  {capex:,.0f}  -> {convention}")
    print(f"    adapter stores      {abs(capex):,.0f}  (always a positive outflow)")

    if isinstance(ocf, int | float):
        derived = ocf - abs(capex)
        print(f"    derived FCF         {derived:,.0f}  = {ocf:,.0f} - {abs(capex):,.0f}")
        reported = cash_flow.get("freeCashFlow")
        if isinstance(reported, int | float):
            drift = abs(derived - reported)
            scale = max(abs(reported), 1.0)
            marker = "ok" if drift / scale < 0.01 else "MISMATCH"
            print(f"    provider FCF        {reported:,.0f}  ({marker})")
            if marker == "MISMATCH":
                problems.append(
                    "derived free cash flow disagrees with the provider's own figure by "
                    f"{drift:,.0f} — the sign convention or the capex field may be wrong"
                )
    return problems


def _check_units(
    profile: dict[str, Any], income: dict[str, Any], expected_market_cap: float | None
) -> list[str]:
    """Check the figures are whole units rather than thousands or millions."""
    problems: list[str] = []
    print("\n  units")

    revenue = income.get("revenue")
    if isinstance(revenue, int | float):
        print(f"    quarterly revenue   {revenue:,.0f}")
        if 0 < revenue < _MIN_PLAUSIBLE_LARGE_CAP_REVENUE:
            problems.append(
                f"quarterly revenue of {revenue:,.0f} is small for a large cap — "
                "the provider may be reporting in thousands or millions"
            )

    market_cap = profile.get("marketCap")
    if isinstance(market_cap, int | float):
        print(f"    market cap          {market_cap:,.0f}")
        if expected_market_cap is not None:
            drift = abs(market_cap - expected_market_cap) / expected_market_cap
            marker = "ok" if drift <= _MARKET_CAP_TOLERANCE else "MISMATCH"
            print(f"    you expected        {expected_market_cap:,.0f}  ({marker})")
            if marker == "MISMATCH":
                problems.append(
                    f"market cap is off by {drift:.0%} from the value you supplied — "
                    "suspect a unit mismatch"
                )
    return problems


def _check_currency(profile: dict[str, Any], income: dict[str, Any]) -> list[str]:
    """Report the reporting currency and whether the two sources agree."""
    print("\n  currency")
    profile_currency = profile.get("currency")
    statement_currency = income.get("reportedCurrency")
    print(f"    profile currency    {profile_currency}")
    print(f"    statement currency  {statement_currency}")

    if statement_currency and str(statement_currency).upper() != "USD":
        print("    -> the screen will exclude this company as UNSUPPORTED_CURRENCY")
    return []


def _check_periods(payload: Any, requested: int) -> list[str]:
    """Report how much history arrived, and which metrics that supports.

    The quarter count is the binding constraint on a metered plan, and it decides
    which metrics are computable at all — so it is reported as a capability list
    rather than a single pass or fail.
    """
    problems: list[str] = []
    dates = [row.get("date") for row in payload if isinstance(row, dict)]
    print("\n  period coverage")
    print(f"    quarters returned   {len(dates)}")
    if dates:
        print(f"    newest / oldest     {dates[0]} / {dates[-1]}")

    print(f"    requested           {requested}")

    available = len(dates)
    requirements = (
        (1, "margins, net cash"),
        (4, "TTM revenue"),
        (5, "YoY revenue growth, gross profit growth, dilution"),
        (6, "previous YoY growth, growth ACCELERATION"),
        (8, "TTM revenue growth"),
        (16, "3-year revenue CAGR"),
    )
    print("\n    metric availability at this history depth:")
    for needed, metrics in requirements:
        mark = "yes" if available >= needed else "NO "
        print(f"      {mark}  {needed:>2} quarters  {metrics}")

    return problems


def _plan_limits(payload: Any) -> list[str]:
    """Return capability limits imposed by the subscription, not by the adapter."""
    available = len([row for row in payload if isinstance(row, dict)])
    limits: list[str] = []
    if available < 6:
        limits.append(
            f"only {available} quarters of history are available on this plan, so growth "
            "acceleration cannot be calculated — one of the most distinctive signals in "
            "the specification"
        )
    if available < 8:
        limits.append("TTM revenue growth needs 8 quarters")
    if available < 16:
        limits.append("the 3-year revenue CAGR needs 16 quarters")
    return limits


def verify(
    ticker: str,
    api_key: str,
    base_url: str,
    base_path: str,
    *,
    raw: bool,
    quarters: int = 5,
    expected_market_cap: float | None = None,
) -> int:
    """Run every check against one ticker and return an exit code."""
    problems: list[str] = []
    print(f"Verifying the fundamentals adapter against {ticker} at {base_url}{base_path}")

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        payloads: dict[str, Any] = {}
        for endpoint in EXPECTED_FIELDS:
            path = f"{base_path}/{endpoint}"
            params: dict[str, Any] = {"symbol": ticker}
            if endpoint != "profile":
                params |= {"period": "quarter", "limit": quarters}
            payloads[endpoint] = _get(client, path, api_key, **params)

    if raw:
        print("\n--- raw first row per endpoint ---")
        for endpoint, payload in payloads.items():
            print(f"\n{endpoint}:")
            print(json.dumps(_first_row(payload, endpoint), indent=2, default=str)[:4000])

    print("\n--- field mapping ---")
    rows: dict[str, dict[str, Any]] = {}
    for endpoint, expected in EXPECTED_FIELDS.items():
        row = _first_row(payloads[endpoint], endpoint)
        rows[endpoint] = row
        missing = _report_fields(endpoint, row, expected)
        problems += [f"{endpoint}: `{field}` is absent from the response" for field in missing]

    print("\n--- plausibility ---")
    problems += _check_capex_sign(rows["cash-flow-statement"])
    problems += _check_units(rows["profile"], rows["income-statement"], expected_market_cap)
    problems += _check_currency(rows["profile"], rows["income-statement"])
    problems += _check_periods(payloads["income-statement"], quarters)
    limits = _plan_limits(payloads["income-statement"])

    print("\n--- summary ---")
    if problems:
        print(f"  {len(problems)} mapping problem(s) — the adapter needs changing:\n")
        for problem in problems:
            print(f"    - {problem}")
        print("\n  Fix packages/api-clients/src/api_clients/fmp.py before ingesting.")
    else:
        print(f"  Mapping confirmed: every expected field was present and plausible for {ticker}.")

    if limits:
        print("\n  Subscription limits — not adapter faults, but they cap what")
        print("  Phase 2 can score:\n")
        for limit in limits:
            print(f"    - {limit}")
        print("\n  Set FUNDAMENTALS_QUARTERS to what your plan allows.")

    return 1 if problems else 0


def main() -> int:
    """Parse arguments and run the verification."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--ticker", default="AAPL", help="symbol to verify against (default: AAPL)")
    parser.add_argument(
        "--api-key",
        default="",
        help="provider API key (default: $FUNDAMENTALS_API_KEY, then .env)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="file to read FUNDAMENTALS_API_KEY from when it is not in the environment",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="provider host")
    parser.add_argument(
        "--base-path",
        default=DEFAULT_BASE_PATH,
        help="path prefix before the endpoint name (try /stable if /api/v3 404s)",
    )
    parser.add_argument(
        "--expected-market-cap",
        type=float,
        default=None,
        help="a market cap you believe is correct, to catch a units mismatch",
    )
    parser.add_argument(
        "--quarters",
        type=int,
        default=5,
        help="quarters to request per statement (free FMP plans cap this at 5)",
    )
    parser.add_argument(
        "--raw", action="store_true", help="also dump the raw first row per endpoint"
    )
    args = parser.parse_args()

    api_key = (
        args.api_key
        or os.environ.get("FUNDAMENTALS_API_KEY", "")
        or _key_from_env_file(args.env_file, "FUNDAMENTALS_API_KEY")
    )
    if not api_key:
        print(
            f"No API key. Set FUNDAMENTALS_API_KEY in the environment or in {args.env_file}, "
            "or pass --api-key.",
            file=sys.stderr,
        )
        return 2

    try:
        return verify(
            args.ticker.strip().upper(),
            api_key,
            args.base_url.rstrip("/"),
            "/" + args.base_path.strip("/"),
            raw=args.raw,
            quarters=args.quarters,
            expected_market_cap=args.expected_market_cap,
        )
    except VerificationError as exc:
        print(f"\nCould not verify: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
