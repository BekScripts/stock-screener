"""What was declared before anything was measured, and the ledger of every run.

Three artifacts, written in one order, and the order is the evidence:

1. **The baseline registry** names every strategy this phase will execute and
   what each one is, before any of them is executed.
2. **The preregistration** freezes the definitions, the parameters, the
   execution and cost assumptions, the metric set, the split boundaries, the
   benchmark and the terminal-leg policy, and is hashed. Nothing may be
   altered afterwards and presented as the same version.
3. **The research ledger** records every run that happened — including the
   ones whose results were unattractive, because a record that keeps only the
   flattering half is not a record.

The holdout is gated on (2). `first_holdout_access` writes the access event and
refuses when the preregistration hash it is handed is not the hash of the file
on disk, so a retroactive preregistration cannot be slipped in behind a result
that has already been seen.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from stock_screener.backtest_harness.diagnostics import (
    DECILES,
    HORIZONS,
    MIN_DECILE_CROSS_SECTION,
    PRIMARY_HORIZON,
)
from stock_screener.backtest_harness.performance import RISK_FREE_RATE, SESSIONS_PER_YEAR
from stock_screener.backtest_harness.portfolio import (
    GROSS,
    MIN_CROSS_SECTION,
    SHARE_POLICY,
    SIMPLE_10BPS_PER_SIDE,
    STARTING_CAPITAL,
    TERMINAL_LEG_POLICY,
)
from stock_screener.backtest_harness.sessions import EXECUTION_RULE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

__all__ = [
    "BASELINES",
    "LEDGER_FILE",
    "MARKET_SIGNALS_READINESS",
    "METRICS_REPORTED",
    "PREREGISTRATION_FILE",
    "REGISTRY_FILE",
    "PreregistrationError",
    "ResearchLedger",
    "baseline_registry",
    "digest_document",
    "first_holdout_access",
    "preregistration",
    "run_manifest",
    "validate_manifest",
    "write_document",
]

REGISTRY_FILE = "baseline_registry.json"
PREREGISTRATION_FILE = "r1_0_preregistration.json"
LEDGER_FILE = "research_ledger.jsonl"
HOLDOUT_ACCESS_FILE = "holdout_access.json"

#: Every metric a run reports, fixed before any run produces one.
METRICS_REPORTED = (
    "total_return",
    "cagr",
    "volatility",
    "sharpe",
    "sortino",
    "max_drawdown",
    "calmar",
    "turnover",
    "rebalances",
    "average_holdings",
    "minimum_holdings",
    "maximum_holdings",
    "cash_utilisation",
    "benchmark_total_return",
    "benchmark_cagr",
    "benchmark_relative_return",
    "tracking_difference",
)


#: The R1.0 readiness verdict for the frozen market-signal layer. It emits
#: multi-label setups, a three-state tradability verdict and peer percentiles,
#: and defines no canonical standalone scalar ranking; inventing one would be a
#: new strategy rather than an evaluation of a frozen component.
MARKET_SIGNALS_READINESS = "MARKET_SIGNALS_V1_REQUIRES_R1_1_HYPOTHESIS_DEFINITION"
CANONICAL_STANDALONE_RANKING = "NO_CANONICAL_STANDALONE_RANKING"


class PreregistrationError(RuntimeError):
    """A holdout was about to be read without a frozen preregistration behind it."""


#: The strategies R1.0 executes. Declared here, executed nowhere else, and
#: never parameterised at a call site.
BASELINES: dict[str, dict[str, object]] = {
    "BASELINE_PASSIVE_V1": {
        "mode": "REFERENCE",
        "what": "The frozen benchmark series, bought at the first execution open and held.",
        "selection": "the benchmark identity itself",
        "weighting": "one position",
        "rebalance": "none",
        "why": "A reference to measure against, not an active strategy.",
    },
    "BASELINE_UNIVERSE_EQUAL_WEIGHT_V1": {
        "mode": "PORTFOLIO_BACKTEST",
        "what": "Every executable name in the historical point-in-time universe, equally weighted.",
        "selection": "the whole executable cross-section",
        "weighting": "equal weight, long only",
        "rebalance": "monthly, at the open after the month's last session",
        "why": "Measures whether ranking adds anything beyond owning what was available.",
    },
    "COMPOUNDER_DECILES_V1": {
        "mode": "CROSS_SECTIONAL_SIGNAL_DIAGNOSTICS",
        "what": (
            f"The executable cross-section cut into {DECILES} deciles by the frozen "
            "COMPOUNDER_V1_2 score, low to high, against the frozen forward-return labels."
        ),
        "selection": "no selection; every decile is reported",
        "weighting": "not applicable — a diagnostic, not a portfolio",
        "rebalance": "not applicable",
        "why": "Asks whether a higher frozen score is associated with different outcomes.",
    },
    "COMPOUNDER_TOP_DECILE_V1": {
        "mode": "PORTFOLIO_BACKTEST",
        "what": "The top tenth of the executable cross-section by the frozen COMPOUNDER_V1_2.",
        "selection": "top 10% by score, descending; ties broken by (kind, identity_id)",
        "weighting": "equal weight, long only",
        "rebalance": "monthly, at the open after the month's last session",
        "why": "The simplest portfolio the frozen score can express.",
    },
}


def digest_document(document: Mapping[str, Any]) -> str:
    """Hash a document exactly as it is written: sorted keys, two-space indent.

    Args:
        document: The document.

    Returns:
        Its lowercase hexadecimal SHA-256.
    """
    return hashlib.sha256(_serialise(document).encode("utf-8")).hexdigest()


def _serialise(document: Mapping[str, Any]) -> str:
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def write_document(path: Path, document: Mapping[str, Any]) -> str:
    """Write a document deterministically and return its digest.

    Args:
        path: The destination.
        document: What to write.

    Returns:
        The file's SHA-256.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_serialise(document), encoding="utf-8")
    return digest_document(document)


def baseline_registry(*, dataset_id: str, contract_version: str) -> dict[str, object]:
    """The strategy registry, written before any performance is calculated.

    Args:
        dataset_id: The dataset the strategies read.
        contract_version: The contract they honour.

    Returns:
        The registry document.
    """
    return {
        "dataset_id": dataset_id,
        "contract_version": contract_version,
        "phase": "R1.0",
        "strategies": {name: dict(sorted(body.items())) for name, body in BASELINES.items()},
        "count": len(BASELINES),
        "market_signals_baseline": "DEFERRED",
        "market_signals_v1_readiness": MARKET_SIGNALS_READINESS,
        "market_signals_v1_canonical_ranking": CANONICAL_STANDALONE_RANKING,
        "market_signals_reason": (
            "MARKET_SIGNALS_V1 emits multi-label setups, a three-state tradability verdict and "
            "peer percentiles. It defines no canonical standalone scalar ranking, and inventing "
            "one here would be a new strategy rather than an evaluation of a frozen component."
        ),
    }


def preregistration(
    *,
    dataset_id: str,
    contract_version: str,
    contract_sha256: str,
    split_sha256: str,
    split: Mapping[str, Any],
    registry_sha256: str,
    benchmark: str,
    code_commit: str,
    bindings: Mapping[str, str],
) -> dict[str, object]:
    """Freeze every definition the holdout will be read under.

    Args:
        dataset_id: The frozen dataset.
        contract_version: The contract honoured.
        contract_sha256: The contract's digest.
        split_sha256: The frozen split's digest.
        split: The split document, so boundaries are inside the hash.
        registry_sha256: The baseline registry's digest.
        benchmark: The benchmark identity.
        code_commit: The commit the run was defined at.
        bindings: Every frozen input digest the run presents.

    Returns:
        The preregistration document.
    """
    return {
        "phase": "R1.0",
        "dataset_id": dataset_id,
        "contract_version": contract_version,
        "contract_sha256": contract_sha256,
        "baseline_registry_sha256": registry_sha256,
        "split_sha256": split_sha256,
        "split": dict(split),
        "code_commit": code_commit,
        "bindings": dict(sorted(bindings.items())),
        "benchmark": benchmark,
        "strategies": {name: dict(sorted(body.items())) for name, body in BASELINES.items()},
        "execution": {
            "rule": EXECUTION_RULE,
            "signal": "close of the last benchmark session of the month",
            "fill": "open of the first benchmark session strictly after the signal",
            "share_policy": SHARE_POLICY,
            "starting_capital": STARTING_CAPITAL,
            "long_only": True,
            "leverage": "NONE",
            "shorting": "NONE",
            "maximum_target_weight": 1.0,
            "minimum_cross_section": MIN_CROSS_SECTION,
            "empty_cross_section_policy": "HOLD_CASH_FOR_THE_PERIOD",
            "unpriced_selection_policy": "UNPRICED_NOT_EXECUTED; never backfilled",
        },
        "costs": {
            "primary": GROSS.name,
            "sensitivity": SIMPLE_10BPS_PER_SIDE.name,
            "basis_points_per_side": SIMPLE_10BPS_PER_SIDE.bps_per_side,
            "existing_frozen_convention": "NONE_FOUND",
            "statement": (
                "A sensitivity assumption, not a claim about real execution. One level only; "
                "no other level is tested. The repository holds no frozen cost convention, so "
                "this one is preregistered here."
            ),
        },
        "terminal_leg_policy": {
            "version": TERMINAL_LEG_POLICY,
            "settled": "liquidated at the stated consideration on its effective date",
            "extinguished": "liquidated at zero, on a reported wind-up only",
            "unknown": (
                "carried at the last observed close, flagged, and reported beside a floor in "
                "which every unknown leg is worth nothing"
            ),
            "never": "a falling price is never read as a settlement",
        },
        "diagnostics": {
            "primary_horizon": PRIMARY_HORIZON,
            "secondary_horizons": [h for h in HORIZONS if h != PRIMARY_HORIZON],
            "deciles": DECILES,
            "minimum_decile_cross_section": MIN_DECILE_CROSS_SECTION,
            "predefined_contrast": "D10 - D1",
            "labels": "the frozen forward_returns; never recomputed",
            "uncertainty_unit": "months",
        },
        "metrics": list(METRICS_REPORTED),
        "risk_free_rate": RISK_FREE_RATE,
        "sessions_per_year": SESSIONS_PER_YEAR,
        "prohibited": [
            "parameter search",
            "threshold search",
            "top-N search",
            "signal combination",
            "sector or industry logic",
            "holdout tuning",
            "post-result rule adjustment",
        ],
        "variants_tried": 1,
        "parameter_sweeps": [],
        "selection_criterion": (
            "NONE — every strategy in this phase is preregistered and none is selected "
            "against a result."
        ),
    }


def first_holdout_access(
    directory: Path, *, preregistration_sha256: str, strategies: Sequence[str]
) -> dict[str, object]:
    """Record the first holdout evaluation, refusing an unfrozen preregistration.

    Args:
        directory: The research output root.
        preregistration_sha256: The digest the caller believes is frozen.
        strategies: The strategy versions about to read the holdout.

    Returns:
        The access event, which is also written to the output root.

    Raises:
        PreregistrationError: If no preregistration is on disk, or the file
            does not hash to what the caller presented.
    """
    path = directory / PREREGISTRATION_FILE
    if not path.is_file():
        raise PreregistrationError(
            "the holdout may not be read before a preregistration exists on disk"
        )
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != preregistration_sha256:
        raise PreregistrationError(
            f"the preregistration hashes to {actual[:16]}…, not {preregistration_sha256[:16]}…; "
            "refusing to read the holdout"
        )
    event: dict[str, object] = {
        "event": "FIRST_HOLDOUT_ACCESS",
        "preregistration_sha256": actual,
        "strategies": sorted(strategies),
        "ordering_proof": "the preregistration existed and hashed as recorded before this event",
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    (directory / HOLDOUT_ACCESS_FILE).write_text(
        json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return event


@dataclass
class ResearchLedger:
    """Every run this phase performed, appended in order and never rewritten.

    Args:
        path: The ledger file.
    """

    path: Path

    def record(
        self,
        *,
        strategy: str,
        mode: str,
        why: str,
        parameters: Mapping[str, Any],
        development: bool,
        validation: bool,
        holdout: bool,
        variants_tried: int,
        parameter_sweeps: Sequence[str],
        selection_criterion: str,
        outcome: Mapping[str, Any],
    ) -> dict[str, object]:
        """Append one run and return the entry.

        Args:
            strategy: The strategy version.
            mode: `PORTFOLIO_BACKTEST` or `CROSS_SECTIONAL_SIGNAL_DIAGNOSTICS`.
            why: Why the run happened.
            parameters: Its parameters.
            development: Whether a development result exists.
            validation: Whether a validation result exists.
            holdout: Whether the holdout was viewed.
            variants_tried: How many variants were tried. One means one.
            parameter_sweeps: Sweeps performed. Empty means none.
            selection_criterion: What, if anything, selected this version.
            outcome: A compact summary, kept whether or not it is attractive.

        Returns:
            The entry as written.
        """
        entry: dict[str, object] = {
            "strategy_version": strategy,
            "mode": mode,
            "why": why,
            "parameters": dict(sorted(parameters.items())),
            "development_result": development,
            "validation_result": validation,
            "holdout_viewed": holdout,
            "variants_tried": variants_tried,
            "parameter_sweeps": list(parameter_sweeps),
            "selection_criterion": selection_criterion,
            "outcome": dict(sorted(outcome.items())),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry


def run_manifest(
    *,
    run_id: str,
    strategy: str,
    code_commit: str,
    dataset_id: str,
    contract_version: str,
    universe_fingerprint: str,
    feature_fingerprints: Mapping[str, str],
    price_fingerprint: str,
    label_fingerprint: str,
    rebalance_rule: str,
    cost: Mapping[str, Any],
    date_range: Mapping[str, str],
    split: Mapping[str, Any],
    parameters: Mapping[str, Any],
    benchmark: str,
    outputs: Mapping[str, str],
    metrics: Mapping[str, Any],
) -> dict[str, object]:
    """Build one run manifest conforming to `backtest_run_manifest_schema.json`.

    Args:
        run_id: The run's identity.
        strategy: The strategy version.
        code_commit: The commit the code was at.
        dataset_id: The dataset read.
        contract_version: The contract honoured.
        universe_fingerprint: The frozen universe projection digest.
        feature_fingerprints: The feature projections' digests.
        price_fingerprint: The frozen price projection digest.
        label_fingerprint: The frozen label projection digest.
        rebalance_rule: How often, and on what.
        cost: The transaction-cost assumption.
        date_range: Start and end.
        split: The development, validation and holdout boundaries.
        parameters: The strategy's parameters.
        benchmark: The benchmark identity.
        outputs: Semantic fingerprints of what the run produced.
        metrics: The headline metrics.

    Returns:
        The manifest.
    """
    return {
        "run_id": run_id,
        "strategy_version": strategy,
        "code_commit": code_commit,
        "dataset_id": dataset_id,
        "contract_version": contract_version,
        "universe_fingerprint": universe_fingerprint,
        "feature_fingerprints": dict(sorted(feature_fingerprints.items())),
        "price_data_fingerprint": price_fingerprint,
        "label_fingerprint": label_fingerprint,
        "rebalance_rule": rebalance_rule,
        "execution_rule": EXECUTION_RULE,
        "transaction_cost_assumptions": dict(cost),
        "terminal_leg_policy": TERMINAL_LEG_POLICY,
        "date_range": dict(date_range),
        "development_validation_holdout": dict(split),
        "parameters": dict(sorted(parameters.items())),
        "variants_tried": 1,
        "parameter_sweeps": [],
        "selection_criterion": "NONE — preregistered; not selected against a result",
        "benchmark": benchmark,
        "output_fingerprints": dict(sorted(outputs.items())),
        "metrics": dict(metrics),
    }


def validate_manifest(manifest: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Check a manifest against the frozen schema, returning every failure.

    Covers the constructs the schema actually uses — `required`, `type`,
    `const`, `enum`, `pattern`, `minimum` and property schemas — and says so
    rather than pretending to be a general validator.

    Args:
        manifest: The manifest to check.
        schema: The frozen schema.

    Returns:
        Human-readable failures. Empty means valid.
    """
    problems: list[str] = []
    for name in schema.get("required", []):
        if name not in manifest:
            problems.append(f"missing required field {name}")
    properties = schema.get("properties", {})
    for name, rule in properties.items():
        if name not in manifest:
            continue
        problems.extend(_check(name, manifest[name], rule))
    return problems


_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "object": dict,
    "array": list,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
}


def _check(name: str, value: Any, rule: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    expected = rule.get("type")
    if isinstance(expected, str) and expected in _TYPES and not isinstance(value, _TYPES[expected]):
        problems.append(f"{name} is not a {expected}")
        return problems
    if "const" in rule and value != rule["const"]:
        problems.append(f"{name} must be {rule['const']!r}, not {value!r}")
    if "enum" in rule and value not in rule["enum"]:
        problems.append(f"{name} must be one of {rule['enum']}, not {value!r}")
    pattern = rule.get("pattern")
    if isinstance(pattern, str) and isinstance(value, str) and not re.fullmatch(pattern, value):
        problems.append(f"{name} does not match {pattern}")
    minimum = rule.get("minimum")
    if isinstance(minimum, int | float) and isinstance(value, int | float) and value < minimum:
        problems.append(f"{name} is below {minimum}")
    for required in rule.get("required", []):
        if isinstance(value, dict) and required not in value:
            problems.append(f"{name} is missing {required}")
    child = rule.get("additionalProperties")
    if isinstance(child, dict) and isinstance(value, dict):
        for key, item in value.items():
            problems.extend(_check(f"{name}.{key}", item, child))
    return problems
