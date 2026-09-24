"""One monthly cross-section: who was in it, what they scored, could they be bought.

This is the decision layer, and the only place the frozen score is rebuilt. It
runs once per decision date and produces one row per identity the frozen
universe admits, carrying the furthest stage that identity reached and, when it
stopped, why. Nothing is dropped silently: an identity that had no filings, or
no proven liquidity, or no opening price to execute at, is a row with a reason.

## What may be read here, and what may not

Every input is bounded by the decision date and comes from code that already
owns that bound — `reconstruct_score` for a company, `reconstruct_historical_score`
for a security that has left the market, the frozen `MARKET_SIGNALS_V1`
snapshot captured on the session itself. The forward-return labels are **not**
imported by this module and must never be: they are what the decision is later
judged against, and a decision that read them would be judging itself.

The one execution fact this layer needs is whether the next session has an
opening price for the identity. That is a property of the session *after* the
decision, which is exactly when the trade happens, so knowing it is not
hindsight — but selecting *on* it is a small one, and it is recorded as an
execution outcome (`UNPRICED_NOT_EXECUTED`) rather than an eligibility rule.

## The conservative lag for historical securities

`HistoricalObservationRepository.available_as_of` admits a filing whose
`filed_date` equals the decision date. A company's availability is decided by
its acceptance instant against the 16:00 close and so is already exact; a
historical security's is decided by a date alone. Rather than weaken the rule
or rewrite frozen code, an identity with a filing dated on the decision session
is excluded from that session with `SAME_DAY_HISTORICAL_FILING`, and the count
is reported.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from domain import Freshness, ScoringStatus, is_rank_eligible
from domain.signals import MARKET_SIGNALS_V1, Tradability
from stock_screener.backtest.historical import reconstruct_historical_score
from stock_screener.backtest.scores import benchmark_returns_as_of, reconstruct_score
from stock_screener.backtest_harness.universe import COMPANY, HISTORICAL
from stock_screener.pit.capture import FEATURE_VERSION

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from datetime import date
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.backtest.historical import HistoricalReconstruction
    from stock_screener.backtest.scores import ReconstructedScore
    from stock_screener.backtest_harness.sessions import DecisionDate
    from stock_screener.backtest_harness.universe import PitUniverse
    from stock_screener.config import Settings

__all__ = [
    "CROSS_SECTION_FILE",
    "SIGNAL_VERSION",
    "CrossSectionRow",
    "MonthlyCoverage",
    "Stage",
    "cross_section",
    "write_cross_sections",
]

#: The frozen signal version whose tradability verdict the harness reads.
SIGNAL_VERSION = MARKET_SIGNALS_V1

CROSS_SECTION_FILE = "cross_sections.jsonl"


class Stage(StrEnum):
    """The furthest point an identity reached on one decision date."""

    UNIVERSE = "UNIVERSE"
    """In the frozen research universe for the month, and no further."""

    FEATURE_READY = "FEATURE_READY"
    """A `MARKET_SIGNALS_V1` snapshot was captured for it on the session."""

    SCORE_READY = "SCORE_READY"
    """The frozen formula produced a number from what had been filed."""

    RANKABLE = "RANKABLE"
    """`is_rank_eligible` — scored, on fundamentals current for its cadence."""

    TRADABLE = "TRADABLE"
    """The frozen signal layer proved a liquidity floor on the session."""

    EXECUTABLE = "EXECUTABLE"
    """The next session has an opening price to execute at."""


@dataclass(frozen=True)
class CrossSectionRow:
    """One identity on one decision date, with everything the decision saw.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`.
        identity_id: The primary key within that class.
        symbol: The symbol the listing record observed. Display only.
        month: The decision month, `YYYY-MM`.
        decision: The decision session.
        execution: The session the decision would be executed at.
        universe_source: What admitted the identity to the universe.
        evidence_date: The acceptance date that classified the month.
        tradability: The frozen `MARKET_SIGNALS_V1` verdict, or `ABSENT`.
        score_status: The frozen `ScoringStatus`.
        final_score: The frozen CompounderScore, or None.
        freshness: Whether the fundamentals behind it were current.
        rank_eligible: The frozen V1.2 ranking policy's verdict.
        execution_price: The next session's opening price, or None.
        adv_dollar: The frozen single-venue median dollar volume on the session,
            or None where the record does not establish one. Diagnostic only.
        stage: The furthest stage reached.
        exclusion: Why it went no further, or None when it is executable.
        classification_is_current: Whether the score read an undated
            classification field — true for every company reconstruction.
    """

    kind: str
    identity_id: int
    symbol: str
    month: str
    decision: str
    execution: str
    universe_source: str
    evidence_date: str | None
    tradability: str
    score_status: str
    final_score: float | None
    freshness: str
    rank_eligible: bool
    execution_price: float | None
    adv_dollar: float | None
    stage: str
    exclusion: str | None
    classification_is_current: bool

    @property
    def key(self) -> tuple[str, int]:
        """The identity key."""
        return (self.kind, self.identity_id)

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping for the cross-section ledger."""
        return dict(sorted(asdict(self).items()))


@dataclass(frozen=True)
class MonthlyCoverage:
    """The funnel for one decision date, so sample shrinkage is never hidden.

    Attributes:
        month: The decision month.
        decision: The decision session.
        execution: The execution session.
        split: Which block the month belongs to.
        frozen_universe_rows: Identity-months the frozen universe holds.
        universe: Identities the evidence admits.
        company: Of those, `COMPANY` identities.
        historical: Of those, `HISTORICAL` identities.
        feature_ready: With a frozen signal snapshot on the session.
        score_ready: With a number from the frozen formula.
        rankable: Passing the frozen V1.2 ranking policy.
        tradable: With a proven liquidity floor.
        executable: With an opening price on the execution session.
        universe_exclusions: Counts by universe exclusion reason.
        exclusions: Counts by cross-section exclusion reason.
    """

    month: str
    decision: str
    execution: str
    split: str
    frozen_universe_rows: int
    universe: int
    company: int
    historical: int
    feature_ready: int
    score_ready: int
    rankable: int
    tradable: int
    executable: int
    universe_exclusions: dict[str, int]
    exclusions: dict[str, int]

    def as_json(self) -> dict[str, object]:
        """A deterministic mapping for the coverage report."""
        return dict(sorted(asdict(self).items()))


def _tradability_at(db: sqlite3.Connection, decision: date) -> dict[tuple[str, int], str]:
    rows: dict[tuple[str, int], str] = {}
    for company_id, historical_id, verdict in db.execute(
        "SELECT company_id, historical_security_id, tradability FROM pit_signal_snapshots"
        " WHERE snapshot_date = ? AND signal_version = ?",
        (decision.isoformat(), SIGNAL_VERSION),
    ):
        key = (
            (COMPANY, int(company_id))
            if company_id is not None
            else (
                HISTORICAL,
                int(historical_id),
            )
        )
        rows[key] = str(verdict)
    return rows


def _adv_at(db: sqlite3.Connection, decision: date) -> dict[tuple[str, int], float]:
    rows: dict[tuple[str, int], float] = {}
    for company_id, historical_id, volume in db.execute(
        "SELECT company_id, historical_security_id, venue_dollar_volume_median"
        " FROM pit_feature_snapshots WHERE snapshot_date = ? AND feature_version = ?"
        " AND venue_dollar_volume_median IS NOT NULL",
        (decision.isoformat(), FEATURE_VERSION),
    ):
        key = (
            (COMPANY, int(company_id))
            if company_id is not None
            else (HISTORICAL, int(historical_id))
        )
        rows[key] = float(volume)
    return rows


def _opens_at(db: sqlite3.Connection, execution: date) -> dict[tuple[str, int], float]:
    rows: dict[tuple[str, int], float] = {}
    for company_id, opening in db.execute(
        "SELECT company_id, open FROM price_history WHERE date = ? AND open IS NOT NULL",
        (execution.isoformat(),),
    ):
        rows[(COMPANY, int(company_id))] = float(opening)
    for historical_id, opening in db.execute(
        "SELECT historical_security_id, open FROM historical_prices"
        " WHERE date = ? AND open IS NOT NULL",
        (execution.isoformat(),),
    ):
        rows[(HISTORICAL, int(historical_id))] = float(opening)
    return rows


def _same_day_historical_filings(db: sqlite3.Connection, decision: date) -> set[int]:
    return {
        int(row[0])
        for row in db.execute(
            "SELECT DISTINCT historical_security_id FROM historical_observations"
            " WHERE filed_date = ?",
            (decision.isoformat(),),
        )
    }


def cross_section(
    session: Session,
    db: sqlite3.Connection,
    settings: Settings,
    universe: PitUniverse,
    when: DecisionDate,
    *,
    split: str,
) -> tuple[list[CrossSectionRow], MonthlyCoverage]:
    """Build one decision date's cross-section and its coverage funnel.

    Args:
        session: An open database session, for the frozen reconstructions.
        db: The same database opened read-only, for the bulk projections.
        settings: Supplies the eligibility thresholds, benchmark and volume
            basis the frozen score needs.
        universe: The frozen research universe.
        when: The decision date.
        split: The block the month belongs to, recorded on the coverage row.

    Returns:
        The rows in identity order, and the funnel.
    """
    members, universe_exclusions = universe.eligible(when.month, when.decision)
    tradability = _tradability_at(db, when.decision)
    opens = _opens_at(db, when.execution)
    same_day = _same_day_historical_filings(db, when.decision)
    adv = _adv_at(db, when.decision)
    benchmark = benchmark_returns_as_of(session, settings.benchmark_symbol, when.decision)

    rows: list[CrossSectionRow] = []
    exclusions: dict[str, int] = defaultdict(int)
    counts = dict.fromkeys(
        ("feature_ready", "score_ready", "rankable", "tradable", "executable"), 0
    )
    for member in members:
        verdict = tradability.get(member.key)
        stage = Stage.UNIVERSE
        exclusion: str | None = None
        status = str(ScoringStatus.INSUFFICIENT_DATA)
        final: float | None = None
        freshness = str(Freshness.CURRENT)
        rankable = False
        classification_current = member.kind == COMPANY

        if verdict is None:
            exclusion = "NO_SIGNAL_SNAPSHOT"
        elif member.kind == HISTORICAL and member.identity_id in same_day:
            exclusion = "SAME_DAY_HISTORICAL_FILING"
        else:
            stage = Stage.FEATURE_READY
            counts["feature_ready"] += 1
            built: ReconstructedScore | HistoricalReconstruction | None
            if member.kind == COMPANY:
                built = reconstruct_score(
                    session,
                    settings,
                    member.identity_id,
                    when.decision,
                    benchmark=benchmark,
                )
            else:
                built = reconstruct_historical_score(
                    session, settings, member.identity_id, when.decision
                )
            if built is None:
                exclusion = "IDENTITY_ABSENT"
            else:
                status = str(built.score.status)
                final = built.score.final_score
                freshness = str(built.freshness)
                rankable = is_rank_eligible(built.score.status, built.freshness)
                if built.score.status is not ScoringStatus.SCORED or final is None:
                    exclusion = f"SCORE_{status}"
                else:
                    stage = Stage.SCORE_READY
                    counts["score_ready"] += 1
                    if not rankable:
                        exclusion = "STALE_FUNDAMENTALS"
                    else:
                        stage = Stage.RANKABLE
                        counts["rankable"] += 1
                        if verdict != str(Tradability.TRADABLE):
                            exclusion = f"NOT_TRADABLE_{verdict}"
                        else:
                            stage = Stage.TRADABLE
                            counts["tradable"] += 1
                            if member.key not in opens:
                                exclusion = "NO_EXECUTION_PRICE"
                            else:
                                stage = Stage.EXECUTABLE
                                counts["executable"] += 1
        if exclusion is not None:
            exclusions[exclusion] += 1
        rows.append(
            CrossSectionRow(
                kind=member.kind,
                identity_id=member.identity_id,
                symbol=member.symbol,
                month=when.month,
                decision=when.decision.isoformat(),
                execution=when.execution.isoformat(),
                universe_source=member.source,
                evidence_date=(member.evidence_date.isoformat() if member.evidence_date else None),
                tradability=verdict or "ABSENT",
                score_status=status,
                final_score=final,
                freshness=freshness,
                rank_eligible=rankable,
                execution_price=opens.get(member.key),
                adv_dollar=adv.get(member.key),
                stage=str(stage),
                exclusion=exclusion,
                classification_is_current=classification_current,
            )
        )
    coverage = MonthlyCoverage(
        month=when.month,
        decision=when.decision.isoformat(),
        execution=when.execution.isoformat(),
        split=split,
        frozen_universe_rows=universe.rows_in(when.month),
        universe=len(members),
        company=sum(1 for m in members if m.kind == COMPANY),
        historical=sum(1 for m in members if m.kind == HISTORICAL),
        feature_ready=counts["feature_ready"],
        score_ready=counts["score_ready"],
        rankable=counts["rankable"],
        tradable=counts["tradable"],
        executable=counts["executable"],
        universe_exclusions=dict(sorted(universe_exclusions.items())),
        exclusions=dict(sorted(exclusions.items())),
    )
    return rows, coverage


def write_cross_sections(path: Path, rows: Sequence[CrossSectionRow]) -> None:
    """Write the cross-section ledger, one JSON object per row, in order.

    Args:
        path: The destination.
        rows: The rows, already ordered by date then identity.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row.as_json(), sort_keys=True) + "\n")
