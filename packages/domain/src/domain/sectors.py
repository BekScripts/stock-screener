"""Which businesses CompounderScore v1 is economically able to judge.

The general model reads revenue, gross margin, free cash flow and net debt. For
a bank, three of those four mean something different: deposits are a liability
the business is built on rather than leverage to be penalised, interest expense
is cost of goods, and EV/Revenue is not a multiple anyone prices the sector on.
Scoring one anyway produces a number that looks comparable and is not.

So they are marked, not modelled and not deleted. A bank stays in the universe,
keeps its metrics, and is excluded from the ranking with
`ScoringStatus.UNSUPPORTED_SECTOR` — which is a statement about the model's
scope, not about the company.

The classification is deliberately shallow: substring matching on the provider's
own industry label. Wrongly marking one company costs a candidate; wrongly
scoring a bank puts a meaningless number in the middle of a shortlist.
"""

from __future__ import annotations

#: Industry substrings whose economics the v1 model misreads. Matched
#: case-insensitively against the provider's industry label.
_UNSUPPORTED_INDUSTRY_TERMS: tuple[str, ...] = (
    "bank",
    "thrift",
    "savings & loan",
    "savings and loan",
    "insurance",
    "insurer",
    "reinsurance",
    "capital market",
    "mortgage",
    "credit service",
    "consumer finance",
    "asset management",
    "financial conglomerate",
    "financial - conglomerates",
    "financial data & stock exchange",
    "shell compan",
    "blank check",
)

#: Sector labels covering the businesses above. A company inside one of these
#: whose industry is unknown is also excluded: within financials the industry is
#: what distinguishes a payments company from a lender, and guessing without it
#: would score whichever way the guess fell.
_FINANCIAL_SECTOR_TERMS: tuple[str, ...] = (
    "financial services",
    "financials",
    "financial",
)


def is_unsupported_sector(sector: str | None, industry: str | None) -> bool:
    """Whether CompounderScore v1 should decline to score this business.

    Args:
        sector: The provider's sector label, when known.
        industry: The provider's narrower industry label, when known.

    Returns:
        True for banks, insurers, lenders, asset managers and shells — and for
        any company in the financial sector whose industry is unknown, because
        the industry is the only thing that would distinguish it from one.
    """
    normalised_industry = (industry or "").strip().lower()
    if any(term in normalised_industry for term in _UNSUPPORTED_INDUSTRY_TERMS):
        return True

    normalised_sector = (sector or "").strip().lower()
    is_financial = any(term in normalised_sector for term in _FINANCIAL_SECTOR_TERMS)
    return is_financial and not normalised_industry
