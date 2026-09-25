"""Holding R1.2 to the specification R1.1 froze before any of this was run.

An experiment that can quietly redefine itself is not an experiment. So the
rules are not written here: they are read out of
`r1_2_experiment_spec.json`, and the executor asserts that every rule it is
about to apply is the rule the specification names. A spec that says something
else — a different frequency, a different fraction, a different cost level —
stops the run rather than being accommodated.

The five falsification criteria are read the same way. The specification states
them and states no way of combining them, so R1.2 reports each separately and
does not invent an aggregate rule after seeing the results.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from stock_screener.signal_review.binding import R1Binding, bind_r1_0, file_sha256

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "EXPECTED_RULES",
    "MONTHLY_TURNOVER_REFERENCE",
    "R1_1_DIR",
    "ExperimentBinding",
    "ExperimentBindingError",
    "bind_experiment",
]

#: Where R1.1 wrote its record, beneath the control root.
R1_1_DIR = "research/r1_1"

#: The monthly turnover the frozen criterion 3 is measured against. Stated in
#: the R1.1 hypothesis record; repeated here only so the comparison is
#: mechanical, and asserted against the record at bind time.
MONTHLY_TURNOVER_REFERENCE = 3.90

#: What the executor is about to do. Every entry must appear verbatim in the
#: frozen specification's rules, or the run stops. This is the mechanism that
#: makes "no deviations" checkable rather than promised.
EXPECTED_RULES: dict[str, str] = {
    "signal": "quarter-end close — the last benchmark session of each calendar quarter",
    "ranking": "frozen COMPOUNDER_V1_2, descending; ties by (kind, identity_id)",
    "selection": "top 10% of the executable cross-section",
    "weighting": "equal weight",
    "execution": "open of the first benchmark session strictly after the signal",
    "rebalance": "calendar quarter ends only",
    "sector_logic": "NONE",
    "leverage": "NONE",
    "shorting": "NONE",
    "cost_sensitivity": "SIMPLE_10BPS_PER_SIDE_V1, unchanged",
    "terminal_policy": "TERMINAL_LEG_POLICY_V1, unchanged",
    "share_policy": "FRACTIONAL_SHARES_RESEARCH_V1, unchanged",
    "benchmark": "SPY, unchanged",
}


class ExperimentBindingError(RuntimeError):
    """The frozen experiment is absent, has moved, or does not say what R1.2 assumes."""


@dataclass(frozen=True)
class ExperimentBinding:
    """The frozen experiment R1.2 executes, and everything it is bound to.

    Attributes:
        spec: The R1.1 experiment specification.
        spec_sha256: Its digest.
        hypothesis: The R1.1 hypothesis record.
        hypothesis_sha256: Its digest.
        r1_1_marker: The R1.1 completion marker.
        r1_1_marker_sha256: Its digest.
        r1_0: The proven R1.0 record.
        criteria: The five falsification criteria, in the order stated.
        aggregate_rule: How the specification says to combine them. None means
            it does not say, and R1.2 must not invent one.
        strategy_version: The one strategy version being executed.
    """

    spec: dict[str, Any]
    spec_sha256: str
    hypothesis: dict[str, Any]
    hypothesis_sha256: str
    r1_1_marker: dict[str, Any]
    r1_1_marker_sha256: str
    r1_0: R1Binding
    criteria: tuple[str, ...]
    aggregate_rule: str | None
    strategy_version: str

    @property
    def rules(self) -> dict[str, Any]:
        """The frozen rules, as the specification states them."""
        return dict(self.spec.get("rules") or {})


def bind_experiment(
    r1_1_directory: Path,
    r1_0_directory: Path,
    *,
    expect_spec_sha: str,
    expect_hypothesis_sha: str,
    expect_r1_1_marker_sha: str,
    expect_r1_0_marker_sha: str,
    expect_split_sha: str,
    expect_preregistration_sha: str,
) -> ExperimentBinding:
    """Prove the frozen experiment and everything under it, or refuse to run.

    Args:
        r1_1_directory: Where R1.1 wrote its record.
        r1_0_directory: Where R1.0 wrote its record.
        expect_spec_sha: The experiment specification's digest.
        expect_hypothesis_sha: The hypothesis record's digest.
        expect_r1_1_marker_sha: The R1.1 completion marker's digest.
        expect_r1_0_marker_sha: The R1.0 completion marker's digest.
        expect_split_sha: The frozen split's digest.
        expect_preregistration_sha: The R1.0 preregistration's digest.

    Returns:
        The proven binding.

    Raises:
        ExperimentBindingError: If a file is absent or has moved, if the
            hypothesis was not accepted, if the specification was already
            executed, or if any rule differs from what the executor applies.
    """
    files = {
        "r1_2_experiment_spec.json": expect_spec_sha,
        "r1_1_hypothesis.json": expect_hypothesis_sha,
        "r1_1_complete.json": expect_r1_1_marker_sha,
    }
    digests: dict[str, str] = {}
    for name, expected in files.items():
        path = r1_1_directory / name
        if not path.is_file():
            raise ExperimentBindingError(f"{path} is absent; there is no frozen experiment")
        actual = file_sha256(path)
        if actual != expected.lower():
            raise ExperimentBindingError(
                f"{name} hashes to {actual[:16]}…, not {expected[:16]}…; refusing to run"
            )
        digests[name] = actual

    spec = json.loads((r1_1_directory / "r1_2_experiment_spec.json").read_text(encoding="utf-8"))
    hypothesis = json.loads((r1_1_directory / "r1_1_hypothesis.json").read_text(encoding="utf-8"))
    marker = json.loads((r1_1_directory / "r1_1_complete.json").read_text(encoding="utf-8"))

    if hypothesis.get("status") != "ACCEPTED_FOR_ONE_EXPERIMENT":
        raise ExperimentBindingError(
            f"the hypothesis is {hypothesis.get('status')}; R1.2 executes only an accepted one"
        )
    if spec.get("executed_in_r1_1") is not False:
        raise ExperimentBindingError("the specification is not marked unexecuted")
    for field, expected_value in (
        ("frequencies_tested", 1),
        ("selection_fractions_tested", 1),
        ("weightings_tested", 1),
        ("execution_rules_tested", 1),
        ("cost_levels_tested", 1),
        ("variants_tried", 1),
    ):
        if spec.get(field) != expected_value:
            raise ExperimentBindingError(
                f"the specification says {field} = {spec.get(field)}; R1.2 runs exactly one"
            )
    if spec.get("parameter_sweeps"):
        raise ExperimentBindingError("the specification carries parameter sweeps; refusing to run")

    rules = dict(spec.get("rules") or {})
    drift = sorted(name for name, value in EXPECTED_RULES.items() if rules.get(name) != value)
    if drift:
        raise ExperimentBindingError(
            f"the executor and the frozen specification disagree on {drift}; refusing to run"
        )

    reference = json.dumps(hypothesis.get("falsification_criteria") or {})
    if f"{MONTHLY_TURNOVER_REFERENCE:.2f}x" not in reference:
        raise ExperimentBindingError(
            "the frozen criteria do not name the monthly turnover reference this run compares "
            "against; refusing to guess it"
        )

    criteria = tuple(
        str(item) for item in (hypothesis.get("falsification_criteria") or {}).get("criteria", ())
    )
    if len(criteria) != 5:
        raise ExperimentBindingError(
            f"the hypothesis states {len(criteria)} falsification criteria; R1.2 expects five"
        )
    aggregate = (hypothesis.get("falsification_criteria") or {}).get("aggregate_rule")

    r1_0 = bind_r1_0(
        r1_0_directory,
        expect_marker_sha=expect_r1_0_marker_sha,
        expect_split_sha=expect_split_sha,
        expect_preregistration_sha=expect_preregistration_sha,
    )
    return ExperimentBinding(
        spec=spec,
        spec_sha256=digests["r1_2_experiment_spec.json"],
        hypothesis=hypothesis,
        hypothesis_sha256=digests["r1_1_hypothesis.json"],
        r1_1_marker=marker,
        r1_1_marker_sha256=digests["r1_1_complete.json"],
        r1_0=r1_0,
        criteria=criteria,
        aggregate_rule=str(aggregate) if aggregate else None,
        strategy_version=str(spec.get("strategy_version")),
    )


def digest_of(document: dict[str, Any]) -> str:
    """The digest a document written by `write_document` will have."""
    return hashlib.sha256(
        (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    ).hexdigest()
