"""What the A3 expansion actually resolved: the post-A3 PIT state against the frozen pre-A3 one.

The planner's expectation — 155,114.479 months — was a forecast made from
catalogue metadata before a byte was downloaded. This module measures the
outcome. It runs the *unchanged* B2.13 assembly over the post-A3 index (the
same identity join, the same reachability, the same 90-day rule) and lays
each universe month's new standing beside the standing the corrected
package froze before acquisition.

## What is attributed, and what is only counted

The assembly names, for every covered month, the newest common-stock
observation on or before the month-end that joined to the identity — the
month's *source*. That is the model's own deterministic rule and is
reported as such. Whether other A3 filings would also have supported the
month is reported separately as *participation*: the set of A3
observations for the identity inside the month's 90-day window. Nothing
here says "filing X solved month Y" beyond what the source rule says.

## What is never changed

`ResearchUniverseMonth` is read, never written. Frozen verdicts stay
`DECIDED`. A month whose listing was never observed stays open whatever
evidence arrived, because the assembly says so. The expected value is
printed beside the observed one and gates nothing.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import TYPE_CHECKING

import structlog

from domain import SecurityClass
from stock_screener.sec_evidence.inputs import (
    A3_INPUTS_MANIFEST,
    CoverageStatus,
    assemble_planning_inputs,
)
from stock_screener.sec_evidence.plan import CARRY_FORWARD_DAYS, GapClass

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence.inputs import CoverageRow, Observation, ResolvedIdentity
    from stock_screener.sec_evidence.manifest import Manifest
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord
    from stock_screener.sec_evidence.targets import ManifestTarget

log = structlog.get_logger(__name__)

__all__ = [
    "PIT_COMPLETE_MARKER",
    "PIT_DIR",
    "PIT_FILES",
    "PIT_MIRROR_PREFIX",
    "PIT_VERSION",
    "MonthOutcome",
    "PitError",
    "PitResult",
    "load_pre_coverage",
    "materialize_pit",
    "mirror_pit_package",
    "write_pit_package",
]

#: Beneath `plans/generation_v2/`.
PIT_DIR = "post_a3_pit"
PIT_MIRROR_PREFIX = "post-a3-derived/pit"
PIT_VERSION = "B2.16/1"
PIT_COMPLETE_MARKER = "pit_final_complete.json"
#: Every file the package holds, in write order. Row files are digested by
#: content; the marker is written last.
PIT_FILES = (
    "pit_month_outcomes.jsonl",
    "pit_target_realization.jsonl",
    "pit_gap_transitions.json",
    "pit_resolution_by_identity.json",
    "pit_resolution_by_year.json",
    "pit_resolution_by_gap.json",
    "pit_evidence_fingerprints.json",
    "pit_final_summary.json",
    PIT_COMPLETE_MARKER,
)
#: The small files the mirror carries.
PIT_MIRRORED = (
    "pit_gap_transitions.json",
    "pit_resolution_by_year.json",
    "pit_resolution_by_gap.json",
    "pit_evidence_fingerprints.json",
    "pit_final_summary.json",
    PIT_COMPLETE_MARKER,
)
#: The planner's forecast, carried for the comparison only.
EXPECTED_SOLVED = 155114.479

_COMMON = str(SecurityClass.COMMON_STOCK)


class PitError(RuntimeError):
    """The pre- and post-A3 states cannot be compared, or a temporal rule broke."""


@dataclass(frozen=True)
class MonthOutcome:
    """One universe month that was open before A3, or whose standing changed.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`.
        identity_id: The security's id in its table.
        cik: The frozen resolution's CIK, if any.
        symbol: The symbol the month recorded.
        month_end: The planner's key date.
        pre_status: `OPEN`, `COVERED` or `DECIDED` before A3.
        pre_gap_class: The gap class before A3, when open.
        post_status: The standing after A3.
        post_gap_class: The gap class after A3, when open.
        transition: `RESOLVED`, `STILL_OPEN`, `REGRESSED` or `UNCHANGED`.
        pre_identity_state: The month-end join before A3.
        post_identity_state: The month-end join after A3.
        source_cik: The filer of the source observation after A3, when covered.
        source_accession: Its accession.
        evidence_date: The source observation's date.
        days_since: Days from it to the month-end.
        source_is_a3: Whether the source is an A3 filing.
        participating_a3: A3 common-stock observations for this identity inside the
            month's window — the set that supports it, not a cause.
    """

    kind: str
    identity_id: int
    cik: int | None
    symbol: str
    month_end: str
    pre_status: str
    pre_gap_class: str | None
    post_status: str
    post_gap_class: str | None
    transition: str
    pre_identity_state: str
    post_identity_state: str
    source_cik: int | None
    source_accession: str | None
    evidence_date: str | None
    days_since: int | None
    source_is_a3: bool
    participating_a3: int


@dataclass
class PitResult:
    """Everything the materialization produced."""

    outcomes: list[MonthOutcome]
    targets: list[dict[str, object]]
    transitions: dict[str, int]
    by_identity: dict[str, object]
    by_year: dict[str, dict[str, int]]
    by_gap: dict[str, dict[str, int]]
    summary: dict[str, object]
    fingerprints: dict[str, dict[str, object]]
    timings: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# The frozen pre-A3 state
# ----------------------------------------------------------------------------


def load_pre_coverage(package: Path) -> tuple[list[dict[str, object]], str]:
    """The corrected package's coverage rows, proven against the digest it recorded.

    Raises:
        PitError: If the file is missing or does not hash as the package manifest says.
    """
    manifest = json.loads((package / A3_INPUTS_MANIFEST).read_text(encoding="utf-8"))
    components = manifest.get("components") or {}
    recorded = components.get("pit_coverage_v2.jsonl") if isinstance(components, dict) else None
    path = package / "pit_coverage_v2.jsonl"
    if not path.is_file():
        raise PitError(f"no pre-A3 coverage at {path}")
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    expected = recorded.get("sha256") if isinstance(recorded, dict) else recorded
    if expected is not None and digest != expected:
        raise PitError(f"pre-A3 coverage hashes to {digest[:16]}…, not {str(expected)[:16]}…")
    rows = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return rows, digest


# ----------------------------------------------------------------------------
# Materialization
# ----------------------------------------------------------------------------


def _transition(pre: str, post: str) -> str:
    if pre == CoverageStatus.OPEN and post == CoverageStatus.COVERED:
        return "RESOLVED"
    if pre == CoverageStatus.OPEN and post == CoverageStatus.OPEN:
        return "STILL_OPEN"
    if pre == CoverageStatus.COVERED and post == CoverageStatus.OPEN:
        return "REGRESSED"
    return "UNCHANGED"


def _a3_observations(
    observations: Iterable[Observation], a3_keys: set[tuple[int, str]]
) -> dict[tuple[str, int], list[tuple[date, int, str]]]:
    """EXACT common-stock observations from A3 filings, per identity, sorted by date."""
    by_identity: dict[tuple[str, int], list[tuple[date, int, str]]] = defaultdict(list)
    for o in observations:
        if (
            o.identity is not None
            and o.security_class == _COMMON
            and (o.cik, o.accession) in a3_keys
        ):
            by_identity[o.identity].append((o.evidence_date, o.cik, o.accession))
    for dates in by_identity.values():
        dates.sort()
    return by_identity


def _canonical_sha(rows: Iterable[Mapping[str, object]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def materialize_pit(
    session: Session,
    index: sqlite3.Connection,
    manifest: Manifest,
    resolution: Sequence[ResolvedIdentity],
    pre_rows: Sequence[Mapping[str, object]],
    plan: Sequence[ManifestTarget],
    *,
    progress: Callable[[str], None] | None = None,
) -> PitResult:
    """Run the frozen assembly over the post-A3 index and measure it against the pre-A3 rows.

    Args:
        session: A read-only session on the frozen PIT database.
        index: The post-A3 derived index, read-only.
        manifest: The final V2 manifest, for the held set.
        resolution: The frozen filer resolution.
        pre_rows: The corrected package's coverage rows.
        plan: The authorized A3 targets.
        progress: Receives one line per stage.

    Returns:
        The outcomes, the target realization, every summary and the fingerprints.

    Raises:
        PitError: If the pre and post universes differ, or a realized
            evidence edge breaks the temporal rule — a defect, not a result.
    """
    say = progress or (lambda line: None)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    assembled = assemble_planning_inputs(session, index, manifest, resolution, progress=progress)
    timings["assembly"] = time.perf_counter() - started
    stage = time.perf_counter()

    pre = {(str(r["kind"]), int(str(r["identity_id"])), str(r["month_end"])): r for r in pre_rows}
    post: dict[tuple[str, int, str], CoverageRow] = {
        (row.kind, row.identity_id, row.month_end): row for row in assembled.coverage
    }
    if set(pre) != set(post):
        raise PitError(
            f"the pre-A3 and post-A3 universes differ: {len(pre)} vs {len(post)} months, "
            f"{len(set(pre) ^ set(post))} not in both"
        )
    a3_keys = {(t.cik, t.accession) for t in plan}
    a3_by_identity = _a3_observations(assembled.observations, a3_keys)

    outcomes: list[MonthOutcome] = []
    transitions: Counter[str] = Counter()
    class_transitions: Counter[str] = Counter()
    violations = {"future_invalid": 0, "source_not_exact": 0, "source_missing": 0}
    edges_checked = 0
    source_count: Counter[tuple[int, str]] = Counter()
    participation_count: Counter[tuple[int, str]] = Counter()
    for key in sorted(pre):
        before, after = pre[key], post[key]
        pre_status, post_status = str(before["status"]), after.status
        transition = _transition(pre_status, post_status)
        if pre_status == CoverageStatus.DECIDED and post_status == CoverageStatus.DECIDED:
            continue
        if pre_status == CoverageStatus.COVERED and post_status == CoverageStatus.COVERED:
            transitions["UNCHANGED_COVERED"] += 1
            continue
        end = date.fromisoformat(after.month_end)
        window_start = end - timedelta(days=CARRY_FORWARD_DAYS)
        identity = (after.kind, after.identity_id)
        participating = [
            (observed, cik, accession)
            for observed, cik, accession in a3_by_identity.get(identity, ())
            if window_start <= observed <= end
        ]
        source = (
            (after.source_cik, after.source_accession)
            if after.source_cik is not None and after.source_accession is not None
            else None
        )
        source_is_a3 = source is not None and source in a3_keys
        if transition == "RESOLVED":
            edges_checked += 1
            if after.days_since is None or not 0 <= after.days_since <= CARRY_FORWARD_DAYS:
                violations["future_invalid"] += 1
            if source is None:
                violations["source_missing"] += 1
            elif source_is_a3:
                observed = date.fromisoformat(str(after.previous_observation))
                if (observed, source[0], source[1]) not in a3_by_identity.get(identity, ()):
                    violations["source_not_exact"] += 1
                source_count[source] += 1
            for _observed, cik, accession in participating:
                participation_count[(cik, accession)] += 1
        pre_class = before.get("gap_class")
        transitions[transition] += 1
        if pre_status == CoverageStatus.OPEN:
            class_transitions[
                f"{pre_class} -> {'RESOLVED' if transition == 'RESOLVED' else after.gap_class}"
            ] += 1
        outcomes.append(
            MonthOutcome(
                kind=after.kind,
                identity_id=after.identity_id,
                cik=after.cik,
                symbol=after.symbol,
                month_end=after.month_end,
                pre_status=pre_status,
                pre_gap_class=str(pre_class) if pre_class is not None else None,
                post_status=post_status,
                post_gap_class=after.gap_class,
                transition=transition,
                pre_identity_state=str(before.get("identity_state")),
                post_identity_state=after.identity_state,
                source_cik=after.source_cik if post_status == CoverageStatus.COVERED else None,
                source_accession=(
                    after.source_accession if post_status == CoverageStatus.COVERED else None
                ),
                evidence_date=(
                    after.previous_observation if post_status == CoverageStatus.COVERED else None
                ),
                days_since=after.days_since if post_status == CoverageStatus.COVERED else None,
                source_is_a3=source_is_a3 and post_status == CoverageStatus.COVERED,
                participating_a3=len(participating),
            )
        )
    timings["comparison"] = time.perf_counter() - stage
    if any(violations.values()):
        raise PitError(f"realized evidence edges break the temporal or identity rule: {violations}")

    # --- per target: what it emitted, and what it supports --------------------
    stage = time.perf_counter()
    cover_rows: dict[tuple[int, str], tuple[int, int, int]] = {}
    for cik, accession, rows, symbols, common in index.execute(
        "SELECT s.cik, s.accession, COUNT(*), SUM(s.raw_symbol IS NOT NULL),"
        " SUM(s.security_class = ?) FROM securities s"
        " JOIN post_a3_delta d ON d.url = s.url GROUP BY s.cik, s.accession",
        (_COMMON,),
    ):
        cover_rows[(int(cik), str(accession))] = (int(rows), int(symbols or 0), int(common or 0))
    targets: list[dict[str, object]] = []
    for target in sorted(plan, key=lambda t: t.key):
        rows, symbols, common = cover_rows.get(target.key, (0, 0, 0))
        targets.append(
            {
                "cik": target.cik,
                "accession": target.accession,
                "form": target.form,
                "filing_date": target.filing_date,
                "predicted_potential_months": target.potential_months,
                "predicted_yield_adjusted_months": target.yield_adjusted_months,
                "cover_rows": rows,
                "cover_rows_with_symbol": symbols,
                "common_stock_rows": common,
                "months_as_source": source_count.get(target.key, 0),
                "months_participating": participation_count.get(target.key, 0),
            }
        )
    timings["targets"] = time.perf_counter() - stage

    # --- summaries -------------------------------------------------------------
    stage = time.perf_counter()
    pre_open = [o for o in outcomes if o.pre_status == CoverageStatus.OPEN]
    resolved = [o for o in pre_open if o.transition == "RESOLVED"]
    still_open = [o for o in pre_open if o.transition == "STILL_OPEN"]
    regressed = [o for o in outcomes if o.transition == "REGRESSED"]
    solvable = str(GapClass.SOLVABLE)
    predicted_solvable = [o for o in pre_open if o.pre_gap_class == solvable]
    predicted_unsolvable = [o for o in pre_open if o.pre_gap_class != solvable]

    by_gap: dict[str, dict[str, int]] = {}
    for o in pre_open:
        bucket = by_gap.setdefault(
            str(o.pre_gap_class), {"open_before": 0, "resolved": 0, "still_open": 0}
        )
        bucket["open_before"] += 1
        bucket["resolved" if o.transition == "RESOLVED" else "still_open"] += 1
    by_year: dict[str, dict[str, int]] = {}
    for o in pre_open:
        bucket = by_year.setdefault(
            o.month_end[:4], {"open_before": 0, "resolved": 0, "still_open": 0}
        )
        bucket["open_before"] += 1
        bucket["resolved" if o.transition == "RESOLVED" else "still_open"] += 1

    per_identity: dict[tuple[str, int], dict[str, object]] = {}
    for o in pre_open:
        row = per_identity.setdefault(
            (o.kind, o.identity_id),
            {
                "kind": o.kind,
                "identity_id": o.identity_id,
                "cik": o.cik,
                "symbol": o.symbol,
                "open_before": 0,
                "resolved": 0,
                "still_open": 0,
                "no_cik": o.cik is None,
            },
        )
        row["open_before"] = int(str(row["open_before"])) + 1
        name = "resolved" if o.transition == "RESOLVED" else "still_open"
        row[name] = int(str(row[name])) + 1
    identity_rows = [per_identity[k] for k in sorted(per_identity)]
    for row in identity_rows:
        solved, remaining = int(str(row["resolved"])), int(str(row["still_open"]))
        row["outcome"] = (
            "FULLY_RESOLVED"
            if remaining == 0 and solved > 0
            else "PARTIALLY_RESOLVED"
            if solved > 0
            else "NO_IMPROVEMENT"
        )
    distribution: Counter[str] = Counter()
    for row in identity_rows:
        solved = int(str(row["resolved"]))
        distribution[
            "0"
            if solved == 0
            else "1-3"
            if solved <= 3
            else "4-6"
            if solved <= 6
            else "7-12"
            if solved <= 12
            else "13+"
        ] += 1
    by_identity: dict[str, object] = {
        "identities_with_open_months": len(identity_rows),
        "improved": sum(1 for r in identity_rows if r["outcome"] != "NO_IMPROVEMENT"),
        "fully_resolved": sum(1 for r in identity_rows if r["outcome"] == "FULLY_RESOLVED"),
        "partially_resolved": sum(1 for r in identity_rows if r["outcome"] == "PARTIALLY_RESOLVED"),
        "no_improvement": sum(1 for r in identity_rows if r["outcome"] == "NO_IMPROVEMENT"),
        "no_cik": sum(1 for r in identity_rows if r["no_cik"]),
        "no_cik_still_open_months": sum(
            int(str(r["still_open"])) for r in identity_rows if r["no_cik"]
        ),
        "no_cik_resolved_months": sum(
            int(str(r["resolved"])) for r in identity_rows if r["no_cik"]
        ),
        "distribution_of_months_resolved": dict(sorted(distribution.items())),
        "rows": identity_rows,
    }

    forms_parsed: Counter[str] = Counter(str(t["form"]) for t in targets)
    forms_with_cover: Counter[str] = Counter(
        str(t["form"]) for t in targets if int(str(t["common_stock_rows"])) > 0
    )
    forms_participating: Counter[str] = Counter(
        str(t["form"]) for t in targets if int(str(t["months_participating"])) > 0
    )
    forms_as_source: Counter[str] = Counter(
        str(t["form"]) for t in targets if int(str(t["months_as_source"])) > 0
    )
    actual_solved = len(resolved)
    summary: dict[str, object] = {
        "pit_version": PIT_VERSION,
        "universe_months": len(pre),
        "pre_open": len(pre_open),
        "pre_covered": sum(1 for r in pre_rows if r["status"] == CoverageStatus.COVERED),
        "pre_decided": sum(1 for r in pre_rows if r["status"] == CoverageStatus.DECIDED),
        "post_open": sum(1 for r in post.values() if r.status == CoverageStatus.OPEN),
        "post_covered": sum(1 for r in post.values() if r.status == CoverageStatus.COVERED),
        "newly_resolved": actual_solved,
        "still_open": len(still_open),
        "regressed": len(regressed),
        "resolution_rate": round(actual_solved / len(pre_open), 6) if pre_open else 0.0,
        "predicted_solvable": len(predicted_solvable),
        "predicted_solvable_resolved": sum(
            1 for o in predicted_solvable if o.transition == "RESOLVED"
        ),
        "predicted_solvable_still_open": sum(
            1 for o in predicted_solvable if o.transition == "STILL_OPEN"
        ),
        "predicted_unsolvable": len(predicted_unsolvable),
        "predicted_unsolvable_resolved": sum(
            1 for o in predicted_unsolvable if o.transition == "RESOLVED"
        ),
        "predicted_unsolvable_still_open": sum(
            1 for o in predicted_unsolvable if o.transition == "STILL_OPEN"
        ),
        "expected_solved": EXPECTED_SOLVED,
        "actual_minus_expected": round(actual_solved - EXPECTED_SOLVED, 3),
        "actual_over_expected": round(actual_solved / EXPECTED_SOLVED, 6),
        "actual_over_predicted_solvable": (
            round(actual_solved / len(predicted_solvable), 6) if predicted_solvable else 0.0
        ),
        "resolved_with_a3_source": sum(1 for o in resolved if o.source_is_a3),
        "resolved_with_baseline_source": sum(1 for o in resolved if not o.source_is_a3),
        "resolved_participation_histogram": dict(
            sorted(Counter(str(min(o.participating_a3, 5)) for o in resolved).items())
        ),
        "identity_state_transitions": dict(
            sorted(
                Counter(
                    f"{o.pre_identity_state} -> {o.post_identity_state}"
                    for o in outcomes
                    if o.pre_identity_state != o.post_identity_state
                ).items()
            )
        ),
        "post_gap_classes_of_still_open": dict(
            sorted(Counter(str(o.post_gap_class) for o in still_open).items())
        ),
        "realized_edges_checked": edges_checked,
        "future_invalid_edges": violations["future_invalid"],
        "source_identity_violations": violations["source_not_exact"] + violations["source_missing"],
        "a3_targets": len(targets),
        "a3_forms_parsed": dict(sorted(forms_parsed.items())),
        "a3_with_common_stock_cover": sum(
            1 for t in targets if int(str(t["common_stock_rows"])) > 0
        ),
        "a3_with_any_cover_rows": sum(1 for t in targets if int(str(t["cover_rows"])) > 0),
        "a3_without_cover_rows": sum(1 for t in targets if int(str(t["cover_rows"])) == 0),
        "a3_participating": sum(1 for t in targets if int(str(t["months_participating"])) > 0),
        "a3_as_source": sum(1 for t in targets if int(str(t["months_as_source"])) > 0),
        "a3_with_cover_but_no_benefit": sum(
            1
            for t in targets
            if int(str(t["common_stock_rows"])) > 0 and int(str(t["months_participating"])) == 0
        ),
        "a3_forms_with_common_stock_cover": dict(sorted(forms_with_cover.items())),
        "a3_forms_participating": dict(sorted(forms_participating.items())),
        "a3_forms_as_source": dict(sorted(forms_as_source.items())),
        "predicted_potential_months_sum": sum(
            int(str(t["predicted_potential_months"])) for t in targets
        ),
        "assembly_summary": {
            k: v for k, v in assembled.summary.items() if not isinstance(v, (list, dict))
        },
    }
    timings["summaries"] = time.perf_counter() - stage

    outcome_rows = [asdict(o) for o in outcomes]
    transition_rows = [{"transition": k, "count": v} for k, v in sorted(class_transitions.items())]
    prints = {
        "month_outcomes": dict(zip(("rows", "sha256"), _canonical_sha(outcome_rows), strict=True)),
        "gap_transitions": dict(
            zip(("rows", "sha256"), _canonical_sha(transition_rows), strict=True)
        ),
        "identity_summary": dict(
            zip(("rows", "sha256"), _canonical_sha(identity_rows), strict=True)
        ),
        "target_realization": dict(zip(("rows", "sha256"), _canonical_sha(targets), strict=True)),
        "summary": dict(zip(("rows", "sha256"), _canonical_sha([summary]), strict=True)),
    }
    say(
        f"pit: {len(pre_open)} previously open months; {actual_solved} newly resolved, "
        f"{len(still_open)} still open, {len(regressed)} regressed; "
        f"expected {EXPECTED_SOLVED}"
    )
    return PitResult(
        outcomes=outcomes,
        targets=targets,
        transitions={
            **dict(sorted(transitions.items())),
            **dict(sorted(class_transitions.items())),
        },
        by_identity=by_identity,
        by_year=dict(sorted(by_year.items())),
        by_gap=dict(sorted(by_gap.items())),
        summary=summary,
        fingerprints=prints,
        timings=timings,
    )


# ----------------------------------------------------------------------------
# The package
# ----------------------------------------------------------------------------


def _write_lines(path: Path, rows: Iterable[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    with path.open("wb") as handle:
        for row in rows:
            line = json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
            handle.write(line)
            digest.update(line)
    return digest.hexdigest()


def _write_json(path: Path, payload: Mapping[str, object]) -> str:
    data = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return hashlib.sha256(data).hexdigest()


def write_pit_package(
    directory: Path, result: PitResult, *, bindings: Mapping[str, object]
) -> dict[str, str]:
    """Write the package: rows, summaries, fingerprints and, last, the marker.

    Args:
        directory: Where the package goes; created if needed.
        result: The materialization.
        bindings: The digests the package is bound to — index, manifests,
            plan, package — recorded in the summary, fingerprints and marker.

    Returns:
        SHA-256 per file, by name.
    """
    directory.mkdir(parents=True, exist_ok=True)
    digests: dict[str, str] = {}
    digests["pit_month_outcomes.jsonl"] = _write_lines(
        directory / "pit_month_outcomes.jsonl", (asdict(o) for o in result.outcomes)
    )
    digests["pit_target_realization.jsonl"] = _write_lines(
        directory / "pit_target_realization.jsonl", result.targets
    )
    digests["pit_gap_transitions.json"] = _write_json(
        directory / "pit_gap_transitions.json",
        {**dict(bindings), "transitions": result.transitions},
    )
    digests["pit_resolution_by_identity.json"] = _write_json(
        directory / "pit_resolution_by_identity.json", {**dict(bindings), **result.by_identity}
    )
    digests["pit_resolution_by_year.json"] = _write_json(
        directory / "pit_resolution_by_year.json", {**dict(bindings), "years": result.by_year}
    )
    digests["pit_resolution_by_gap.json"] = _write_json(
        directory / "pit_resolution_by_gap.json", {**dict(bindings), "gap_classes": result.by_gap}
    )
    digests["pit_evidence_fingerprints.json"] = _write_json(
        directory / "pit_evidence_fingerprints.json",
        {**dict(bindings), "pit_version": PIT_VERSION, "fingerprints": result.fingerprints},
    )
    digests["pit_final_summary.json"] = _write_json(
        directory / "pit_final_summary.json",
        {**dict(bindings), **result.summary, "timings": result.timings},
    )
    digests[PIT_COMPLETE_MARKER] = _write_json(
        directory / PIT_COMPLETE_MARKER,
        {
            "stage": "PIT_FINAL_COMPLETE",
            **dict(bindings),
            "pit_version": PIT_VERSION,
            "pre_open": result.summary["pre_open"],
            "newly_resolved": result.summary["newly_resolved"],
            "still_open": result.summary["still_open"],
            "regressed": result.summary["regressed"],
            "expected_solved": EXPECTED_SOLVED,
            "future_invalid_edges": result.summary["future_invalid_edges"],
            "fingerprints": result.fingerprints,
            "files": {name: sha for name, sha in digests.items() if name != PIT_COMPLETE_MARKER},
        },
    )
    for name, sha in digests.items():
        (directory / f"{name.rsplit('.', 1)[0]}.sha256").write_text(f"{sha}  {name}\n")
    log.info("pit package written", directory=str(directory), files=len(digests))
    return digests


def mirror_pit_package(
    mirror: ControlMirror, directory: Path, *, generation_id: str
) -> list[MirrorRecord]:
    """Mirror the small package files as snapshots under `post-a3-derived/pit/`, and read back."""
    records = []
    for name in PIT_MIRRORED:
        local = directory / name
        if not local.is_file():
            raise PitError(f"{local} does not exist; nothing to mirror")
        kind = f"{PIT_MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise PitError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records
