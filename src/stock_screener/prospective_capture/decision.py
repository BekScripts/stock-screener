"""Stage D: the executor that actually produces a prospective decision packet.

R1.11 found `decision_packet()` with zero production callers. This is the caller.
It consumes the sealed six-input package and nothing else, applies the frozen
candidate through the one authoritative implementation, and writes exactly one
packet per decision session.

The order is fixed and the reasons are not stylistic:

1. a session already missed is refused before anything is computed, because the
   temptation to reconstruct one is strongest when the code to do it finally
   exists;
2. the arm and activation are checked, so a packet can never be produced under a
   superseded authorisation;
3. the time gate, because a decision taken before its instant is a forecast;
4. the package is verified recoverable, so the inputs are provably the sealed
   ones rather than whatever is in memory;
5. scoring, ranking and selection run with no network reachable at all;
6. the six fingerprints are required *before* the packet is constructed — five is
   not a degraded packet, it is not a packet;
7. the packet is written, read back, digest-verified, and only then does the state
   advance. A state that moved on an unverified write would claim an observation
   nobody can audit.

Incumbents come from the prospective stream's own prior decisions and from
nowhere else. With no valid observation yet, the first decision is ordinary
top-decile, which is what the frozen candidate says it is.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.membership_hypothesis import H2_ID
from stock_screener.prospective_protocol import CANDIDATE_ID, PROTOCOL_ID
from stock_screener.prospective_runner import (
    ProspectiveState,
    decision_packet,
    finalise,
    load_state,
    packet_digest,
    paired_targets,
    save_state,
)
from stock_screener.prospective_source.window import refuse_missed_session

from .crosssection import build_cross_section
from .readiness import require_six_fingerprints
from .score_inputs import build_score_inputs
from .scoring import score_cross_section

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from domain import EligibilityThresholds, PriceBar, VolumeBasis
    from stock_screener.prospective_source import SnapshotStore

    from .calendar_binding import CalendarBinding
    from .identity import ProspectiveIdentity
    from .package import SealedPackage

log = structlog.get_logger(__name__)

__all__ = [
    "DECISION_FILE",
    "DecisionCapture",
    "DecisionRefusedError",
    "capture_decision",
    "prior_incumbents",
]

#: One packet per decision session, named by it.
DECISION_FILE = "decision_{session}.json"


class DecisionRefusedError(RuntimeError):
    """Raised when a decision must not be produced."""


@dataclass(frozen=True)
class DecisionCapture:
    """The result of a stage D capture.

    Attributes:
        decision_session: The session decided.
        packet_path: Where the packet landed.
        packet_digest: Its digest, which the state transition is tied to.
        written: False when an identical packet already existed.
        fingerprints: The six required input fingerprints.
        targets: The paired target sets.
        state: The state after the capture.
    """

    decision_session: str
    packet_path: Path
    packet_digest: str
    written: bool
    fingerprints: dict[str, str]
    targets: dict[str, Any]
    state: str

    def as_json(self) -> dict[str, Any]:
        """Return the capture as a JSON-safe mapping."""
        return {
            "decision_session": self.decision_session,
            "packet_path": str(self.packet_path),
            "packet_sha256": self.packet_digest,
            "written": self.written,
            "fingerprints": dict(sorted(self.fingerprints.items())),
            "target_count": self.targets.get("target_count"),
            "cross_section_size": self.targets.get("cross_section_size"),
            "h2_target_set_size": len(self.targets.get("h2_target_set") or ()),
            "sets_are_equal": self.targets.get("sets_are_equal"),
            "state": self.state,
            "broker_requests": 0,
        }


def prior_incumbents(directory: Path, *, before_session: str) -> list[tuple[str, int]]:
    """What the prospective stream held going into a decision.

    Read from the stream's own earlier decision packets and nowhere else. The
    historical experiment's holdings, the missed September session and any
    brokerage position are all inadmissible here: an incumbent set imported from
    one of those would make the first prospective decision a continuation of
    something that never prospectively happened.

    Args:
        directory: Where the window's packets live.
        before_session: The session being decided.

    Returns:
        The identities held, or empty when there is no prior decision — which is
        what makes the first decision ordinary top-decile.
    """
    earlier = sorted(
        path
        for path in directory.glob("decision_*.json")
        if _is_decision_packet(path) and path.stem.removeprefix("decision_") < before_session
    )
    if not earlier:
        return []
    body = json.loads(earlier[-1].read_text(encoding="utf-8"))
    return [(str(row["kind"]), int(row["identity_id"])) for row in body.get("h2_target_set") or ()]


def _is_decision_packet(path: Path) -> bool:
    """Whether a path is a decision packet rather than something adjacent.

    The sealed input manifest lives in the same directory and is also called
    `decision_…`, so the name alone is not enough: a packet is keyed by its
    session, so the suffix must parse as a date.

    Args:
        path: The candidate file.

    Returns:
        True when the filename's key is an ISO date.
    """
    try:
        datetime.strptime(path.stem.removeprefix("decision_"), "%Y-%m-%d")  # noqa: DTZ007
    except ValueError:
        return False
    return True


def capture_decision(
    store: SnapshotStore,
    directory: Path,
    *,
    package: SealedPackage,
    activation: Mapping[str, Any],
    arm: Mapping[str, Any],
    calendar: CalendarBinding,
    identities: Sequence[ProspectiveIdentity],
    benchmark_bars: Sequence[PriceBar],
    benchmark_symbol: str,
    bar_volume_basis: VolumeBasis,
    thresholds: EligibilityThresholds | None,
    semantic_fingerprint: str,
    code_commit: str,
    state_path: Path,
    now: datetime,
) -> DecisionCapture:
    """Produce the one authoritative decision packet for a session.

    Args:
        store: The prospective snapshot store.
        directory: Where the window's artifacts and packets live.
        package: The sealed, completed six-input package.
        activation: The semantic activation record in force.
        arm: The arm record in force.
        calendar: The verified calendar binding.
        identities: The resolved identities the package covers.
        benchmark_bars: The benchmark's bars, bounded to the decision session.
        benchmark_symbol: The benchmark's ticker.
        bar_volume_basis: What the bars' volume represents.
        thresholds: The frozen eligibility thresholds.
        semantic_fingerprint: The fingerprint now, which must match the arm's.
        code_commit: The commit now, which must match the arm's.
        state_path: The runner's state file.
        now: The current moment. Must be aware.

    Returns:
        The capture.

    Raises:
        DecisionRefusedError: If the session was missed, the authorisation does not
            bind, the instant has not been reached, or the package is incomplete.
    """
    session = package.decision_session
    # 1. A missed session is refused before any source is read or value computed.
    refuse_missed_session(session)

    # 2. The authorisation must still be the one that armed this window.
    if arm.get("semantic_code_fingerprint") != semantic_fingerprint:
        raise DecisionRefusedError(
            f"SEMANTIC_RESEARCH_CHANGE: the arm bound fingerprint "
            f"{str(arm.get('semantic_code_fingerprint'))[:16]}… and the code now "
            f"fingerprints to {semantic_fingerprint[:16]}…. A decision under a "
            "superseded activation is not a confirmatory observation."
        )
    if arm.get("code_commit") != code_commit:
        raise DecisionRefusedError(
            f"COMMIT_DRIFT: the arm bound {str(arm.get('code_commit'))[:12]} and HEAD "
            f"is {code_commit[:12]}"
        )
    for label, record in (("activation", activation), ("arm", arm)):
        if record.get("candidate_id") != CANDIDATE_ID or record.get("protocol_id") != PROTOCOL_ID:
            raise DecisionRefusedError(
                f"the {label} record does not bind this candidate and protocol"
            )
    if arm.get("decision_session") != session:
        raise DecisionRefusedError(
            f"the arm authorises {arm.get('decision_session')}, not {session}"
        )
    if not calendar.authoritative:
        raise DecisionRefusedError(
            "CALENDAR_BINDING_MISMATCH: the calendar source is not authoritative"
        )

    # 3. The time gate. A decision before its instant is a forecast.
    instant = datetime.fromisoformat(package.decision_instant)
    if now < instant:
        raise DecisionRefusedError(
            f"DECISION_INSTANT_NOT_REACHED: {instant.isoformat()} is "
            f"{(instant - now).total_seconds():.0f}s away"
        )

    # 4. The inputs must be the sealed ones, and provably recoverable.
    if package.stage != "COMPLETE":
        raise DecisionRefusedError(
            f"SOURCE_INCOMPLETE: the input package is at stage {package.stage}"
        )
    recovery = package.verify(store)

    # 5. Scoring, ranking and selection. No network is reachable from here on.
    universe = store.get("universe", session, package.envelopes["universe"].content_sha256[:16])[
        "payload"
    ]["listings"]
    observations = store.get(
        "filing_observations",
        session,
        package.envelopes["filing_observations"].content_sha256[:16],
    )["payload"]["observations"]
    bars = store.get("market", session, package.envelopes["market"].content_sha256[:16])["payload"][
        "bars"
    ]
    actions = store.get(
        "corporate_actions",
        session,
        package.envelopes["corporate_actions"].content_sha256[:16],
    )["payload"]["actions"]

    inputs = build_score_inputs(
        identities,
        observation_records=observations,
        bar_records=bars,
        action_records=actions,
        decision_session=instant.date(),
        decision_instant=package.decision_instant,
        source_fingerprints=package.fingerprints,
        bar_volume_basis=bar_volume_basis,
    )
    scores = score_cross_section(
        inputs,
        benchmark_bars=benchmark_bars,
        benchmark_symbol=benchmark_symbol,
        decision_session=instant.date(),
        thresholds=thresholds,
    )
    tradable = {
        (identity.kind, identity.identity_id): "TRADABLE"
        for identity in identities
        if _is_tradable(universe, identity.symbol)
    }
    priced = [
        (identity.kind, identity.identity_id)
        for identity in identities
        if any(
            row["ticker"].upper() == identity.symbol and row["session"] == session for row in bars
        )
    ]
    cross_section = build_cross_section(
        scores,
        tradability=tradable,
        session_priced=priced,
        source_fingerprints={
            **package.fingerprints,
            "score_inputs": inputs.fingerprint(),
            "scores": scores.fingerprint(),
        },
    )

    # 6. The six-fingerprint gate, before the packet exists.
    fingerprints = require_six_fingerprints(
        {
            "universe": package.fingerprints.get("universe"),
            "tradability": package.fingerprints.get("tradability"),
            "market": package.fingerprints.get("market"),
            "sec_availability": package.fingerprints.get("sec_availability"),
            "score_inputs": inputs.fingerprint(),
            "ranked_cross_section": cross_section.fingerprint(),
        },
        decision_session=session,
    )

    # 7. The frozen rule, through its one implementation.
    incumbents = prior_incumbents(directory, before_session=session)
    targets = paired_targets(cross_section.candidates, incumbents)

    body = decision_packet(
        packet_key=session,
        decision_timestamp=package.decision_instant,
        benchmark_session=session,
        execution_session=str(arm["execution_reference_session"]),
        code_commit=code_commit,
        runner_armed_at=str(arm["armed_at"]),
        protocol_version=str(arm.get("arm_version") or PROTOCOL_ID),
        fingerprints={
            **fingerprints,
            "filing_observations": package.fingerprints.get("filing_observations", ""),
            "corporate_actions": package.fingerprints.get("corporate_actions", ""),
            "scores": scores.fingerprint(),
            "input_package": package.fingerprint(),
            "semantic_code": semantic_fingerprint,
            "calendar_sessions": calendar.sessions_sha256,
        },
        targets=dict(targets),
        holdings_before=incumbents,
        forced_exits=list(targets.get("h2_forced_out") or ()),
        notes=[
            f"H2 {H2_ID} through ONE_DECILE_HYSTERESIS_V1, unmodified",
            f"input package {package.fingerprint()[:16]} verified recoverable: "
            f"{recovery['all_recovered']}",
            f"{len(identities)} identities, "
            f"{cross_section.as_json()['executable_count']} executable",
            "no network call occurred after the inputs were sealed",
        ],
    )

    # 8. Durable, exactly once, then the state moves — in that order.
    path = directory / DECISION_FILE.format(session=session)
    sealed = finalise(path, body)
    recovered = json.loads(path.read_text(encoding="utf-8"))
    if packet_digest(recovered) != sealed.sha256:
        raise DecisionRefusedError(f"the written packet does not read back as written: {path}")

    state = load_state(state_path)
    if session not in state.decisions:
        if state.state is ProspectiveState.WAITING_FOR_DECISION:
            state.transition(ProspectiveState.DECISION_CAPTURED)
        state.decisions.append(session)
        save_state(state_path, state)

    (directory / f"score_inputs_{session}.json").write_text(
        json.dumps(inputs.as_json(), indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    (directory / f"scores_{session}.json").write_text(
        json.dumps(scores.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (directory / f"cross_section_{session}.json").write_text(
        json.dumps(cross_section.as_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    log.info(
        "prospective.decision_captured",
        session=session,
        digest=sealed.sha256[:16],
        written=sealed.written,
        targets=len(targets.get("h2_target_set") or ()),
    )
    return DecisionCapture(
        decision_session=session,
        packet_path=path,
        packet_digest=sealed.sha256,
        written=sealed.written,
        fingerprints=fingerprints,
        targets=dict(targets),
        state=str(load_state(state_path).state),
    )


def _is_tradable(listings: Sequence[Mapping[str, Any]], symbol: str) -> bool:
    """Whether the broker's asset list says a symbol can be traded.

    Absence is never read as tradable, which is why this is a lookup rather than
    a default.

    Args:
        listings: The universe snapshot's records.
        symbol: The ticker to check.

    Returns:
        True only when a record exists, is active and is not a fund.
    """
    for row in listings:
        if str(row.get("ticker", "")).upper() == symbol:
            return bool(row.get("is_active")) and not row.get("is_fund")
    return False
