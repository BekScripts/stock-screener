"""Tests for the two-source fundamentals provider."""

from __future__ import annotations

from datetime import date

import pytest

from api_clients import CompositeFundamentals, MockFundamentals, ProviderError
from domain import CompanyProfile, FinancialPeriod

RICH = CompanyProfile(
    ticker="XYZ",
    name="Example Corp",
    exchange="NASDAQ",
    sector="Technology",
    market_cap=1_200_000_000.0,
)
SPARSE = CompanyProfile(ticker="XYZ", name="Example Corp")
PERIODS = [FinancialPeriod(period_end=date(2026, 6, 30), revenue=100.0, source="statements")]


class Failing(MockFundamentals):
    """A profile source that is unavailable."""

    def get_company_profile(self, ticker: str) -> CompanyProfile | None:
        raise ProviderError("profile endpoint is down")


@pytest.mark.unit
def test_the_profile_comes_from_the_source_that_knows_market_cap() -> None:
    composite = CompositeFundamentals(
        profile_source=MockFundamentals({"XYZ": RICH}),
        statement_source=MockFundamentals({"XYZ": SPARSE}),
    )

    profile = composite.get_company_profile("XYZ")

    assert profile is not None
    assert profile.market_cap == pytest.approx(1_200_000_000.0)


@pytest.mark.unit
def test_the_statements_come_from_the_statement_source() -> None:
    composite = CompositeFundamentals(
        profile_source=MockFundamentals({"XYZ": RICH}),
        statement_source=MockFundamentals({"XYZ": SPARSE}, {"XYZ": PERIODS}),
    )

    periods = composite.get_financial_statements("XYZ")

    assert [p.source for p in periods] == ["statements"]


@pytest.mark.unit
def test_a_symbol_the_profile_source_lacks_falls_back() -> None:
    # The result has a name but no market cap, so the screen excludes it with
    # MISSING_REQUIRED_DATA — visible, rather than vanishing from the universe.
    composite = CompositeFundamentals(
        profile_source=MockFundamentals(),
        statement_source=MockFundamentals({"XYZ": SPARSE}),
    )

    profile = composite.get_company_profile("XYZ")

    assert profile is not None
    assert profile.market_cap is None


@pytest.mark.unit
def test_a_failing_profile_source_does_not_lose_the_company() -> None:
    # Statements cannot be reconstructed from anywhere else; a market cap can.
    composite = CompositeFundamentals(
        profile_source=Failing(),
        statement_source=MockFundamentals({"XYZ": SPARSE}, {"XYZ": PERIODS}),
    )

    assert composite.get_company_profile("XYZ") == SPARSE
    assert len(composite.get_financial_statements("XYZ")) == 1


@pytest.mark.unit
def test_a_failing_statement_source_is_not_swallowed() -> None:
    class FailingStatements(MockFundamentals):
        def get_financial_statements(self, ticker: str, limit: int = 20) -> list[FinancialPeriod]:
            raise ProviderError("statements endpoint is down")

    composite = CompositeFundamentals(
        profile_source=MockFundamentals({"XYZ": RICH}),
        statement_source=FailingStatements(),
    )

    with pytest.raises(ProviderError, match="statements endpoint"):
        composite.get_financial_statements("XYZ")


@pytest.mark.unit
def test_the_fallback_can_be_disabled() -> None:
    composite = CompositeFundamentals(
        profile_source=MockFundamentals(),
        statement_source=MockFundamentals({"XYZ": SPARSE}),
        fall_back_to_statement_profile=False,
    )

    assert composite.get_company_profile("XYZ") is None
