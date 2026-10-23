"""The rules for reading Nasdaq's monthly market-share files.

Every test here corresponds to a way Phase 9K found the source misleads a reader.
"""

from __future__ import annotations

from datetime import date

import pytest

from domain import (
    NYSE_TAPE_A_FROM,
    ListingEvidenceState,
    MonthlyObservation,
    SourceFamily,
    Tape,
    Venue,
    classify_tape,
    continuous_runs,
    daily_state,
    month_state,
    tape_for,
    venue_for_tape,
)

pytestmark = pytest.mark.unit


def seen(symbol: str, venue: Venue, *months: tuple[int, int]) -> list[MonthlyObservation]:
    """Build observations for a symbol over the given (year, month) pairs."""
    return [MonthlyObservation(symbol=symbol, venue=venue, month=date(y, m, 1)) for y, m in months]


class TestTheListedFamilyChangesMeaning:
    """The single worst mistake available with this source."""

    def test_listed_is_tape_b_before_2022(self) -> None:
        assert tape_for(SourceFamily.LISTED, date(2021, 12, 1)) is Tape.B

    def test_listed_is_tape_a_from_2022(self) -> None:
        assert tape_for(SourceFamily.LISTED, NYSE_TAPE_A_FROM) is Tape.A

    def test_the_nasdaq_family_is_tape_c_throughout(self) -> None:
        for year in (2016, 2021, 2022, 2026):
            assert tape_for(SourceFamily.NASDAQ, date(year, 6, 1)) is Tape.C

    def test_tape_b_establishes_no_venue(self) -> None:
        assert venue_for_tape(Tape.B) is None

    def test_tape_a_is_nyse_and_tape_c_is_nasdaq(self) -> None:
        assert venue_for_tape(Tape.A) is Venue.NYSE
        assert venue_for_tape(Tape.C) is Venue.NASDAQ


class TestTapeIsReadFromContentsNotFromTheCalendar:
    """A month rule alone would mislabel 2021-07, which really is Tape A."""

    def test_nyse_sentinels_identify_tape_a(self) -> None:
        assert classify_tape({"JPM", "GE", "XOM", "BA", "T"}) is Tape.A

    def test_arca_sentinels_identify_tape_b(self) -> None:
        assert classify_tape({"SPY", "GDX", "IWM", "QQQ"}) is Tape.B

    def test_an_empty_file_identifies_no_tape(self) -> None:
        assert classify_tape(set()) is Tape.UNKNOWN

    def test_too_few_nyse_sentinels_does_not_claim_tape_a(self) -> None:
        assert classify_tape({"JPM", "GE", "AAPL", "MSFT"}) is not Tape.A


class TestAbsenceIsNeverNonMembership:
    """There is no NOT_LISTED state, and this is why."""

    def test_a_missing_month_between_two_observed_months_is_unknown(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 1), (2022, 3))

        assert month_state(observations, Venue.NASDAQ, date(2022, 2, 1)) is (
            ListingEvidenceState.UNKNOWN
        )

    def test_after_the_final_observation_is_unknown(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 1), (2022, 2))

        assert month_state(observations, Venue.NASDAQ, date(2024, 6, 1)) is (
            ListingEvidenceState.UNKNOWN
        )

    def test_before_the_first_observation_is_unknown(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2023, 5))

        assert month_state(observations, Venue.NASDAQ, date(2022, 1, 1)) is (
            ListingEvidenceState.UNKNOWN
        )

    def test_no_state_in_the_enum_asserts_non_membership(self) -> None:
        assert not any("NOT_LISTED" in str(state) for state in ListingEvidenceState)


class TestMonthlyEvidenceIsNeverDailyEvidence:
    """A file attests to a month. A day inside it is an inference, every time."""

    def test_an_observed_month_is_observed_at_month_resolution(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 4))

        assert month_state(observations, Venue.NASDAQ, date(2022, 4, 1)) is (
            ListingEvidenceState.MARKET_SHARE_OBSERVED
        )

    def test_a_day_inside_an_observed_month_is_only_inferred(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 4))

        assert daily_state(observations, Venue.NASDAQ, date(2022, 4, 20)) is (
            ListingEvidenceState.INFERRED_INTERVAL
        )

    def test_no_day_is_ever_reported_as_observed(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 4), (2022, 5), (2022, 6))

        states = {
            daily_state(observations, Venue.NASDAQ, date(2022, m, d))
            for m in (4, 5, 6)
            for d in (1, 15, 28)
        }

        assert ListingEvidenceState.MARKET_SHARE_OBSERVED not in states

    def test_a_day_in_an_unobserved_month_is_unknown(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 4))

        assert daily_state(observations, Venue.NASDAQ, date(2022, 5, 2)) is (
            ListingEvidenceState.UNKNOWN
        )


class TestRunsAreNotForwardFilledAcrossAHole:
    """Bed Bath & Beyond's symbol, and the reuse that must not be joined to it."""

    def test_a_hole_splits_one_symbol_into_two_runs(self) -> None:
        observations = seen("BBBY", Venue.NASDAQ, (2022, 1), (2022, 2), (2025, 8), (2025, 9))

        runs = continuous_runs(observations, Venue.NASDAQ)

        assert runs == (
            (date(2022, 1, 1), date(2022, 2, 1)),
            (date(2025, 8, 1), date(2025, 9, 1)),
        )

    def test_consecutive_months_form_one_run(self) -> None:
        observations = seen("ACME", Venue.NASDAQ, (2022, 11), (2022, 12), (2023, 1))

        assert continuous_runs(observations, Venue.NASDAQ) == (
            (date(2022, 11, 1), date(2023, 1, 1)),
        )

    def test_a_run_on_one_venue_ignores_observations_on_another(self) -> None:
        observations = [
            *seen("XFER", Venue.NASDAQ, (2022, 1), (2022, 2)),
            *seen("XFER", Venue.NYSE, (2022, 3), (2022, 4)),
        ]

        assert continuous_runs(observations, Venue.NASDAQ) == (
            (date(2022, 1, 1), date(2022, 2, 1)),
        )
        assert continuous_runs(observations, Venue.NYSE) == ((date(2022, 3, 1), date(2022, 4, 1)),)
