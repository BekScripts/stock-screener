"""Which businesses CompounderScore v1 can judge, read from the statements.

`domain.sectors` asks the same question of a provider's *label*. This module
asks it of the filing, because a label can be wrong in a way that matters: a
deposit-funded bank in Kazakhstan is classified `Technology / Software -
Infrastructure` by one vendor and `Services-Business Services, NEC` by the SEC's
own SIC list, and on those two opinions it scores 74.78 and lands in the middle
of a shortlist.

Nothing about that score is arithmetically wrong. It is meaningless for the
reason `sectors` already gives: interest expense is a bank's cost of goods and
sits nowhere near cost of sales, so the gross margin is fiction; and a bank funds
itself with deposits, which carry no `Borrowings` tag, so debt reads as unknown
and the leverage penalty is skipped entirely. A company whose funding is
invisible to the model gets the risk profile of one with no debt.

The statements cannot be wrong about this. A company that reports customer
deposits, a loan book and central-bank balances **is** a bank, whatever anyone
labelled it.

Deliberately conservative. Wrongly marking one company costs a candidate;
wrongly scoring a bank puts a meaningless number in a ranking that sorts on
exactly that field.

## What the rule is, and what it refuses to use

Every tag below was measured across fifty-six real filers — eighteen banks,
eight non-bank financials, thirty operating companies — and kept only if no
operating company reported it. The ones that did are named here so they are not
optimistically re-added later:

* `AllowanceAccountForCreditLossesOfFinancialAssets` is reported by TSM, NVO,
  CYD, RCI and SPOT. It is ordinary IFRS 9 impairment on trade receivables, not
  a loan book.
* `BalancesWithBanks` is reported by ABEV, NVO, GRVY and SAP. It means cash held
  at a bank, which is what every company does with cash.
* `NotesReceivableNet`, `FinancingReceivableAllowanceForCreditLosses`,
  `ProvisionForLoanAndLeaseLosses` and `LoansAndLeasesReceivableNetReportedAmount`
  are reported by TSLA, CAT, F, DE, GM, WMT and DELL. Manufacturers with captive
  finance arms lend money; they are not banks, and the model reads their
  statements correctly.

What survived is **deposit funding**, corroborated by at least **two** of a loan
book, banking interest revenue, central-bank balances, or a loan-loss allowance
specific to lending.

Two rather than one, because deposit funding turned out not to be the clean
discriminator a thirty-company sample suggested. Widening the negative set to
seventy-eight found operating companies reporting it for entirely ordinary
reasons: a lithium miner tags customer prepayments on supply contracts as
`DepositsFromCustomers`, and a pharmaceutical company tags the cash it holds at
banks as `DepositsFromBanks` — an asset, not funding. Both cleared a one-signal
rule; neither comes close to two, because neither has anything else a bank has.
No real bank in the reference set has fewer than two either, except where its
concepts are too sparse to classify at all.

## What this deliberately does not catch

Two banks in the reference set stay `GENERAL` here: one tags no deposit concept
in company-facts at all, and one reports central-bank balances and nothing else.
Both are caught by `domain.sectors` on their industry label, which is the point —
this is a **second** gate for filers whose labels are wrong, not a replacement
for the one that already works. Missing a bank that the label gate catches costs
nothing; classifying a miner as a bank costs a candidate.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

#: XBRL namespaces this module understands. Anything else is ignored rather than
#: guessed at: a namespace whose concepts have not been measured cannot support
#: a claim about statement shape.
_NAMESPACES: tuple[str, ...] = ("ifrs-full", "us-gaap")

#: Money held as deposits. Necessary but **not sufficient**: an operating company
#: reaches for these tags for customer prepayments and for its own bank balances,
#: which is why corroboration is required rather than optional.
_DEPOSIT_FUNDING: dict[str, frozenset[str]] = {
    "ifrs-full": frozenset(
        {
            "DepositsFromCustomers",
            "DepositsFromBanks",
            "BalancesOnCurrentAccountsFromCustomers",
        }
    ),
    "us-gaap": frozenset(
        {
            "Deposits",
            "InterestBearingDepositLiabilities",
            "NoninterestBearingDepositLiabilities",
            "DepositsSavingsDeposits",
            "TimeDepositLiabilities",
        }
    ),
}

#: Lending as the business rather than as a trade term. The US-GAAP side is
#: deliberately thin: the obvious tags there are also used by manufacturers
#: financing their own dealers, so only the one measured clean is listed.
_LOAN_BOOK: dict[str, frozenset[str]] = {
    "ifrs-full": frozenset({"LoansAndAdvancesToCustomers", "LoansAndAdvancesToBanks"}),
    "us-gaap": frozenset(
        {"FinancingReceivableExcludingAccruedInterestBeforeAllowanceForCreditLoss"}
    ),
}

#: Interest as revenue from lending, not interest earned on spare cash. The
#: effective-interest method is an accounting treatment for a loan portfolio.
_BANKING_INTEREST: dict[str, frozenset[str]] = {
    "ifrs-full": frozenset(
        {
            "InterestRevenueCalculatedUsingEffectiveInterestMethod",
            "InterestIncomeOnLoansAndAdvancesToCustomers",
        }
    ),
    "us-gaap": frozenset(
        {
            "InterestAndDividendIncomeOperating",
            "InterestIncomeExpenseAfterProvisionForLoanLoss",
            "InterestAndFeeIncomeLoansAndLeases",
        }
    ),
}

#: A relationship with the central bank, which only a licensed institution has.
#: Reserve requirements and the federal funds market are not open to anyone else.
_CENTRAL_BANK: dict[str, frozenset[str]] = {
    "ifrs-full": frozenset(
        {"CashAndBankBalancesAtCentralBanks", "MandatoryReserveDepositsAtCentralBanks"}
    ),
    "us-gaap": frozenset(
        {
            "InterestBearingDepositsInBanks",
            "CashAndDueFromBanks",
            "FederalFundsPurchased",
            "FederalFundsSoldAndSecuritiesPurchasedUnderAgreementsToResell",
        }
    ),
}

#: Expected credit losses on money that was *lent*, as distinct from money owed
#: by a customer for goods. The generic IFRS 9 allowance is excluded above.
_LOAN_LOSS_ALLOWANCE: dict[str, frozenset[str]] = {
    "ifrs-full": frozenset(
        {
            "LoansAndAdvancesAtAmortisedCostAllowanceForExpectedCreditLosses",
            "IncreaseDecreaseInAllowanceAccountForCreditLossesOfFinancialAssets",
        }
    ),
    "us-gaap": frozenset(),
}

_CORROBORATING: tuple[dict[str, frozenset[str]], ...] = (
    _LOAN_BOOK,
    _BANKING_INTEREST,
    _CENTRAL_BANK,
    _LOAN_LOSS_ALLOWANCE,
)

#: How many of the four categories above must appear alongside deposit funding.
#:
#: Two, measured rather than chosen. At one, a lithium miner tagging customer
#: prepayments and a pharmaceutical company tagging its bank balances both
#: classified as banks. At two, neither does, every bank in the reference set
#: that tags its concepts at all still does, and the false-positive count across
#: seventy-eight operating companies is zero.
#:
#: Categories, not tags: three deposit-adjacent tags from the same balance-sheet
#: line are one piece of evidence, not three.
_REQUIRED_CORROBORATING = 2


class StatementProfile(StrEnum):
    """The shape of a company's financial statements, for scoring purposes.

    Not a sector and not an industry — a statement about which accounting model
    the filing follows, and therefore whether the general operating-company
    metrics mean what they normally mean.
    """

    GENERAL = "GENERAL"
    """An operating company. CompounderScore v1 reads these statements correctly."""

    FINANCIAL_INSTITUTION = "FINANCIAL_INSTITUTION"
    """Deposit-funded lending. Revenue, gross margin, debt and leverage all
    mean something different here, so the company is excluded from the ranking
    rather than scored on figures that do not describe it."""


def _matches(facts: Mapping[str, Iterable[str]], table: Mapping[str, frozenset[str]]) -> bool:
    """Whether any concept in `facts` appears in `table`, per namespace."""
    for namespace in _NAMESPACES:
        wanted = table.get(namespace)
        if not wanted:
            continue
        if any(concept in wanted for concept in facts.get(namespace, ())):
            return True
    return False


def classify_statement_profile(facts: Mapping[str, Iterable[str]]) -> StatementProfile:
    """Classify a filer from the concepts its statements use.

    Pure and offline: it reads concept *names*, never values, so it costs nothing
    beyond a fetch the caller has already made.

    Args:
        facts: Concept names by XBRL namespace, as company-facts publishes them —
            `{"ifrs-full": [...], "us-gaap": [...]}`. Values are ignored, so a
            full company-facts document may be passed directly. Unrecognised
            namespaces are ignored.

    Returns:
        `FINANCIAL_INSTITUTION` when the filer reports deposit funding *and* at
        least `_REQUIRED_CORROBORATING` of the corroborating categories,
        otherwise `GENERAL`. A filer with no recognised concepts is `GENERAL`:
        absence of evidence is not evidence, and the label gate in
        `domain.sectors` still applies to it.
    """
    if not _matches(facts, _DEPOSIT_FUNDING):
        return StatementProfile.GENERAL

    corroborating = sum(1 for table in _CORROBORATING if _matches(facts, table))
    if corroborating >= _REQUIRED_CORROBORATING:
        return StatementProfile.FINANCIAL_INSTITUTION
    return StatementProfile.GENERAL
