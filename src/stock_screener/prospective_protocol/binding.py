"""Binding R1.6 to the closed historical record, and refusing everything else.

This phase freezes a candidate and writes a protocol. It observes nothing, and
the guards here are what make that checkable rather than merely stated:
`capability_reachable` reads this package's own source and refuses if any module
imports something that could simulate, fetch, or reach a broker. A phase that
defines a future experiment must not be able to start it.

The historical record is verified first and fails closed. A candidate frozen
against a record that moved is a candidate for nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_screener.membership_hypothesis import (
    H2_ID,
    RULE,
    RULE_VERSION,
    H2BindingError,
)
from stock_screener.signal_review.binding import file_sha256

__all__ = [
    "FORBIDDEN_IMPORTS",
    "H2_RECORD_SHA256",
    "R1_5_EVALUATION_SHA256",
    "R1_5_MARKER_SHA256",
    "R1_5_SPEC_SHA256",
    "R1_6_DIR",
    "HistoricalBinding",
    "bind_r1_5_record",
    "capability_reachable",
]

#: Where R1.6 writes its record.
R1_6_DIR = "research/r1_6"

#: The closed historical record, as the R1.6 brief names it.
H2_RECORD_SHA256 = "56f2e51fb74e0eec2a3296431bca80a5ca2dea29729d9568b540c00f690e34ec"
R1_5_SPEC_SHA256 = "6d2664485bffab32260b940f6d532a451139cdb47c5a11b1bec7698040ebb14a"
R1_5_EVALUATION_SHA256 = "4d006e504b08ddd3829b91a9bf3bcfcbbf6aa653b4cd07865fff26c5fa07a371"
R1_5_MARKER_SHA256 = "9fb0f28b641511c7a573b867f78412553efa5487ea5511b13f28f85bd78d9d7a"

#: Names whose presence would mean this phase can run, fetch or trade something.
#: It may do none of those: it writes documents.
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "simulate",
    "PriceBook",
    "Rebalance",
    "performance",
    "benchmark_path",
    "run_experiment",
    "run_h2",
    "load_labels",
    "httpx",
    "requests",
    "alpaca",
    "AlpacaClient",
    "broker",
    "EdgarClient",
    "create_engine",
)


def capability_reachable(package: Path | None = None) -> list[str]:
    """Report any import that would let this phase act rather than describe.

    Args:
        package: The package directory to scan. Defaults to this package.

    Returns:
        One entry per offending `module: name`, empty when nothing can act.
    """
    root = package or Path(__file__).parent
    found: list[str] = []
    for module in sorted(root.glob("*.py")):
        for line in module.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            found.extend(f"{module.name}: {name}" for name in FORBIDDEN_IMPORTS if name in stripped)
    return found


@dataclass(frozen=True)
class HistoricalBinding:
    """The closed historical record a candidate is frozen against.

    Attributes:
        contract_sha256: The backtest data contract's digest.
        h2_record_sha256: The frozen H2 hypothesis record.
        spec_sha256: The frozen R1.5 specification.
        evaluation_sha256: R1.5's criterion evaluation.
        marker_sha256: R1.5's completion marker.
        outcome: R1.5's aggregate verdict, carried forward unchanged.
        measurements: The historical figures the candidate records as references.
        checks: One line per verification performed, in order.
    """

    contract_sha256: str
    h2_record_sha256: str
    spec_sha256: str
    evaluation_sha256: str
    marker_sha256: str
    outcome: str
    measurements: dict[str, Any]
    checks: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "contract_sha256": self.contract_sha256,
            "h2_record_sha256": self.h2_record_sha256,
            "r1_5_experiment_spec_sha256": self.spec_sha256,
            "r1_5_evaluation_sha256": self.evaluation_sha256,
            "r1_5_completion_marker_sha256": self.marker_sha256,
            "r1_5_outcome": self.outcome,
            "historical_measurements": dict(sorted(self.measurements.items())),
            "checks": list(self.checks),
            "phase_kind": "CANDIDATE_FREEZE_AND_PROTOCOL_DEFINITION",
            "observations_collected": 0,
        }


def bind_r1_5_record(
    r1_4_directory: Path,
    r1_5_directory: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
    expect_h2_record_sha256: str = H2_RECORD_SHA256,
    expect_spec_sha256: str = R1_5_SPEC_SHA256,
    expect_evaluation_sha256: str = R1_5_EVALUATION_SHA256,
    expect_marker_sha256: str = R1_5_MARKER_SHA256,
) -> HistoricalBinding:
    """Verify the closed historical record, or refuse to freeze anything.

    Args:
        r1_4_directory: Where R1.4 wrote the H2 record and the R1.5 specification.
        r1_5_directory: Where R1.5 wrote its executed record.
        contract_sha256: The contract digest observed by the caller.
        expect_contract_sha256: The digest the caller requires.
        expect_h2_record_sha256: The frozen H2 record's digest.
        expect_spec_sha256: The frozen specification's digest.
        expect_evaluation_sha256: R1.5's evaluation digest.
        expect_marker_sha256: R1.5's completion marker digest.

    Returns:
        The verified binding.

    Raises:
        H2BindingError: If anything drifted, or R1.5 did not reach SUPPORTED.
    """
    checks: list[str] = []
    if contract_sha256 != expect_contract_sha256:
        raise H2BindingError(f"contract is {contract_sha256}, expected {expect_contract_sha256}")
    checks.append("backtest data contract matched")

    reachable = capability_reachable()
    if reachable:
        raise H2BindingError(
            "R1.6 defines a future experiment and must not be able to run, fetch "
            "or trade: " + ", ".join(reachable)
        )
    checks.append("no simulator, data client or broker is importable from this package")

    for label, path, expected in (
        ("H2 record", r1_4_directory / "h2_membership_stability.json", expect_h2_record_sha256),
        (
            "R1.5 specification",
            r1_4_directory / "r1_5_experiment_spec.json",
            expect_spec_sha256,
        ),
        (
            "R1.5 evaluation",
            r1_5_directory / "r1_5_h2_evaluation.json",
            expect_evaluation_sha256,
        ),
        (
            "R1.5 completion marker",
            r1_5_directory / "r1_5_complete.json",
            expect_marker_sha256,
        ),
    ):
        observed = file_sha256(path)
        if observed != expected:
            raise H2BindingError(f"the {label} is {observed}, not the frozen one")
        checks.append(f"{label} matched")

    evaluation = json.loads((r1_5_directory / "r1_5_h2_evaluation.json").read_text("utf-8"))
    if str(evaluation["hypothesis_id"]) != H2_ID:
        raise H2BindingError(f"the evaluated hypothesis is not {H2_ID}")
    if str(evaluation["rule_version"]) != RULE_VERSION:
        raise H2BindingError(f"the evaluated rule is not {RULE_VERSION}")
    outcome = str(evaluation["interpretation"]["outcome"])
    if outcome != "SUPPORTED":
        raise H2BindingError(f"R1.5 reached {outcome}; a candidate is frozen only on SUPPORTED")
    if bool(evaluation.get("criteria_changed_after_results")):
        raise H2BindingError("the frozen criteria were changed after the results")
    if str(evaluation["historical_test_classification"]) != "EXPLORATORY_HISTORICAL_TEST":
        raise H2BindingError("R1.5's result is not classified as exploratory")
    if str(evaluation["independent_confirmation"]) != "NOT_PROVIDED_BY_R1_5":
        raise H2BindingError("R1.5 must not claim independent confirmation")
    checks.append("R1.5 outcome SUPPORTED, exploratory, confirming nothing")

    if dict(evaluation.get("comparator") or {}).get("strategy_version") != (
        "COMPOUNDER_QUARTERLY_TOP_DECILE_V1"
    ):
        raise H2BindingError("R1.5's comparator is not the frozen one")

    summary = json.loads((r1_5_directory / "r1_5_summary.json").read_text("utf-8"))
    membership = json.loads(
        (r1_5_directory / "r1_5_membership_diagnostics.json").read_text("utf-8")
    )
    performance = json.loads((r1_5_directory / "r1_5_performance.json").read_text("utf-8"))
    measurements = {
        "h2_annual_turnover": float(summary["turnover"]["h2_annual"]),
        "comparator_annual_turnover": float(summary["turnover"]["comparator_annual"]),
        "h2_churn_annual_turnover": float(summary["turnover"]["h2_churn_annual"]),
        "comparator_churn_annual_turnover": float(summary["turnover"]["comparator_churn_annual"]),
        "h2_retention": float(summary["retention"]["h2"]),
        "comparator_retention": float(summary["retention"]["comparator"]),
        "turnover_reduction_fraction": (
            (
                float(summary["turnover"]["comparator_annual"])
                - float(summary["turnover"]["h2_annual"])
            )
            / float(summary["turnover"]["comparator_annual"])
        ),
        "retention_change_absolute": float(membership["retention_change"]["absolute"]),
        "h2_total_return": float(performance["gross"]["total_return"]),
        "comparator_total_return": float(performance["comparator"]["gross"]["total_return"]),
        "h2_sharpe": float(performance["gross"]["sharpe"]),
        "comparator_sharpe": float(performance["comparator"]["gross"]["sharpe"]),
        "criteria_met": list(evaluation["interpretation"]["criteria_met"]),
        "criteria_failed": list(evaluation["interpretation"]["criteria_failed"]),
        "rule": dict(RULE),
    }
    checks.append("historical measurements read for reference, not for thresholds")

    return HistoricalBinding(
        contract_sha256=contract_sha256,
        h2_record_sha256=expect_h2_record_sha256,
        spec_sha256=expect_spec_sha256,
        evaluation_sha256=expect_evaluation_sha256,
        marker_sha256=expect_marker_sha256,
        outcome=outcome,
        measurements=measurements,
        checks=checks,
    )
