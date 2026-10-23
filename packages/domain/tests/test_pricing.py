"""Recovering a past price from an adjusted one, against real split history."""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    CorporateActionKind,
    CorporateActionRecord,
    as_traded_price,
    cumulative_split_factor,
    market_cap_as_of,
)


def _split(ex_date: date, factor: float) -> CorporateActionRecord:
    """Build a forward split, the only field most of these tests vary."""
    return CorporateActionRecord(
        symbol="AAPL",
        kind=CorporateActionKind.FORWARD_SPLIT,
        ex_date=ex_date,
        split_factor=factor,
        source="test",
    )


#: AAPL's two splits in the stored price window, as Alpaca reports them.
AAPL_SPLITS = (_split(date(2020, 8, 31), 4.0),)

#: A hand-built history: a 2-for-1, then a 1-for-10 reverse eighteen months on.
MIXED_ACTIONS = (
    _split(date(2021, 3, 1), 2.0),
    CorporateActionRecord(
        symbol="AAPL",
        kind=CorporateActionKind.REVERSE_SPLIT,
        ex_date=date(2022, 9, 1),
        split_factor=0.1,
        source="test",
    ),
)


class TestCumulativeSplitFactor:
    """Which splits count for a given date, and which do not."""

    def test_no_actions_leaves_the_price_alone(self) -> None:
        assert cumulative_split_factor((), date(2020, 8, 28)) == 1.0

    def test_a_later_split_applies(self) -> None:
        assert cumulative_split_factor(AAPL_SPLITS, date(2020, 8, 28)) == 4.0

    def test_an_earlier_split_does_not(self) -> None:
        assert cumulative_split_factor(AAPL_SPLITS, date(2021, 1, 4)) == 1.0

    def test_the_ex_date_itself_does_not_apply(self) -> None:
        """The ex-date's own session already trades on the new basis.

        Counting it would apply the factor twice on the one day that pivots.
        """
        assert cumulative_split_factor(AAPL_SPLITS, date(2020, 8, 31)) == 1.0

    def test_the_session_before_the_ex_date_does_apply(self) -> None:
        assert cumulative_split_factor(AAPL_SPLITS, date(2020, 8, 30)) == 4.0

    def test_splits_compound_in_both_directions(self) -> None:
        """A 2-for-1 and a later 1-for-10 leave a net factor of 0.2."""
        assert cumulative_split_factor(MIXED_ACTIONS, date(2021, 1, 1)) == pytest.approx(0.2)

    def test_only_the_reverse_remains_after_the_forward(self) -> None:
        assert cumulative_split_factor(MIXED_ACTIONS, date(2021, 6, 1)) == pytest.approx(0.1)

    def test_a_dividend_never_changes_the_share_basis(self) -> None:
        dividend = CorporateActionRecord(
            symbol="AAPL",
            kind=CorporateActionKind.CASH_DIVIDEND,
            ex_date=date(2021, 2, 5),
            cash_amount=0.205,
            currency="USD",
            source="test",
        )
        assert cumulative_split_factor((dividend, *AAPL_SPLITS), date(2020, 8, 28)) == 4.0

    def test_a_missing_factor_is_skipped_rather_than_assumed(self) -> None:
        broken = CorporateActionRecord(
            symbol="AAPL",
            kind=CorporateActionKind.FORWARD_SPLIT,
            ex_date=date(2021, 1, 1),
            split_factor=None,
            source="test",
        )
        assert cumulative_split_factor((broken,), date(2020, 1, 1)) == 1.0


class TestAsTradedPrice:
    """The stored close, returned to the basis it was quoted in."""

    def test_aapl_before_the_2020_split(self) -> None:
        """The stored close for 2020-08-28 is 120.98; the security traded near 499.

        The recovered 483.92 is low by the dividends the adjustment also removed
        — about 3% over six years — which is the documented one-directional
        residual, not a bug. What matters is that it is not 120.98.
        """
        assert as_traded_price(120.98, AAPL_SPLITS, date(2020, 8, 28)) == pytest.approx(483.92)

    def test_after_the_split_the_stored_close_is_already_right(self) -> None:
        assert as_traded_price(129.04, AAPL_SPLITS, date(2021, 1, 4)) == pytest.approx(129.04)

    def test_no_close_yields_no_price(self) -> None:
        assert as_traded_price(None, AAPL_SPLITS, date(2020, 8, 28)) is None


class TestMarketCapAsOf:
    """Share count and price, multiplied on one basis rather than two."""

    def test_aapl_on_the_eve_of_the_split(self) -> None:
        """4.276bn pre-split shares at the pre-split price, not the post-split one.

        The share count is the 10-Q's cover-page figure, stated as of 17 July
        and filed on 31 July — the last one available on the date it values.
        Multiplying it by the *stored* close gives $517bn,
        understating a two-trillion-dollar company by a factor of four — which
        is exactly the error this function exists to prevent.
        """
        cap = market_cap_as_of(4_275_634_000, 120.98, AAPL_SPLITS, date(2020, 7, 17))
        assert cap is not None
        assert cap == pytest.approx(2.069e12, rel=0.01)

    def test_the_unadjusted_multiplication_is_wrong_by_the_split_factor(self) -> None:
        """Naming the bug, so a future change that reintroduces it fails here."""
        naive = 4_275_634_000 * 120.98
        correct = market_cap_as_of(4_275_634_000, 120.98, AAPL_SPLITS, date(2020, 7, 17))
        assert correct is not None
        assert correct / naive == pytest.approx(4.0)

    def test_the_window_between_a_split_and_the_next_filing(self) -> None:
        """The case that makes the anchor the count's date rather than the day's.

        AAPL split on 2020-08-31 and did not file again until 2020-10-30. On
        2020-09-01 the newest available count is still the 4.28bn pre-split
        figure from 17 July, and the price has already quartered. Anchoring on
        the valuation date applies no factor — the split is in the past — and
        values Apple at 557bn. Anchoring on 17 July applies the four, because
        the split had not happened when that count was stated.
        """
        cap = market_cap_as_of(4_275_634_000, 130.31, AAPL_SPLITS, date(2020, 7, 17))
        assert cap is not None
        assert cap == pytest.approx(2.229e12, rel=0.01)

    def test_anchoring_on_the_valuation_date_would_undervalue_by_the_factor(self) -> None:
        """Naming the wrong anchor, so reintroducing it fails here."""
        anchored_wrongly = market_cap_as_of(4_275_634_000, 130.31, AAPL_SPLITS, date(2020, 9, 1))
        anchored_rightly = market_cap_as_of(4_275_634_000, 130.31, AAPL_SPLITS, date(2020, 7, 17))
        assert anchored_wrongly is not None
        assert anchored_rightly is not None
        assert anchored_rightly / anchored_wrongly == pytest.approx(4.0)

    def test_the_valuation_is_continuous_across_the_split(self) -> None:
        """Same company, three days apart, one 4-for-1 in between.

        The size of a company does not change because its shares were divided,
        so the two valuations must agree to within the price move — and the
        naive pairing disagrees by 300%.
        """
        before = market_cap_as_of(4_275_634_000, 120.98, AAPL_SPLITS, date(2020, 7, 17))
        after = market_cap_as_of(4_275_634_000, 130.31, AAPL_SPLITS, date(2020, 7, 17))
        assert before is not None
        assert after is not None
        assert after / before == pytest.approx(130.31 / 120.98)

    def test_a_later_filing_cannot_change_an_earlier_market_cap(self) -> None:
        """The regression that matters: the split is in the price, not the count.

        AAPL's next 10-Q reported 17.0bn shares on the post-split basis. That
        filing is dated after this date, so a point-in-time caller never passes
        it — and if one did, it would be paired with a post-split price, giving
        the same company the same size. Neither path lets a later filing rewrite
        an earlier valuation.
        """
        before = market_cap_as_of(4_275_634_000, 120.98, AAPL_SPLITS, date(2020, 7, 17))
        after = market_cap_as_of(16_976_763_000, 120.98, AAPL_SPLITS, date(2020, 10, 16))
        assert before is not None
        assert after is not None
        assert after / before == pytest.approx(0.9926, rel=0.01)

    def test_no_share_count_yields_no_market_cap(self) -> None:
        assert market_cap_as_of(None, 120.98, AAPL_SPLITS, date(2020, 7, 17)) is None

    def test_a_zero_share_count_is_refused_rather_than_valued_at_nothing(self) -> None:
        assert market_cap_as_of(0.0, 120.98, AAPL_SPLITS, date(2020, 7, 17)) is None
