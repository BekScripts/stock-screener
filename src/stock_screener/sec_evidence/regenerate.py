"""Running the unchanged Plan A3 planner over a frozen input package, and comparing.

Phase B2.13 froze everything Plan A3 is computed from. This module loads that
package — never reassembles it — runs the planner exactly as Phase 9L.4 did,
validates what it chose against the package it was chosen from, writes the
plan with the historical writer, and compares the result to the one thing
that survived of the first generation's plan: its row count and its digest.

## The historical configuration

Plan A3 was "Plan A" — `plan_coverage_first` run to exhaustion, no limit —
which Phase 9L.2 showed returns the same selection as the minimum temporal
cover. Its rows were written by `plan.write_plan`: one JSON object per line
with sorted keys, a trailing newline, and the file's SHA-256 as the digest.
Both survive byte-identically from the pre-B2 checkpoint. What does not
survive is the plan itself, so the comparison is against a count and a
digest, and a digest that differs is *inconclusive* about the selection
unless the count and the selection can be shown to differ on their own.

## What this module refuses to do

It does not touch the planner, its order key, its tie-breaks, its yield or
size inputs, or the writer. It does not adjust anything to move the count
toward 61,941. It does not download. A plan that differs from the historical
one is reported as a difference, with the fingerprints that let a later
phase say where.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from api_clients import FILING_FORMS
from stock_screener.sec_evidence.generation import A3_EXPECTED_SHA256, A3_EXPECTED_TARGETS
from stock_screener.sec_evidence.inputs import (
    A3_INPUTS_MANIFEST,
    SUPERSEDED_SIDECAR,
    InputsBindings,
    PackageError,
    load_planning_inputs,
)
from stock_screener.sec_evidence.plan import (
    PLAN_VERSION,
    GapClass,
    Plan,
    PlanningInputs,
    plan_coverage_first,
    write_plan,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "A3_ALGORITHM_FILES",
    "A3_AUTHORITATIVE_RECORD",
    "A3_PLAN_COMPLETE_MARKER",
    "A3_PLAN_FILE",
    "A3_PLAN_META",
    "A3_STRATEGY",
    "A3Result",
    "PlanValidation",
    "PlanValidationError",
    "algorithm_digests",
    "mirror_plan",
    "plan_fingerprints",
    "plan_metrics",
    "regenerate_a3",
    "serialize_plan",
    "validate_plan",
    "write_a3",
    "write_authorization",
]

#: The historical configuration: Plan A, coverage-first, run to exhaustion.
A3_STRATEGY = "COVERAGE_FIRST"

#: Output names. `write_plan` also writes its own `expansion_plan_v1.*`
#: beside them — the historical names, byte-identical to the V2 plan file.
A3_PLAN_FILE = "plan_a3_v2.jsonl"
A3_PLAN_SHA = "plan_a3_v2.sha256"
A3_PLAN_SUMMARY = "plan_a3_v2.summary.json"
A3_PLAN_META = "plan_a3_v2.meta.json"
A3_PLAN_FINGERPRINTS = "plan_a3_v2.fingerprints.json"
A3_PLAN_COMPLETE_MARKER = "plan_a3_v2_complete.json"
#: Written only when every gate has passed: the plan is the one to execute.
A3_AUTHORITATIVE_RECORD = "authoritative_a3_plan.json"

#: The source files the selection depends on, relative to the repository.
#: `plan.py` is the algorithm and the writer; the rest shaped its inputs and
#: are digested so a later reader can tell exactly which code produced a plan.
A3_ALGORITHM_FILES = (
    "src/stock_screener/sec_evidence/plan.py",
    "src/stock_screener/sec_evidence/reachability.py",
    "src/stock_screener/sec_evidence/join.py",
    "src/stock_screener/sec_evidence/coverage.py",
    "src/stock_screener/sec_evidence/sample.py",
    "src/stock_screener/sec_evidence/expand.py",
    "src/stock_screener/sec_evidence/inputs.py",
)


class PlanValidationError(RuntimeError):
    """The plan the planner returned is not a plan over the frozen package."""


@dataclass(frozen=True)
class PlanValidation:
    """Every check a plan must pass before it is frozen. Zero everywhere, or it is not.

    Attributes:
        outside_candidates: Targets not in the frozen candidate pool.
        already_held: Targets the frozen held set already holds.
        duplicates: Repeated `(cik, accession)` identities.
        invalid_forms: Targets whose form is not a qualifying form.
        without_cover: Targets whose candidate covers no solvable gap.
        wrong_bytes: Targets whose estimate is not the candidate's.
        budget_violations: Always zero — the planner has no byte budget.
    """

    outside_candidates: int
    already_held: int
    duplicates: int
    invalid_forms: int
    without_cover: int
    wrong_bytes: int
    budget_violations: int = 0

    @property
    def passed(self) -> bool:
        """Whether every count is zero."""
        return not any(asdict(self).values())


@dataclass
class A3Result:
    """One regeneration: the plan, its validation, its metrics and the comparison."""

    plan: Plan
    validation: PlanValidation
    metrics: dict[str, object]
    fingerprints: dict[str, str]
    serialized: bytes
    sha256: str
    determinism: dict[str, object] = field(default_factory=dict)
    comparison: dict[str, object] = field(default_factory=dict)
    package_manifest: dict[str, object] = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        """Whether the count and the digest both equal the historical plan's."""
        return len(self.plan.targets) == A3_EXPECTED_TARGETS and self.sha256 == A3_EXPECTED_SHA256


# ----------------------------------------------------------------------------
# The historical serialization, reproduced without being reimplemented
# ----------------------------------------------------------------------------


def serialize_plan(plan: Plan) -> bytes:
    """The exact bytes `plan.write_plan` writes to its JSONL, and hashes.

    One `json.dumps(asdict(target), sort_keys=True)` per target, in plan
    order, each followed by a newline. Kept identical to the writer so an
    in-memory digest and the file's digest cannot disagree; the writer is
    still what puts the file on disk.
    """
    return b"".join(
        json.dumps(asdict(target), sort_keys=True).encode() + b"\n" for target in plan.targets
    )


def plan_fingerprints(plan: Plan) -> dict[str, str]:
    """Deterministic digests that separate ordering, membership and composition.

    `ordered` is the historical file digest. `unordered_targets` hashes the
    sorted identity set, so two plans with the same targets in another order
    agree on it; `ordered_identities` hashes the identity sequence alone, so
    a difference in a stamped field does not hide behind it; `per_form` and
    `per_cik` hash the composition.
    """
    ordered = hashlib.sha256(serialize_plan(plan)).hexdigest()
    keys = [(t.cik, t.accession) for t in plan.targets]
    unordered = hashlib.sha256(
        "".join(f"{cik}\t{accession}\n" for cik, accession in sorted(keys)).encode()
    ).hexdigest()
    sequence = hashlib.sha256(
        "".join(f"{cik}\t{accession}\n" for cik, accession in keys).encode()
    ).hexdigest()
    per_form: dict[str, int] = defaultdict(int)
    per_cik: dict[int, int] = defaultdict(int)
    for target in plan.targets:
        per_form[target.form] += 1
        per_cik[target.cik] += 1
    form_digest = hashlib.sha256(
        "".join(f"{form}\t{count}\n" for form, count in sorted(per_form.items())).encode()
    ).hexdigest()
    cik_digest = hashlib.sha256(
        "".join(f"{cik}\t{count}\n" for cik, count in sorted(per_cik.items())).encode()
    ).hexdigest()
    return {
        "ordered": ordered,
        "unordered_targets": unordered,
        "ordered_identities": sequence,
        "per_form": form_digest,
        "per_cik": cik_digest,
    }


# ----------------------------------------------------------------------------
# Validation and metrics
# ----------------------------------------------------------------------------


def validate_plan(plan: Plan, inputs: PlanningInputs) -> PlanValidation:
    """Check a plan against the package it was planned from.

    Args:
        plan: What the planner returned.
        inputs: The frozen inputs it ran over.

    Returns:
        The counts. Every one must be zero for the plan to be frozen.
    """
    candidates = {candidate.key: candidate for candidate in inputs.candidates}
    solvable = {
        (gap.identity, gap.month_end) for gap in inputs.gaps if gap.gap_class is GapClass.SOLVABLE
    }
    seen: set[tuple[int, str]] = set()
    outside = held = duplicates = invalid = uncovered = wrong_bytes = 0
    for target in plan.targets:
        key = (target.cik, target.accession)
        if key in seen:
            duplicates += 1
        seen.add(key)
        if key in inputs.held:
            held += 1
        if target.form not in FILING_FORMS:
            invalid += 1
        candidate = candidates.get(key)
        if candidate is None:
            outside += 1
            continue
        if not (candidate.covers & solvable):
            uncovered += 1
        if target.estimated_bytes != candidate.estimated_bytes:
            wrong_bytes += 1
    return PlanValidation(
        outside_candidates=outside,
        already_held=held,
        duplicates=duplicates,
        invalid_forms=invalid,
        without_cover=uncovered,
        wrong_bytes=wrong_bytes,
    )


def plan_metrics(plan: Plan, inputs: PlanningInputs) -> dict[str, object]:
    """The planner's own figures, plus the package's, in the phase-report vocabulary.

    `safe_potential_months` is `Plan.potential_months`; `expected_months` is
    `Plan.yield_adjusted_months`; the raw `UNKNOWN` floor is every open gap
    the planner cannot reach; expected remaining is what the plan leaves of
    the open gaps once its expectation is subtracted.
    """
    by_class = inputs.by_class()
    open_months = sum(by_class.values())
    solvable = by_class.get(str(GapClass.SOLVABLE), 0)
    forms: dict[str, int] = defaultdict(int)
    form_bytes: dict[str, int] = defaultdict(int)
    for target in plan.targets:
        forms[target.form] += 1
        form_bytes[target.form] += target.estimated_bytes
    return {
        "strategy": plan.strategy,
        "selected_targets": len(plan.targets),
        "unique_ciks": len({t.cik for t in plan.targets}),
        "unique_accessions": len({t.accession for t in plan.targets}),
        "estimated_bytes": plan.estimated_bytes,
        "estimated_gib": round(plan.estimated_bytes / 1024**3, 3),
        "candidate_pool": len(inputs.candidates),
        "candidates_with_cover": sum(1 for c in inputs.candidates if c.covers),
        "held": len(inputs.held),
        "open_months": open_months,
        "gap_classes": dict(sorted(by_class.items())),
        "solvable_gaps": solvable,
        "raw_unknown_floor": open_months - solvable,
        "safe_potential_months": plan.potential_months,
        "expected_months": round(plan.yield_adjusted_months, 3),
        "unreached_solvable": solvable - plan.potential_months,
        "expected_remaining": round(open_months - plan.yield_adjusted_months, 3),
        "selected_by_form": dict(sorted(forms.items())),
        "estimated_bytes_by_form": dict(sorted(form_bytes.items())),
        "first_target_month": min((t.first_target_month or "" for t in plan.targets), default=None),
        "last_target_month": max((t.last_target_month or "" for t in plan.targets), default=None),
    }


def algorithm_digests(repository: Path) -> dict[str, str]:
    """SHA-256 of every file the selection depends on, for the metadata."""
    digests: dict[str, str] = {}
    for relative in A3_ALGORITHM_FILES:
        path = repository / relative
        digests[relative] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    return digests


# ----------------------------------------------------------------------------
# The run
# ----------------------------------------------------------------------------


def regenerate_a3(
    package: Path,
    *,
    expect: InputsBindings | None = None,
    runs: int = 2,
    forensic: tuple[int, str] | None = None,
) -> A3Result:
    """Load the frozen package, run Plan A3 as configured historically, validate, compare.

    Args:
        package: The frozen inputs package directory.
        expect: The bindings the caller has proven against the live sources.
            Any difference refuses the package before the planner runs.
        runs: How many times the planner runs. Every run after the first
            must reproduce the first byte for byte.
        forensic: A `(rows, ordered sha256)` expectation from an earlier
            forensic run over the same inputs, when there is one. Recorded
            as `EXACT_FORENSIC_REPRODUCTION` or `FORENSIC_PRODUCTION_MISMATCH`
            in the comparison; never used to change the run.

    Returns:
        The result, including the determinism and historical comparisons.

    Raises:
        PackageError: If the package or its bindings do not verify.
        PlanValidationError: If the plan fails a validation, or a repeated
            run does not reproduce the first.
    """
    if (package / SUPERSEDED_SIDECAR).exists():
        raise PackageError(f"{package} is marked SUPERSEDED; it cannot feed a plan")
    inputs, manifest = load_planning_inputs(package, expect=expect)
    plan = plan_coverage_first(inputs.candidates)
    validation = validate_plan(plan, inputs)
    if not validation.passed:
        raise PlanValidationError(f"the plan does not validate against its package: {validation}")
    serialized = serialize_plan(plan)
    sha = hashlib.sha256(serialized).hexdigest()
    metrics = plan_metrics(plan, inputs)
    prints = plan_fingerprints(plan)

    determinism: dict[str, object] = {"runs": runs, "identical": True}
    for run in range(2, runs + 1):
        again = plan_coverage_first(inputs.candidates)
        same = (
            again.targets == plan.targets
            and serialize_plan(again) == serialized
            and plan_metrics(again, inputs) == metrics
        )
        determinism[f"run_{run}_rows"] = len(again.targets)
        determinism[f"run_{run}_sha256"] = hashlib.sha256(serialize_plan(again)).hexdigest()
        if not same:
            determinism["identical"] = False
            raise PlanValidationError(f"run {run} did not reproduce run 1")

    comparison: dict[str, object] = {
        "historical_rows": A3_EXPECTED_TARGETS,
        "historical_sha256": A3_EXPECTED_SHA256,
        "rows": len(plan.targets),
        "sha256": sha,
        "row_count_match": len(plan.targets) == A3_EXPECTED_TARGETS,
        "sha256_match": sha == A3_EXPECTED_SHA256,
        "row_delta": len(plan.targets) - A3_EXPECTED_TARGETS,
        "serializer": (
            "plan.write_plan — JSONL, json.dumps(asdict(target), sort_keys=True) + newline; "
            "SHA-256 over the file bytes"
        ),
        "serializer_survives": True,
        "serializer_at_a3_time_provable": False,
        "old_rows_available": False,
        "byte_comparison": (
            "A3_REPRODUCED_EXACTLY"
            if sha == A3_EXPECTED_SHA256 and len(plan.targets) == A3_EXPECTED_TARGETS
            else "BYTE_DIGEST_COMPARISON_INCONCLUSIVE"
        ),
    }
    if forensic is not None:
        rows_expected, sha_expected = forensic
        comparison["forensic_rows"] = rows_expected
        comparison["forensic_sha256"] = sha_expected
        comparison["forensic_classification"] = (
            "EXACT_FORENSIC_REPRODUCTION"
            if len(plan.targets) == rows_expected and sha == sha_expected
            else "FORENSIC_PRODUCTION_MISMATCH"
        )
    log.info(
        "plan a3 regenerated",
        targets=len(plan.targets),
        sha256=sha[:16],
        exact=comparison["sha256_match"],
    )
    return A3Result(
        plan=plan,
        validation=validation,
        metrics=metrics,
        fingerprints=prints,
        serialized=serialized,
        sha256=sha,
        determinism=determinism,
        comparison=comparison,
        package_manifest=manifest,
    )


def _write_json(path: Path, payload: Mapping[str, object]) -> str:
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_a3(
    directory: Path,
    result: A3Result,
    *,
    generation_id: str,
    algorithm: Mapping[str, str],
    repo_commit: str | None,
) -> dict[str, str]:
    """Write the plan with the historical writer, plus the V2 files beside it.

    `write_plan` puts `expansion_plan_v1.{csv,jsonl,sha256}` in the directory
    — the historical names — and `plan_a3_v2.jsonl` is written from the same
    bytes, so the two digests are one. Then the summary, the metadata, the
    fingerprints and, last, the completion marker.

    Returns:
        SHA-256 per file written, by name.

    Raises:
        PackageError: If the directory holds a plan marked SUPERSEDED.
        PlanValidationError: If the historical writer did not produce the in-memory bytes.
    """
    if (directory / SUPERSEDED_SIDECAR).exists():
        raise PackageError(f"{directory} holds a SUPERSEDED plan; it is never overwritten")
    directory.mkdir(parents=True, exist_ok=True)
    _, jsonl_path, sha = write_plan(directory, result.plan)
    written = jsonl_path.read_bytes()
    if written != result.serialized or sha != result.sha256:
        raise PlanValidationError("the historical writer did not produce the in-memory bytes")
    plan_path = directory / A3_PLAN_FILE
    plan_path.write_bytes(written)
    (directory / A3_PLAN_SHA).write_text(f"{sha}  {A3_PLAN_FILE}\n", encoding="utf-8")
    digests = {
        A3_PLAN_FILE: sha,
        "expansion_plan_v1.jsonl": sha,
        "expansion_plan_v1.csv": hashlib.sha256(
            (directory / "expansion_plan_v1.csv").read_bytes()
        ).hexdigest(),
    }
    package_sha = str(result.package_manifest.get("package_sha256"))
    digests[A3_PLAN_SUMMARY] = _write_json(
        directory / A3_PLAN_SUMMARY,
        {
            "generation_id": generation_id,
            "package_sha256": package_sha,
            "rows": len(result.plan.targets),
            "sha256": sha,
            **result.metrics,
        },
    )
    digests[A3_PLAN_FINGERPRINTS] = _write_json(
        directory / A3_PLAN_FINGERPRINTS,
        {"generation_id": generation_id, "package_sha256": package_sha, **result.fingerprints},
    )
    bindings = dict(result.package_manifest.get("bindings") or {})  # type: ignore[call-overload]
    digests[A3_PLAN_META] = _write_json(
        directory / A3_PLAN_META,
        {
            "generation_id": generation_id,
            "package_sha256": package_sha,
            "plan_sha256": sha,
            "candidate_window": {
                "definition": bindings.get("candidate_window_definition"),
                "start": bindings.get("window_start"),
                "end": bindings.get("window_end"),
                "date_field": "catalogue filing_date",
                "semantics": "WINDOW_START <= filing_date <= WINDOW_END, both inclusive",
            },
            "package_components": dict(result.package_manifest.get("components") or {}),  # type: ignore[call-overload]
            "package_bindings": dict(result.package_manifest.get("bindings") or {}),  # type: ignore[call-overload]
            "algorithm_files": dict(algorithm),
            "repo_commit": repo_commit,
            "configuration": {
                "entrypoint": "plan.plan_coverage_first",
                "strategy": A3_STRATEGY,
                "score": "coverage",
                "limit": None,
                "min_marginal": 1,
                "plan_version_stamp": PLAN_VERSION,
                "writer": "plan.write_plan",
            },
            "metrics": result.metrics,
            "validation": asdict(result.validation),
            "determinism": result.determinism,
            "comparison": result.comparison,
            "fingerprints": result.fingerprints,
            "written_at": datetime.now(UTC).isoformat(),
        },
    )
    digests[A3_PLAN_COMPLETE_MARKER] = _write_json(
        directory / A3_PLAN_COMPLETE_MARKER,
        {
            "stage": "PLAN_A3_V2_COMPLETE",
            "generation_id": generation_id,
            "package_sha256": package_sha,
            "rows": len(result.plan.targets),
            "sha256": sha,
            "historical_rows": A3_EXPECTED_TARGETS,
            "historical_sha256": A3_EXPECTED_SHA256,
            "exact_match": result.exact,
            "byte_comparison": result.comparison["byte_comparison"],
            "fingerprints": result.fingerprints,
            "validation_passed": result.validation.passed,
            "determinism_identical": result.determinism.get("identical"),
        },
    )
    return digests


def write_authorization(
    directory: Path,
    result: A3Result,
    *,
    generation_id: str,
    chain: Mapping[str, object],
) -> str:
    """Record that this plan is the authoritative one, with its whole provenance chain.

    Deterministic — no timestamp — so a rerun over the same plan mirrors as a
    `MIRROR_HIT`. Refused unless the plan validated and every repeated run
    reproduced the first: a plan that is not proven is not authorized.

    Args:
        directory: The plan directory.
        result: The regeneration result.
        generation_id: The evidence generation.
        chain: Superseded package and plan digests, the forensic and
            correction commits, and the corrected package digest.

    Returns:
        The record's SHA-256.

    Raises:
        PlanValidationError: If the plan did not validate or was not
            reproduced, or a forensic expectation was given and not met.
    """
    if not result.validation.passed or not result.determinism.get("identical"):
        raise PlanValidationError("an unvalidated or non-reproduced plan cannot be authorized")
    if result.comparison.get("forensic_classification") == "FORENSIC_PRODUCTION_MISMATCH":
        raise PlanValidationError("the plan does not reproduce the forensic expectation")
    return _write_json(
        directory / A3_AUTHORITATIVE_RECORD,
        {
            "stage": "AUTHORITATIVE_V2_A3_PLAN",
            "status": ["AUTHORITATIVE_V2_A3_PLAN", "AUTHORIZED_FOR_EXPANSION"],
            "generation_id": generation_id,
            "plan_sha256": result.sha256,
            "plan_rows": len(result.plan.targets),
            "unordered_target_set_sha256": result.fingerprints["unordered_targets"],
            "fingerprints": result.fingerprints,
            "package_sha256": str(result.package_manifest.get("package_sha256")),
            "candidate_window_definition": (
                dict(result.package_manifest.get("bindings") or {}).get(  # type: ignore[call-overload]
                    "candidate_window_definition"
                )
            ),
            "estimated_bytes": result.plan.estimated_bytes,
            "solvable_gaps": result.metrics["solvable_gaps"],
            "safe_potential_months": result.metrics["safe_potential_months"],
            "expected_months": result.metrics["expected_months"],
            "historical": {
                "rows": A3_EXPECTED_TARGETS,
                "sha256": A3_EXPECTED_SHA256,
                "row_delta": len(result.plan.targets) - A3_EXPECTED_TARGETS,
                "classification": "COUNT_ONLY_COMPARATOR",
            },
            "forensic_classification": result.comparison.get("forensic_classification"),
            "chain": dict(chain),
            "validation": {"passed": result.validation.passed, **asdict(result.validation)},
        },
    )


#: Every file the mirror carries, in order.
A3_PLAN_FILES = (
    A3_PLAN_FILE,
    A3_PLAN_SHA,
    "expansion_plan_v1.jsonl",
    "expansion_plan_v1.csv",
    "expansion_plan_v1.sha256",
    A3_PLAN_SUMMARY,
    A3_PLAN_FINGERPRINTS,
    A3_PLAN_META,
    A3_PLAN_COMPLETE_MARKER,
)


def mirror_plan(
    mirror: ControlMirror, directory: Path, *, generation_id: str, prefix: str = "a3"
) -> Sequence[MirrorRecord]:
    """Mirror every plan file as an immutable snapshot under `<prefix>/`, and read each back.

    The authoritative record is carried too when the directory holds one.

    Raises:
        PackageError: If a file is missing or its newest snapshot is not the local file.
    """
    records = []
    names = [*A3_PLAN_FILES]
    if (directory / A3_AUTHORITATIVE_RECORD).is_file():
        names.append(A3_AUTHORITATIVE_RECORD)
    for name in names:
        local = directory / name
        if not local.is_file():
            raise PackageError(f"{local} does not exist; nothing to mirror")
        kind = f"{prefix}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or newest[0].get("sha256") != record.sha256:
            raise PackageError(f"the newest mirrored snapshot of {name} is not the local file")
        records.append(record)
    return records


def package_bindings_from_manifest(package: Path) -> InputsBindings:
    """The bindings a package records, for a caller that overrides what it can prove."""
    manifest = json.loads((package / A3_INPUTS_MANIFEST).read_text(encoding="utf-8"))
    return InputsBindings(**dict(manifest["bindings"]))
