"""Binding the runner to the frozen protocol, and proving it cannot trade.

Every state-changing operation passes through `bind_runner` first. It verifies
the candidate, the protocol, the prospective contract and the R1.6 marker by
digest, checks that the calendar reproduces the sessions the research already
used, and scans this package's own source for anything resembling an order.

That last check is the one that matters most. A shadow runner that merely does
not call a broker today is one import away from being something else; a shadow
runner that contains no order-submission capability at all cannot become one by
accident. There is no `force` parameter anywhere in this package.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stock_screener.membership_hypothesis import RULE, RULE_VERSION, H2BindingError
from stock_screener.prospective_protocol import (
    CANDIDATE_ID,
    PROSPECTIVE_CONTRACT_ID,
    PROTOCOL_ID,
    REQUIRED_DECISIONS,
    REQUIRED_TRANSITIONS,
    STREAM_ID,
)
from stock_screener.signal_review.binding import file_sha256

from .calendar import CALENDAR_ID, calendar_descriptor, verify_against_history
from .fingerprint import semantic_fingerprint

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

__all__ = [
    "CANDIDATE_SHA256",
    "CONTRACT_SHA256",
    "FORBIDDEN_CAPABILITIES",
    "MARKER_SHA256",
    "PROTOCOL_SHA256",
    "RUNNER_VERSION",
    "RunnerBinding",
    "bind_runner",
    "order_capability_reachable",
]

#: The runner's own version. A change here is a semantic event.
RUNNER_VERSION = "PROSPECTIVE_SHADOW_RUNNER_V1"

#: The R1.6 artifacts, by digest.
CANDIDATE_SHA256 = "a7ace1d1e9226d5d2049879aecd4edba5a6598d24cdcfad01f1d68841ed0d08d"
PROTOCOL_SHA256 = "60c171369407bac9827e0b4146874b774a4b2dba69b79006ca84c727c8320fa3"
CONTRACT_SHA256 = "bcade4125f7d7bbd1424f49699ec15a38187ab63847efdd2da08befab70bb315"
MARKER_SHA256 = "ba028a9f9fdf278039599ba7dad2b963d6c6ae2d9b64b0f0b63ee980907615df"

#: Anything that would give this package the ability to send an order.
FORBIDDEN_CAPABILITIES: tuple[str, ...] = (
    "submit_order",
    "place_order",
    "create_order",
    "TradingClient",
    "OrderRequest",
    "broker",
    "alpaca.trading",
)


class RunnerBindingError(H2BindingError):
    """Raised when the runner may not proceed."""


def order_capability_reachable(package: Path | None = None) -> list[str]:
    """Report anything in this package that could submit an order.

    Capability is a property of code, not of text: a string that mentions a
    broker is data, while an import or a call is an ability. So the check parses
    each module and inspects imported names, attribute accesses and call targets
    rather than grepping, which keeps the guard honest about its own
    declarations.

    Args:
        package: The package directory to scan. Defaults to this package.

    Returns:
        One entry per offending `module: name`, empty when the runner is
        structurally incapable of trading.
    """
    import ast

    root = package or Path(__file__).parent
    found: list[str] = []
    for module in sorted(root.glob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.add(node.module or "")
                names.update(alias.name for alias in node.names)
        found.extend(
            f"{module.name}: {capability}"
            for capability in FORBIDDEN_CAPABILITIES
            if any(capability in name for name in names)
        )
    return found


@dataclass(frozen=True)
class RunnerBinding:
    """Everything verified before the runner changed any state.

    Attributes:
        candidate_sha256: The frozen candidate record.
        protocol_sha256: The frozen protocol.
        contract_sha256: The frozen prospective contract.
        marker_sha256: R1.6's completion marker.
        semantic: The semantic code fingerprint.
        calendar: The frozen calendar descriptor and its historical check.
        checks: One line per verification performed, in order.
    """

    candidate_sha256: str
    protocol_sha256: str
    contract_sha256: str
    marker_sha256: str
    semantic: dict[str, Any]
    calendar: dict[str, Any]
    checks: list[str] = field(default_factory=list)

    def as_json(self) -> dict[str, Any]:
        """Return the binding as a JSON-safe mapping."""
        return {
            "runner_version": RUNNER_VERSION,
            "stream_id": STREAM_ID,
            "candidate_id": CANDIDATE_ID,
            "protocol_id": PROTOCOL_ID,
            "prospective_contract_id": PROSPECTIVE_CONTRACT_ID,
            "rule_version": RULE_VERSION,
            "rule": dict(RULE),
            "candidate_sha256": self.candidate_sha256,
            "protocol_sha256": self.protocol_sha256,
            "prospective_contract_sha256": self.contract_sha256,
            "r1_6_completion_marker_sha256": self.marker_sha256,
            "semantic_code_fingerprint": self.semantic["semantic_fingerprint"],
            "semantic_modules": self.semantic["modules"],
            "calendar": self.calendar,
            "required_decisions": REQUIRED_DECISIONS,
            "required_transitions": REQUIRED_TRANSITIONS,
            "order_capability": "NONE",
            "force_override": "NONE",
            "checks": list(self.checks),
        }


def bind_runner(
    r1_6_directory: Path,
    *,
    historical_sessions: Sequence[date] = (),
    expect_candidate_sha256: str = CANDIDATE_SHA256,
    expect_protocol_sha256: str = PROTOCOL_SHA256,
    expect_contract_sha256: str = CONTRACT_SHA256,
    expect_marker_sha256: str = MARKER_SHA256,
) -> RunnerBinding:
    """Verify the frozen protocol before the runner changes anything.

    Args:
        r1_6_directory: Where R1.6 wrote the candidate, protocol and contract.
        historical_sessions: Benchmark sessions from the frozen dataset, for
            checking the calendar reproduces them. Empty skips that check.
        expect_candidate_sha256: The frozen candidate's digest.
        expect_protocol_sha256: The frozen protocol's digest.
        expect_contract_sha256: The frozen prospective contract's digest.
        expect_marker_sha256: R1.6's completion marker digest.

    Returns:
        The verified binding.

    Raises:
        RunnerBindingError: On any drift, or any order capability.
    """
    checks: list[str] = []
    orders = order_capability_reachable()
    if orders:
        raise RunnerBindingError(
            "the prospective runner is a shadow runner and must not be able to "
            "submit an order: " + ", ".join(orders)
        )
    checks.append("no order-submission capability exists in the runner")

    for label, name, expected in (
        ("candidate", "h2_candidate_freeze.json", expect_candidate_sha256),
        ("protocol", "h2_prospective_protocol_v1.json", expect_protocol_sha256),
        ("prospective contract", "prospective_shadow_contract_v1.json", expect_contract_sha256),
        ("R1.6 completion marker", "r1_6_complete.json", expect_marker_sha256),
    ):
        observed = file_sha256(r1_6_directory / name)
        if observed != expected:
            raise RunnerBindingError(f"the {label} is {observed}, not the frozen one")
        checks.append(f"{label} matched")

    candidate = json.loads((r1_6_directory / "h2_candidate_freeze.json").read_text("utf-8"))
    if candidate["candidate_id"] != CANDIDATE_ID:
        raise RunnerBindingError("the candidate is not the frozen one")
    if candidate["rule_version"] != RULE_VERSION:
        raise RunnerBindingError("the candidate's rule is not the frozen one")
    if dict(candidate["rule"]) != dict(RULE):
        raise RunnerBindingError("the candidate's rule drifted from the frozen rule")
    if "FROZEN_CANDIDATE" not in candidate["state"]:
        raise RunnerBindingError("the candidate is not frozen")
    for key in ("broker_connectivity", "paper_orders", "live_orders", "real_capital"):
        if candidate["authorisations"][key] != "NOT AUTHORISED":
            raise RunnerBindingError(f"{key} is authorised; the runner refuses to run")
    checks.append("candidate frozen, rule unchanged, no trading authorisation")

    protocol = json.loads((r1_6_directory / "h2_prospective_protocol_v1.json").read_text("utf-8"))
    if protocol["protocol_id"] != PROTOCOL_ID:
        raise RunnerBindingError("the protocol is not the frozen one")
    if protocol["data_stream"] != STREAM_ID:
        raise RunnerBindingError("the protocol's stream is not the frozen one")
    if protocol["window"]["required_transitions"] != REQUIRED_TRANSITIONS:
        raise RunnerBindingError("the protocol's window changed")
    if protocol["status_at_freeze"]["prospective_verdict"] != "PENDING":
        raise RunnerBindingError("the protocol already carries a verdict")
    if protocol["execution"]["broker_connectivity"] != "NOT AUTHORISED":
        raise RunnerBindingError("the protocol authorises broker connectivity")
    checks.append("protocol frozen, window intact, verdict PENDING")

    descriptor = calendar_descriptor()
    history = (
        verify_against_history(list(historical_sessions))
        if historical_sessions
        else {"checked": 0, "agrees": None, "reason": "not checked in this call"}
    )
    if historical_sessions and not history["agrees"]:
        raise RunnerBindingError(
            f"the prospective calendar does not reproduce the frozen benchmark sessions: {history}"
        )
    if historical_sessions:
        checks.append(f"calendar {CALENDAR_ID} reproduced {history['checked']} frozen sessions")

    return RunnerBinding(
        candidate_sha256=expect_candidate_sha256,
        protocol_sha256=expect_protocol_sha256,
        contract_sha256=expect_contract_sha256,
        marker_sha256=expect_marker_sha256,
        semantic=semantic_fingerprint(),
        calendar={**descriptor, "historical_check": history},
        checks=checks,
    )
