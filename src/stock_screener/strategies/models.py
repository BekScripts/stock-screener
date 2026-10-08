"""What the strategy reads and what it decides.

The types here are the contract between the frozen research record and the
portfolio layer. They carry evidence and a decision; they carry no order, no
quantity and no cash, because a strategy that knew the cash balance would be
tempted to size around the risk engine.

One asymmetry is deliberate throughout: a record that cannot be *proved*
admissible is refused rather than skipped. Skipping a name quietly shrinks the
universe, which changes the decile boundary, which changes the selection — so a
missing timestamp would silently alter the decision rather than stop it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from domain.scores import Freshness, ScoringStatus, is_rank_eligible

from .errors import Code, StrategyError

if TYPE_CHECKING:
    from stock_screener.portfolio_risk import SecurityIdentity

__all__ = [
    "StrategyDecision",
    "StrategyInputBundle",
    "StrategyInputRecord",
    "StrategySelection",
]


@dataclass(frozen=True)
class StrategyInputRecord:
    """One security's research evidence, as the strategy sees it.

    Attributes:
        identity: The durable key. The ticker is display metadata on it and
            never participates in equality, ordering or selection.
        final_score: The `COMPOUNDER_V1_2` score, or `None` when the company has
            no number. `None` is not zero and is never substituted.
        scoring_status: The frozen `ScoringStatus` value.
        freshness: The frozen `Freshness` value.
        score_date: The cross-section date the score belongs to.
        score_version: Which formula produced it.
        calculated_at: When the scoring run computed it, ISO-8601 with offset.
            This is the admissibility instant — see `StrategyInputBundle`.
        source_fingerprint: The digest of the evidence behind this record.
    """

    identity: SecurityIdentity
    final_score: float | None
    scoring_status: str
    freshness: str
    score_date: str
    score_version: str
    calculated_at: str
    source_fingerprint: str

    @property
    def is_selectable(self) -> bool:
        """Whether this record may be ranked for a *current* selection.

        Delegates to the frozen `is_rank_eligible`, which is the whole of the
        V1.2 policy. Re-deriving it here would let the strategy and the ranking
        views disagree about the same company on the same day.

        Returns:
            True only for a scored company on current fundamentals that also
            carries a number. A status of `SCORED` with a `None` score is a
            contradiction in the record and is treated as unselectable rather
            than as a zero.
        """
        if self.final_score is None:
            return False
        try:
            status = ScoringStatus(self.scoring_status)
            fresh = Freshness(self.freshness)
        except ValueError:
            return False
        return is_rank_eligible(status, fresh)

    def body(self) -> dict[str, Any]:
        """Return the record's semantic content."""
        return {
            "identity": self.identity.label,
            "final_score": self.final_score,
            "scoring_status": self.scoring_status,
            "freshness": self.freshness,
            "score_date": self.score_date,
            "score_version": self.score_version,
            "calculated_at": self.calculated_at,
            "source_fingerprint": self.source_fingerprint,
        }


@dataclass(frozen=True)
class StrategyInputBundle:
    """One cross-section of research evidence, offered for one decision.

    Admissibility rests on `calculated_at` rather than on per-filing acceptance
    times, and that choice is worth defending because it looks like a shortcut
    and is not.

    A `ScoreSnapshot` does not record which filings it read. What it does record
    is when the scoring run computed it. A run that computed at instant T read
    the database as it stood at T, so it cannot have used anything published
    after T — whatever the individual acceptance times were. If every record in
    the bundle satisfies `calculated_at <= decision_at`, no information from
    after the decision reached the decision. That is the property §7 asks for,
    established from a timestamp the record actually carries instead of from one
    it does not.

    The limit of the argument, stated rather than buried: this bounds
    availability relative to the *decision*, not relative to `score_date`. A
    backfilled snapshot — an old `score_date` computed recently — contains no
    information from after its `calculated_at`, so it is admissible for a
    decision made later, but it is not a reconstruction of what was knowable on
    its `score_date`. For prospective operation, where the score is computed
    forward and the decision follows it, the two coincide.

    Attributes:
        as_of: The cross-section date every record belongs to.
        score_version: The formula version. Must be the frozen one.
        universe_fingerprint: The digest of the population considered.
        records: The evidence, in any order. Ordering never affects a result.
        signal_version: `MARKET_SIGNALS_V1` where a signal was read. The first
            strategy reads none — see `compounder_monthly`.
        signal_fingerprint: That signal's digest, where one was read.
    """

    as_of: str
    score_version: str
    universe_fingerprint: str
    records: tuple[StrategyInputRecord, ...] = ()
    signal_version: str | None = None
    signal_fingerprint: str | None = None

    @property
    def ordered(self) -> tuple[StrategyInputRecord, ...]:
        """The records in canonical order, by durable identity.

        Returns:
            The records sorted by `(kind, identity_id)`. Every fingerprint and
            every ranking is computed from this rather than from the order the
            caller happened to supply, which is what makes the decision
            invariant to input ordering.
        """
        return tuple(sorted(self.records, key=lambda row: row.identity.key))

    def require_admissible(self, *, decision_at: str) -> None:
        """Refuse the bundle unless every record predates the decision.

        Args:
            decision_at: The decision instant, ISO-8601 with offset.

        Raises:
            StrategyError: `STRATEGY_INPUT_INCOMPLETE` if a record carries no
                `calculated_at`, because admissibility cannot then be
                established and a record of unknown provenance must not quietly
                shrink the universe.
                `STRATEGY_INPUT_AFTER_DECISION_TIME` if any record was computed
                after the decision instant. There is no fallback to an older
                record: a decision that silently reached backwards for
                admissible evidence would be a different decision wearing this
                one's fingerprint.
        """
        missing = [row.identity.label for row in self.ordered if not row.calculated_at]
        if missing:
            raise StrategyError(
                Code.STRATEGY_INPUT_INCOMPLETE,
                f"{len(missing)} record(s) carry no calculated_at, so availability "
                f"cannot be established: {missing[:5]}. A record whose provenance is "
                "unknown is refused rather than dropped — dropping it would change "
                "the eligible count, the decile boundary and therefore the selection",
            )
        # Compared as instants, never as strings. Lexicographic comparison of
        # ISO-8601 is wrong whenever two offsets differ, and wrong in both
        # directions: "2026-10-30T18:00:00+00:00" is 14:00 New York and sorts
        # after a 16:00-04:00 close it actually precedes, while
        # "2026-10-30T13:00:00-08:00" is 17:00 New York and sorts before the
        # same close it actually follows. The second mistake admits evidence
        # from after the decision, which is the one thing this check exists to
        # prevent.
        from stock_screener.paper_operations import parse_instant

        try:
            cutoff = parse_instant(decision_at)
        except ValueError as exc:
            raise StrategyError(
                Code.STRATEGY_INPUT_NOT_ADMISSIBLE,
                f"the decision instant {decision_at!r} carries no UTC offset, so "
                "admissibility cannot be decided",
            ) from exc
        future: list[tuple[str, str]] = []
        unreadable: list[str] = []
        for row in self.ordered:
            try:
                computed = parse_instant(row.calculated_at)
            except ValueError:
                unreadable.append(row.identity.label)
                continue
            if computed > cutoff:
                future.append((row.identity.label, row.calculated_at))
        if unreadable:
            raise StrategyError(
                Code.STRATEGY_INPUT_NOT_ADMISSIBLE,
                f"{len(unreadable)} record(s) carry a calculated_at with no UTC "
                f"offset: {unreadable[:5]}. A timestamp whose zone was guessed is "
                "worse than one that failed to parse, because nobody notices the "
                "guess",
            )
        if future:
            raise StrategyError(
                Code.STRATEGY_INPUT_AFTER_DECISION_TIME,
                f"{len(future)} record(s) were computed after the decision instant "
                f"{decision_at}: {future[:5]}. No older record is substituted",
            )

    def body(self) -> dict[str, Any]:
        """Return the bundle's semantic content, canonically ordered."""
        return {
            "as_of": self.as_of,
            "score_version": self.score_version,
            "universe_fingerprint": self.universe_fingerprint,
            "signal_version": self.signal_version,
            "signal_fingerprint": self.signal_fingerprint,
            "records": [row.body() for row in self.ordered],
        }

    def fingerprint(self) -> str:
        """Digest the bundle. Invariant to the order records arrived in."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())


@dataclass(frozen=True)
class StrategySelection:
    """One selected security and the exposure the strategy wants for it.

    Attributes:
        identity: The durable key.
        final_score: The score that earned the place.
        rank: Position in the ranking, 1 is best.
        desired_weight: The *relative* strategy exposure, `1 / K`. A preference,
            never an authorised size — the risk engine decides that.
    """

    identity: SecurityIdentity
    final_score: float
    rank: int
    desired_weight: float

    def body(self) -> dict[str, Any]:
        """Return the selection's semantic content."""
        return {
            "identity": self.identity.label,
            "final_score": self.final_score,
            "rank": self.rank,
            "desired_weight": self.desired_weight,
        }


@dataclass(frozen=True)
class StrategyDecision:
    """One immutable monthly decision.

    Attributes:
        strategy_id: Which strategy.
        strategy_version: Its frozen version.
        strategy_fingerprint: The digest of the policy that decided.
        decision_session: The market session decided on.
        decision_at: The decision instant, the session's close.
        first_execution_session: The earliest session this may be executed in —
            strictly after the decision session, so a close-derived decision is
            never executed earlier in the same session.
        input_fingerprint: The evidence digest.
        universe_fingerprint: The population digest.
        calendar_fingerprint: The calendar digest.
        eligible_count: How many records were selectable.
        considered_count: How many records were offered.
        target_count: How many the decile rule asked for.
        selections: The chosen securities and their relative exposures.
        exits: Securities the previous decision wanted and this one does not.
            They receive desired exposure zero through the intent contract; no
            order is implied here and none is computed.
        previous_decision_fingerprint: What this supersedes, when anything.
        excluded_counts: Why records were not selectable, by reason.
    """

    strategy_id: str
    strategy_version: str
    strategy_fingerprint: str
    decision_session: str
    decision_at: str
    first_execution_session: str
    input_fingerprint: str
    universe_fingerprint: str
    calendar_fingerprint: str
    eligible_count: int
    considered_count: int
    target_count: int
    selections: tuple[StrategySelection, ...] = ()
    exits: tuple[SecurityIdentity, ...] = ()
    previous_decision_fingerprint: str | None = None
    excluded_counts: dict[str, int] = field(default_factory=dict)

    @property
    def selected_identities(self) -> tuple[SecurityIdentity, ...]:
        """The selected securities, in rank order."""
        return tuple(row.identity for row in self.selections)

    def body(self) -> dict[str, Any]:
        """Return the decision's semantic content. Carries no credential."""
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "strategy_fingerprint": self.strategy_fingerprint,
            "decision_session": self.decision_session,
            "decision_at": self.decision_at,
            "first_execution_session": self.first_execution_session,
            "input_fingerprint": self.input_fingerprint,
            "universe_fingerprint": self.universe_fingerprint,
            "calendar_fingerprint": self.calendar_fingerprint,
            "considered_count": self.considered_count,
            "eligible_count": self.eligible_count,
            "target_count": self.target_count,
            "selections": [row.body() for row in self.selections],
            "exits": [identity.label for identity in self.exits],
            "previous_decision_fingerprint": self.previous_decision_fingerprint,
            "excluded_counts": dict(sorted(self.excluded_counts.items())),
        }

    def fingerprint(self) -> str:
        """Digest the decision."""
        from stock_screener.execution import fingerprint_of

        return fingerprint_of(self.body())

    @property
    def decision_id(self) -> str:
        """A derived, stable id for this decision.

        Returns:
            The strategy, the session and the decision digest. Derived rather
            than generated so re-deriving the same decision from the same
            evidence produces the same id, and a retry is recognisable as a
            retry rather than as a second decision.
        """
        return f"{self.strategy_id}:{self.decision_session}:{self.fingerprint()[:16]}"

    def as_json(self) -> dict[str, Any]:
        """Return the decision with its id and fingerprint."""
        return {
            **self.body(),
            "decision_id": self.decision_id,
            "decision_fingerprint": self.fingerprint(),
        }
