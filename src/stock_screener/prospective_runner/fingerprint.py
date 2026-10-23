"""A fingerprint over the code that can change a decision.

A prospective window runs for a year, and code changes during it. Most of those
changes are harmless; some would silently make the fifth decision incomparable
with the first. Remembering which is which is not a reliable control, so the
distinction is computed: every module that can affect the universe, the
features, the score, the ranking, the selection, the target count, identity, the
execution reference or the accounting contributes its source to one digest.

A change to any of them moves the fingerprint and the runner refuses to capture
a confirmatory decision until someone reviews it. A change to logging does not.
The list is therefore load-bearing, and a module added to the selection path and
omitted here would be a real hole — `SEMANTIC_MODULES` is asserted against the
package layout in the tests rather than trusted.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

__all__ = [
    "SEMANTIC_MODULES",
    "compare",
    "semantic_fingerprint",
    "source_root",
]

#: Everything whose behaviour can move a decision. Paths are relative to
#: `src/stock_screener/`, and a missing file is an error rather than a skip.
SEMANTIC_MODULES: tuple[str, ...] = (
    # the frozen rule itself
    "membership_hypothesis/rule.py",
    # the cross-section, the score inputs, the ranking and the target count
    "backtest_harness/crosssection.py",
    "backtest_harness/universe.py",
    "backtest_harness/diagnostics.py",
    "backtest_harness/sessions.py",
    # execution, accounting and the terminal policy
    "backtest_harness/portfolio.py",
    "backtest_harness/binding.py",
    # the prospective path
    "prospective_runner/calendar.py",
    "prospective_runner/packets.py",
    "prospective_runner/selection.py",
    "prospective_protocol/protocol.py",
    "prospective_protocol/contract.py",
    "prospective_protocol/candidate.py",
    # the prospective source plane: what data reaches a decision, and the
    # as-of rule that decides what is admissible
    "prospective_source/envelope.py",
    "prospective_source/adapters.py",
    "prospective_source/readiness.py",
    "prospective_source/governance.py",
    "prospective_source/store.py",
    "prospective_source/window.py",
    # the capture pipeline: what turns sealed snapshots into a decision
    "prospective_capture/readiness.py",
    "prospective_capture/calendar_binding.py",
    "prospective_capture/identity.py",
    "prospective_capture/observations.py",
    "prospective_capture/actions.py",
    "prospective_capture/provenance.py",
    "prospective_capture/score_inputs.py",
    "prospective_capture/scoring.py",
    "prospective_capture/crosssection.py",
    "prospective_capture/package.py",
    "prospective_capture/decision.py",
    "prospective_capture/execution.py",
)


def source_root() -> Path:
    """Where the application package lives.

    Returns:
        The `src/stock_screener` directory.
    """
    return Path(__file__).resolve().parents[1]


def semantic_fingerprint(root: Path | None = None) -> dict[str, Any]:
    """Digest every module that can change a decision.

    Args:
        root: The application package root. Defaults to this installation's.

    Returns:
        The combined digest and each module's own, so a drift can be located
        rather than merely detected.

    Raises:
        FileNotFoundError: If a listed module is absent. A semantic module that
            vanished is a semantic change, not a reason to skip it.
    """
    base = root or source_root()
    per_module: dict[str, str] = {}
    combined = hashlib.sha256()
    for relative in SEMANTIC_MODULES:
        path = base / relative
        if not path.is_file():
            raise FileNotFoundError(f"semantic module missing: {relative}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        per_module[relative] = digest
        combined.update(relative.encode("utf-8"))
        combined.update(digest.encode("utf-8"))
    return {
        "semantic_fingerprint": combined.hexdigest(),
        "modules": dict(sorted(per_module.items())),
        "module_count": len(per_module),
        "covers": (
            "universe, features, score inputs, ranking, selection, target count, "
            "identity, execution reference and accounting"
        ),
        "excludes": (
            "logging, presentation, operational retry and anything else that "
            "cannot change a decision"
        ),
    }


def compare(previous: str, current: str) -> dict[str, Any]:
    """Classify a fingerprint change.

    Args:
        previous: The fingerprint recorded at activation.
        current: The fingerprint now.

    Returns:
        The classification, and whether confirmatory capture may continue.
    """
    unchanged = previous == current
    return {
        "previous": previous,
        "current": current,
        "unchanged": unchanged,
        "classification": ("NONE" if unchanged else "SEMANTIC_RESEARCH_CHANGE"),
        "confirmatory_capture_permitted": unchanged,
        "consequence": (
            None
            if unchanged
            else (
                "the runner refuses to capture a confirmatory decision under V1 "
                "until the change is reviewed. If it really did affect a decision, "
                "the window needs a new candidate, protocol and clock."
            )
        ),
    }
