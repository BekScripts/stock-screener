"""R1.4 — preregister H2, and run nothing.

The phase writes two documents and a binder, and that is the whole of it. It
computes no turnover, no return and no portfolio, because the experiment does
not exist yet: `bind_r1_3` scans this package's imports for anything that could
price or simulate and refuses if it finds one.

What it does do is make the later run harder to fudge. The rule is frozen with
its alternatives named as prohibited, the four criteria are fixed against
measurements taken before anything was written, the aggregate rule that combines
them is fixed too — H1 had none and its absence had to be respected afterwards —
and the record states in plain terms that no part of the frozen history can
confirm any of it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.backtest_harness.registry import (
    ResearchLedger,
    digest_document,
    write_document,
)
from stock_screener.sec_evidence.mirror import MirrorConflictError

from .binding import H2Binding, bind_r1_3, simulation_reachable
from .record import (
    H2_ID,
    HISTORICAL_STATUS,
    SPEC_STATUS,
    experiment_spec,
    hypothesis_record,
)
from .rule import RULE_VERSION

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "PREREGISTRATION_DIR",
    "PreregistrationResult",
    "mirror_r1_4",
    "preregister_h2",
    "r1_3_directory",
]

PHASE = "R1.4"
PREREGISTRATION_DIR = "research/r1_4"
MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-4"

MIRRORED_FILES = (
    "r1_4_binding.json",
    "h2_membership_stability.json",
    "r1_5_experiment_spec.json",
    "r1_4_fingerprints.json",
    "r1_4_complete.json",
)


@dataclass
class PreregistrationResult:
    """What R1.4 produced.

    Attributes:
        binding: The verified upstream record.
        record: The H2 hypothesis record.
        spec: The R1.5 experiment specification.
        documents: `{name: digest}` for every document written.
        fingerprints: The semantic fingerprints determinism is checked on.
    """

    binding: H2Binding
    record: dict[str, Any] = field(default_factory=dict)
    spec: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)


def _evidence(binding: H2Binding, r1_3_directory: Path) -> dict[str, Any]:
    """Collect the R1.3 measurements the hypothesis rests on."""
    import json

    summary = json.loads((r1_3_directory / "r1_3_summary.json").read_text("utf-8"))
    boundary = json.loads((r1_3_directory / "r1_3_boundary_churn.json").read_text("utf-8"))
    membership = json.loads(
        (r1_3_directory / "r1_3_membership_transitions.json").read_text("utf-8")
    )
    churn = boundary["churn"]
    exits = membership["transitions"]["exit_shares"]
    entries = membership["transitions"]["entry_shares"]
    moves = churn["rank_move_shares"]
    return {
        "source_phase": "R1.3",
        "quarterly_annual_turnover": binding.r1_3_measurements["quarterly_annual_turnover"],
        "membership_floor": binding.r1_3_measurements["membership_change_floor_annual"],
        "per_rebalance_ratio": float(summary["frequency_decomposition"]["per_rebalance_ratio"]),
        "frequency_benefit_offset_share": float(
            summary["frequency_decomposition"]["frequency_benefit_offset_share"]
        ),
        "ranking_churn_share": binding.r1_3_measurements["ranking_churn_share"],
        "resizing_share": binding.r1_3_measurements["resizing_share"],
        "quarterly_retention": binding.r1_3_measurements["quarterly_retention"],
        "monthly_three_month_survival": binding.r1_3_measurements["monthly_three_month_survival"],
        "share_moving_under_10_points": float(churn["share_moving_under_10_points"]),
        "share_moving_over_20_points": float(moves["OVER_20_POINTS"]),
        "exit_share_not_ranking": 1.0 - float(exits["RANKING"]),
        "entry_share_not_ranking": 1.0 - float(entries["RANKING"]),
        "entrants_from_beyond_20th_percentile": float(
            churn["entrant_prior_shares"]["BEYOND_20_PCT"]
        ),
    }


def preregister_h2(
    directory: Path,
    r1_1_directory: Path,
    r1_2_directory: Path,
    r1_3_path: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
    code_commit: str,
) -> PreregistrationResult:
    """Write the H2 record and the R1.5 specification, and execute nothing.

    Args:
        directory: Where to write R1.4's record.
        r1_1_directory: R1.1's frozen hypothesis and specification.
        r1_2_directory: R1.2's frozen record.
        r1_3_path: R1.3's frozen forensic record.
        contract_sha256: The observed contract digest.
        expect_contract_sha256: The digest the caller requires.
        code_commit: The commit this preregistration was written at.

    Returns:
        The preregistration result.

    Raises:
        H2BindingError: If the record H2 is derived from does not verify.
    """
    binding = bind_r1_3(
        r1_1_directory,
        r1_2_directory,
        r1_3_path,
        contract_sha256=contract_sha256,
        expect_contract_sha256=expect_contract_sha256,
    )
    directory.mkdir(parents=True, exist_ok=True)
    log.info("r1.4.bound", checks=len(binding.checks))

    evidence = _evidence(binding, r1_3_path)
    provenance = {
        "contract_version": "BACKTEST_DATA_CONTRACT_V1",
        "dataset_id": "PIT_RESEARCH_DATASET_V2",
        "contract_sha256": binding.contract_sha256,
        "r1_1_hypothesis_sha256": binding.r1_1_hypothesis_sha256,
        "r1_2_experiment_spec_sha256": binding.r1_2_spec_sha256,
        "r1_2_completion_marker_sha256": binding.r1_2_marker_sha256,
        "r1_3_completion_marker_sha256": binding.r1_3_marker_sha256,
        "code_commit": code_commit,
        "acknowledgement": (
            "R1.3's analysis spans the whole frozen period, so this hypothesis is "
            "derived from observed data across every block and cannot be confirmed "
            "by any part of it."
        ),
    }

    record = hypothesis_record(
        evidence=evidence, comparator=binding.comparator, provenance=provenance
    )
    documents: dict[str, str] = {}
    documents["r1_4_binding.json"] = write_document(
        directory / "r1_4_binding.json", binding.as_json()
    )
    documents["h2_membership_stability.json"] = write_document(
        directory / "h2_membership_stability.json", record
    )
    record_sha = documents["h2_membership_stability.json"]

    spec = experiment_spec(
        hypothesis_sha256=record_sha,
        comparator=binding.comparator,
        bindings=provenance,
    )
    documents["r1_5_experiment_spec.json"] = write_document(
        directory / "r1_5_experiment_spec.json", spec
    )

    fingerprints = {
        "binding": digest_document(binding.as_json()),
        "h2_record": digest_document(record),
        "r1_5_spec": digest_document(spec),
        "rule": digest_document(dict(record["rule"])),
        "criteria": digest_document({"criteria": record["falsification_criteria"]["criteria"]}),
        "aggregate_rule": digest_document(dict(record["falsification_criteria"]["aggregate_rule"])),
        "historical_status": digest_document(dict(record["historical_evidence_status"])),
    }
    documents["r1_4_fingerprints.json"] = write_document(
        directory / "r1_4_fingerprints.json",
        {"fingerprints": dict(sorted(fingerprints.items()))},
    )

    ledger = ResearchLedger(directory / "research_ledger.jsonl")
    ledger.record(
        strategy="COMPOUNDER_QUARTERLY_HYSTERESIS_V1",
        mode="HYPOTHESIS_PREREGISTRATION",
        why=(
            "define one hypothesis and one rule from R1.3's mechanism evidence, and "
            "freeze them before anything is measured. Nothing is executed here."
        ),
        parameters={
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "entry": "ordinary top-decile target count",
            "retention": "two selection widths",
            "frequency": "calendar quarter ends",
        },
        development=False,
        validation=False,
        holdout=False,
        variants_tried=0,
        parameter_sweeps=[],
        selection_criterion="NONE — nothing is selected by this phase",
        outcome={
            "status": SPEC_STATUS,
            "new_strategy_variants_evaluated": 0,
            "new_parameter_sweeps": 0,
            "new_return_calculations": 0,
            "new_holdout_evaluations": 0,
            "strategies_simulated": 0,
            "turnover_calculated": False,
            "code_commit": code_commit,
        },
    )

    complete = {
        "phase": PHASE,
        "phase_kind": "HYPOTHESIS_PREREGISTRATION",
        "code_commit": code_commit,
        "contract_sha256": contract_sha256,
        "hypothesis_id": H2_ID,
        "rule_version": RULE_VERSION,
        "spec_status": SPEC_STATUS,
        "historical_run_classification": HISTORICAL_STATUS,
        "documents": dict(sorted(documents.items())),
        "fingerprints": dict(sorted(fingerprints.items())),
        "strategies_simulated": 0,
        "return_calculations": 0,
        "turnover_calculations": 0,
        "simulator_reachable_from_this_package": simulation_reachable(),
        "next_phase": "R1.5 — EXECUTE THE SINGLE PREREGISTERED H2 EXPLORATORY TEST",
        "limitations": list(record["limitations"]),
    }
    documents["r1_4_complete.json"] = write_document(directory / "r1_4_complete.json", complete)

    return PreregistrationResult(
        binding=binding,
        record=record,
        spec=spec,
        documents=documents,
        fingerprints=fingerprints,
    )


def mirror_r1_4(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the phase's compact metadata and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The preregistration output root.

    Returns:
        One record per mirrored file.

    Raises:
        MirrorConflictError: If a remote copy disagrees or does not read back.
    """
    records: list[MirrorRecord] = []
    for name in MIRRORED_FILES:
        local = directory / name
        if not local.is_file():
            continue
        kind = f"{MIRROR_PREFIX}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, phase=PHASE)
        newest = mirror.newest_snapshot(kind)
        if newest is None or hashlib.sha256(newest[1]).hexdigest() != record.sha256:
            raise MirrorConflictError(f"{name} did not read back from the mirror as mirrored")
        records.append(record)
    return records


def r1_3_directory(control_root: Path) -> Path:
    """Where R1.3 wrote its record beneath a control root.

    Args:
        control_root: The control plane root.

    Returns:
        The R1.3 forensic directory.
    """
    from stock_screener.turnover_forensics.binding import R1_3_DIR

    return control_root / R1_3_DIR
