"""Which businesses the v1 model declines to score."""

from __future__ import annotations

import pytest

from domain import is_unsupported_sector


@pytest.mark.unit
@pytest.mark.parametrize(
    "industry",
    [
        "Banks - Regional",
        "Banks—Diversified",
        "BANKS - DIVERSIFIED",
        "Insurance - Life",
        "Insurance Brokers",
        "Reinsurance",
        "Capital Markets",
        "Financial - Mortgages",
        "Financial - Credit Services",
        "Asset Management",
        "Shell Companies",
    ],
)
def test_financial_businesses_are_unsupported(industry: str) -> None:
    assert is_unsupported_sector("Financial Services", industry) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    ("sector", "industry"),
    [
        ("Technology", "Software - Application"),
        ("Healthcare", "Biotechnology"),
        ("Industrials", "Aerospace & Defense"),
        ("Consumer Cyclical", "Internet Retail"),
        ("Energy", "Oil & Gas Midstream"),
        ("Real Estate", "REIT - Industrial"),
    ],
)
def test_operating_businesses_are_supported(sector: str, industry: str) -> None:
    assert is_unsupported_sector(sector, industry) is False


@pytest.mark.unit
def test_a_financial_company_with_no_industry_is_not_scored() -> None:
    # Within financials the industry is the only thing separating a payments
    # company from a lender, so an unknown one is not guessed at.
    assert is_unsupported_sector("Financial Services", None) is True


@pytest.mark.unit
def test_an_unknown_sector_and_industry_is_scoreable() -> None:
    assert is_unsupported_sector(None, None) is False


@pytest.mark.unit
def test_a_bank_is_recognised_from_its_industry_whatever_its_sector() -> None:
    assert is_unsupported_sector("Technology", "Banks - Regional") is True
