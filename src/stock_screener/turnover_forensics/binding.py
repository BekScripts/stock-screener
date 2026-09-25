"""Binding R1.3 to the frozen R1.0, R1.1 and R1.2 record.

R1.3 is a forensic phase: it explains a number another phase produced. That
only means anything if the number cannot have moved in the meantime, so nothing
here trusts the files on disk. Every artifact R1.3 reads is re-digested and
compared against the fingerprint the producing phase stored, and the R1.1
hypothesis and the R1.2 experiment specification are additionally checked
against the digests the R1.3 brief names.

There is no `force` parameter and no way to repair a mismatch. A mismatch means
the record moved, and a forensic account of a record that moved is not a
forensic account of R1.2.

One further guard lives here rather than in a test, because it is the whole
reason this package is separate from the others: `simulation_reachable` reads
this package's own source and refuses if any module imports a simulator. R1.3
may recompute diagnostics from frozen ledgers; it may not execute a strategy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from stock_screener.signal_review.binding import (
    LEDGER_FINGERPRINTS as R1_0_LEDGERS,
)
from stock_screener.signal_review.binding import (
    file_sha256,
    ledger_fingerprint,
    read_jsonl,
)

__all__ = [
    "FORBIDDEN_IMPORTS",
    "H1_HYPOTHESIS_SHA256",
    "R1_2_ARTEFACTS",
    "R1_2_DIR",
    "R1_2_EXPERIMENT_SPEC_SHA256",
    "R1_2_LEDGERS",
    "R1_3_DIR",
    "ForensicBinding",
    "ForensicBindingError",
    "bind_r1_2",
    "simulation_reachable",
]

#: Where R1.2 wrote its record, beneath the control root.
R1_2_DIR = "research/r1_2"

#: Where R1.3 writes its own.
R1_3_DIR = "research/r1_3"

#: The frozen H1 record, as the R1.3 brief names it.
H1_HYPOTHESIS_SHA256 = "a64268efec41f8edffea8c2756def1fd3465b9eb0fa986ad394fd276785eed00"

#: The frozen R1.2 experiment specification, as the R1.3 brief names it.
R1_2_EXPERIMENT_SPEC_SHA256 = "1b30916d7b131ce816209a92c39e0cc3125b777e70ea499e8c27f2d14f2a5ce4"

#: `{fingerprint name: file}` for the R1.2 documents R1.3 reads.
R1_2_ARTEFACTS: dict[str, str] = {
    "metrics": "quarterly_metrics.json",
    "matched_window": "monthly_matched_window_comparison.json",
    "decile_diagnostics": "quarterly_decile_diagnostics.json",
    "attrition": "quarterly_sample_attrition.json",
    "hypothesis_evaluation": "r1_2_hypothesis_evaluation.json",
    "summary": "r1_2_summary.json",
    "run_manifest": "r1_2_run_manifest.json",
    "experiment_binding": "r1_2_experiment_binding.json",
}

#: `{fingerprint name: file}` for the R1.2 ledgers R1.3 reads.
R1_2_LEDGERS: dict[str, str] = {
    "trades": "quarterly_trade_ledger.jsonl",
    "holdings": "quarterly_holdings_ledger.jsonl",
    "nav": "quarterly_nav.jsonl",
    "selection": "quarterly_selection.jsonl",
    "quarter_calendar": "quarterly_decision_calendar.jsonl",
    "cross_sections": "quarterly_cross_sections.jsonl",
}

#: Module-level names whose presence would mean R1.3 can execute a strategy.
FORBIDDEN_IMPORTS: tuple[str, ...] = (
    "simulate",
    "Rebalance",
    "PriceBook",
    "run_r1_0",
    "run_experiment",
)


class ForensicBindingError(RuntimeError):
    """Raised when the frozen record R1.3 explains is not the record on disk."""


@dataclass(frozen=True)
class ForensicBinding:
    """Everything R1.3 verified before reading a single number.

    Attributes:
        contract_sha256: The backtest data contract's digest.
        r1_0_ledgers: `{name: digest}` re-derived from R1.0's ledgers.
        r1_2_documents: `{name: digest}` re-derived from R1.2's documents.
        r1_2_ledgers: `{name: digest}` re-derived from R1.2's ledgers.
        hypothesis_sha256: The frozen H1 record's digest.
        experiment_spec_sha256: The frozen R1.2 specification's digest.
        h1_status: R1.2's interpretation, carried forward unchanged.
        failed_criterion: The identifier of the criterion R1.2 found unmet.
        checks: One line per verification performed, in order.
    """

    contract_sha256: str
    r1_0_ledgers: dict[str, str]
    r1_2_documents: dict[str, str]
    r1_2_ledgers: dict[str, str]
    hypothesis_sha256: str
    experiment_spec_sha256: str
    h1_status: str
    failed_criterion: str
    checks: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "contract_sha256": self.contract_sha256,
            "r1_0_ledgers": dict(sorted(self.r1_0_ledgers.items())),
            "r1_2_documents": dict(sorted(self.r1_2_documents.items())),
            "r1_2_ledgers": dict(sorted(self.r1_2_ledgers.items())),
            "hypothesis_sha256": self.hypothesis_sha256,
            "experiment_spec_sha256": self.experiment_spec_sha256,
            "h1_status_entering_r1_3": self.h1_status,
            "failed_criterion": self.failed_criterion,
            "checks": list(self.checks),
            "phase_kind": "FORENSIC_ANALYSIS",
        }


def simulation_reachable(package: Path | None = None) -> list[str]:
    """Report any import in this package that would let R1.3 run a strategy.

    The check reads source rather than introspecting a loaded module, so it sees
    an import that exists even when the code path holding it never runs.

    Args:
        package: The package directory to scan. Defaults to this package.

    Returns:
        One entry per offending `module: name`, empty when R1.3 cannot simulate.
    """
    root = package or Path(__file__).parent
    found: list[str] = []
    for module in sorted(root.glob("*.py")):
        text = module.read_text(encoding="utf-8")
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            for name in FORBIDDEN_IMPORTS:
                if name in stripped:
                    found.append(f"{module.name}: {name}")
    return found


def _digest_documents(directory: Path, files: dict[str, str]) -> dict[str, str]:
    """Digest each named document by its own bytes."""
    digests: dict[str, str] = {}
    for name, relative in files.items():
        path = directory / relative
        if not path.exists():
            raise ForensicBindingError(f"{name} is missing at {path}")
        digests[name] = file_sha256(path)
    return digests


def _digest_ledgers(directory: Path, files: dict[str, str]) -> dict[str, str]:
    """Digest each named ledger over its parsed rows."""
    digests: dict[str, str] = {}
    for name, relative in files.items():
        path = directory / relative
        if not path.exists():
            raise ForensicBindingError(f"{name} is missing at {path}")
        digests[name] = ledger_fingerprint(read_jsonl(path))
    return digests


def _compare(
    observed: dict[str, str], expected: dict[str, Any], label: str, checks: list[str]
) -> None:
    """Fail closed on any digest that differs from the stored fingerprint."""
    for name in sorted(observed):
        stored = expected.get(name)
        if stored is None:
            raise ForensicBindingError(f"{label} has no stored fingerprint for {name}")
        if stored != observed[name]:
            raise ForensicBindingError(
                f"{label} {name} is {observed[name]}, the record says {stored}"
            )
        checks.append(f"{label}.{name} re-derived and matched")


def bind_r1_2(
    r1_0_directory: Path,
    r1_2_directory: Path,
    r1_1_directory: Path,
    *,
    contract_sha256: str,
    expect_contract_sha256: str,
) -> ForensicBinding:
    """Verify every frozen artifact R1.3 is about to read, or refuse to read it.

    Args:
        r1_0_directory: Where R1.0 wrote its record.
        r1_2_directory: Where R1.2 wrote its record.
        r1_1_directory: Where R1.1 wrote the hypothesis and the specification.
        contract_sha256: The contract digest observed by the caller.
        expect_contract_sha256: The digest the caller requires.

    Returns:
        The verified binding.

    Raises:
        ForensicBindingError: If any digest, status or guard does not hold.
    """
    checks: list[str] = []
    if contract_sha256 != expect_contract_sha256:
        raise ForensicBindingError(
            f"contract is {contract_sha256}, expected {expect_contract_sha256}"
        )
    checks.append("backtest data contract matched")

    reachable = simulation_reachable()
    if reachable:
        raise ForensicBindingError(
            "R1.3 is a forensic phase and must not be able to simulate: " + ", ".join(reachable)
        )
    checks.append("no simulator is importable from the forensic package")

    hypothesis_path = r1_1_directory / "r1_1_hypothesis.json"
    spec_path = r1_1_directory / "r1_2_experiment_spec.json"
    hypothesis_sha = file_sha256(hypothesis_path)
    spec_sha = file_sha256(spec_path)
    if hypothesis_sha != H1_HYPOTHESIS_SHA256:
        raise ForensicBindingError(
            f"H1 record is {hypothesis_sha}, expected {H1_HYPOTHESIS_SHA256}"
        )
    if spec_sha != R1_2_EXPERIMENT_SPEC_SHA256:
        raise ForensicBindingError(
            f"R1.2 specification is {spec_sha}, expected {R1_2_EXPERIMENT_SPEC_SHA256}"
        )
    checks.append("R1.1 hypothesis and R1.2 specification matched")

    hypothesis = json.loads(hypothesis_path.read_text(encoding="utf-8"))
    if hypothesis.get("status") != "ACCEPTED_FOR_ONE_EXPERIMENT":
        raise ForensicBindingError("the H1 record is not the accepted one")

    stored_r1_0 = json.loads(
        (r1_0_directory / "r1_0_fingerprints.json").read_text(encoding="utf-8")
    )["fingerprints"]
    r1_0_ledgers = _digest_ledgers(r1_0_directory, dict(R1_0_LEDGERS))
    _compare(r1_0_ledgers, stored_r1_0, "R1.0", checks)

    stored_r1_2 = json.loads(
        (r1_2_directory / "r1_2_fingerprints.json").read_text(encoding="utf-8")
    )["fingerprints"]
    documents = _digest_documents(r1_2_directory, R1_2_ARTEFACTS)
    ledgers = _digest_ledgers(r1_2_directory, R1_2_LEDGERS)
    _compare(documents, stored_r1_2, "R1.2", checks)
    _compare(ledgers, stored_r1_2, "R1.2", checks)

    evaluation = json.loads(
        (r1_2_directory / "r1_2_hypothesis_evaluation.json").read_text(encoding="utf-8")
    )
    status = str(evaluation["interpretation"]["outcome"])
    if status != "PARTIALLY_SUPPORTED_BY_R1_2":
        raise ForensicBindingError(
            f"R1.2 interpreted H1 as {status}; R1.3 explains a partial result"
        )
    unmet = [int(row["number"]) for row in evaluation["criteria"] if not bool(row["met"])]
    if unmet != [3]:
        raise ForensicBindingError(
            f"R1.3 explains the turnover premise; R1.2 found {unmet or 'nothing'} unmet"
        )
    if bool(evaluation.get("criteria_changed_after_results")):
        raise ForensicBindingError("the frozen criteria were changed after the results")
    checks.append("R1.2 interpretation is PARTIALLY_SUPPORTED_BY_R1_2 with C3 unmet")

    return ForensicBinding(
        contract_sha256=contract_sha256,
        r1_0_ledgers=r1_0_ledgers,
        r1_2_documents=documents,
        r1_2_ledgers=ledgers,
        hypothesis_sha256=hypothesis_sha,
        experiment_spec_sha256=spec_sha,
        h1_status=status,
        failed_criterion=f"C{unmet[0]}_TURNOVER_HALVES",
        checks=checks,
    )
