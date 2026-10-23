"""Reading security-level facts off an inline-XBRL cover page.

The fixtures here are the real shapes Phase 9I found in the corpus: a
single-class undimensioned page, a dimensioned page carrying common stock beside
a rights line, an ADS paired with untraded ordinary shares, and a page that
registers two classes without dimensioning them — the one shape that cannot be
read at all and must be surrendered rather than guessed.
"""

from __future__ import annotations

from datetime import date
from typing import ClassVar

import pytest

from api_clients.sec_cover import parse_cover_page
from domain import (
    CoverSecurity,
    SecurityClass,
    classify_security,
    common_stock_for_symbol,
    traded_securities,
)
from stock_screener.listing import FilingCover, class_for_observation

pytestmark = pytest.mark.integration


def fact(concept: str, context: str, value: str) -> str:
    """One inline-XBRL cover fact."""
    return f'<ix:nonNumeric name="dei:{concept}" contextRef="{context}">{value}</ix:nonNumeric>'


def context(identifier: str, member: str | None = None) -> str:
    """One XBRL context, optionally carrying a class dimension."""
    body = (
        f'<xbrldi:explicitMember dimension="us-gaap:StatementClassOfStockAxis">'
        f"{member}</xbrldi:explicitMember>"
        if member
        else ""
    )
    return f'<xbrli:context id="{identifier}"><entity>{body}</entity></xbrli:context>'


SINGLE_CLASS = (
    context("c-1")
    + fact("Security12bTitle", "c-1", "Common Stock, $0.01 par value")
    + fact("TradingSymbol", "c-1", "BBBY")
    + fact("SecurityExchangeName", "c-1", "The Nasdaq Stock Market LLC")
)

COMMON_AND_RIGHTS = (
    context("c-com", "us-gaap:CommonStockMember")
    + context("c-rts", "twtr:PreferredStockPurchaseRightsMember")
    + fact("Security12bTitle", "c-com", "Common Stock, par value $0.000005 per share")
    + fact("TradingSymbol", "c-com", "TWTR")
    + fact("SecurityExchangeName", "c-com", "New York Stock Exchange")
    + fact("Security12bTitle", "c-rts", "Preferred Stock Purchase Rights")
    + fact("SecurityExchangeName", "c-rts", "New York Stock Exchange")
)

ADS_AND_ORDINARY = (
    context("c-ads", "snre:AmericanDepositarySharesMember")
    + context("c-ord", "ifrs-full:OrdinarySharesMember")
    + fact("Security12bTitle", "c-ads", "American Depositary Shares each representing one share")
    + fact("TradingSymbol", "c-ads", "SNRE")
    + fact("SecurityExchangeName", "c-ads", "Nasdaq")
    + fact("Security12bTitle", "c-ord", "Class A common shares, nominal value CHF 0.1")
    + fact("NoTradingSymbolFlag", "c-ord", "true")
    + fact("SecurityExchangeName", "c-ord", "Nasdaq")
)

TWO_CLASSES_ONE_CONTEXT = (
    context("c-1")
    + fact("Security12bTitle", "c-1", "Common Stock, $0.01 par value")
    + fact("TradingSymbol", "c-1", "ACME")
    + fact("Security12bTitle", "c-1", "Redeemable Warrants")
    + fact("TradingSymbol", "c-1", "ACMEW")
)


class TestASingleClassPage:
    def test_the_cover_facts_are_read_without_a_dimension(self) -> None:
        page = parse_cover_page(SINGLE_CLASS)

        assert len(page.securities) == 1
        assert page.dimensioned is False
        assert page.securities[0].symbol == "BBBY"
        assert classify_security(page.securities[0]).security_class is SecurityClass.COMMON_STOCK


class TestCommonStockBesideARightsLine:
    """The mis-attribution this reader exists to prevent."""

    def test_both_classes_are_kept_apart(self) -> None:
        page = parse_cover_page(COMMON_AND_RIGHTS)

        assert len(page.securities) == 2
        assert page.dimensioned is True

    def test_the_symbol_resolves_to_common_stock(self) -> None:
        page = parse_cover_page(COMMON_AND_RIGHTS)

        verdict = common_stock_for_symbol(page.securities, "TWTR")

        assert verdict is not None
        assert verdict.security_class is SecurityClass.COMMON_STOCK

    def test_the_rights_line_never_acquires_the_symbol(self) -> None:
        page = parse_cover_page(COMMON_AND_RIGHTS)

        rights = [s for s in page.securities if s.member and "Rights" in s.member]

        assert len(rights) == 1
        assert rights[0].symbol is None
        assert classify_security(rights[0]).security_class is SecurityClass.RIGHT


class TestAnAdsPairedWithUntradedOrdinaryShares:
    def test_only_the_ads_is_a_traded_security(self) -> None:
        page = parse_cover_page(ADS_AND_ORDINARY)

        assert [s.symbol for s in traded_securities(page.securities)] == ["SNRE"]

    def test_the_ordinary_shares_carry_the_no_trading_symbol_flag(self) -> None:
        page = parse_cover_page(ADS_AND_ORDINARY)

        ordinary = [s for s in page.securities if s.member and "Ordinary" in s.member]

        assert ordinary[0].no_trading_symbol is True
        assert ordinary[0].symbol is None


class TestTwoClassesSharingOneContext:
    """Unreadable by construction. Surrendered, never resolved by document order."""

    def test_the_page_is_marked_ambiguous_and_yields_no_security(self) -> None:
        page = parse_cover_page(TWO_CLASSES_ONE_CONTEXT)

        assert page.ambiguous is True
        assert page.securities == ()

    def test_the_common_title_is_not_paired_with_the_warrant_symbol(self) -> None:
        page = parse_cover_page(TWO_CLASSES_ONE_CONTEXT)

        assert common_stock_for_symbol(page.securities, "ACME") is None


class TestAFilingWithNoCoverFacts:
    """Everything before 2019. Not an error."""

    def test_a_document_without_inline_xbrl_yields_nothing(self) -> None:
        page = parse_cover_page("<html><body>FORM 10-K</body></html>")

        assert page.securities == ()
        assert page.ambiguous is False


class TestAFilingMayOnlyClassifyItsOwnOccupant:
    """The BBBY defect, asserted.

    `company_tickers.json` maps a ticker to its *current* registrant, so `BBBY`
    resolves to Beyond, Inc. A join that ignores dates reads Beyond's 2025 cover
    page and stamps its class onto Bed Bath & Beyond's 2022 months.
    """

    @staticmethod
    def beyond_cover() -> FilingCover:
        return FilingCover(
            accepted=date(2025, 10, 15),
            securities=[
                CoverSecurity(
                    title="Common Stock, $0.0001 par value",
                    symbol="BBBY",
                    exchange="New York Stock Exchange",
                )
            ],
            accession="beyond-2025",
        )

    def test_a_later_occupants_filing_cannot_classify_an_earlier_run(self) -> None:
        bed_bath_run = [(date(2022, 1, 1), date(2023, 5, 1))]

        verdict, cover = class_for_observation(
            [self.beyond_cover()], "BBBY", date(2022, 6, 1), bed_bath_run
        )

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert cover is None

    def test_the_same_filing_classifies_its_own_run(self) -> None:
        beyond_run = [(date(2025, 8, 1), date(2026, 7, 1))]

        verdict, cover = class_for_observation(
            [self.beyond_cover()], "BBBY", date(2025, 12, 1), beyond_run
        )

        assert verdict.security_class is SecurityClass.COMMON_STOCK
        assert cover is not None
        assert cover.accession == "beyond-2025"

    def test_a_filing_accepted_after_the_month_is_not_used(self) -> None:
        """PIT: a researcher on 2025-09-01 could not have read an October filing."""
        beyond_run = [(date(2025, 8, 1), date(2026, 7, 1))]

        verdict, cover = class_for_observation(
            [self.beyond_cover()], "BBBY", date(2025, 9, 1), beyond_run
        )

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert cover is None

    def test_the_newest_pit_valid_filing_inside_the_run_wins(self) -> None:
        run = [(date(2022, 1, 1), date(2023, 5, 1))]
        older = FilingCover(
            accepted=date(2022, 3, 1),
            securities=[CoverSecurity(title="Common Stock", symbol="ACME", exchange="Nasdaq")],
            accession="old",
        )
        newer = FilingCover(
            accepted=date(2022, 11, 1),
            securities=[
                CoverSecurity(title="Redeemable Warrants", symbol="ACME", exchange="Nasdaq")
            ],
            accession="new",
        )

        verdict, cover = class_for_observation([older, newer], "ACME", date(2023, 1, 1), run)

        assert cover is not None
        assert cover.accession == "new"
        assert verdict.security_class is SecurityClass.WARRANT

    def test_a_symbol_absent_from_the_cover_page_is_unknown(self) -> None:
        run = [(date(2022, 1, 1), date(2023, 5, 1))]
        cover = FilingCover(
            accepted=date(2022, 3, 1),
            securities=[CoverSecurity(title="Common Stock", symbol="OTHER", exchange="Nasdaq")],
        )

        verdict, used = class_for_observation([cover], "ACME", date(2022, 6, 1), run)

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert used is not None


class TestTheBedBathOccupantsNeverCross:
    """Phase 9K.4/9K.5 recovered both occupants of `BBBY` from dated SEC evidence.

    Bed Bath & Beyond is CIK 886158 on Nasdaq through 2023-05; Beyond, Inc. is
    CIK 1130713 on NYSE from 2025-08. Both cover pages print `BBBY`. The only
    thing keeping them apart is the occupancy window, so it is asserted here
    against the real accession dates rather than left to the harness.
    """

    # Both runs are passed on every call — that is the realistic shape, and it
    # is what lets occupancy separate the two tenants rather than merely PIT.
    RUNS: ClassVar[list[tuple[date, date]]] = [
        (date(2022, 1, 1), date(2023, 5, 1)),
        (date(2025, 8, 1), date(2026, 7, 1)),
    ]
    BED_BATH_RUN: ClassVar[list[tuple[date, date]]] = RUNS
    BEYOND_RUN: ClassVar[list[tuple[date, date]]] = RUNS

    @staticmethod
    def bed_bath() -> FilingCover:
        return FilingCover(
            accepted=date(2023, 1, 26),
            securities=[
                CoverSecurity(
                    title="Common Stock, $0.01 par value",
                    symbol="BBBY",
                    exchange="The Nasdaq Stock Market LLC",
                )
            ],
            accession="cik-886158",
        )

    @staticmethod
    def beyond() -> FilingCover:
        return FilingCover(
            accepted=date(2026, 4, 27),
            securities=[
                CoverSecurity(
                    title="Common Stock, $0.0001 par value",
                    symbol="BBBY",
                    exchange="New York Stock Exchange",
                )
            ],
            accession="cik-1130713",
        )

    def test_the_bed_bath_run_resolves_to_the_bed_bath_filing(self) -> None:
        covers = [self.bed_bath(), self.beyond()]

        verdict, used = class_for_observation(covers, "BBBY", date(2023, 4, 1), self.BED_BATH_RUN)

        assert used is not None
        assert used.accession == "cik-886158"
        assert verdict.security_class is SecurityClass.COMMON_STOCK

    def test_the_beyond_run_resolves_to_the_beyond_filing(self) -> None:
        covers = [self.bed_bath(), self.beyond()]

        verdict, used = class_for_observation(covers, "BBBY", date(2026, 6, 1), self.BEYOND_RUN)

        assert used is not None
        assert used.accession == "cik-1130713"
        assert verdict.security_class is SecurityClass.COMMON_STOCK

    def test_an_older_pit_valid_filing_outside_the_run_is_still_refused(self) -> None:
        """Bed Bath's filing is PIT-valid for a 2026 observation — accepted long
        before it — and is still refused, because it belongs to the other
        occupancy. PIT alone would admit it; occupancy is what does not."""
        covers = [self.bed_bath(), self.beyond()]

        _, used = class_for_observation(covers, "BBBY", date(2026, 6, 1), self.BEYOND_RUN)

        assert used is not None
        assert used.accession == "cik-1130713"

    def test_a_month_before_its_own_run_has_a_filing_has_no_evidence(self) -> None:
        """The strictness cuts both ways: the Bed Bath run starts 2022-01 but its
        surviving cover page was accepted 2023-01-26, so early months of the run
        carry no class evidence at all. UNKNOWN, not back-dated."""
        verdict, used = class_for_observation(
            [self.bed_bath()], "BBBY", date(2022, 6, 1), self.BED_BATH_RUN
        )

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert used is None

    def test_a_symbol_named_only_in_prose_never_reaches_a_verdict(self) -> None:
        """A third party naming `BBBY` files no cover fact for it. 9K.5 measured
        raw full-text precision at 16.7%; this is the rule that discards the rest."""
        third_party = FilingCover(
            accepted=date(2022, 6, 1),
            securities=[CoverSecurity(title="Common Stock", symbol="OTHER", exchange="Nasdaq")],
            accession="unrelated-issuer",
        )

        verdict, used = class_for_observation(
            [third_party], "BBBY", date(2022, 7, 1), self.BED_BATH_RUN
        )

        assert verdict.security_class is SecurityClass.UNKNOWN
        assert used is not None
        assert used.accession == "unrelated-issuer"
