"""Adversarial, end-to-end tests for the backtest-readiness harness.

Each test names a way a study over this data could lie to itself — a survivor
standing in for a universe, a fact read before it was filed, a delisting that
disappears — and asserts the harness refuses it. They run against a real
database because every property is a property of the join between the immutable
record and the frozen calculations, not of either alone.
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import TYPE_CHECKING

import pytest

from data_access import (
    EVENT_ACTIVE,
    OBSERVATION_BASELINE,
    BenchmarkPriceRepository,
    CompanyRepository,
    DelistingRepository,
    FundamentalObservationRepository,
    PitFeatureSnapshotRepository,
    PitSignalSnapshotRepository,
    PriceHistoryRepository,
    UniverseEventRepository,
)
from domain import (
    CURRENT_SIGNAL_VERSION,
    CompanyProfile,
    CorporateActionKind,
    DelistingReason,
    OutcomeStatus,
    PriceBar,
)
from stock_screener.backtest import (
    reconstruct_membership_record,
    reconstruct_score,
    reconstruct_universe,
    security_readiness,
)
from stock_screener.config import Settings
from stock_screener.pit import FEATURE_VERSION, capture_day
from stock_screener.signals import derive_market_signals

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.orm import Session

BENCHMARK = "SPY"


def _sessions(count: int, start: date = date(2024, 1, 2)) -> list[date]:
    """Return `count` consecutive weekdays, standing in for a calendar."""
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day = date.fromordinal(day.toordinal() + 1)
    return days


CALENDAR = _sessions(90)
FILING = CALENDAR[10]
AS_OF = CALENDAR[30]


def _company(session: Session, ticker: str) -> int:
    """Store one company row and return its primary key."""
    companies = CompanyRepository(session)
    # A real sector, so the scorer does not reject it as unsupported.
    companies.upsert_profile(_profile(ticker))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _profile(ticker: str) -> CompanyProfile:
    """A listing in a supported sector, with no supplied market cap."""
    return CompanyProfile(
        ticker=ticker,
        name=f"{ticker} Inc",
        exchange="NASDAQ",
        sector="Technology",
        industry="Software",
        market_cap=None,
    )


def _company_with_market_cap(session: Session, ticker: str, current_market_cap: float) -> int:
    """Store a company carrying a (deliberately wrong) current market cap.

    Used to prove today's live quote never reaches a reconstructed past score.
    """
    companies = CompanyRepository(session)
    companies.upsert_profile(_profile(ticker).model_copy(update={"market_cap": current_market_cap}))
    stored = companies.get_by_ticker(ticker)
    assert stored is not None
    return stored.id


def _prices(session: Session, company_id: int, days: Sequence[date], price: float = 25.0) -> None:
    """Give a company a flat price series over these sessions."""
    PriceHistoryRepository(session).upsert_bars(
        company_id,
        [
            PriceBar(date=day, open=price, high=price, low=price, close=price, volume=1_000_000)
            for day in days
        ],
    )


def _benchmark(session: Session, days: Sequence[date]) -> None:
    """Give the benchmark a bar on every session, defining the calendar."""
    BenchmarkPriceRepository(session).upsert_bars(
        BENCHMARK,
        [
            PriceBar(date=day, open=500.0, high=500.0, low=500.0, close=500.0, volume=1_000_000)
            for day in days
        ],
    )


def _observe(
    session: Session,
    company_id: int,
    concept: str,
    value: float,
    filed: date,
    *,
    taxonomy: str = "us-gaap",
    unit: str = "USD",
    period_end: date = date(2023, 12, 31),
    period_start: date | None = date(2023, 10, 1),
) -> None:
    """Append one filing observation, as ingestion would."""
    fingerprint = hashlib.sha256(
        f"{concept}|{value}|{filed}|{period_end}|{unit}".encode()
    ).hexdigest()
    FundamentalObservationRepository(session).add_all(
        company_id,
        [
            {
                "observation_fingerprint": fingerprint,
                "taxonomy": taxonomy,
                "concept": concept,
                "value": value,
                "unit": unit,
                "period_start": period_start,
                "period_end": period_end,
                "filed_date": filed,
                "accession": f"acc-{concept}-{filed}",
                "form": "10-K",
                "source": "sec-edgar",
            }
        ],
    )
    session.flush()


def _baseline_event(session: Session, company_id: int, ticker: str, on: date) -> None:
    """Record a company as an active baseline member of the universe."""
    UniverseEventRepository(session).add_all(
        [
            {
                "company_id": company_id,
                "event_fingerprint": hashlib.sha256(f"{ticker}|baseline".encode()).hexdigest(),
                "ticker": ticker,
                "event_type": EVENT_ACTIVE,
                "observation_kind": OBSERVATION_BASELINE,
                "effective_date": on,
                "is_active": True,
                "exchange": "NASDAQ",
                "source": "test",
            }
        ]
    )
    session.flush()


def _delist(
    session: Session,
    ticker: str,
    kind: CorporateActionKind,
    on: date,
    *,
    cash_per_share: float | None = None,
    source_id: str = "d1",
) -> None:
    """Record a security as having left the market."""
    DelistingRepository(session).add_all(
        [
            {
                "ticker": ticker,
                "kind": kind.value,
                "effective_date": on,
                "cash_per_share": cash_per_share,
                "acquirer_ticker": None,
                "acquirer_rate": None,
                "acquiree_rate": None,
                "source": "test",
                "source_id": source_id,
            }
        ]
    )
    session.flush()


# ---------------------------------------------------------------------------
# Universe reconstruction — survivorship
# ---------------------------------------------------------------------------


class TestUniverseReconstruction:
    """Which securities existed in the universe on the date."""

    @pytest.mark.integration
    def test_a_baseline_member_is_reconstructed_for_the_date(self, session: Session) -> None:
        company_id = _company(session, "AAA")
        _baseline_event(session, company_id, "AAA", CALENDAR[0])

        universe = reconstruct_universe(session, AS_OF)

        assert company_id in universe.member_ids

    @pytest.mark.integration
    def test_a_security_delisted_before_the_date_is_not_a_member(self, session: Session) -> None:
        """Pure survivorship: a name that left the market before the date must
        not appear in that date's universe, even though its company row and its
        active baseline event both survive."""
        company_id = _company(session, "BBB")
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[5])

        universe = reconstruct_universe(session, AS_OF)

        assert company_id not in universe.member_ids

    @pytest.mark.integration
    def test_the_same_security_is_a_member_before_it_left(self, session: Session) -> None:
        company_id = _company(session, "BBB")
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[20])

        before = reconstruct_universe(session, CALENDAR[10])

        assert company_id in before.member_ids

    @pytest.mark.integration
    def test_a_reused_ticker_does_not_retire_the_company_that_inherited_it(
        self, session: Session
    ) -> None:
        """The identity hazard, adversarially: one issuer delists and a different
        issuer later trades under the same letters. A delisting joined on ticker
        alone would retire the survivor on every date after the first one's exit.
        The survivor is still printing bars, and that is what settles it."""
        survivor_id = _company(session, "BBB")
        _prices(session, survivor_id, CALENDAR)
        _baseline_event(session, survivor_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[5])

        universe = reconstruct_universe(session, AS_OF)

        assert survivor_id in universe.member_ids

    @pytest.mark.integration
    def test_a_company_that_stopped_trading_at_its_delisting_is_still_retired(
        self, session: Session
    ) -> None:
        """The other half of the same rule: the price record must only rescue a
        company that genuinely kept trading. One whose bars stop at the effective
        date is the issuer the delisting describes, and it leaves the universe."""
        company_id = _company(session, "BBB")
        _prices(session, company_id, CALENDAR[:6])
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[5])

        universe = reconstruct_universe(session, AS_OF)

        assert company_id not in universe.member_ids

    @pytest.mark.integration
    def test_a_company_with_no_price_record_cannot_refute_its_delisting(
        self, session: Session
    ) -> None:
        """Silence is not a refutation. With no bars at all there is no evidence
        the company kept trading, so the delisting stands — the conservative
        direction, because a dead name wrongly kept is survivorship bias."""
        company_id = _company(session, "BBB")
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[5])

        universe = reconstruct_universe(session, AS_OF)

        assert company_id not in universe.member_ids

    @pytest.mark.integration
    def test_a_live_company_does_not_inherit_the_fate_of_a_previous_symbol_holder(
        self, session: Session
    ) -> None:
        """The readiness answer must not report a live company as acquired
        because an earlier holder of its ticker was. A company still printing
        bars after the recorded exit is not the security that exited."""
        company_id = _company(session, "BBB")
        _prices(session, company_id, CALENDAR)
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[5], cash_per_share=12.0)

        readiness = security_readiness(session, settings_for(), AS_OF, "BBB")

        assert readiness.delisting is None
        assert readiness.company_id == company_id

    @pytest.mark.integration
    def test_an_acquired_company_is_a_member_before_and_absent_after(
        self, session: Session
    ) -> None:
        """A company acquired inside the study period is not continuously alive.
        The same reconstruction must place it in the universe before the merger
        and out of it afterwards, from one unchanged set of records."""
        company_id = _company(session, "BBB")
        _prices(session, company_id, CALENDAR[:21])
        _baseline_event(session, company_id, "BBB", CALENDAR[0])
        _delist(session, "BBB", CorporateActionKind.CASH_MERGER, CALENDAR[20], cash_per_share=42.0)

        before = reconstruct_universe(session, CALENDAR[10])
        after = reconstruct_universe(session, CALENDAR[30])

        assert company_id in before.member_ids
        assert company_id not in after.member_ids


# ---------------------------------------------------------------------------
# Score reconstruction — look-ahead
# ---------------------------------------------------------------------------


class TestScoreReconstruction:
    """The CompounderScore rebuilt from point-in-time inputs."""

    @pytest.mark.integration
    def test_nothing_filed_by_the_date_yields_no_metrics(self, session: Session) -> None:
        """A date before the first filing has nothing to build from, and the
        reconstruction says so rather than reaching for a later filing."""
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)

        result = reconstruct_score(session, settings_for(), company_id, CALENDAR[5])

        assert result is not None
        assert result.metrics is None

    @pytest.mark.integration
    def test_a_filing_becomes_visible_only_after_it_was_filed(self, session: Session) -> None:
        """The same company, the same query, two dates: the look-ahead boundary
        is the filing date and nothing else."""
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _benchmark(session, CALENDAR)
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)

        before = reconstruct_score(session, settings_for(), company_id, CALENDAR[5])
        after = reconstruct_score(session, settings_for(), company_id, AS_OF)

        assert before is not None
        assert after is not None
        assert before.metrics is None
        assert after.metrics is not None

    @pytest.mark.integration
    def test_a_point_in_time_market_cap_is_recovered_from_shares_and_price(
        self, session: Session
    ) -> None:
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)
        _observe(
            session,
            company_id,
            "EntityCommonStockSharesOutstanding",
            40_000_000.0,
            FILING,
            taxonomy="dei",
            unit="shares",
            period_start=None,
        )

        after = reconstruct_score(session, settings_for(), company_id, AS_OF)

        assert after is not None
        assert after.metrics is not None
        assert after.metrics.market_cap is not None
        # 40M shares at a flat 25.0 close, no split to undo.
        assert after.metrics.market_cap == pytest.approx(1_000_000_000.0)

    @pytest.mark.integration
    def test_a_reconstructed_score_is_not_a_captured_one(self, session: Session) -> None:
        """No capture has run, so the harness must mark the score reconstructed
        rather than recorded — the distinction blocker 3 turns on."""
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _benchmark(session, CALENDAR)
        _baseline_event(session, company_id, "AAA", CALENDAR[0])
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)

        readiness = security_readiness(session, settings_for(), AS_OF, "AAA")

        assert readiness.score_is_reconstructed is True
        assert readiness.score_was_captured is False

    @pytest.mark.integration
    def test_a_missing_company_cannot_be_reconstructed(self, session: Session) -> None:
        """A company id with no row yields None, never an invented score."""
        assert reconstruct_score(session, settings_for(), 9999, AS_OF) is None


# ---------------------------------------------------------------------------
# The seven-question harness
# ---------------------------------------------------------------------------


class TestSecurityReadiness:
    """The harness answers every question or names the gap."""

    @pytest.mark.integration
    def test_a_live_security_is_answerable_across_the_questions(self, session: Session) -> None:
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _benchmark(session, CALENDAR)
        _baseline_event(session, company_id, "AAA", CALENDAR[0])
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)
        # Membership is now resolved from the Phase 9C record rather than from
        # the company-keyed event log, so the canonical record has to exist for
        # the harness to place anything. This is the production sequence.
        reconstruct_membership_record(session)

        readiness = security_readiness(session, settings_for(), AS_OF, "AAA")

        assert readiness.company_id == company_id
        assert readiness.in_universe is True
        # Every MEMBER verdict the current evidence supports is INFERRED, because
        # exchange and security type are undated everywhere in this system.
        assert readiness.membership_kind == "INFERRED"
        assert readiness.fundamentals_known is True
        assert readiness.forward_outcomes  # measured over the horizons
        assert readiness.score_status is not None

    @pytest.mark.integration
    def test_a_delisted_only_security_is_answerable_through_its_fate(
        self, session: Session
    ) -> None:
        """Survivorship's hardest case: a security with no company row at all.
        It must not vanish — the harness answers it through the registry, with a
        classified reason."""
        _delist(
            session,
            "ZZZ",
            CorporateActionKind.CASH_MERGER,
            CALENDAR[15],
            cash_per_share=12.5,
        )

        readiness = security_readiness(session, settings_for(), AS_OF, "ZZZ")

        assert readiness.company_id is None
        assert readiness.in_universe is False
        assert readiness.delisting is not None
        assert readiness.delisting.reason is DelistingReason.ACQUIRED
        assert readiness.delisting.cash_per_share == 12.5

    @pytest.mark.integration
    def test_a_worthless_removal_is_reported_as_bankruptcy(self, session: Session) -> None:
        _delist(session, "DEAD", CorporateActionKind.WORTHLESS_REMOVAL, CALENDAR[15])

        readiness = security_readiness(session, settings_for(), AS_OF, "DEAD")

        assert readiness.delisting is not None
        assert readiness.delisting.reason is DelistingReason.BANKRUPT
        # Nothing, reported as None — never a stored 0.0 that a study could
        # mistake for a known terminal value.
        assert readiness.delisting.cash_per_share is None

    @pytest.mark.integration
    def test_a_ticker_absent_from_the_record_is_flagged_not_invented(
        self, session: Session
    ) -> None:
        """A symbol that is neither a company nor a recorded delisting cannot be
        studied, and the harness says so rather than returning a hollow pass."""
        readiness = security_readiness(session, settings_for(), AS_OF, "NOPE")

        assert readiness.company_id is None
        assert readiness.delisting is None
        assert "NOT_IN_RECORD" in readiness.gaps
        assert readiness.answerable is False

    @pytest.mark.integration
    def test_missing_signals_and_benchmark_are_named_as_gaps(self, session: Session) -> None:
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _baseline_event(session, company_id, "AAA", CALENDAR[0])
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)

        readiness = security_readiness(session, settings_for(), AS_OF, "AAA")

        assert "NO_SIGNALS_DERIVED" in readiness.gaps
        assert "NO_BENCHMARK_CALENDAR" in readiness.gaps

    @pytest.mark.integration
    def test_a_security_that_stops_trading_has_an_interrupted_outcome(
        self, session: Session
    ) -> None:
        """The forward-return half of survivorship: the benchmark keeps a
        calendar the security no longer trades on, so a horizon reaching past its
        last bar is interrupted, never a fabricated zero."""
        company_id = _company(session, "CCC")
        _prices(session, company_id, CALENDAR[:41])  # stops trading at session 40
        _benchmark(session, CALENDAR)
        _baseline_event(session, company_id, "CCC", CALENDAR[0])
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)

        readiness = security_readiness(session, settings_for(), CALENDAR[20], "CCC")

        statuses = {
            outcome.horizon_sessions: outcome.status for outcome in readiness.forward_outcomes
        }
        # 1 and 5 sessions land while it still traded; 60 reaches past its end.
        assert statuses[1] is OutcomeStatus.REALISED
        assert statuses[60] is OutcomeStatus.INTERRUPTED


class TestSignalBackfill:
    """Market signals are backfillable for a past date, point-in-time correct."""

    @pytest.mark.integration
    def test_signals_backfill_for_a_past_date_from_stored_prices(self, session: Session) -> None:
        """The daily-accumulation process, run against history: capture features
        bounded to a past date, then derive that day's signals. Both accept an
        arbitrary date, so a study can build signal history from stored bars
        without a provider and without look-ahead."""
        company_id = _company(session, "AAA")
        _prices(session, company_id, CALENDAR)
        _benchmark(session, CALENDAR)

        captured = capture_day(session, settings_for(), snapshot_date=AS_OF)
        assert captured.features >= 1

        result = derive_market_signals(session, snapshot_date=AS_OF)

        assert result.derived >= 1
        signals = PitSignalSnapshotRepository(session).for_date(
            AS_OF, signal_version=CURRENT_SIGNAL_VERSION
        )
        assert [row.ticker for row in signals] == ["AAA"]
        # Point-in-time: the feature watermark never reaches past the backfilled
        # date, so the signal describes the day it is dated and not a later one.
        features = PitFeatureSnapshotRepository(session).for_date(
            AS_OF, feature_version=FEATURE_VERSION
        )
        assert features[0].feature_as_of <= AS_OF


class TestNoLookAheadFromCurrentState:
    """A reconstructed historical score must never depend on today's state."""

    def _observe_fundamentals(self, session: Session, company_id: int) -> None:
        _observe(session, company_id, "Revenues", 100_000_000.0, FILING)
        _observe(
            session,
            company_id,
            "EntityCommonStockSharesOutstanding",
            40_000_000.0,
            FILING,
            taxonomy="dei",
            unit="shares",
            period_start=None,
        )

    @pytest.mark.integration
    def test_todays_market_cap_never_becomes_the_pit_market_cap(self, session: Session) -> None:
        """The company row carries an absurd current market cap; the reconstructed
        PIT market cap is the filed share count times the price on the date."""
        company_id = _company_with_market_cap(session, "AAA", 9.9e14)
        _prices(session, company_id, CALENDAR)
        self._observe_fundamentals(session, company_id)

        result = reconstruct_score(session, settings_for(), company_id, AS_OF)

        assert result is not None
        assert result.metrics is not None
        # 40M shares at a flat 25.0 close — never the 9.9e14 quote on the row.
        assert result.metrics.market_cap == pytest.approx(1_000_000_000.0)

    @pytest.mark.integration
    def test_identical_filings_score_identically_despite_different_current_state(
        self, session: Session
    ) -> None:
        """Two securities with the same filings and prices but different current
        market caps reconstruct to the same score — today's state cannot leak."""
        a = _company_with_market_cap(session, "AAA", 1_000_000_000.0)
        b = _company_with_market_cap(session, "BBB", 9.9e14)
        _benchmark(session, CALENDAR)
        for cid in (a, b):
            _prices(session, cid, CALENDAR)
            self._observe_fundamentals(session, cid)

        ra = reconstruct_score(session, settings_for(), a, AS_OF)
        rb = reconstruct_score(session, settings_for(), b, AS_OF)

        assert ra is not None and rb is not None
        assert ra.metrics is not None and rb.metrics is not None
        assert ra.metrics.market_cap == rb.metrics.market_cap
        assert ra.score.final_score == rb.score.final_score
        assert ra.score.status is rb.score.status


def settings_for() -> Settings:
    """A settings object for the calls that do not take the fixture."""
    return Settings(environment="test", benchmark_symbol=BENCHMARK)
