"""The executable prospective cross-section, and its ranking.

R1.11 found `ranked_cross_section` marked READY with a null fingerprint because
its ingredients existed. A cross-section is not its ingredients: it is the funnel
that decides which identities a decision may actually hold, and every identity
that falls out of it does so for exactly one recorded reason.

The funnel mirrors the historical one stage for stage, because the prospective
stream has to be comparable to the research it is testing. An identity reaches
`EXECUTABLE` only when it is in the universe with a resolved identity, the frozen
formula scored it, the frozen policy permits ranking it, the broker says it is
tradable, and a bar exists for the decision session. Anything less is excluded
and counted.

Ranking is `membership_hypothesis.rank` — descending score, ties broken by
`(kind, identity_id)`. Ticker never enters it. Nothing in this module can see a
forward return: `Candidate` has no field one could arrive in, which is the point
of that type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.membership_hypothesis import Candidate, rank
from stock_screener.prospective_source.envelope import content_digest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .scoring import ProspectiveScoreSet

log = structlog.get_logger(__name__)

__all__ = [
    "CROSS_SECTION_ARTIFACT_ID",
    "CrossSectionRow",
    "ProspectiveCrossSection",
    "ProspectiveStage",
    "build_cross_section",
]

#: The artifact's identity, recorded in the decision packet's provenance.
CROSS_SECTION_ARTIFACT_ID = "PROSPECTIVE_CROSS_SECTION_V1"


class ProspectiveStage(StrEnum):
    """The furthest point an identity reached at this decision."""

    UNIVERSE = "UNIVERSE"
    """In the prospective universe with a resolved research identity."""

    SCORE_READY = "SCORE_READY"
    """The frozen formula produced a number from what had been filed."""

    RANKABLE = "RANKABLE"
    """Scored, on fundamentals current for its cadence."""

    TRADABLE = "TRADABLE"
    """The broker's asset list says the security can be traded."""

    EXECUTABLE = "EXECUTABLE"
    """A bar exists for the decision session, so the decision can be priced."""


@dataclass(frozen=True)
class CrossSectionRow:
    """One identity at one prospective decision, carrying no outcome.

    Attributes:
        kind: `COMPANY`.
        identity_id: The research identity.
        symbol: Display metadata only, never a ranking key.
        score_status: The frozen `ScoringStatus`.
        final_score: The frozen CompounderScore, or None.
        freshness: Whether the fundamentals are current for the cadence.
        rank_eligible: Whether the frozen policy permits ranking it.
        tradability: The broker's verdict.
        has_session_bar: Whether the decision session priced.
        stage: How far it got.
        exclusion: The one reason it got no further, or None.
    """

    kind: str
    identity_id: int
    symbol: str
    score_status: str
    final_score: float | None
    freshness: str
    rank_eligible: bool
    tradability: str
    has_session_bar: bool
    stage: str
    exclusion: str | None = None

    @property
    def key(self) -> tuple[str, int]:
        """The identity, as the harness addresses it."""
        return (self.kind, self.identity_id)

    @property
    def executable(self) -> bool:
        """Whether this identity may be selected at this decision."""
        return self.stage == str(ProspectiveStage.EXECUTABLE)

    def as_json(self) -> dict[str, Any]:
        """Return the row as a JSON-safe mapping."""
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "symbol": self.symbol,
            "score_status": self.score_status,
            "final_score": self.final_score,
            "freshness": self.freshness,
            "rank_eligible": self.rank_eligible,
            "tradability": self.tradability,
            "has_session_bar": self.has_session_bar,
            "stage": self.stage,
            "exclusion": self.exclusion,
        }


@dataclass
class ProspectiveCrossSection:
    """The ranked executable cross-section for one decision.

    Attributes:
        decision_session: The session decided.
        decision_instant: The instant the inputs were bounded to.
        rows: Every identity considered, in identity order.
        ranks: `{identity: rank}` over the executable set, 1-based.
        source_fingerprints: What this was built from.
    """

    decision_session: str
    decision_instant: str
    rows: list[CrossSectionRow] = field(default_factory=list)
    ranks: dict[tuple[str, int], int] = field(default_factory=dict)
    source_fingerprints: dict[str, str] = field(default_factory=dict)

    @property
    def executable(self) -> list[CrossSectionRow]:
        """The identities a decision may hold, best-ranked first."""
        return sorted(
            (row for row in self.rows if row.executable),
            key=lambda row: self.ranks.get(row.key, 0),
        )

    @property
    def candidates(self) -> list[Candidate]:
        """The executable set as the frozen rule's own input type."""
        return [
            Candidate(
                kind=row.kind,
                identity_id=row.identity_id,
                final_score=float(row.final_score or 0.0),
                eligible=True,
            )
            for row in self.executable
        ]

    @property
    def funnel(self) -> dict[str, int]:
        """How many identities reached each stage."""
        counts = dict.fromkeys((str(stage) for stage in ProspectiveStage), 0)
        order = [str(stage) for stage in ProspectiveStage]
        for row in self.rows:
            reached = order.index(row.stage)
            for name in order[: reached + 1]:
                counts[name] += 1
        return counts

    @property
    def exclusions(self) -> dict[str, int]:
        """How many identities each exclusion reason accounts for."""
        counts: dict[str, int] = {}
        for row in self.rows:
            if row.exclusion:
                counts[row.exclusion] = counts.get(row.exclusion, 0) + 1
        return dict(sorted(counts.items()))

    def body(self) -> dict[str, Any]:
        """The fingerprinted body: rows, ranks and bindings, no run metadata."""
        return {
            "artifact_id": CROSS_SECTION_ARTIFACT_ID,
            "decision_session": self.decision_session,
            "decision_instant": self.decision_instant,
            "score_version": "COMPOUNDER_V1_2",
            "identity_key": "(kind, identity_id)",
            "source_fingerprints": dict(sorted(self.source_fingerprints.items())),
            "rows": [row.as_json() for row in self.rows],
            "ranks": [
                {"kind": kind, "identity_id": identity_id, "rank": position}
                for (kind, identity_id), position in sorted(self.ranks.items())
            ],
        }

    def fingerprint(self) -> str:
        """The artifact's deterministic digest."""
        return content_digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artifact with its fingerprint, funnel and exclusions."""
        return {
            **self.body(),
            "fingerprint": self.fingerprint(),
            "identity_count": len(self.rows),
            "executable_count": len(self.executable),
            "funnel": self.funnel,
            "exclusions": self.exclusions,
            "ranking": "descending final_score, ties by (kind, identity_id) ascending",
            "ticker_is_display_only": True,
            "carries_no_outcome": True,
        }


def build_cross_section(
    scores: ProspectiveScoreSet,
    *,
    tradability: Mapping[tuple[str, int], str],
    session_priced: Sequence[tuple[str, int]],
    source_fingerprints: Mapping[str, str],
) -> ProspectiveCrossSection:
    """Build and rank the executable cross-section.

    Args:
        scores: The score artifact.
        tradability: `{identity: verdict}` from the broker asset list. An
            identity absent from it is never treated as tradable.
        session_priced: Identities with a bar for the decision session.
        source_fingerprints: What this was built from.

    Returns:
        The artifact, ranked over the executable set.
    """
    priced = set(session_priced)
    built = ProspectiveCrossSection(
        decision_session=scores.decision_session,
        decision_instant=scores.decision_instant,
        source_fingerprints=dict(source_fingerprints),
    )
    for score in scores.scores:
        verdict = tradability.get(score.key, "ABSENT")
        stage, exclusion = ProspectiveStage.UNIVERSE, None
        if score.final_score is None:
            exclusion = f"SCORE_{score.status}"
        else:
            stage = ProspectiveStage.SCORE_READY
            if not score.rank_eligible:
                exclusion = "STALE_FUNDAMENTALS"
            else:
                stage = ProspectiveStage.RANKABLE
                if verdict != "TRADABLE":
                    exclusion = f"NOT_TRADABLE_{verdict}"
                else:
                    stage = ProspectiveStage.TRADABLE
                    if score.key not in priced:
                        exclusion = "NO_DECISION_SESSION_PRICE"
                    else:
                        stage = ProspectiveStage.EXECUTABLE
        built.rows.append(
            CrossSectionRow(
                kind=score.kind,
                identity_id=score.identity_id,
                symbol=score.symbol,
                score_status=score.status,
                final_score=score.final_score,
                freshness=score.freshness,
                rank_eligible=score.rank_eligible,
                tradability=verdict,
                has_session_bar=score.key in priced,
                stage=str(stage),
                exclusion=exclusion,
            )
        )
    built.rows.sort(key=lambda row: (row.kind, row.identity_id))
    executable = [
        Candidate(
            kind=row.kind,
            identity_id=row.identity_id,
            final_score=float(row.final_score or 0.0),
        )
        for row in built.rows
        if row.executable
    ]
    built.ranks = {
        candidate.key: position for position, candidate in enumerate(rank(executable), start=1)
    }
    log.info(
        "prospective.cross_section",
        identities=len(built.rows),
        executable=len(executable),
        fingerprint=built.fingerprint()[:16],
    )
    return built
