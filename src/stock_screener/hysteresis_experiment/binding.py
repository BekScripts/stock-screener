"""Binding R1.5 to the preregistration it is about to spend.

R1.4 wrote the gate; this module calls it. `verify_h2` refuses a moved record, a
drifted rule, a second parameterisation, a relabelled holdout and a specification
already executed, and it is called before a single price is read.

The one thing R1.4 could not do from where it stood is stop a second execution,
because that needs durable state on disk. `ExecutionLock` supplies it: a marker
written beside the outputs before the run begins, recording what was executed and
when. A second attempt finds it and stops. The preregistered specification itself
is never rewritten — a document that gains a result afterwards is no longer the
document that was frozen — so the lock and the results live in separate files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import (
    COMPARATOR,
    H2_ID,
    RULE,
    RULE_VERSION,
    H2BindingError,
    verify_h2,
)
from stock_screener.signal_review.binding import file_sha256
from stock_screener.turnover_forensics.binding import bind_r1_2

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "EXECUTION_MARKER",
    "EXPERIMENT_DIR",
    "H2_RECORD_SHA256",
    "R1_5_SPEC_SHA256",
    "STRATEGY",
    "ExecutionLock",
    "ExperimentBinding",
    "bind_r1_5",
]

#: Where R1.5 writes its record.
EXPERIMENT_DIR = "research/r1_5"

#: The strategy version this experiment executes.
STRATEGY = "COMPOUNDER_QUARTERLY_HYSTERESIS_V1"

#: The file whose existence means this specification has already been spent.
EXECUTION_MARKER = "r1_5_execution_state.json"

#: The frozen preregistration, as the R1.5 brief names it.
H2_RECORD_SHA256 = "56f2e51fb74e0eec2a3296431bca80a5ca2dea29729d9568b540c00f690e34ec"
R1_5_SPEC_SHA256 = "6d2664485bffab32260b940f6d532a451139cdb47c5a11b1bec7698040ebb14a"


@dataclass
class ExecutionLock:
    """Durable state preventing a preregistered experiment from running twice.

    Args:
        path: The marker file.
    """

    path: Path

    def claim(self, *, spec_sha256: str, code_commit: str) -> dict[str, Any]:
        """Record that this specification is being spent, or refuse.

        Args:
            spec_sha256: The specification being executed.
            code_commit: The commit executing it.

        Returns:
            The state as written.

        Raises:
            H2BindingError: If the marker already exists for this specification.
        """
        if self.path.exists():
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            raise H2BindingError(
                f"the R1.5 specification was already executed at "
                f"{existing.get('executed_at')} under commit "
                f"{existing.get('code_commit')}; it permits exactly one run"
            )
        state = {
            "experiment_id": "R1.5",
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "spec_sha256": spec_sha256,
            "status": "EXECUTED",
            "executed_at": datetime.now(UTC).isoformat(),
            "code_commit": code_commit,
            "runs_permitted": 1,
            "runs_executed": 1,
            "note": (
                "the preregistered specification is not rewritten. It stays as it "
                "was frozen; this marker records that it has been spent, and the "
                "results live in their own artifacts."
            ),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return state

    def release(self) -> None:
        """Remove the marker after a run that did not complete.

        A specification is spent by a finished experiment, not by a crash.
        """
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class ExperimentBinding:
    """Everything verified before the experiment read a price.

    Attributes:
        contract_sha256: The backtest data contract's digest.
        h2_record_sha256: The frozen H2 hypothesis record.
        spec_sha256: The frozen R1.5 specification.
        r1_4_marker_sha256: R1.4's completion marker.
        comparator: The frozen R1.2 figures H2 is judged against.
        rules: The rules the executor asserted and the binder matched.
        checks: One line per verification performed, in order.
    """

    contract_sha256: str
    h2_record_sha256: str
    spec_sha256: str
    r1_4_marker_sha256: str
    comparator: dict[str, Any]
    rules: dict[str, str]
    checks: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "contract_sha256": self.contract_sha256,
            "h2_record_sha256": self.h2_record_sha256,
            "r1_5_spec_sha256": self.spec_sha256,
            "r1_4_completion_marker_sha256": self.r1_4_marker_sha256,
            "comparator": dict(sorted(self.comparator.items())),
            "rules": dict(sorted(self.rules.items())),
            "checks": list(self.checks),
            "strategy_version": STRATEGY,
            "hypothesis_id": H2_ID,
            "rule_version": RULE_VERSION,
            "phase_kind": "PREREGISTERED_EXPLORATORY_HISTORICAL_EXPERIMENT",
            "executable_variants": 1,
        }


def bind_r1_5(
    r1_0_directory: Path,
    r1_1_directory: Path,
    r1_2_directory: Path,
    r1_3_directory: Path,
    r1_4_directory: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
    expect_h2_record_sha256: str = H2_RECORD_SHA256,
    expect_spec_sha256: str = R1_5_SPEC_SHA256,
) -> ExperimentBinding:
    """Verify every frozen artifact, then the preregistration itself.

    Args:
        r1_0_directory: R1.0's frozen record.
        r1_1_directory: R1.1's frozen hypothesis and specification.
        r1_2_directory: R1.2's frozen record, the comparator.
        r1_3_directory: R1.3's frozen forensic record.
        r1_4_directory: R1.4's preregistration.
        contract_sha256: The contract digest observed by the caller.
        expect_contract_sha256: The digest the caller requires.
        expect_h2_record_sha256: The frozen H2 record's digest.
        expect_spec_sha256: The frozen specification's digest.

    Returns:
        The verified binding.

    Raises:
        H2BindingError: If anything drifted, or the preregistration is not intact.
    """
    checks: list[str] = []
    upstream = bind_r1_2(
        r1_0_directory,
        r1_2_directory,
        r1_1_directory,
        contract_sha256=contract_sha256,
        expect_contract_sha256=expect_contract_sha256,
    )
    checks.extend(upstream.checks)

    forensic = json.loads((r1_3_directory / "r1_3_h1_forensic_evaluation.json").read_text("utf-8"))
    if str(forensic["h1_final_status"]["status"]) != "H1_MECHANISTICALLY_FALSIFIED":
        raise H2BindingError("R1.3's record moved; H1 is not closed as it was")
    checks.append("R1.3 forensic record unchanged")

    record_path = r1_4_directory / "h2_membership_stability.json"
    spec_path = r1_4_directory / "r1_5_experiment_spec.json"
    record_sha = file_sha256(record_path)
    spec_sha = file_sha256(spec_path)
    record = json.loads(record_path.read_text("utf-8"))
    spec = json.loads(spec_path.read_text("utf-8"))

    checks.extend(
        verify_h2(
            record,
            spec,
            record_sha256=record_sha,
            spec_sha256=spec_sha,
            expect_record_sha256=expect_h2_record_sha256,
            expect_spec_sha256=expect_spec_sha256,
            executor_rules=RULE,
        )
    )

    comparator = dict(spec["comparator"])
    if str(comparator.get("strategy_version")) != COMPARATOR:
        raise H2BindingError(f"the comparator is not {COMPARATOR}")
    stored = json.loads((r1_2_directory / "quarterly_metrics.json").read_text("utf-8"))["gross"][
        "turnover"
    ]["annual_estimate"]
    if abs(float(stored) - float(comparator["annual_turnover"])) > 1e-12:
        raise H2BindingError(
            "the comparator's turnover does not match R1.2's own metric; the "
            "experiment would be measured against a rebuilt variant"
        )
    checks.append("comparator turnover matched R1.2's stored metric exactly")

    return ExperimentBinding(
        contract_sha256=contract_sha256,
        h2_record_sha256=record_sha,
        spec_sha256=spec_sha,
        r1_4_marker_sha256=file_sha256(r1_4_directory / "r1_4_complete.json"),
        comparator=comparator,
        rules=dict(RULE),
        checks=checks,
    )
