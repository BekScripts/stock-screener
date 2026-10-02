"""Producing the prospective COMPOUNDER_V1_2 scores, with the frozen scorer.

There is exactly one CompounderScore implementation in this repository and this
module calls it. `evaluate_eligibility`, `score_company`, `return_6m` and
`return_12m` are the same functions the historical reconstruction uses, invoked
on inputs built from prospective snapshots instead of from stored rows. Nothing
here recomputes a weight, a curve or a threshold.

That matters more than it sounds. A prospective-only scorer would be a second
definition of the thing the whole experiment is about, and the two could drift
without a single test failing. The frozen score records its own rules, so the
prospective stream must produce an ordinary `COMPOUNDER_V1_2` number or it
produces nothing comparable at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from domain import (
    BenchmarkReturns,
    EligibilityWarning,
    Freshness,
    ScoringStatus,
    evaluate_eligibility,
    is_rank_eligible,
    return_6m,
    return_12m,
    score_company,
)
from stock_screener.prospective_source.envelope import content_digest

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from domain import EligibilityThresholds, PriceBar

    from .score_inputs import ScoreInputSet

log = structlog.get_logger(__name__)

__all__ = [
    "SCORE_ARTIFACT_ID",
    "ProspectiveScore",
    "ProspectiveScoreSet",
    "benchmark_from_bars",
    "score_cross_section",
]

#: The artifact's identity, recorded in the decision packet's provenance.
SCORE_ARTIFACT_ID = "PROSPECTIVE_SCORES_V1"


def benchmark_from_bars(bars: Sequence[PriceBar], *, symbol: str, as_of: date) -> BenchmarkReturns:
    """Measure the benchmark as it stood at the decision, not as it stands now.

    Args:
        bars: The benchmark's bars, any order.
        symbol: The benchmark's ticker, recorded on the result.
        as_of: The decision session. Bars after it are dropped first.

    Returns:
        The returns, each None where the bounded history does not reach back far
        enough — never borrowed from a later window.
    """
    bounded = sorted((bar for bar in bars if bar.date <= as_of), key=lambda bar: bar.date)
    return BenchmarkReturns(
        symbol=symbol, return_6m=return_6m(bounded), return_12m=return_12m(bounded)
    )


@dataclass(frozen=True)
class ProspectiveScore:
    """One identity's frozen score at the decision instant.

    Attributes:
        kind: `COMPANY`.
        identity_id: The research identity.
        symbol: Display only.
        status: The frozen `ScoringStatus`.
        final_score: The CompounderScore, or None when not scored.
        freshness: Whether the fundamentals behind it are current for the cadence.
        rank_eligible: Whether the frozen policy permits ranking it.
        eligible: The eligibility verdict passed to the scorer.
        warnings: The eligibility warnings, for the record.
    """

    kind: str
    identity_id: int
    symbol: str
    status: str
    final_score: float | None
    freshness: str
    rank_eligible: bool
    eligible: bool
    warnings: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, int]:
        """The identity, as the harness addresses it."""
        return (self.kind, self.identity_id)

    def as_json(self) -> dict[str, Any]:
        """Return the score as a JSON-safe mapping."""
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "symbol": self.symbol,
            "status": self.status,
            "final_score": self.final_score,
            "freshness": self.freshness,
            "rank_eligible": self.rank_eligible,
            "eligible": self.eligible,
            "warnings": list(self.warnings),
        }


@dataclass
class ProspectiveScoreSet:
    """Every score for one decision, as a sealable artifact.

    Attributes:
        decision_session: The session scored.
        decision_instant: The instant the inputs were bounded to.
        benchmark: What relative strength was measured against.
        scores: One entry per identity, in identity order.
        score_input_fingerprint: The artifact these were computed from.
    """

    decision_session: str
    decision_instant: str
    benchmark: dict[str, Any] = field(default_factory=dict)
    scores: list[ProspectiveScore] = field(default_factory=list)
    score_input_fingerprint: str = ""

    @property
    def scored(self) -> int:
        """How many identities the frozen formula produced a number for."""
        return sum(1 for row in self.scores if row.final_score is not None)

    @property
    def rankable(self) -> int:
        """How many the frozen policy permits ranking."""
        return sum(1 for row in self.scores if row.rank_eligible)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body: scores and bindings, no run metadata."""
        return {
            "artifact_id": SCORE_ARTIFACT_ID,
            "score_version": "COMPOUNDER_V1_2",
            "decision_session": self.decision_session,
            "decision_instant": self.decision_instant,
            "benchmark": dict(sorted(self.benchmark.items())),
            "score_input_fingerprint": self.score_input_fingerprint,
            "scores": [row.as_json() for row in self.scores],
        }

    def fingerprint(self) -> str:
        """The artifact's deterministic digest."""
        return content_digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artifact with its fingerprint and counts."""
        return {
            **self.body(),
            "fingerprint": self.fingerprint(),
            "identity_count": len(self.scores),
            "scored_count": self.scored,
            "rankable_count": self.rankable,
            "scorer": "domain.score_company — the one frozen implementation",
            "reimplemented_formula": False,
        }


def score_cross_section(
    inputs: ScoreInputSet,
    *,
    benchmark_bars: Sequence[PriceBar],
    benchmark_symbol: str,
    decision_session: date,
    thresholds: EligibilityThresholds | None = None,
) -> ProspectiveScoreSet:
    """Score every identity with the frozen scorer.

    Args:
        inputs: The score-input artifact.
        benchmark_bars: The benchmark's bars, bounded no later than the session.
        benchmark_symbol: The benchmark's ticker.
        decision_session: The session being decided, used as the eligibility
            `today` so staleness is judged at the decision rather than now.
        thresholds: The frozen eligibility thresholds.

    Returns:
        The score artifact. An identity with no metrics is present and
        unscored — `INSUFFICIENT_DATA` rather than absent, because an identity
        that could not be scored and one that was never considered are
        different facts.
    """
    market = benchmark_from_bars(benchmark_bars, symbol=benchmark_symbol, as_of=decision_session)
    built = ProspectiveScoreSet(
        decision_session=inputs.decision_session,
        decision_instant=inputs.decision_instant,
        benchmark={
            "symbol": market.symbol,
            "return_6m": market.return_6m,
            "return_12m": market.return_12m,
            "measured_as_of": decision_session.isoformat(),
        },
        score_input_fingerprint=inputs.fingerprint(),
    )
    for row in inputs.inputs:
        if row.metrics is None:
            built.scores.append(
                ProspectiveScore(
                    kind=row.kind,
                    identity_id=row.identity_id,
                    symbol=row.symbol,
                    status=str(ScoringStatus.INSUFFICIENT_DATA),
                    final_score=None,
                    freshness=str(Freshness.CURRENT),
                    rank_eligible=False,
                    eligible=False,
                    warnings=(str(row.provenance.get("unusable_reason") or "NO_METRICS"),),
                )
            )
            continue
        eligibility = evaluate_eligibility(
            row.profile, row.metrics, thresholds, today=decision_session
        )
        score = score_company(row.profile, row.metrics, market, eligible=eligibility.eligible)
        freshness = (
            Freshness.STALE
            if EligibilityWarning.STALE_FUNDAMENTALS in eligibility.warnings
            else Freshness.CURRENT
        )
        built.scores.append(
            ProspectiveScore(
                kind=row.kind,
                identity_id=row.identity_id,
                symbol=row.symbol,
                status=str(score.status),
                final_score=score.final_score,
                freshness=str(freshness),
                rank_eligible=is_rank_eligible(score.status, freshness),
                eligible=eligibility.eligible,
                warnings=tuple(sorted(str(w) for w in eligibility.warnings)),
            )
        )
    built.scores.sort(key=lambda row: (row.kind, row.identity_id))
    log.info(
        "prospective.scores",
        identities=len(built.scores),
        scored=built.scored,
        rankable=built.rankable,
        fingerprint=built.fingerprint()[:16],
    )
    return built
