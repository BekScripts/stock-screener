"""Classifying a registered class from its cover-page facts.

Every test is a way the SEC cover page misleads a reader who takes a shortcut:
reading the first title, trusting the XBRL axis, or assuming a ticker implies
common stock.
"""

from __future__ import annotations

import pytest

from domain import (
    ClassEvidence,
    CoverSecurity,
    SecurityClass,
    classify_member,
    classify_security,
    classify_title,
    common_stock_for_symbol,
    traded_securities,
)

pytestmark = pytest.mark.unit


class TestTitlesAreReadBeforeMembers:
    """71.5% of filings carry no dimension at all, so the title has to carry it."""

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Common Stock, $0.01 par value", SecurityClass.COMMON_STOCK),
            ("Class A Common Stock, par value $0.0001", SecurityClass.COMMON_STOCK),
            ("Ordinary Shares, nominal value CHF 0.1", SecurityClass.COMMON_STOCK),
            ("Preferred Stock Purchase Rights", SecurityClass.RIGHT),
            ("7% Cumulative Trust Preferred Securities", SecurityClass.PREFERRED_STOCK),
            ("Redeemable Warrants, each exercisable for one share", SecurityClass.WARRANT),
            (
                "Units, each consisting of one share of Class A Common Stock",
                SecurityClass.UNIT,
            ),
            ("3.500% Notes due 2032", SecurityClass.NOTE_OR_DEBT),
            (
                "American Depositary Shares each representing one Class A common share",
                SecurityClass.ADR_OR_ADS,
            ),
            ("", SecurityClass.UNKNOWN),
        ],
    )
    def test_a_title_names_its_class(self, title: str, expected: SecurityClass) -> None:
        assert classify_title(title) is expected

    def test_a_unit_containing_common_stock_is_a_unit(self) -> None:
        """The ordering trap: a SPAC unit's title mentions common stock."""
        title = "Units, each consisting of one share of Class A Common Stock and one warrant"

        assert classify_title(title) is SecurityClass.UNIT

    def test_purchase_rights_are_not_preferred_stock(self) -> None:
        assert classify_title("Preferred Stock Purchase Rights") is SecurityClass.RIGHT


class TestFilerExtensionMembersAreNotTaxonomy:
    """Only 9.2% of filings use `us-gaap:CommonStockMember`; the rest invent names."""

    def test_a_us_gaap_member_names_a_class(self) -> None:
        assert classify_member("us-gaap:CommonStockMember") is SecurityClass.COMMON_STOCK

    def test_a_filer_extension_member_names_nothing(self) -> None:
        assert classify_member("twtr:PreferredStockPurchaseRightsMember") is (SecurityClass.UNKNOWN)
        assert classify_member("snre:AmericanDepositarySharesMember") is SecurityClass.UNKNOWN

    def test_no_member_is_unknown_rather_than_common(self) -> None:
        assert classify_member(None) is SecurityClass.UNKNOWN


class TestDisagreementIsNeverResolvedByPreference:
    """A wrong class admits a warrant to a common-stock screen."""

    def test_title_and_member_agreeing_is_the_strongest_evidence(self) -> None:
        verdict = classify_security(
            CoverSecurity(
                title="Common Stock, par value $0.000005 per share",
                symbol="TWTR",
                exchange="New York Stock Exchange",
                member="us-gaap:CommonStockMember",
            )
        )

        assert verdict.security_class is SecurityClass.COMMON_STOCK
        assert verdict.evidence is ClassEvidence.TITLE_AND_MEMBER

    def test_a_conflict_produces_unknown_not_a_winner(self) -> None:
        verdict = classify_security(
            CoverSecurity(
                title="Redeemable Warrants",
                symbol="ACME",
                exchange="NYSE",
                member="us-gaap:CommonStockMember",
            )
        )

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert verdict.evidence is ClassEvidence.CONFLICTING
        assert verdict.title_class is SecurityClass.WARRANT
        assert verdict.member_class is SecurityClass.COMMON_STOCK

    def test_an_undimensioned_page_still_classifies_from_its_title(self) -> None:
        verdict = classify_security(
            CoverSecurity(title="Common Stock, $0.01 par value", symbol="BBBY", exchange="Nasdaq")
        )

        assert verdict.security_class is SecurityClass.COMMON_STOCK
        assert verdict.evidence is ClassEvidence.TITLE_ONLY


class TestASymbolIsAnsweredByItsOwnClass:
    """Twitter's final cover page, which is why this function matches on symbol."""

    @staticmethod
    def twitter_page() -> list[CoverSecurity]:
        return [
            CoverSecurity(
                title="Common Stock, par value $0.000005 per share",
                symbol="TWTR",
                exchange="New York Stock Exchange",
                member="us-gaap:CommonStockMember",
            ),
            CoverSecurity(
                title="Preferred Stock Purchase Rights",
                symbol=None,
                exchange="New York Stock Exchange",
                member="twtr:PreferredStockPurchaseRightsMember",
            ),
        ]

    def test_the_symbol_resolves_to_its_own_class(self) -> None:
        verdict = common_stock_for_symbol(self.twitter_page(), "TWTR")

        assert verdict is not None
        assert verdict.security_class is SecurityClass.COMMON_STOCK

    def test_the_rights_line_is_not_attributed_to_the_symbol(self) -> None:
        """The rights security carries no symbol, so nothing answers for it."""
        assert common_stock_for_symbol(self.twitter_page(), "TWTRW") is None

    def test_a_symbol_no_class_carries_returns_nothing(self) -> None:
        assert common_stock_for_symbol(self.twitter_page(), "UNRELATED") is None

    def test_the_first_security_is_not_preferred_by_position(self) -> None:
        """Reversing the page must not change the answer."""
        reversed_page = list(reversed(self.twitter_page()))

        verdict = common_stock_for_symbol(reversed_page, "TWTR")

        assert verdict is not None
        assert verdict.security_class is SecurityClass.COMMON_STOCK

    def test_two_classes_claiming_one_symbol_are_ambiguous(self) -> None:
        page = [
            CoverSecurity(title="Common Stock", symbol="DUP", exchange="NYSE"),
            CoverSecurity(title="Redeemable Warrants", symbol="DUP", exchange="NYSE"),
        ]

        verdict = common_stock_for_symbol(page, "DUP")

        assert verdict is not None
        assert verdict.security_class is SecurityClass.UNKNOWN


class TestARegisteredClassIsNotNecessarilyTraded:
    """The ordinary-shares half of an ADS pair is registered and does not trade."""

    def test_a_no_trading_symbol_class_is_not_a_traded_security(self) -> None:
        page = [
            CoverSecurity(
                title="American Depositary Shares",
                symbol="SNRE",
                exchange="Nasdaq",
                member="snre:AmericanDepositarySharesMember",
            ),
            CoverSecurity(
                title="Class A common shares, nominal value CHF 0.1",
                symbol=None,
                exchange="Nasdaq",
                member="ifrs-full:OrdinarySharesMember",
                no_trading_symbol=True,
            ),
        ]

        traded = traded_securities(page)

        assert [s.symbol for s in traded] == ["SNRE"]

    def test_the_ads_keeps_its_own_class_rather_than_the_ordinary_shares(self) -> None:
        ads = CoverSecurity(
            title="American Depositary Shares each representing one Class A common share",
            symbol="SNRE",
            exchange="Nasdaq",
            member="snre:AmericanDepositarySharesMember",
        )

        assert classify_security(ads).security_class is SecurityClass.ADR_OR_ADS
