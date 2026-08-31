"""Adversarial tests for the survivorship layer.

Each test names a way a study could quietly become dishonest and asserts the
code refuses it: a security counted present before it was ever seen, a delisting
that vanishes from a past roster, a fate invented for a company whose reason the
source never gave.
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING

from domain import (
    CorporateActionKind,
    DelistingReason,
    MembershipEvent,
    MembershipKind,
    ScoringStatus,
    assess_universe_readiness,
    classify_delisting,
    score_company,
    universe_members_as_of,
)

if TYPE_CHECKING:
    from conftest import Make


class TestDelistingReason:
    """The fate a study reasons about, derived only where it can be defended."""

    def test_a_cash_merger_is_an_acquisition(self) -> None:
        assert classify_delisting(CorporateActionKind.CASH_MERGER) is DelistingReason.ACQUIRED

    def test_every_merger_settlement_is_an_acquisition(self) -> None:
        for kind in (
            CorporateActionKind.CASH_MERGER,
            CorporateActionKind.STOCK_MERGER,
            CorporateActionKind.STOCK_AND_CASH_MERGER,
        ):
            assert classify_delisting(kind) is DelistingReason.ACQUIRED

    def test_a_worthless_removal_is_bankruptcy(self) -> None:
        """The extinguished equity is the reported fact, not an assumption."""
        assert classify_delisting(CorporateActionKind.WORTHLESS_REMOVAL) is DelistingReason.BANKRUPT

    def test_no_kind_at_all_is_unknown(self) -> None:
        """A security known to have left with no action explaining it."""
        assert classify_delisting(None) is DelistingReason.UNKNOWN

    def test_a_non_terminal_action_does_not_invent_a_fate(self) -> None:
        """A split is not a delisting; classifying one is UNKNOWN, never a guess."""
        assert classify_delisting(CorporateActionKind.FORWARD_SPLIT) is DelistingReason.UNKNOWN
        assert classify_delisting(CorporateActionKind.CASH_DIVIDEND) is DelistingReason.UNKNOWN

    def test_receivership_only_enters_by_assertion(self) -> None:
        """No action feed files a receivership, so it cannot be derived — only
        asserted from evidence a caller supplies."""
        assert (
            classify_delisting(None, asserted_reason=DelistingReason.RECEIVERSHIP)
            is DelistingReason.RECEIVERSHIP
        )

    def test_an_assertion_overrides_the_mechanism(self) -> None:
        """A fate established outside the feed is more specific than the kind."""
        assert (
            classify_delisting(
                CorporateActionKind.WORTHLESS_REMOVAL,
                asserted_reason=DelistingReason.DELISTED,
            )
            is DelistingReason.DELISTED
        )


def _baseline(company_id: int, ticker: str, on: date, *, active: bool = True) -> MembershipEvent:
    return MembershipEvent(
        company_id=company_id,
        ticker=ticker,
        effective_date=on,
        is_active=active,
        is_baseline=True,
    )


def _transition(company_id: int, ticker: str, on: date, *, active: bool) -> MembershipEvent:
    return MembershipEvent(
        company_id=company_id,
        ticker=ticker,
        effective_date=on,
        is_active=active,
        is_baseline=False,
    )


class TestUniverseAsOf:
    """Membership on a past date, reconstructed without inventing history."""

    def test_a_baseline_member_is_present_but_marked_as_such(self) -> None:
        events = [_baseline(1, "AAA", date(2024, 1, 2))]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        assert universe.member_ids == frozenset({1})
        assert universe.members[0].kind is MembershipKind.BASELINE

    def test_a_company_first_seen_after_the_date_is_undetermined(self) -> None:
        """The look-ahead trap: a name whose record begins after D says nothing
        about D, so it is neither a member nor silently dropped."""
        events = [_baseline(1, "AAA", date(2024, 7, 1))]
        universe = universe_members_as_of(events, date(2024, 1, 1))
        assert universe.member_ids == frozenset()
        assert universe.undetermined == (1,)

    def test_a_security_delisted_before_the_date_is_not_a_member(self) -> None:
        events = [
            _baseline(1, "AAA", date(2024, 1, 2)),
            _transition(1, "AAA", date(2024, 3, 1), active=False),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        assert universe.member_ids == frozenset()

    def test_the_delisted_security_is_still_a_member_before_it_left(self) -> None:
        """Survivorship in its pure form: a study run on a date before the exit
        must see the company that later left."""
        events = [
            _baseline(1, "AAA", date(2024, 1, 2)),
            _transition(1, "AAA", date(2024, 3, 1), active=False),
        ]
        universe = universe_members_as_of(events, date(2024, 2, 1))
        assert universe.member_ids == frozenset({1})
        # Only the baseline is on or before the query date; the exit is later and
        # correctly says nothing about it, so the verdict rests on the baseline.
        assert universe.members[0].kind is MembershipKind.BASELINE

    def test_an_observed_member_before_a_later_exit_is_observed(self) -> None:
        """When the presence itself was witnessed, the verdict is observed even
        though the security later left."""
        events = [
            _transition(1, "AAA", date(2024, 1, 2), active=True),
            _transition(1, "AAA", date(2024, 3, 1), active=False),
        ]
        universe = universe_members_as_of(events, date(2024, 2, 1))
        assert universe.member_ids == frozenset({1})
        assert universe.members[0].kind is MembershipKind.OBSERVED

    def test_the_delisting_registry_excludes_a_never_witnessed_exit(self) -> None:
        """A security can leave without the universe pass ever seeing it; the
        delisting registry removes it even though its events look active."""
        events = [_baseline(1, "AAA", date(2024, 1, 2))]
        universe = universe_members_as_of(events, date(2024, 6, 1), delisted_ids=(1,))
        assert universe.member_ids == frozenset()

    def test_a_reactivation_after_a_delisting_restores_membership(self) -> None:
        events = [
            _baseline(1, "AAA", date(2024, 1, 2)),
            _transition(1, "AAA", date(2024, 3, 1), active=False),
            _transition(1, "AAA", date(2024, 4, 1), active=True),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        assert universe.member_ids == frozenset({1})

    def test_an_observed_transition_supersedes_a_same_day_baseline(self) -> None:
        events = [
            _baseline(1, "AAA", date(2024, 3, 1), active=False),
            _transition(1, "AAA", date(2024, 3, 1), active=True),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        assert universe.member_ids == frozenset({1})
        assert universe.members[0].kind is MembershipKind.OBSERVED

    def test_members_are_returned_in_ticker_order(self) -> None:
        events = [
            _baseline(2, "ZZZ", date(2024, 1, 2)),
            _baseline(1, "AAA", date(2024, 1, 2)),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        assert [m.ticker for m in universe.members] == ["AAA", "ZZZ"]


class TestUniverseReadiness:
    """Whether a reconstructed date is safe to run a study on."""

    def test_a_baseline_only_universe_is_flagged(self) -> None:
        events = [_baseline(1, "AAA", date(2024, 1, 2))]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        readiness = assess_universe_readiness(universe)
        assert not readiness.trustworthy
        assert any(gap.code == "BASELINE_ONLY_UNIVERSE" for gap in readiness.gaps)

    def test_undetermined_members_disqualify_a_date(self) -> None:
        events = [
            _transition(1, "AAA", date(2024, 3, 1), active=True),
            _baseline(2, "BBB", date(2024, 7, 1)),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        readiness = assess_universe_readiness(universe)
        assert any(gap.code == "UNDETERMINED_MEMBERS" for gap in readiness.gaps)

    def test_an_empty_universe_is_flagged(self) -> None:
        universe = universe_members_as_of([], date(2024, 6, 1))
        readiness = assess_universe_readiness(universe)
        assert readiness.member_count == 0
        assert any(gap.code == "EMPTY_UNIVERSE" for gap in readiness.gaps)

    def test_a_fully_observed_universe_is_trustworthy(self) -> None:
        events = [
            _transition(1, "AAA", date(2024, 1, 2), active=True),
            _transition(2, "BBB", date(2024, 1, 2), active=True),
        ]
        universe = universe_members_as_of(events, date(2024, 6, 1))
        readiness = assess_universe_readiness(universe)
        assert readiness.trustworthy
        assert readiness.observed_share == 1.0


class TestScoreNeedsNoCompanyRow:
    """The score is a pure function of one security's own figures.

    This is the property that makes a delisted security scoreable at all: the
    CompounderScore has no cross-sectional term and reads no `companies` table,
    so a name that no longer exists can be scored from the figures its own
    filings and prices supply — exactly what EDGAR-by-CIK and Alpaca serve for a
    delisted issuer. A live peer is never required.
    """

    def test_a_delisted_security_scores_from_its_own_figures(self, make: type[Make]) -> None:
        profile = make.profile(ticker="ATVI", name="Activision Blizzard", market_cap=None)
        metrics = make.metrics(ticker="ATVI")
        score = score_company(profile, metrics, make.benchmark())
        assert score.status is ScoringStatus.SCORED
        assert score.final_score is not None

    def test_the_score_ignores_the_profiles_current_market_cap(self, make: type[Make]) -> None:
        """A profile's `market_cap` is a live quote; the score reads the metrics'
        figure instead, so a stale quote on a delisted issuer cannot leak in."""
        metrics = make.metrics(ticker="ATVI", market_cap=1_000_000_000.0)
        honest = score_company(
            make.profile(ticker="ATVI", market_cap=None), metrics, make.benchmark()
        )
        with_stale_quote = score_company(
            make.profile(ticker="ATVI", market_cap=9.9e14), metrics, make.benchmark()
        )
        assert honest.final_score == with_stale_quote.final_score
