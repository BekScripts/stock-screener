"""Recognising a bank from its statements rather than from its label.

The shapes below are taken from real filings — the concept sets of Kaspi.kz, a
US regional bank, Bullish, TSM and Deere — because the whole point of this
classifier is that it is calibrated against what filers actually tag. A test
built from imagined concept names would prove nothing about the rule.
"""

from __future__ import annotations

import pytest

from domain import StatementProfile, classify_statement_profile

#: Kaspi.kz, the filer this classifier exists for: labelled `Technology /
#: Software - Infrastructure` by one vendor and `Services-Business Services` by
#: the SEC, and unmistakably a bank underneath.
IFRS_BANK = {
    "ifrs-full": [
        "Revenue",
        "CostOfSales",
        "DepositsFromCustomers",
        "DepositsFromBanks",
        "LoansAndAdvancesToCustomers",
        "CashAndBankBalancesAtCentralBanks",
        "InterestRevenueCalculatedUsingEffectiveInterestMethod",
    ]
}

#: A US regional bank, which reports the same economics in a different namespace.
US_GAAP_BANK = {
    "us-gaap": [
        "Revenues",
        "Deposits",
        "DepositsSavingsDeposits",
        "InterestAndDividendIncomeOperating",
        "CashAndDueFromBanks",
    ]
}


@pytest.mark.unit
def test_an_ifrs_bank_is_recognised_from_its_statements() -> None:
    assert classify_statement_profile(IFRS_BANK) is StatementProfile.FINANCIAL_INSTITUTION


@pytest.mark.unit
def test_a_us_gaap_bank_is_recognised_from_its_statements() -> None:
    # The rule is not IFRS-only: the same economics tagged in the other
    # namespace must reach the same conclusion, or half the universe escapes it.
    assert classify_statement_profile(US_GAAP_BANK) is StatementProfile.FINANCIAL_INSTITUTION


@pytest.mark.unit
@pytest.mark.parametrize(
    ("shape", "concepts"),
    [
        # Bullish: holds one central-bank balance and nothing else bank-shaped.
        ("a lone central-bank balance", ["CashAndBankBalancesAtCentralBanks", "Revenue"]),
        # TSM, NVO, CYD, RCI, SPOT: ordinary IFRS 9 impairment on trade
        # receivables, which is not a loan book.
        ("a trade-receivable allowance", ["AllowanceAccountForCreditLossesOfFinancialAssets"]),
        # ABEV, NVO, GRVY, SAP: cash held at a bank, which is what cash is.
        ("cash held at a bank", ["BalancesWithBanks", "Revenue"]),
        (
            "interest earned on spare cash",
            ["InterestIncomeOnFinancialAssetsDesignatedAtFairValueThroughProfitOrLoss"],
        ),
    ],
)
def test_one_weak_signal_does_not_make_a_financial_institution(
    shape: str, concepts: list[str]
) -> None:
    # Deposit funding is required, and none of these is deposit funding. Without
    # that floor the rule would exclude a fifth of the universe for holding cash.
    assert classify_statement_profile({"ifrs-full": concepts}) is StatementProfile.GENERAL, shape


@pytest.mark.unit
def test_deposit_funding_alone_does_not_classify() -> None:
    # Corroboration is the second half of the rule. One concept, however
    # suggestive, is a tagging choice rather than a business model.
    assert (
        classify_statement_profile({"ifrs-full": ["DepositsFromCustomers", "Revenue"]})
        is StatementProfile.GENERAL
    )


@pytest.mark.unit
def test_a_miner_taking_customer_prepayments_is_not_a_bank() -> None:
    # SQM, a lithium producer. It tags prepayments on supply contracts as
    # `DepositsFromCustomers` and earns interest on customer financing — one
    # corroborating category, and a one-signal rule called it a bank.
    prepayments = {
        "ifrs-full": [
            "Revenue",
            "DepositsFromCustomers",
            "InterestIncomeOnLoansAndAdvancesToCustomers",
        ]
    }
    assert classify_statement_profile(prepayments) is StatementProfile.GENERAL


@pytest.mark.unit
def test_bank_balances_a_company_holds_are_not_deposit_funding() -> None:
    # Kamada, a pharmaceutical company. `DepositsFromBanks` and
    # `LoansAndAdvancesToBanks` here are cash it holds *at* banks — assets, not
    # funding — which is the opposite of what the same tags mean on a bank.
    holdings = {"ifrs-full": ["Revenue", "DepositsFromBanks", "LoansAndAdvancesToBanks"]}
    assert classify_statement_profile(holdings) is StatementProfile.GENERAL


@pytest.mark.unit
def test_one_corroborating_category_is_not_enough() -> None:
    # The threshold itself. Deposit funding plus a single category is the shape
    # both real false positives had.
    assert (
        classify_statement_profile(
            {"ifrs-full": ["DepositsFromCustomers", "LoansAndAdvancesToCustomers"]}
        )
        is StatementProfile.GENERAL
    )


@pytest.mark.unit
def test_a_manufacturer_with_a_finance_arm_is_not_a_financial_institution() -> None:
    # Deere, Caterpillar, Ford, GM, Tesla, Walmart and Dell all report lending
    # concepts because they finance their own customers. They are not banks, they
    # take no deposits, and the general model reads their statements correctly.
    captive_finance = {
        "us-gaap": [
            "Revenues",
            "NotesReceivableNet",
            "LoansAndLeasesReceivableNetReportedAmount",
            "FinancingReceivableAllowanceForCreditLosses",
            "ProvisionForLoanAndLeaseLosses",
            "InterestIncomeExpenseNet",
        ]
    }
    assert classify_statement_profile(captive_finance) is StatementProfile.GENERAL


@pytest.mark.unit
def test_an_ordinary_operating_company_is_general() -> None:
    ordinary = {
        "ifrs-full": ["Revenue", "CostOfSales", "Borrowings", "CashAndCashEquivalents"],
        "us-gaap": ["Revenues", "OperatingIncomeLoss", "Assets"],
    }
    assert classify_statement_profile(ordinary) is StatementProfile.GENERAL


@pytest.mark.unit
@pytest.mark.parametrize("facts", [{}, {"dei": ["EntityCommonStockSharesOutstanding"]}])
def test_nothing_to_read_is_not_evidence_of_a_bank(facts: dict[str, list[str]]) -> None:
    # Absence of evidence is not evidence. An unclassifiable filer stays GENERAL
    # and the label gate still applies to it.
    assert classify_statement_profile(facts) is StatementProfile.GENERAL


@pytest.mark.unit
def test_a_full_company_facts_document_can_be_passed_directly() -> None:
    # The caller holds `{namespace: {concept: {"units": ...}}}` and should not
    # have to reshape it: mappings iterate their keys, which are the concepts.
    document = {
        "ifrs-full": {
            "DepositsFromCustomers": {"units": {"KZT": []}},
            "LoansAndAdvancesToCustomers": {"units": {"KZT": []}},
            "CashAndBankBalancesAtCentralBanks": {"units": {"KZT": []}},
        }
    }
    assert classify_statement_profile(document) is StatementProfile.FINANCIAL_INSTITUTION
