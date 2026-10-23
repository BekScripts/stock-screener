"""R1.6 — freeze the candidate, write the protocol, and start nothing.

The phase produces seven documents and no observation. It cannot produce one:
`capability_reachable` scans this package's imports for anything that could
simulate, fetch or reach a broker, and `bind_r1_5_record` refuses before reading
a value if it finds any.

What the phase is really doing is spending the last of what history can offer
and writing down, in advance, what would count as evidence from somewhere else.
Everything historical is closed here — the parameter search, the variants, the
possibility of a clean holdout — and the protocol that replaces it is fixed
before the first observation exists to shape it.
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
from stock_screener.membership_hypothesis import H2_ID, RULE_VERSION
from stock_screener.sec_evidence.mirror import MirrorConflictError

from .binding import HistoricalBinding, bind_r1_5_record, capability_reachable
from .candidate import CANDIDATE_ID, HISTORICAL_CLASSIFICATION, candidate_freeze
from .contract import PROSPECTIVE_CONTRACT_ID, prospective_contract
from .protocol import (
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
    STREAM_ID,
    evaluate_prospective,
    interpret_prospective,
    prospective_protocol,
)
from .schemas import decision_packet_schema, evaluation_schema, ledger_schema

if TYPE_CHECKING:
    from pathlib import Path

    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "MIRRORED_FILES",
    "MIRROR_GENERATION",
    "MIRROR_PREFIX",
    "PHASE",
    "PROTOCOL_DIR",
    "FreezeResult",
    "freeze_candidate_and_protocol",
    "mirror_r1_6",
    "r1_5_directory",
]

PHASE = "R1.6"
PROTOCOL_DIR = "research/r1_6"
MIRROR_GENERATION = "PIT_RESEARCH_DATASET_V2"
MIRROR_PREFIX = "research/r1-6"

#: Where prospective observations will be mirrored once they exist. Nothing is
#: written here by this phase; the prefix is declared so the separation is
#: established before the first observation rather than after it.
PROSPECTIVE_MIRROR_PREFIX = "control/PROSPECTIVE_SHADOW_STREAM_V1/h2/"

MIRRORED_FILES = (
    "r1_6_binding.json",
    "h2_candidate_freeze.json",
    "prospective_shadow_contract_v1.json",
    "h2_prospective_protocol_v1.json",
    "prospective_decision_packet_schema.json",
    "prospective_research_ledger_schema.json",
    "prospective_evaluation_schema.json",
    "r1_6_fingerprints.json",
    "r1_6_summary.json",
    "r1_6_complete.json",
)

LIMITATIONS = (
    "COMPOUNDER_V1_2 reads an undated classification internally; the exposure "
    "remains material and unresolved",
    "sector and industry history is unavailable; H2 is sector-agnostic and no "
    "result is attributed to a sector",
    "exchange and listing membership is partly inferred",
    "market capitalisation is a lower bound where a share count is a cover-page figure",
    "average daily volume is single-venue (IEX) and partial",
    "the historical support for H2 is EXPLORATORY_SUPPORT_ONLY and cannot be "
    "upgraded by any further work on the frozen dataset",
    "four prospective transitions can establish a mechanism effect and cannot "
    "establish a return effect",
    "the legacy validation block trailed its benchmark by 7.3 percentage points "
    "under both the comparator and H2, and nothing here addresses that",
    "spinoffs are unsupported historically and remain unsupported prospectively",
)


@dataclass
class FreezeResult:
    """What R1.6 produced.

    Attributes:
        binding: The verified historical record.
        candidate: The frozen candidate record.
        protocol: The frozen prospective protocol.
        documents: `{name: digest}` for every document written.
        fingerprints: The semantic fingerprints determinism is checked on.
        summary: The phase summary.
    """

    binding: HistoricalBinding
    candidate: dict[str, Any] = field(default_factory=dict)
    protocol: dict[str, Any] = field(default_factory=dict)
    documents: dict[str, str] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


def freeze_candidate_and_protocol(
    directory: Path,
    r1_4_directory: Path,
    r1_5_path: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
    code_commit: str,
) -> FreezeResult:
    """Freeze the H2 candidate and the prospective protocol. Observe nothing.

    Args:
        directory: Where to write R1.6's record.
        r1_4_directory: R1.4's preregistration.
        r1_5_path: R1.5's executed record.
        contract_sha256: The observed contract digest.
        expect_contract_sha256: The digest the caller requires.
        code_commit: The commit this was frozen at.

    Returns:
        The freeze result.

    Raises:
        H2BindingError: If the historical record does not verify.
    """
    binding = bind_r1_5_record(
        r1_4_directory,
        r1_5_path,
        contract_sha256=contract_sha256,
        expect_contract_sha256=expect_contract_sha256,
    )
    directory.mkdir(parents=True, exist_ok=True)
    log.info("r1.6.bound", checks=len(binding.checks))

    candidate = candidate_freeze(
        binding=binding.as_json(), protocol_id=PROTOCOL_ID, code_commit=code_commit
    )
    protocol = prospective_protocol(code_commit=code_commit)
    contract = prospective_contract()
    packet_schema = decision_packet_schema()
    ledger = ledger_schema()
    evaluation = evaluation_schema()

    pending = evaluate_prospective(completed_transitions=0)
    verdict = interpret_prospective(pending)
    if verdict["outcome"] != "PENDING":
        raise RuntimeError("a protocol with no observations cannot have a verdict")

    documents: dict[str, str] = {}
    documents["r1_6_binding.json"] = write_document(
        directory / "r1_6_binding.json", binding.as_json()
    )
    documents["h2_candidate_freeze.json"] = write_document(
        directory / "h2_candidate_freeze.json", candidate
    )
    documents["prospective_shadow_contract_v1.json"] = write_document(
        directory / "prospective_shadow_contract_v1.json", contract
    )
    documents["h2_prospective_protocol_v1.json"] = write_document(
        directory / "h2_prospective_protocol_v1.json", protocol
    )
    documents["prospective_decision_packet_schema.json"] = write_document(
        directory / "prospective_decision_packet_schema.json", packet_schema
    )
    documents["prospective_research_ledger_schema.json"] = write_document(
        directory / "prospective_research_ledger_schema.json", ledger
    )
    documents["prospective_evaluation_schema.json"] = write_document(
        directory / "prospective_evaluation_schema.json", evaluation
    )

    summary = {
        "phase": PHASE,
        "phase_kind": "CANDIDATE_FREEZE_AND_PROTOCOL_DEFINITION",
        "code_commit": code_commit,
        "candidate_id": CANDIDATE_ID,
        "hypothesis_id": H2_ID,
        "rule_version": RULE_VERSION,
        "candidate_state": list(candidate["state"]),
        "historical_classification": HISTORICAL_CLASSIFICATION,
        "historical_outcome": binding.outcome,
        "historical_development": "CLOSED",
        "why_frozen": candidate["why_this_is_frozen"]["reason"],
        "not_why_frozen": candidate["why_this_is_frozen"]["not_the_reason"],
        "protocol_id": PROTOCOL_ID,
        "prospective_contract_id": PROSPECTIVE_CONTRACT_ID,
        "prospective_stream": STREAM_ID,
        "prospective_mirror_prefix": PROSPECTIVE_MIRROR_PREFIX,
        "required_decisions": REQUIRED_DECISIONS,
        "required_transitions": REQUIRED_TRANSITIONS,
        "criteria": [row["id"] for row in protocol["criteria"]],
        "aggregate_rule": protocol["aggregate_rule"]["expression"],
        "historical_magnitude_required": False,
        "performance_is_a_criterion": False,
        "status": dict(protocol["status_at_freeze"]),
        "observations_collected": 0,
        "completed_transitions": 0,
        "prospective_verdict": verdict["outcome"],
        "independent_confirmation": "PENDING",
        "authorisations": dict(candidate["authorisations"]),
        "historical_dataset_mutated": False,
        "limitations": list(LIMITATIONS),
        "language": (
            "research-state only. Nothing is confirmed, validated, production ready "
            "or authorised for capital, and no prospective observation exists."
        ),
    }
    documents["r1_6_summary.json"] = write_document(directory / "r1_6_summary.json", summary)

    fingerprints = {
        "binding": digest_document(binding.as_json()),
        "candidate_freeze": digest_document(candidate),
        "prospective_contract": digest_document(contract),
        "protocol": digest_document(protocol),
        "decision_packet_schema": digest_document(packet_schema),
        "ledger_schema": digest_document(ledger),
        "evaluation_schema": digest_document(evaluation),
        "summary": digest_document(summary),
    }
    documents["r1_6_fingerprints.json"] = write_document(
        directory / "r1_6_fingerprints.json",
        {"fingerprints": dict(sorted(fingerprints.items()))},
    )

    ResearchLedger(directory / "research_ledger.jsonl").record(
        strategy=CANDIDATE_ID,
        mode="CANDIDATE_FREEZE_AND_PROTOCOL_DEFINITION",
        why=(
            "freeze the H2 candidate on its preregistered mechanism result and "
            "define the prospective protocol that could provide independent "
            "mechanism evidence. Nothing is observed or executed here."
        ),
        parameters={
            "candidate_id": CANDIDATE_ID,
            "rule_version": RULE_VERSION,
            "protocol_id": PROTOCOL_ID,
            "required_transitions": REQUIRED_TRANSITIONS,
        },
        development=False,
        validation=False,
        holdout=False,
        variants_tried=0,
        parameter_sweeps=[],
        selection_criterion="NONE — nothing is selected by this phase",
        outcome={
            "candidate_state": "FROZEN_CANDIDATE",
            "historical_classification": HISTORICAL_CLASSIFICATION,
            "prospective_verdict": "PENDING",
            "observations_collected": 0,
            "new_historical_variants": 0,
            "parameter_sweeps": 0,
            "broker_connections": 0,
            "orders_placed": 0,
            "data_requests": 0,
            "code_commit": code_commit,
        },
    )

    documents["r1_6_complete.json"] = write_document(
        directory / "r1_6_complete.json",
        {
            "phase": PHASE,
            "phase_kind": "CANDIDATE_FREEZE_AND_PROTOCOL_DEFINITION",
            "code_commit": code_commit,
            "contract_sha256": contract_sha256,
            "candidate_id": CANDIDATE_ID,
            "protocol_id": PROTOCOL_ID,
            "candidate_state": list(candidate["state"]),
            "historical_classification": HISTORICAL_CLASSIFICATION,
            "independent_confirmation": "PENDING",
            "documents": dict(sorted(documents.items())),
            "fingerprints": dict(sorted(fingerprints.items())),
            "observations_collected": 0,
            "completed_transitions": 0,
            "prospective_runner": "NOT_YET_ACTIVE",
            "first_eligible_decision": "NOT_YET_ASSIGNED",
            "capability_reachable_from_this_package": capability_reachable(),
            "next_phase": ("R1.7 — BUILD AND ACTIVATE THE PROSPECTIVE H2 SHADOW RUNNER"),
            "limitations": list(LIMITATIONS),
        },
    )

    return FreezeResult(
        binding=binding,
        candidate=candidate,
        protocol=protocol,
        documents=documents,
        fingerprints=fingerprints,
        summary=summary,
    )


def mirror_r1_6(mirror: ControlMirror, directory: Path) -> list[MirrorRecord]:
    """Mirror the frozen protocol definition and read each copy back.

    Args:
        mirror: The control mirror, opened on the dataset's namespace.
        directory: The protocol output root.

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


def r1_5_directory(control_root: Path) -> Path:
    """Where R1.5 wrote its executed record beneath a control root.

    Args:
        control_root: The control plane root.

    Returns:
        The R1.5 directory.
    """
    from stock_screener.hysteresis_experiment import EXPERIMENT_DIR

    return control_root / EXPERIMENT_DIR
