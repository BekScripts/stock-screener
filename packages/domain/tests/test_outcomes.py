"""Forward returns over a hand-built calendar, where every price is known."""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from domain import FORWARD_HORIZONS, OutcomeStatus, forward_returns

#: Twenty consecutive weekdays, standing in for a market calendar.
CALENDAR = [date(2024, 1, 1) + timedelta(days=n) for n in range(20)]


def _flat(prices: dict[date, float]) -> dict[date, float]:
    """Return the mapping unchanged, named for what it is at a call site."""
    return prices


class TestEntrySelection:
    """Where the entry falls, which is the whole look-ahead question."""

    def test_entry_is_the_session_after_the_observation(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(1,))
        assert results[0].entry_date == CALENDAR[1]

    def test_an_observation_between_sessions_enters_at_the_next_one(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        gap = CALENDAR[3] - timedelta(hours=0)
        results = forward_returns(CALENDAR, opens, opens, gap, horizons=(1,))
        assert results[0].entry_date == CALENDAR[4]

    def test_the_observation_session_is_never_the_entry(self) -> None:
        """A snapshot computable at D's close cannot be filled at D's open."""
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[5], horizons=(1,))
        assert results[0].entry_date is not None
        assert results[0].entry_date > CALENDAR[5]

    def test_no_price_on_the_entry_session_refuses_every_horizon(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        del opens[CALENDAR[1]]
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0])
        assert {r.status for r in results} == {OutcomeStatus.NO_ENTRY}

    def test_an_observation_after_the_last_session_has_no_entry(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[-1])
        assert {r.status for r in results} == {OutcomeStatus.NO_ENTRY}


class TestRealisedReturns:
    """The arithmetic, over prices chosen so the answer is exact."""

    def test_a_ten_percent_gain_over_five_sessions(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        opens[CALENDAR[6]] = 110.0
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(5,))
        assert results[0].status is OutcomeStatus.REALISED
        assert results[0].total_return == pytest.approx(0.10)

    def test_a_loss_is_negative(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        opens[CALENDAR[6]] = 75.0
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(5,))
        assert results[0].total_return == pytest.approx(-0.25)

    def test_the_horizon_counts_sessions_not_days(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(5,))
        assert results[0].exit_date == CALENDAR[6]

    def test_every_horizon_is_returned_in_the_order_asked_for(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0])
        assert tuple(r.horizon_sessions for r in results) == FORWARD_HORIZONS

    def test_a_split_inside_the_horizon_is_invisible(self) -> None:
        """Both ends are on the same adjusted basis, so a 4-for-1 is not a crash.

        This is the property that makes `adjustment=all` right for returns and
        wrong for market caps: the two modules want opposite things from it.
        """
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(5,))
        assert results[0].total_return == pytest.approx(0.0)


class TestTruncated:
    """Horizons the record does not reach yet."""

    def test_a_horizon_past_the_calendar_is_truncated_not_zero(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(
            CALENDAR, opens, opens, CALENDAR[-3], horizons=(20,), stale_after=0
        )
        assert results[0].status is OutcomeStatus.TRUNCATED
        assert results[0].total_return is None

    def test_a_truncated_horizon_still_records_its_entry(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(
            CALENDAR, opens, opens, CALENDAR[-3], horizons=(20,), stale_after=0
        )
        assert results[0].entry_price == pytest.approx(100.0)

    def test_short_horizons_realise_while_long_ones_truncate(self) -> None:
        opens = dict.fromkeys(CALENDAR, 100.0)
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[15])
        statuses = {r.horizon_sessions: r.status for r in results}
        assert statuses[1] is OutcomeStatus.REALISED
        assert statuses[20] is OutcomeStatus.TRUNCATED


class TestInterrupted:
    """A security that stops trading while the market does not."""

    def test_a_security_that_stops_trading_is_interrupted_not_missing(self) -> None:
        opens = dict.fromkeys(CALENDAR[:8], 100.0)
        closes = _flat(dict.fromkeys(CALENDAR[:8], 100.0))
        results = forward_returns(
            CALENDAR, opens, closes, CALENDAR[0], horizons=(20,), stale_after=0
        )
        assert results[0].status is OutcomeStatus.INTERRUPTED

    def test_an_interrupted_outcome_has_no_return(self) -> None:
        """Neither -100% nor 0%: a price series cannot say which happened."""
        opens = dict.fromkeys(CALENDAR[:8], 100.0)
        closes = _flat(dict.fromkeys(CALENDAR[:8], 100.0))
        results = forward_returns(
            CALENDAR, opens, closes, CALENDAR[0], horizons=(20,), stale_after=0
        )
        assert results[0].total_return is None

    def test_an_interrupted_outcome_records_where_it_was_last_seen(self) -> None:
        opens = dict.fromkeys(CALENDAR[:8], 100.0)
        closes = _flat(dict.fromkeys(CALENDAR[:8], 42.0))
        results = forward_returns(
            CALENDAR, opens, closes, CALENDAR[0], horizons=(20,), stale_after=0
        )
        assert results[0].last_seen_date == CALENDAR[7]
        assert results[0].last_seen_price == pytest.approx(42.0)

    def test_horizons_inside_the_traded_window_still_realise(self) -> None:
        """A delisting truncates the long horizons and leaves the short ones."""
        opens = dict.fromkeys(CALENDAR[:8], 100.0)
        opens[CALENDAR[6]] = 120.0
        closes = _flat(dict.fromkeys(CALENDAR[:8], 100.0))
        results = forward_returns(
            CALENDAR, opens, closes, CALENDAR[0], horizons=(5, 20), stale_after=0
        )
        assert results[0].status is OutcomeStatus.REALISED
        assert results[0].total_return == pytest.approx(0.20)
        assert results[1].status is OutcomeStatus.INTERRUPTED

    def test_a_one_day_hole_is_unpriced_not_interrupted(self) -> None:
        """A gap in a still-trading security is our problem, not the market's.

        Counting it as a delisting would inflate every count of how often
        securities leave the market by however many bars we happen to be
        missing.
        """
        opens = dict.fromkeys(CALENDAR, 100.0)
        del opens[CALENDAR[6]]
        results = forward_returns(CALENDAR, opens, opens, CALENDAR[0], horizons=(5,))
        assert results[0].status is OutcomeStatus.UNPRICED
        assert results[0].total_return is None
        assert results[0].last_seen_date is None
