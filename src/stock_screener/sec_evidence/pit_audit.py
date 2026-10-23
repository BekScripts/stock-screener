"""What is left after A3, and whether more SEC evidence could change it.

B2.16 measured the outcome: 155,620 of the 242,269 previously open months
resolved, 86,649 not. This module asks the next question — what the 86,649
*are* — without touching the model that produced them. It re-proves the
materialization from its own fingerprints, runs the unchanged B2.13
assembly over the post-A3 index once more (for the reasons, the reach and
the post-A3 candidates), and classifies every residual month by what
blocks it, using only rules the assembly already states:

- the identity has no resolved CIK (`G2`) — identity work, not filings;
- the month's symbol does not attach to the identity, or the issuer's
  filings cannot be attributed to it (`G3`) — identity work;
- the listing record never observed the month (`G5`) — the model refuses
  to admit it whatever evidence arrives;
- the issuer's filings attribute to no class (`G5`) — class continuity;
- no unheld qualifying filing reaches the month (`G4`): either qualifying
  filings *are* held within reach and carry no usable cover, or none exist;
- an unheld qualifying filing does reach it (`G1`) — the only class more
  SEC evidence can address, and the only one counted as such.

Opportunity is then measured, never taken: the post-A3 candidates and the
unchanged planner give a forensic residual plan whose status is
`FORENSIC_ONLY` / `NOT_AUTHORIZED_FOR_ACQUISITION`, written for the
decision and for nothing else. The A3 marginal curve is a prefix *union*
of the months each target's evidence supports, in plan order — no month is
credited to one document.
"""

from __future__ import annotations

import bisect
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
    DECLINED_MAP_REASON,
    CoverageStatus,
    assemble_planning_inputs,
)
from stock_screener.sec_evidence.pit_final import PIT_COMPLETE_MARKER
from stock_screener.sec_evidence.plan import (
    CARRY_FORWARD_DAYS,
    GapClass,
    plan_coverage_first,
)

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence.inputs import CoverageRow, ResolvedIdentity
    from stock_screener.sec_evidence.manifest import Manifest
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord
    from stock_screener.sec_evidence.plan import Candidate
    from stock_screener.sec_evidence.targets import ManifestTarget

log = structlog.get_logger(__name__)

__all__ = [
    "AUDIT_COMPLETE_MARKER",
    "AUDIT_DIR",
    "AUDIT_MIRROR_PREFIX",
    "AUDIT_VERSION",
    "FORENSIC_PLAN_FILE",
    "FORENSIC_STATUS",
    "AuditError",
    "AuditResult",
    "audit_pit",
    "load_outcomes",
    "mirror_audit",
    "write_audit",
]

AUDIT_DIR = "pit_audit"
AUDIT_MIRROR_PREFIX = "pit-audit"
AUDIT_VERSION = "B2.17/1"
AUDIT_COMPLETE_MARKER = "pit_audit_complete.json"
FORENSIC_PLAN_FILE = "residual_sec_forensic_plan.jsonl"
FORENSIC_STATUS = ("FORENSIC_ONLY", "NOT_AUTHORIZED_FOR_ACQUISITION")
#: The small files the mirror carries; the row files are rebuildable.
AUDIT_MIRRORED = (
    "pit_residual_summary.json",
    "pit_residual_reasons.json",
    "pit_residual_by_year.json",
    "pit_residual_concentration.json",
    "pit_a3_marginal_curve.json",
    "pit_residual_sec_opportunity.json",
    "pit_residual_decision.json",
    AUDIT_COMPLETE_MARKER,
)
#: Prefix milestones of the plan, as fractions.
PREFIXES = (0.10, 0.25, 0.50, 0.75, 0.90, 1.0)
#: Forms filed by foreign private issuers.
FOREIGN_FORMS = frozenset({"20-F", "20-F/A", "40-F", "40-F/A"})

_COMMON = str(SecurityClass.COMMON_STOCK)
_SOLVABLE = str(GapClass.SOLVABLE)

#: Residual audit classes and the sufficiency category each implies.
SUFFICIENCY = {
    "R2_NO_HISTORICAL_CIK": "IDENTITY_DATA_REQUIRED",
    "R3_NO_ELIGIBLE_SEC_FILINGS": "NON_SEC_EVIDENCE_REQUIRED",
    "R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE": "SEC_MORE_UNLIKELY_TO_HELP",
    "R5_SECURITY_CLASS_AMBIGUITY": "IDENTITY_DATA_REQUIRED",
    "R6_EXCHANGE_EVIDENCE_MISSING": "STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL",
    "R7_TICKER_OR_IDENTITY_AMBIGUITY": "IDENTITY_DATA_REQUIRED",
    "R9_POTENTIALLY_RESOLVABLE_WITH_EXISTING_UNHELD_SEC_FILINGS": "SEC_MORE_MAY_HELP",
    "R11_OTHER": "UNKNOWN",
}
ALL_CLASSES = (
    "R1_NO_ISSUER_IDENTITY",
    "R2_NO_HISTORICAL_CIK",
    "R3_NO_ELIGIBLE_SEC_FILINGS",
    "R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE",
    "R5_SECURITY_CLASS_AMBIGUITY",
    "R6_EXCHANGE_EVIDENCE_MISSING",
    "R7_TICKER_OR_IDENTITY_AMBIGUITY",
    "R8_FOREIGN_OR_SPECIAL_ISSUER_LIMITATION",
    "R9_POTENTIALLY_RESOLVABLE_WITH_EXISTING_UNHELD_SEC_FILINGS",
    "R10_REQUIRES_DIFFERENT_EVIDENCE_SOURCE",
    "R11_OTHER",
)
ALL_SUFFICIENCY = (
    "SEC_MORE_MAY_HELP",
    "SEC_MORE_UNLIKELY_TO_HELP",
    "IDENTITY_DATA_REQUIRED",
    "NON_SEC_EVIDENCE_REQUIRED",
    "STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL",
    "UNKNOWN",
)


class AuditError(RuntimeError):
    """The audit's inputs do not reproduce, or a rule the audit relies on broke."""


@dataclass
class AuditResult:
    """Everything the audit produced, in the order the package writes it."""

    reproduction: dict[str, object]
    planning: dict[str, object]
    transitions: dict[str, dict[str, int]]
    residual_reasons: dict[str, int]
    residual_rows: list[dict[str, object]]
    classes: dict[str, int]
    sufficiency: dict[str, int]
    no_cik: dict[str, object]
    exhausted: dict[str, object]
    utility: dict[str, object]
    curve: dict[str, object]
    opportunity: dict[str, object]
    forensic_rows: list[dict[str, object]]
    by_year: dict[str, dict[str, object]]
    identities: dict[str, object]
    concentration: dict[str, object]
    decision: dict[str, object]
    fingerprints: dict[str, dict[str, object]]
    timings: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Re-proving the materialization
# ----------------------------------------------------------------------------


def _sha_lines(rows: Iterable[Mapping[str, object]]) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in rows:
        digest.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def load_outcomes(pit_dir: Path) -> tuple[list[dict[str, object]], list[dict[str, object]], str]:
    """The B2.16 month outcomes and target realization, proven against the package's fingerprints.

    Raises:
        AuditError: If a file is missing or does not reproduce the recorded digest.
    """
    marker_path = pit_dir / PIT_COMPLETE_MARKER
    if not marker_path.is_file():
        raise AuditError(f"no {PIT_COMPLETE_MARKER} at {pit_dir}")
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    recorded = marker.get("fingerprints") or {}
    outcomes = [
        json.loads(line)
        for line in (pit_dir / "pit_month_outcomes.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    targets = [
        json.loads(line)
        for line in (pit_dir / "pit_target_realization.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    for name, rows in (("month_outcomes", outcomes), ("target_realization", targets)):
        count, sha = _sha_lines(rows)
        want = recorded.get(name) or {}
        if count != want.get("rows") or sha != want.get("sha256"):
            raise AuditError(f"{name} does not reproduce the fingerprint the PIT marker recorded")
    marker_sha = hashlib.sha256(marker_path.read_bytes()).hexdigest()
    return outcomes, targets, marker_sha


def _reproduction(outcomes: Sequence[Mapping[str, object]]) -> dict[str, object]:
    keys = [(o["kind"], o["identity_id"], o["month_end"]) for o in outcomes]
    by_transition = Counter(str(o["transition"]) for o in outcomes)
    future_invalid = sum(
        1
        for o in outcomes
        if o["transition"] == "RESOLVED"
        and (o["days_since"] is None or not 0 <= int(str(o["days_since"])) <= CARRY_FORWARD_DAYS)
    )
    pre_open = sum(1 for o in outcomes if o["pre_status"] == CoverageStatus.OPEN)
    return {
        "rows": len(outcomes),
        "duplicate_identity_months": len(keys) - len(set(keys)),
        "pre_open": pre_open,
        "newly_resolved": by_transition.get("RESOLVED", 0),
        "still_open": by_transition.get("STILL_OPEN", 0),
        "regressed": by_transition.get("REGRESSED", 0),
        "unknown_terminal_state": len(outcomes)
        - sum(by_transition.get(t, 0) for t in ("RESOLVED", "STILL_OPEN", "REGRESSED")),
        "future_invalid": future_invalid,
        "reconciles": pre_open
        == by_transition.get("RESOLVED", 0) + by_transition.get("STILL_OPEN", 0),
    }


def _planning(outcomes: Sequence[Mapping[str, object]], expected: float) -> dict[str, object]:
    cells: Counter[str] = Counter()
    for o in outcomes:
        if o["pre_status"] != CoverageStatus.OPEN:
            continue
        predicted = "solvable" if o["pre_gap_class"] == _SOLVABLE else "unsolvable"
        cells[f"{predicted}->{'resolved' if o['transition'] == 'RESOLVED' else 'unresolved'}"] += 1
    solvable = cells["solvable->resolved"] + cells["solvable->unresolved"]
    unsolvable = cells["unsolvable->resolved"] + cells["unsolvable->unresolved"]
    actual = cells["solvable->resolved"] + cells["unsolvable->resolved"]
    return {
        "predicted_solvable": solvable,
        "predicted_unsolvable": unsolvable,
        **{
            name: cells[name]
            for name in (
                "solvable->resolved",
                "solvable->unresolved",
                "unsolvable->resolved",
                "unsolvable->unresolved",
            )
        },
        "total": sum(cells.values()),
        "solvable_realization_rate": round(cells["solvable->resolved"] / solvable, 6),
        "surprise_resolution_rate": round(cells["unsolvable->resolved"] / unsolvable, 6),
        "expected_solved": expected,
        "actual_solved": actual,
        "actual_minus_expected": round(actual - expected, 3),
        "relative_error": round((actual - expected) / expected, 6),
    }


def _transition_matrix(outcomes: Sequence[Mapping[str, object]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, Counter[str]] = defaultdict(Counter)
    for o in outcomes:
        if o["pre_status"] != CoverageStatus.OPEN:
            continue
        post = "RESOLVED" if o["transition"] == "RESOLVED" else str(o["post_gap_class"])
        matrix[str(o["pre_gap_class"])][post] += 1
    return {pre: dict(sorted(cols.items())) for pre, cols in sorted(matrix.items())}


# ----------------------------------------------------------------------------
# Residual classification
# ----------------------------------------------------------------------------


@dataclass(frozen=True)
class _Held:
    """A held filing's evidence, for the exhausted-filings question."""

    evidence_date: date
    accession: str
    form: str
    common_symbols: frozenset[str]
    any_cover: bool


def _held_by_cik(index: sqlite3.Connection) -> dict[int, list[_Held]]:
    """Every held filing per issuer, with what its cover said, sorted by evidence date."""
    covers: dict[str, tuple[set[str], int]] = {}
    for url, symbol, klass in index.execute(
        "SELECT url, normalized_symbol, security_class FROM securities"
    ):
        symbols, rows = covers.setdefault(str(url), (set(), 0))
        covers[str(url)] = (symbols, rows + 1)
        if klass == _COMMON and symbol:
            symbols.add(str(symbol))
    held: dict[int, list[_Held]] = defaultdict(list)
    for url, cik, accession, form, evidence in index.execute(
        "SELECT url, cik, accession, form, evidence_date FROM filings"
        " WHERE cik IS NOT NULL AND evidence_date IS NOT NULL"
    ):
        symbols, rows = covers.get(str(url), (set(), 0))
        held[int(cik)].append(
            _Held(
                date.fromisoformat(str(evidence)),
                str(accession),
                str(form),
                frozenset(symbols),
                rows > 0,
            )
        )
    for filings in held.values():
        filings.sort(key=lambda h: (h.evidence_date, h.accession))
    return held


def _foreign_issuers(index: sqlite3.Connection) -> set[int]:
    """Issuers whose window catalogue is foreign forms only (20-F/40-F families)."""
    forms: dict[int, Counter[str]] = defaultdict(Counter)
    for cik, form in index.execute("SELECT cik, form FROM catalogue WHERE cik IS NOT NULL"):
        forms[int(cik)][str(form)] += 1
    return {
        cik for cik, counts in forms.items() if counts and all(f in FOREIGN_FORMS for f in counts)
    }


@dataclass(frozen=True)
class _Residual:
    """One residual month's classification, typed for the tallies."""

    audit_class: str
    sufficiency: str
    flags: list[str]
    held_in_reach: int


def _classify_residual(
    outcome: Mapping[str, object],
    post: CoverageRow,
    held: Sequence[_Held],
    declined: set[tuple[str, int]],
    foreign: set[int],
) -> tuple[_Residual, dict[str, object]]:
    """One residual month's primary audit class, sufficiency and diagnostic flags."""
    end = date.fromisoformat(str(outcome["month_end"]))
    start = end - timedelta(days=CARRY_FORWARD_DAYS)
    lo = bisect.bisect_left(held, start, key=lambda h: h.evidence_date)
    hi = bisect.bisect_right(held, end, key=lambda h: h.evidence_date)
    in_reach = held[lo:hi]
    symbol = str(post.symbol)
    flags: list[str] = []
    klass = str(post.gap_class)
    reason = post.reason
    if klass == _SOLVABLE:
        primary = "R9_POTENTIALLY_RESOLVABLE_WITH_EXISTING_UNHELD_SEC_FILINGS"
    elif klass == str(GapClass.NO_CIK):
        primary = "R2_NO_HISTORICAL_CIK"
        if post.identity in declined:
            flags.append("DECLINED_CURRENT_MAP_MATCH")
    elif klass == str(GapClass.AMBIGUOUS_IDENTITY):
        primary = "R7_TICKER_OR_IDENTITY_AMBIGUITY"
        if "not credited" in reason:
            primary = "R5_SECURITY_CLASS_AMBIGUITY"
            flags.append("IDENTITY_NOT_CREDITED_BY_ISSUER_FILINGS")
        elif "IDENTITY_AMBIGUOUS" in reason:
            flags.append("ISSUER_REACH_IDENTITY_AMBIGUOUS")
        else:
            flags.append(f"MONTH_JOIN_{post.identity_state}")
    elif klass == str(GapClass.OTHER):
        if "no listing observation" in reason:
            primary = "R6_EXCHANGE_EVIDENCE_MISSING"
            flags.append("LISTING_NOT_OBSERVED_THIS_MONTH")
        elif "MULTI_CLASS_UNASSIGNED" in reason:
            primary = "R5_SECURITY_CLASS_AMBIGUITY"
            flags.append("ISSUER_REACH_MULTI_CLASS_UNASSIGNED")
        else:
            primary = "R11_OTHER"
    elif klass == str(GapClass.NO_CANDIDATE):
        if not in_reach:
            primary = "R3_NO_ELIGIBLE_SEC_FILINGS"
        elif any(symbol in h.common_symbols for h in in_reach):
            # A held filing in reach names this very symbol as common stock and
            # the assembly still did not join it: the join, not the document.
            primary = "R7_TICKER_OR_IDENTITY_AMBIGUITY"
            flags.append("HELD_COVER_NAMES_SYMBOL_BUT_DID_NOT_JOIN")
        elif any(h.common_symbols for h in in_reach):
            primary = "R5_SECURITY_CLASS_AMBIGUITY"
            flags.append("HELD_COVER_NAMES_OTHER_SYMBOLS")
        else:
            primary = "R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE"
            flags.append(
                "HELD_COVER_ROWS_WITHOUT_COMMON_SYMBOL"
                if any(h.any_cover for h in in_reach)
                else "HELD_FILINGS_WITHOUT_COVER_ROWS"
            )
    else:
        primary = "R11_OTHER"
    if post.cik is not None and post.cik in foreign:
        flags.append("FOREIGN_ISSUER")
    return _Residual(primary, SUFFICIENCY[primary], sorted(flags), len(in_reach)), {
        "kind": post.kind,
        "identity_id": post.identity_id,
        "cik": post.cik,
        "symbol": symbol,
        "month_end": outcome["month_end"],
        "pre_gap_class": outcome["pre_gap_class"],
        "post_gap_class": klass,
        "reach": post.reach,
        "identity_state": post.identity_state,
        "listing_observed": post.listing_observed,
        "audit_class": primary,
        "sufficiency": SUFFICIENCY[primary],
        "held_in_reach": len(in_reach),
        "held_in_reach_with_cover": sum(1 for h in in_reach if h.any_cover),
        "held_in_reach_forms": sorted({h.form for h in in_reach}),
        "participating_a3": outcome.get("participating_a3", 0),
        "flags": sorted(flags),
    }


# ----------------------------------------------------------------------------
# The audit
# ----------------------------------------------------------------------------


@dataclass
class _IdentityStat:
    """One identity's months before and after A3, typed for the tallies."""

    kind: str
    identity_id: int
    cik: int | None
    symbol: str
    bucket: str
    foreign: bool
    open_before: int = 0
    resolved: int = 0
    still_open: int = 0
    residual_sufficiency: Counter[str] = field(default_factory=Counter)

    @property
    def outcome(self) -> str:
        """`FULLY_RESOLVED`, `PARTIALLY_RESOLVED` or `NO_IMPROVEMENT`."""
        if self.still_open == 0:
            return "FULLY_RESOLVED"
        return "PARTIALLY_RESOLVED" if self.resolved > 0 else "NO_IMPROVEMENT"

    def row(self) -> dict[str, object]:
        """The identity as a package row."""
        return {
            "kind": self.kind,
            "identity_id": self.identity_id,
            "cik": self.cik,
            "symbol": self.symbol,
            "bucket": self.bucket,
            "foreign": self.foreign,
            "open_before": self.open_before,
            "resolved": self.resolved,
            "still_open": self.still_open,
            "outcome": self.outcome,
            "residual_sufficiency": dict(sorted(self.residual_sufficiency.items())),
        }


def _bucket(value: int) -> str:
    return (
        "1"
        if value == 1
        else "2-3"
        if value <= 3
        else "4-6"
        if value <= 6
        else "7-12"
        if value <= 12
        else "13+"
    )


def audit_pit(
    session: Session,
    index: sqlite3.Connection,
    manifest: Manifest,
    resolution: Sequence[ResolvedIdentity],
    pit_dir: Path,
    plan: Sequence[ManifestTarget],
    plan_bytes: Mapping[tuple[int, str], int],
    *,
    expected_solved: float,
    progress: Callable[[str], None] | None = None,
) -> AuditResult:
    """Audit the residual and measure the opportunity, from frozen inputs only.

    Args:
        session: A read-only session on the frozen PIT database.
        index: The post-A3 derived index, read-only.
        manifest: The final V2 manifest, for the held set.
        resolution: The frozen filer resolution.
        pit_dir: The B2.16 package.
        plan: The authorized A3 targets, in plan order.
        plan_bytes: Actual stored bytes per A3 target, from the manifest.
        expected_solved: The planner's forecast, for the comparison only.
        progress: Receives one line per stage.

    Returns:
        The audit.

    Raises:
        AuditError: If the materialization does not reproduce, the universes
            differ, or a candidate edge breaks the temporal rule.
    """
    say = progress or (lambda line: None)
    timings: dict[str, float] = {}
    started = time.perf_counter()
    outcomes, targets, _ = load_outcomes(pit_dir)
    reproduction = _reproduction(outcomes)
    if not reproduction["reconciles"] or reproduction["duplicate_identity_months"]:
        raise AuditError(f"the materialization does not reconcile: {reproduction}")
    say(f"reproduced: {reproduction}")
    planning = _planning(outcomes, expected_solved)
    transitions = _transition_matrix(outcomes)
    timings["reproduction"] = time.perf_counter() - started

    stage = time.perf_counter()
    assembled = assemble_planning_inputs(session, index, manifest, resolution, progress=progress)
    post = {(r.kind, r.identity_id, r.month_end): r for r in assembled.coverage}
    residual = [o for o in outcomes if o["transition"] in ("STILL_OPEN", "REGRESSED")]
    if any(
        (str(o["kind"]), int(str(o["identity_id"])), str(o["month_end"])) not in post
        for o in residual
    ):
        raise AuditError("a residual month is not in the post-A3 universe")
    timings["assembly"] = time.perf_counter() - stage

    # --- classification --------------------------------------------------------
    stage = time.perf_counter()
    held = _held_by_cik(index)
    foreign = _foreign_issuers(index)
    declined = {r.identity for r in resolution if DECLINED_MAP_REASON in r.reason}
    unresolved = {r.identity for r in resolution if r.cik is None}
    classified: list[tuple[_Residual, dict[str, object]]] = []
    for o in residual:
        row = post[(str(o["kind"]), int(str(o["identity_id"])), str(o["month_end"]))]
        classified.append(
            _classify_residual(o, row, held.get(row.cik, ()) if row.cik else (), declined, foreign)
        )
    classified.sort(
        key=lambda c: (str(c[1]["kind"]), int(str(c[1]["identity_id"])), str(c[1]["month_end"]))
    )
    typed = [c[0] for c in classified]
    rows = [c[1] for c in classified]
    classes = dict.fromkeys(ALL_CLASSES, 0)
    for t in typed:
        classes[t.audit_class] += 1
    sufficiency = dict.fromkeys(ALL_SUFFICIENCY, 0)
    for t in typed:
        sufficiency[t.sufficiency] += 1
    residual_reasons = dict(sorted(Counter(str(r["post_gap_class"]) for r in rows).items()))
    flags = dict(sorted(Counter(f for t in typed for f in t.flags).items()))
    residual_keys_by_row = [(str(r["kind"]), int(str(r["identity_id"]))) for r in rows]
    timings["classification"] = time.perf_counter() - stage

    # --- NO_CIK and declined mappings ----------------------------------------
    residual_ids = Counter(residual_keys_by_row)
    pre_open_ids = Counter(
        (str(o["kind"]), int(str(o["identity_id"])))
        for o in outcomes
        if o["pre_status"] == CoverageStatus.OPEN
    )
    declined_months = [t for t in typed if "DECLINED_CURRENT_MAP_MATCH" in t.flags]
    no_cik = {
        "unresolved_identities_in_resolution": len(unresolved),
        "declined_current_map_identities": len(declined),
        "residual_identities_unresolved": sum(1 for i in residual_ids if i in unresolved),
        "residual_months_unresolved_identities": sum(
            n for i, n in residual_ids.items() if i in unresolved
        ),
        "residual_months_no_cik_class": classes["R2_NO_HISTORICAL_CIK"],
        "declined_identities_with_open_months": sum(1 for i in pre_open_ids if i in declined),
        "declined_months_open_before": sum(n for i, n in pre_open_ids.items() if i in declined),
        "declined_months_residual": len(declined_months),
        "declined_months_resolved_by_a3": sum(n for i, n in pre_open_ids.items() if i in declined)
        - len(declined_months),
        "declined_upper_bound_recoverable_months": len(declined_months),
        "declined_benefit_classification": "UPPER_BOUND_ONLY",
    }

    # --- eligible filings held but useless -------------------------------------
    with_held = [i for i, t in enumerate(typed) if t.held_in_reach > 0]
    held_forms: Counter[str] = Counter()
    for i in with_held:
        forms_here = rows[i]["held_in_reach_forms"]
        if isinstance(forms_here, list):
            held_forms.update(str(f) for f in forms_here)
    exhausted = {
        "residual_months_with_held_filing_in_reach": len(with_held),
        "residual_months_with_held_filing_but_no_common_cover": classes[
            "R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE"
        ],
        "residual_identities_with_held_filing_in_reach": len(
            {residual_keys_by_row[i] for i in with_held}
        ),
        "forms_of_held_filings_in_reach": dict(sorted(held_forms.items())),
    }

    # --- A3 utility and the marginal curve -------------------------------------
    stage = time.perf_counter()
    a3_keys = {(t.cik, t.accession) for t in plan}
    a3_obs: dict[tuple[str, int], list[tuple[date, tuple[int, str]]]] = defaultdict(list)
    for ob in assembled.observations:
        if (
            ob.identity is not None
            and ob.security_class == _COMMON
            and (ob.cik, ob.accession) in a3_keys
        ):
            a3_obs[ob.identity].append((ob.evidence_date, (ob.cik, ob.accession)))
    for obs in a3_obs.values():
        obs.sort()
    supports: dict[tuple[int, str], set[int]] = defaultdict(set)
    resolved_index = 0
    for o in outcomes:
        if o["transition"] != "RESOLVED":
            continue
        end = date.fromisoformat(str(o["month_end"]))
        start = end - timedelta(days=CARRY_FORWARD_DAYS)
        for observed, key in a3_obs.get((str(o["kind"]), int(str(o["identity_id"]))), ()):
            if start <= observed <= end:
                supports[key].add(resolved_index)
        resolved_index += 1
    total_resolved = resolved_index
    by_key = {(int(str(t["cik"])), str(t["accession"])): t for t in targets}
    ordered = list(plan)
    n = len(ordered)
    union: set[int] = set()
    cumulative_bytes = 0
    milestones: list[dict[str, object]] = []
    deciles: list[dict[str, object]] = []
    marks = {max(1, round(n * f)): f for f in PREFIXES}
    decile_edges = [round(n * d / 10) for d in range(11)]
    decile_start_union = 0
    decile_start_bytes = 0
    for position, target in enumerate(ordered, start=1):
        union |= supports.get(target.key, set())
        cumulative_bytes += plan_bytes.get(target.key, 0)
        if position in marks:
            milestones.append(
                {
                    "prefix": marks[position],
                    "documents": position,
                    "bytes": cumulative_bytes,
                    "resolved_months_union": len(union),
                    "share_of_resolved": round(len(union) / total_resolved, 6),
                    "remaining_of_previously_open": len(outcomes) - len(union),
                    "months_per_1000_documents": round(len(union) / position * 1000, 3),
                    "months_per_gb": round(len(union) / (cumulative_bytes / 1024**3), 3)
                    if cumulative_bytes
                    else 0.0,
                }
            )
        if position in decile_edges[1:]:
            decile = decile_edges.index(position)
            documents = position - decile_edges[decile - 1]
            added = len(union) - decile_start_union
            bytes_ = cumulative_bytes - decile_start_bytes
            deciles.append(
                {
                    "decile": decile,
                    "documents": documents,
                    "bytes": bytes_,
                    "added_resolved_months_union": added,
                    "months_per_1000_documents": round(added / documents * 1000, 3)
                    if documents
                    else 0.0,
                    "months_per_gb": round(added / (bytes_ / 1024**3), 3) if bytes_ else 0.0,
                    "documents_with_common_cover": sum(
                        1
                        for t in ordered[decile_edges[decile - 1] : position]
                        if int(str(by_key[t.key]["common_stock_rows"])) > 0
                    ),
                    "documents_participating": sum(
                        1
                        for t in ordered[decile_edges[decile - 1] : position]
                        if supports.get(t.key)
                    ),
                }
            )
            decile_start_union = len(union)
            decile_start_bytes = cumulative_bytes
    total_bytes = sum(plan_bytes.get(t.key, 0) for t in ordered)
    by_form: dict[str, Counter[str]] = defaultdict(Counter)
    for realized in targets:
        form = str(realized["form"])
        target_key = (int(str(realized["cik"])), str(realized["accession"]))
        by_form[form]["downloaded"] += 1
        by_form[form]["bytes"] += plan_bytes.get(target_key, 0)
        by_form[form]["structured_cover"] += int(int(str(realized["common_stock_rows"])) > 0)
        by_form[form]["participating"] += int(bool(supports.get(target_key)))
        by_form[form]["zero_utility"] += int(not supports.get(target_key))
        by_form[form]["months_supported"] += len(supports.get(target_key, ()))
    utility = {
        "documents": n,
        "bytes": total_bytes,
        "documents_with_cover_rows": sum(1 for t in targets if int(str(t["cover_rows"])) > 0),
        "documents_with_common_stock_cover": sum(
            1 for t in targets if int(str(t["common_stock_rows"])) > 0
        ),
        "documents_participating": sum(1 for t in ordered if supports.get(t.key)),
        "documents_zero_cover": sum(1 for t in targets if int(str(t["cover_rows"])) == 0),
        "documents_cover_but_no_benefit": sum(
            1
            for t in ordered
            if int(str(by_key[t.key]["common_stock_rows"])) > 0 and not supports.get(t.key)
        ),
        "documents_zero_utility": sum(1 for t in ordered if not supports.get(t.key)),
        "resolved_months": total_resolved,
        "resolved_months_per_1000_documents": round(total_resolved / n * 1000, 3),
        "resolved_months_per_gb": round(total_resolved / (total_bytes / 1024**3), 3),
        "by_form": {
            form: {
                **dict(counts),
                "months_supported_per_document": round(
                    counts["months_supported"] / counts["downloaded"], 3
                ),
                "months_supported_per_gb": round(
                    counts["months_supported"] / (counts["bytes"] / 1024**3), 3
                )
                if counts["bytes"]
                else 0.0,
            }
            for form, counts in sorted(by_form.items())
        },
    }
    curve: dict[str, object] = {"prefixes": milestones, "deciles": deciles}
    timings["curve"] = time.perf_counter() - stage

    # --- residual opportunity: the post-A3 candidates and the unchanged planner --
    stage = time.perf_counter()
    candidates: Sequence[Candidate] = assembled.inputs.candidates
    residual_keys = {
        ((str(r["kind"]), int(str(r["identity_id"]))), date.fromisoformat(str(r["month_end"])))
        for r in rows
    }
    reaching = [c for c in candidates if c.covers]
    reachable = set().union(*(c.covers for c in reaching)) if reaching else set()
    edge_violations = sum(
        1
        for c in reaching
        for _identity, end in c.covers
        if end < c.filing_date or (end - c.filing_date).days > CARRY_FORWARD_DAYS
    )
    if edge_violations:
        raise AuditError(f"{edge_violations} residual candidate edges break the temporal rule")
    if not reachable <= residual_keys:
        raise AuditError("a post-A3 candidate reaches a month that is not residual")
    forensic = plan_coverage_first(candidates)
    forensic_rows = [{**asdict(t), "status": list(FORENSIC_STATUS)} for t in forensic.targets]
    density = Counter(_bucket(len(c.covers)) for c in reaching)
    projected_docs = len(forensic.targets)
    projected_bytes = forensic.estimated_bytes
    projected_months = forensic.potential_months
    opportunity = {
        "status": list(FORENSIC_STATUS),
        "unheld_eligible_candidates": len(candidates),
        "candidates_reaching_residual_months": len(reaching),
        "residual_months_reachable": len(reachable),
        "residual_months": len(rows),
        "share_of_residual_reachable": round(len(reachable) / len(rows), 6) if rows else 0.0,
        "residual_months_unreachable": len(rows) - len(reachable),
        "candidate_estimated_bytes": sum(c.estimated_bytes for c in reaching),
        "candidate_forms": dict(sorted(Counter(c.form for c in reaching).items())),
        "candidate_unique_ciks": len({c.cik for c in reaching}),
        "candidate_marginal_month_density": dict(sorted(density.items())),
        "candidate_edges_checked": sum(len(c.covers) for c in reaching),
        "future_invalid_candidate_edges": edge_violations,
        "forensic_plan": {
            "targets": projected_docs,
            "residual_months_reached": projected_months,
            "yield_adjusted_months": round(forensic.yield_adjusted_months, 3),
            "estimated_bytes": projected_bytes,
            "packing_months_per_target": round(projected_months / projected_docs, 4)
            if projected_docs
            else 0.0,
            "forms": dict(sorted(Counter(t.form for t in forensic.targets).items())),
            "projected_months_per_1000_documents": round(
                projected_months / projected_docs * 1000, 3
            )
            if projected_docs
            else 0.0,
            "projected_months_per_gb": round(projected_months / (projected_bytes / 1024**3), 3)
            if projected_bytes
            else 0.0,
            "share_of_residual": round(projected_months / len(rows), 6) if rows else 0.0,
            "remaining_after_theoretical_acquisition": len(rows) - projected_months,
            "document_availability": "UNKNOWN_UNTIL_EXECUTION",
        },
    }
    timings["opportunity"] = time.perf_counter() - stage

    # --- year, identity, concentration -----------------------------------------
    year_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for o in outcomes:
        if o["pre_status"] != CoverageStatus.OPEN:
            continue
        year = str(o["month_end"])[:4]
        year_counts[year]["open_before"] += 1
        year_counts[year]["resolved" if o["transition"] == "RESOLVED" else "still_open"] += 1
    year_sufficiency: dict[str, Counter[str]] = defaultdict(Counter)
    for classified_month, r in zip(typed, rows, strict=True):
        year_sufficiency[str(r["month_end"])[:4]][classified_month.sufficiency] += 1
    by_year: dict[str, dict[str, object]] = {
        year: {
            "open_before": counts["open_before"],
            "resolved": counts["resolved"],
            "still_open": counts["still_open"],
            "resolution_rate": round(counts["resolved"] / counts["open_before"], 6),
            "residual_sufficiency": dict(sorted(year_sufficiency[year].items())),
        }
        for year, counts in sorted(year_counts.items())
    }

    per_identity: dict[tuple[str, int], _IdentityStat] = {}
    for o in outcomes:
        if o["pre_status"] != CoverageStatus.OPEN:
            continue
        identity_key = (str(o["kind"]), int(str(o["identity_id"])))
        cik_value = int(str(o["cik"])) if o["cik"] is not None else None
        stat = per_identity.setdefault(
            identity_key,
            _IdentityStat(
                kind=identity_key[0],
                identity_id=identity_key[1],
                cik=cik_value,
                symbol=str(o["symbol"]),
                bucket="LIVE" if identity_key[0] == "COMPANY" else "DELISTED",
                foreign=cik_value is not None and cik_value in foreign,
            ),
        )
        stat.open_before += 1
        if o["transition"] == "RESOLVED":
            stat.resolved += 1
        else:
            stat.still_open += 1
    for classified_month, identity_key in zip(typed, residual_keys_by_row, strict=True):
        per_identity[identity_key].residual_sufficiency[classified_month.sufficiency] += 1
    stats = [per_identity[k] for k in sorted(per_identity)]
    identity_rows = [st.row() for st in stats]
    unresolved_stats = [st for st in stats if st.still_open > 0]

    def bucket_summary(selected: Sequence[_IdentityStat]) -> dict[str, object]:
        open_before = sum(st.open_before for st in selected)
        resolved = sum(st.resolved for st in selected)
        return {
            "identities": len(selected),
            "open_before": open_before,
            "resolved": resolved,
            "still_open": sum(st.still_open for st in selected),
            "resolution_rate": round(resolved / open_before, 6) if open_before else 0.0,
        }

    identities: dict[str, object] = {
        "identities_with_open_months": len(stats),
        "fully_resolved": sum(1 for st in stats if st.outcome == "FULLY_RESOLVED"),
        "partially_resolved": sum(1 for st in stats if st.outcome == "PARTIALLY_RESOLVED"),
        "no_improvement": sum(1 for st in stats if st.outcome == "NO_IMPROVEMENT"),
        "all_months_unresolved": sum(1 for st in stats if st.resolved == 0),
        "distribution_of_open_months_among_unresolved": dict(
            sorted(Counter(_bucket(st.still_open) for st in unresolved_stats).items())
        ),
        "by_bucket": {
            bucket: bucket_summary([st for st in stats if st.bucket == bucket])
            for bucket in ("LIVE", "DELISTED")
        },
        "foreign": bucket_summary([st for st in stats if st.foreign]),
        "rows": identity_rows,
    }
    ranked = sorted(unresolved_stats, key=lambda st: (-st.still_open, st.kind, st.identity_id))
    total_residual = len(rows)
    concentration: dict[str, object] = {
        "residual_months": total_residual,
        "identities_with_residual": len(ranked),
        **{
            f"top_{k}_share": round(sum(st.still_open for st in ranked[:k]) / total_residual, 6)
            if total_residual
            else 0.0
            for k in (10, 50, 100, 500)
        },
        "top_10": [st.row() for st in ranked[:10]],
        "by_bucket": {
            b: sum(st.still_open for st in ranked if st.bucket == b) for b in ("LIVE", "DELISTED")
        },
        "by_kind": {
            k: sum(st.still_open for st in ranked if st.kind == k)
            for k in ("COMPANY", "HISTORICAL")
        },
        "foreign_issuer_residual_months": sum(st.still_open for st in ranked if st.foreign),
        "flags": flags,
    }

    # --- decision --------------------------------------------------------------
    sec_reachable = sufficiency["SEC_MORE_MAY_HELP"]
    decision = {
        "remaining_months": total_residual,
        "sec_reachable_months": sec_reachable,
        "share_sec_reachable": round(sec_reachable / total_residual, 6) if total_residual else 0.0,
        "forensic_documents_required": projected_docs,
        "forensic_estimated_bytes": projected_bytes,
        "forensic_months_reached": projected_months,
        "forensic_months_per_1000_documents": opportunity["forensic_plan"][
            "projected_months_per_1000_documents"
        ],  # type: ignore[index]
        "forensic_months_per_gb": opportunity["forensic_plan"]["projected_months_per_gb"],  # type: ignore[index]
        "a3_months_per_1000_documents": utility["resolved_months_per_1000_documents"],
        "a3_months_per_gb": utility["resolved_months_per_gb"],
        "a3_final_decile_months_per_1000_documents": deciles[-1]["months_per_1000_documents"]
        if deciles
        else None,
        "a3_final_decile_months_per_gb": deciles[-1]["months_per_gb"] if deciles else None,
        "identity_data_required_months": sufficiency["IDENTITY_DATA_REQUIRED"],
        "non_sec_evidence_required_months": sufficiency["NON_SEC_EVIDENCE_REQUIRED"],
        "structurally_unresolvable_months": sufficiency[
            "STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL"
        ],
        "sec_unlikely_months": sufficiency["SEC_MORE_UNLIKELY_TO_HELP"],
        "unknown_months": sufficiency["UNKNOWN"],
        "remaining_after_theoretical_extra_sec": total_residual - projected_months,
        "previously_open": len(outcomes),
        "resolved_by_a3": total_resolved,
        "expected_solved": expected_solved,
        "actual_minus_expected": planning["actual_minus_expected"],
    }
    summary = {
        "audit_version": AUDIT_VERSION,
        "reproduction": reproduction,
        "planning": planning,
        "residual_reasons": residual_reasons,
        "classes": classes,
        "sufficiency": sufficiency,
        "flags": flags,
        "no_cik": no_cik,
        "exhausted": exhausted,
        "utility": {k: v for k, v in utility.items() if k != "by_form"},
    }
    prints = {
        "residual_classification": dict(zip(("rows", "sha256"), _sha_lines(rows), strict=True)),
        "year_summary": dict(zip(("rows", "sha256"), _sha_lines([by_year]), strict=True)),
        "identity_summary": dict(zip(("rows", "sha256"), _sha_lines(identity_rows), strict=True)),
        "marginal_curve": dict(zip(("rows", "sha256"), _sha_lines([curve]), strict=True)),
        "sec_opportunity": dict(zip(("rows", "sha256"), _sha_lines([opportunity]), strict=True)),
        "forensic_plan": dict(zip(("rows", "sha256"), _sha_lines(forensic_rows), strict=True)),
        "decision": dict(zip(("rows", "sha256"), _sha_lines([decision]), strict=True)),
        "summary": dict(zip(("rows", "sha256"), _sha_lines([summary]), strict=True)),
    }
    timings["total"] = time.perf_counter() - started
    say(
        f"audit: {total_residual} residual months — sufficiency {sufficiency}; forensic plan "
        f"{projected_docs} targets reaching {projected_months} ({projected_bytes:,} bytes)"
    )
    return AuditResult(
        reproduction=reproduction,
        planning=planning,
        transitions=transitions,
        residual_reasons=residual_reasons,
        residual_rows=rows,
        classes=classes,
        sufficiency=sufficiency,
        no_cik=no_cik,
        exhausted=exhausted,
        utility=utility,
        curve=curve,
        opportunity=opportunity,
        forensic_rows=forensic_rows,
        by_year=dict(sorted(by_year.items())),
        identities=identities,
        concentration=concentration,
        decision=decision,
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


def write_audit(
    directory: Path, result: AuditResult, *, bindings: Mapping[str, object]
) -> dict[str, str]:
    """Write the audit package; the forensic plan carries its status in every row and a sidecar.

    Returns:
        SHA-256 per file, by name.
    """
    directory.mkdir(parents=True, exist_ok=True)
    b = dict(bindings)
    digests: dict[str, str] = {}
    digests["pit_residual_months.jsonl"] = _write_lines(
        directory / "pit_residual_months.jsonl", result.residual_rows
    )
    digests[FORENSIC_PLAN_FILE] = _write_lines(directory / FORENSIC_PLAN_FILE, result.forensic_rows)
    digests["residual_sec_forensic_plan.status.json"] = _write_json(
        directory / "residual_sec_forensic_plan.status.json",
        {
            **b,
            "status": list(FORENSIC_STATUS),
            "file": FORENSIC_PLAN_FILE,
            "sha256": digests[FORENSIC_PLAN_FILE],
            "targets": len(result.forensic_rows),
            "note": "A counterfactual over the post-A3 candidates for the B2.17 decision. "
            "It is not an acquisition plan and no record authorizes it.",
        },
    )
    digests["pit_residual_summary.json"] = _write_json(
        directory / "pit_residual_summary.json",
        {
            **b,
            "audit_version": AUDIT_VERSION,
            "reproduction": result.reproduction,
            "planning": result.planning,
            "transitions": result.transitions,
            "residual_reasons": result.residual_reasons,
            "classes": result.classes,
            "sufficiency": result.sufficiency,
            "no_cik": result.no_cik,
            "exhausted": result.exhausted,
            "utility": result.utility,
            "timings": result.timings,
        },
    )
    digests["pit_residual_reasons.json"] = _write_json(
        directory / "pit_residual_reasons.json",
        {
            **b,
            "residual_reasons": result.residual_reasons,
            "classes": result.classes,
            "sufficiency": result.sufficiency,
            "flags": result.concentration.get("flags", {}),
        },
    )
    digests["pit_residual_by_year.json"] = _write_json(
        directory / "pit_residual_by_year.json", {**b, "years": result.by_year}
    )
    digests["pit_residual_by_identity.json"] = _write_json(
        directory / "pit_residual_by_identity.json", {**b, **result.identities}
    )
    digests["pit_residual_concentration.json"] = _write_json(
        directory / "pit_residual_concentration.json", {**b, **result.concentration}
    )
    digests["pit_a3_marginal_curve.json"] = _write_json(
        directory / "pit_a3_marginal_curve.json",
        {**b, **result.curve, "utility_by_form": result.utility["by_form"]},
    )
    digests["pit_residual_sec_opportunity.json"] = _write_json(
        directory / "pit_residual_sec_opportunity.json", {**b, **result.opportunity}
    )
    digests["pit_residual_decision.json"] = _write_json(
        directory / "pit_residual_decision.json", {**b, **result.decision}
    )
    digests[AUDIT_COMPLETE_MARKER] = _write_json(
        directory / AUDIT_COMPLETE_MARKER,
        {
            "stage": "PIT_AUDIT_COMPLETE",
            **b,
            "audit_version": AUDIT_VERSION,
            "residual_months": result.decision["remaining_months"],
            "sufficiency": result.sufficiency,
            "forensic_plan_status": list(FORENSIC_STATUS),
            "fingerprints": result.fingerprints,
            "files": {k: v for k, v in digests.items() if k != AUDIT_COMPLETE_MARKER},
        },
    )
    for name, sha in digests.items():
        (directory / f"{name.rsplit('.', 1)[0]}.sha256").write_text(f"{sha}  {name}\n")
    log.info("pit audit written", directory=str(directory), files=len(digests))
    return digests


def mirror_audit(
    mirror: ControlMirror, directory: Path, *, generation_id: str
) -> list[MirrorRecord]:
    """Mirror the small audit files as snapshots under `pit-audit/`, and read each back."""
    records = []
    for name in AUDIT_MIRRORED:
        local = directory / name
        if not local.is_file():
            raise AuditError(f"{local} does not exist; nothing to mirror")
        kind = f"{AUDIT_MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise AuditError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records
