"""Binding R1.4 to the frozen record, and the gate R1.5 will have to pass.

Two jobs live here. The first is R1.4's own: verify that the R1.0, R1.1, R1.2
and R1.3 records a hypothesis is being derived from are the ones on disk, and
that R1.3's headline measurements still reproduce. A hypothesis derived from a
record that moved is a hypothesis about nothing.

The second is R1.5's, written now rather than later on purpose. The temptation a
preregistration exists to resist arrives at execution time, not at writing time,
so the refusals are fixed here while nothing is yet at stake: one rule, one
band, one frequency, one weighting, one execution rule, one cost level, one run.
`verify_h2` is what R1.5 must call before it may compute anything, and there is
no `force` parameter for the same reason `verify_bindings` has none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stock_screener.signal_review.binding import file_sha256
from stock_screener.turnover_forensics.binding import (
    H1_HYPOTHESIS_SHA256,
    R1_2_EXPERIMENT_SPEC_SHA256,
)

from .record import (
    AGGREGATE_RULE,
    COMPARATOR,
    CRITERIA,
    H2_ID,
    HISTORICAL_STATUS,
    LEGACY_H1_TURNOVER_REFERENCE,
    SPEC_STATUS,
)
from .rule import RETENTION_WIDTHS, RULE, RULE_VERSION

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "FORBIDDEN_IMPORTS",
    "R1_3_EXPECTED",
    "R1_4_DIR",
    "SINGULAR_FIELDS",
    "H2Binding",
    "H2BindingError",
    "bind_r1_3",
    "simulation_reachable",
    "verify_h2",
]

#: Where R1.4 writes its record.
R1_4_DIR = "research/r1_4"

#: What R1.3 measured, as the R1.4 brief names it. A preregistration derived
#: from these numbers is void if the numbers moved.
R1_3_EXPECTED: dict[str, float | int] = {
    "quarterly_annual_turnover": 2.253982668222645,
    "membership_change_floor_annual": 2.1242318895,
    "legacy_turnover_threshold": 1.95,
    "quarterly_retention": 0.457707,
    "monthly_three_month_survival": 0.459757,
    "ranking_churn_share": 0.937946,
    "resizing_share": 0.059780,
    "implementation_defects": 0,
    "accounting_defects": 0,
}

#: Fields of the R1.5 specification that must each carry exactly one value. A
#: sweep is a two in any of them.
SINGULAR_FIELDS: tuple[str, ...] = (
    "frequencies_tested",
    "entry_thresholds_tested",
    "retention_bands_tested",
    "selection_fractions_tested",
    "weightings_tested",
    "execution_rules_tested",
    "cost_levels_tested",
    "variants_tried",
)

#: Names whose presence here would mean R1.4 can run a strategy. It may not:
#: the experiment does not exist yet.
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "simulate",
    "PriceBook",
    "Rebalance",
    "performance",
    "benchmark_path",
    "run_experiment",
    "run_r1_0",
    "load_labels",
)


class H2BindingError(RuntimeError):
    """Raised when H2 cannot be preregistered, or later cannot be executed."""


def simulation_reachable(package: Path | None = None) -> list[str]:
    """Report any import that would let this package price or simulate anything.

    Args:
        package: The package directory to scan. Defaults to this package.

    Returns:
        One entry per offending `module: name`, empty when nothing can run.
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
class H2Binding:
    """Everything verified before a hypothesis was written against it.

    Attributes:
        contract_sha256: The backtest data contract's digest.
        r1_1_hypothesis_sha256: The frozen H1 record.
        r1_2_spec_sha256: The frozen R1.2 specification.
        r1_2_marker_sha256: R1.2's completion marker.
        r1_3_marker_sha256: R1.3's completion marker.
        r1_3_measurements: The R1.3 values H2 rests on, re-read and matched.
        comparator: The frozen comparator figures H2 will be judged against.
        checks: One line per verification performed, in order.
    """

    contract_sha256: str
    r1_1_hypothesis_sha256: str
    r1_2_spec_sha256: str
    r1_2_marker_sha256: str
    r1_3_marker_sha256: str
    r1_3_measurements: dict[str, float]
    comparator: dict[str, Any]
    checks: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "contract_sha256": self.contract_sha256,
            "r1_1_hypothesis_sha256": self.r1_1_hypothesis_sha256,
            "r1_2_experiment_spec_sha256": self.r1_2_spec_sha256,
            "r1_2_completion_marker_sha256": self.r1_2_marker_sha256,
            "r1_3_completion_marker_sha256": self.r1_3_marker_sha256,
            "r1_3_measurements": dict(sorted(self.r1_3_measurements.items())),
            "comparator": dict(sorted(self.comparator.items())),
            "checks": list(self.checks),
            "phase_kind": "HYPOTHESIS_PREREGISTRATION",
        }


def _close(observed: float, expected: float, tolerance: float) -> bool:
    """Whether a re-read measurement still matches what the brief names."""
    return abs(observed - expected) <= tolerance


def bind_r1_3(
    r1_1_directory: Path,
    r1_2_directory: Path,
    r1_3_directory: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
) -> H2Binding:
    """Verify the record H2 is derived from, or refuse to derive anything.

    Args:
        r1_1_directory: Where R1.1 wrote the H1 record and the R1.2 specification.
        r1_2_directory: Where R1.2 wrote its record.
        r1_3_directory: Where R1.3 wrote its forensic record.
        contract_sha256: The contract digest observed by the caller.
        expect_contract_sha256: The digest the caller requires.

    Returns:
        The verified binding, carrying the comparator H2 will be judged against.

    Raises:
        H2BindingError: If any digest, status or measurement does not hold.
    """
    checks: list[str] = []
    if contract_sha256 != expect_contract_sha256:
        raise H2BindingError(f"contract is {contract_sha256}, expected {expect_contract_sha256}")
    checks.append("backtest data contract matched")

    reachable = simulation_reachable()
    if reachable:
        raise H2BindingError(
            "R1.4 preregisters an experiment and must not be able to run one: "
            + ", ".join(reachable)
        )
    checks.append("no simulator or pricing path is importable from this package")

    hypothesis_sha = file_sha256(r1_1_directory / "r1_1_hypothesis.json")
    spec_sha = file_sha256(r1_1_directory / "r1_2_experiment_spec.json")
    if hypothesis_sha != H1_HYPOTHESIS_SHA256:
        raise H2BindingError(f"the H1 record is {hypothesis_sha}, not the frozen one")
    if spec_sha != R1_2_EXPERIMENT_SPEC_SHA256:
        raise H2BindingError(f"the R1.2 specification is {spec_sha}, not the frozen one")
    checks.append("R1.1 hypothesis and R1.2 specification matched")

    evaluation = json.loads((r1_2_directory / "r1_2_hypothesis_evaluation.json").read_text("utf-8"))
    if str(evaluation["interpretation"]["outcome"]) != "PARTIALLY_SUPPORTED_BY_R1_2":
        raise H2BindingError("R1.2's interpretation is not the one R1.3 explained")
    checks.append("R1.2 interpretation unchanged")

    forensic = json.loads((r1_3_directory / "r1_3_h1_forensic_evaluation.json").read_text("utf-8"))
    if str(forensic["h1_final_status"]["status"]) != "H1_MECHANISTICALLY_FALSIFIED":
        raise H2BindingError("H1 is not closed as mechanistically falsified; H2 does not follow")
    if bool(forensic["mechanism"]["implementation_defect"]):
        raise H2BindingError("R1.3 found an implementation defect; H2 does not follow")
    if bool(forensic["mechanism"]["accounting_defect"]):
        raise H2BindingError("R1.3 found an accounting defect; H2 does not follow")
    if str(forensic["mechanism"]["primary"]) != (
        "M2_REBALANCE_FREQUENCY_REDUCTION_OFFSET_BY_MEMBERSHIP_DRIFT"
    ):
        raise H2BindingError("R1.3's mechanism is not the one H2 addresses")
    checks.append("H1 closed, no defect, mechanism is membership drift")

    summary = json.loads((r1_3_directory / "r1_3_summary.json").read_text("utf-8"))
    floor = json.loads((r1_3_directory / "r1_3_turnover_lower_bound.json").read_text("utf-8"))
    reconciliation = json.loads(
        (r1_3_directory / "r1_3_turnover_reconciliation.json").read_text("utf-8")
    )
    shares = reconciliation["decomposition"]["share_of_all_traded_notional"]
    measured = {
        "quarterly_annual_turnover": float(summary["turnover"]["quarterly_annual"]),
        "membership_change_floor_annual": float(floor["floor"]["membership_change_floor_annual"]),
        "legacy_turnover_threshold": float(floor["floor"]["frozen_threshold"]),
        "quarterly_retention": float(summary["membership"]["quarterly_retention"]),
        "monthly_three_month_survival": float(summary["membership"]["monthly_survival"]["lag_3"]),
        "ranking_churn_share": float(reconciliation["decomposition"]["ranking_churn_share"]),
        "resizing_share": float(shares["resize"]),
        "implementation_defects": 0.0,
        "accounting_defects": 0.0,
    }
    for name, expected in R1_3_EXPECTED.items():
        tolerance = 1e-12 if name == "quarterly_annual_turnover" else 1e-6
        if not _close(measured[name], float(expected), tolerance):
            raise H2BindingError(f"R1.3's {name} is {measured[name]}, the brief names {expected}")
        checks.append(f"R1.3 {name} reproduced")

    churn_annual = measured["quarterly_annual_turnover"] * measured["ranking_churn_share"]
    comparator = {
        "strategy_version": COMPARATOR,
        "annual_turnover": measured["quarterly_annual_turnover"],
        "ranking_churn_share": measured["ranking_churn_share"],
        "ranking_churn_annual_turnover": churn_annual,
        "resizing_share": measured["resizing_share"],
        "quarterly_retention": measured["quarterly_retention"],
        "rebalances": 17,
        "measured_by": "R1.2, re-derived and verified by R1.3",
    }

    return H2Binding(
        contract_sha256=contract_sha256,
        r1_1_hypothesis_sha256=hypothesis_sha,
        r1_2_spec_sha256=spec_sha,
        r1_2_marker_sha256=file_sha256(r1_2_directory / "r1_2_complete.json"),
        r1_3_marker_sha256=file_sha256(r1_3_directory / "r1_3_complete.json"),
        r1_3_measurements=measured,
        comparator=comparator,
        checks=checks,
    )


def verify_h2(
    record: Mapping[str, Any],
    spec: Mapping[str, Any],
    *,
    record_sha256: str,
    spec_sha256: str,
    expect_record_sha256: str,
    expect_spec_sha256: str,
    executor_rules: Mapping[str, str],
) -> list[str]:
    """The gate R1.5 must pass before it may compute anything.

    Written here rather than in R1.5 because the pressure a preregistration
    exists to resist arrives at execution time. There is no `force` parameter.

    Args:
        record: The H2 hypothesis record, as stored.
        spec: The R1.5 experiment specification, as stored.
        record_sha256: The record's digest as read.
        spec_sha256: The specification's digest as read.
        expect_record_sha256: The digest the caller requires of the record.
        expect_spec_sha256: The digest the caller requires of the specification.
        executor_rules: What the executor is about to apply, stated by the
            executor itself rather than read from the specification.

    Returns:
        One line per check performed, in order.

    Raises:
        H2BindingError: On any drift, sweep, relabelling or second run.
    """
    checks: list[str] = []
    if record_sha256 != expect_record_sha256:
        raise H2BindingError(f"the H2 record is {record_sha256}, not the frozen one")
    if spec_sha256 != expect_spec_sha256:
        raise H2BindingError(f"the R1.5 specification is {spec_sha256}, not the frozen one")
    checks.append("H2 record and R1.5 specification matched their frozen digests")

    if str(record.get("hypothesis_id")) != H2_ID:
        raise H2BindingError(f"the record is not {H2_ID}")
    if str(spec.get("tests_hypothesis")) != H2_ID:
        raise H2BindingError(f"the specification does not test {H2_ID}")
    if str(spec.get("hypothesis_sha256")) != record_sha256:
        raise H2BindingError("the specification binds to a different H2 record")
    checks.append("specification binds to this exact hypothesis")

    if bool(spec.get("executed")) or str(spec.get("status")) != SPEC_STATUS:
        raise H2BindingError("this specification has already been executed")
    if int(spec.get("runs_permitted", 0)) != 1:
        raise H2BindingError("an experiment specification permits exactly one run")
    checks.append("specification is unexecuted and permits exactly one run")

    for name in SINGULAR_FIELDS:
        value = spec.get(name)
        if value != 1:
            raise H2BindingError(f"{name} is {value!r}; H2 contains exactly one parameterisation")
    if spec.get("parameter_sweeps"):
        raise H2BindingError("a parameter sweep is not H2")
    if str(spec.get("optimization_performed")) != "NONE":
        raise H2BindingError("H2 is not an optimisation")
    checks.append(f"no sweep: {len(SINGULAR_FIELDS)} fields each carry exactly one value")

    stored_rules = dict(spec.get("rules") or {})
    if stored_rules != dict(RULE):
        differing = sorted(
            name
            for name in set(stored_rules) | set(RULE)
            if stored_rules.get(name) != RULE.get(name)
        )
        raise H2BindingError(f"the stored rule drifted from the frozen one: {differing}")
    if dict(executor_rules) != dict(RULE):
        differing = sorted(
            name
            for name in set(executor_rules) | set(RULE)
            if executor_rules.get(name) != RULE.get(name)
        )
        raise H2BindingError(f"the executor would apply different rules: {differing}")
    if str(spec.get("rule_version")) != RULE_VERSION:
        raise H2BindingError(f"the rule version is not {RULE_VERSION}")
    if int(record.get("retention_widths", 0)) != RETENTION_WIDTHS:
        raise H2BindingError("the retention band is not two selection widths")
    checks.append(f"all {len(RULE)} frozen rules matched the executor verbatim")

    criteria = list(spec.get("criteria") or [])
    if criteria != list(CRITERIA):
        raise H2BindingError(f"the criteria are {criteria}, not the four frozen ones")
    if dict(spec.get("aggregate_rule") or {}) != dict(AGGREGATE_RULE):
        raise H2BindingError("the aggregate rule drifted from the frozen one")
    checks.append("four frozen criteria and the frozen aggregate rule matched")

    legacy = dict(spec.get("legacy_h1_turnover_reference") or {})
    if float(legacy.get("value", 0.0)) != LEGACY_H1_TURNOVER_REFERENCE:
        raise H2BindingError("the legacy H1 reference was moved")
    if bool(legacy.get("is_a_criterion")):
        raise H2BindingError("1.95x is a continuity diagnostic, never an H2 criterion")
    checks.append("legacy 1.95x present as a diagnostic and not as a criterion")

    comparator = dict(spec.get("comparator") or {})
    if str(comparator.get("strategy_version")) != COMPARATOR:
        raise H2BindingError(f"the comparator is not {COMPARATOR}")
    checks.append(f"comparator is {COMPARATOR}")

    status = dict(spec.get("historical_evidence_status") or {})
    if str(spec.get("result_classification")) != HISTORICAL_STATUS:
        raise H2BindingError(f"a historical run must be classified {HISTORICAL_STATUS}")
    if status.get("clean_h2_holdout_remains") is not False:
        raise H2BindingError("no clean H2 holdout exists; the record must say so")
    if status.get("holdout_reset"):
        raise H2BindingError("a historical subperiod may not be relabelled as a holdout")
    if not status.get("CONFIRMATION_REQUIRED_AFTER_EXPLORATORY_TEST"):
        raise H2BindingError("independent confirmation is required and must be recorded")
    checks.append("historical evidence classified exploratory; no holdout reset")

    return checks
