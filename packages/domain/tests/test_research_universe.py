"""What the evidence permits for one security in one month.

Every test names a way a security could be wrongly admitted, or wrongly written
off. The two that matter most are `test_a_missing_month_is_unknown_not_excluded`
— absence is never exclusion — and
`test_class_evidence_past_the_bound_becomes_unknown`, because a stale class is
the quietest way a warrant enters a common-stock screen.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    CLASS_CARRY_FORWARD_DAYS,
    ClassEvidenceMode,
    ListingEvidenceState,
    SecurityClass,
    UniverseState,
    Venue,
    class_mode_for,
    classify_month,
)

pytestmark = pytest.mark.unit

WINDOW = date(2022, 1, 1)


def verdict(**kwargs: object):
    """Classify a month, defaulting everything to the eligible case."""
    args = {
        "listing_state": ListingEvidenceState.MARKET_SHARE_OBSERVED,
        "venue": Venue.NASDAQ,
        "security_class": SecurityClass.COMMON_STOCK,
        "class_age_days": 30,
        "month": date(2023, 6, 1),
        "supported_from": WINDOW,
    }
    args.update(kwargs)
    return classify_month(**args)  # type: ignore[arg-type]


class TestTheEligibleCase:
    def test_observed_on_a_supported_venue_and_classified_common_is_eligible(self) -> None:
        result = verdict()

        assert result.state is UniverseState.ELIGIBLE
        assert result.eligible is True
        assert result.class_mode is ClassEvidenceMode.CARRIED_FORWARD


class TestAbsenceIsNeverExclusion:
    """The rule the whole 9K sequence exists to protect."""

    def test_a_missing_month_is_unknown_not_excluded(self) -> None:
        result = verdict(listing_state=ListingEvidenceState.UNKNOWN)

        assert result.state is UniverseState.UNKNOWN
        assert result.state.censored is False

    def test_unknown_is_not_counted_as_censored(self) -> None:
        assert UniverseState.UNKNOWN.censored is False
        assert UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE.censored is True
        assert UniverseState.NON_COMMON_OR_EXCLUDED.censored is True

    def test_no_state_asserts_non_membership(self) -> None:
        assert not any("NOT_LISTED" in str(state) for state in UniverseState)


class TestVenueIsNeverWidened:
    @pytest.mark.parametrize("venue", [None])
    def test_an_unestablished_venue_cannot_be_eligible(self, venue: Venue | None) -> None:
        result = verdict(venue=venue)

        assert result.state is UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE

    def test_positive_censored_venue_evidence_is_not_unknown(self) -> None:
        """NYSE American and Arca are findings, not gaps."""
        result = verdict(venue_is_censored=True)

        assert result.state is UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE
        assert result.state.censored is True

    def test_only_nasdaq_and_nyse_are_admitted(self) -> None:
        assert verdict(venue=Venue.NASDAQ).state is UniverseState.ELIGIBLE
        assert verdict(venue=Venue.NYSE).state is UniverseState.ELIGIBLE


class TestClassEvidenceExpires:
    def test_evidence_inside_the_month_is_direct(self) -> None:
        assert class_mode_for(0) is ClassEvidenceMode.DIRECT

    def test_evidence_within_the_bound_is_carried_forward(self) -> None:
        assert class_mode_for(CLASS_CARRY_FORWARD_DAYS) is ClassEvidenceMode.CARRIED_FORWARD

    def test_carried_evidence_is_never_reported_as_direct(self) -> None:
        result = verdict(class_age_days=45)

        assert result.class_mode is ClassEvidenceMode.CARRIED_FORWARD
        assert result.class_mode is not ClassEvidenceMode.DIRECT

    def test_class_evidence_past_the_bound_becomes_unknown(self) -> None:
        result = verdict(class_age_days=CLASS_CARRY_FORWARD_DAYS + 1)

        assert result.state is UniverseState.UNKNOWN
        assert result.security_class is SecurityClass.UNKNOWN
        assert result.class_mode is ClassEvidenceMode.ABSENT

    def test_no_class_evidence_at_all_is_unknown(self) -> None:
        result = verdict(class_age_days=None)

        assert result.state is UniverseState.UNKNOWN

    def test_future_evidence_is_refused_rather_than_used(self) -> None:
        """A negative age means the filing post-dates the month."""
        result = verdict(class_age_days=-10)

        assert result.class_mode is ClassEvidenceMode.ABSENT
        assert result.state is UniverseState.UNKNOWN


class TestClassGovernsAdmission:
    @pytest.mark.parametrize(
        "security_class",
        [
            SecurityClass.UNIT,
            SecurityClass.WARRANT,
            SecurityClass.RIGHT,
            SecurityClass.NOTE_OR_DEBT,
            SecurityClass.FUND,
            SecurityClass.PREFERRED_STOCK,
            SecurityClass.ADR_OR_ADS,
        ],
    )
    def test_a_non_common_class_is_excluded_not_unknown(
        self, security_class: SecurityClass
    ) -> None:
        result = verdict(security_class=security_class)

        assert result.state is UniverseState.NON_COMMON_OR_EXCLUDED
        assert result.state.censored is True

    def test_an_unestablished_class_is_unknown_rather_than_excluded(self) -> None:
        result = verdict(security_class=SecurityClass.UNKNOWN)

        assert result.state is UniverseState.UNKNOWN


class TestTheWindow:
    def test_a_month_before_the_window_is_outside_it(self) -> None:
        result = verdict(month=date(2021, 12, 1))

        assert result.state is UniverseState.OUTSIDE_WINDOW

    def test_the_first_supported_month_is_inside(self) -> None:
        assert verdict(month=WINDOW).state is UniverseState.ELIGIBLE


class TestTheChainOrderIsFixed:
    """A later step must never rescue a security that failed an earlier one."""

    def test_a_good_class_cannot_rescue_a_missing_observation(self) -> None:
        result = verdict(
            listing_state=ListingEvidenceState.UNKNOWN,
            security_class=SecurityClass.COMMON_STOCK,
            class_age_days=0,
        )

        assert result.state is UniverseState.UNKNOWN

    def test_a_good_class_cannot_rescue_an_unsupported_venue(self) -> None:
        result = verdict(venue=None, security_class=SecurityClass.COMMON_STOCK)

        assert result.state is UniverseState.OUTSIDE_OBSERVABLE_UNIVERSE

    def test_the_window_is_checked_before_anything_else(self) -> None:
        result = verdict(month=date(2021, 6, 1), listing_state=ListingEvidenceState.UNKNOWN)

        assert result.state is UniverseState.OUTSIDE_WINDOW
