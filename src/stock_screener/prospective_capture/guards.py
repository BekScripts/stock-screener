"""Static guards over the capture package, checked rather than promised.

Three things must stay absent from the path that produces a prospective
decision, and all three are the kind of thing that creeps in during a
well-intentioned refactor rather than arriving deliberately:

- **An outcome.** Nothing selecting a portfolio may read a forward return, a
  later price, a next-session return or any prospective criterion. A decision
  that can see its own result is not a decision.
- **Sector logic.** The frozen candidate is sector-agnostic and the frozen
  contract records sector history as unavailable. A cap or a sector ranking here
  would be a different strategy wearing H2's name.
- **A merged statement.** Reported fundamentals must come from the
  accession-scoped observation read. A merged or current-statement operation
  resolves each period to its latest filed value, so it can carry a figure from a
  filing accepted after the instant and cannot say which filing supplied it.
- **An order.** The execution reference is a recorded price, never an
  instruction. No broker client may be reachable at all.

These are checked by walking the package's syntax trees rather than by grepping,
because a grep over source text flags the declarations that say an order is
forbidden — which is how the first version of this guard failed in R1.7.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = [
    "FORBIDDEN_BROKER_NAMES",
    "FORBIDDEN_FUNDAMENTAL_NAMES",
    "FORBIDDEN_OUTCOME_NAMES",
    "FORBIDDEN_SECTOR_NAMES",
    "GuardViolationError",
    "audit_package",
    "reachable_names",
]


class GuardViolationError(RuntimeError):
    """Raised when the capture path can reach something it must not."""


#: Anything that would let a decision see its own result.
FORBIDDEN_OUTCOME_NAMES: frozenset[str] = frozenset(
    {
        "forward_returns",
        "forward_return",
        "future_price",
        "future_prices",
        "next_session_return",
        "terminal_outcome",
        "realised_return",
        "realized_return",
        "evaluate_prospective",
        "interpret_prospective",
    }
)

#: Sector machinery the frozen candidate does not have and must not acquire.
FORBIDDEN_SECTOR_NAMES: frozenset[str] = frozenset(
    {
        "sector_cap",
        "sector_caps",
        "industry_cap",
        "industry_caps",
        "sector_rank",
        "sector_ranking",
        "sector_attribution",
        "sector_neutral",
        "sector_limit",
    }
)

#: Merged and current-statement reads. Each resolves every period to its most
#: recently filed value, so it can carry a figure from a filing accepted after the
#: decision instant and cannot say which filing supplied it. The frozen
#: `NON_PIT_SAFE_OPERATIONS` names them; this makes them unreachable rather than
#: merely discouraged, because the unsafe aggregate is always the convenient one.
FORBIDDEN_FUNDAMENTAL_NAMES: frozenset[str] = frozenset(
    {
        "get_financial_statements",
        "get_statement_profile_by_cik",
        "companyfacts",
        "build_metrics_as_of",
        "reconstruct_score",
        "reconstruct_historical_score",
    }
)

#: Order capability. The shadow stream places nothing, ever.
FORBIDDEN_BROKER_NAMES: frozenset[str] = frozenset(
    {
        "submit_order",
        "place_order",
        "create_order",
        "TradingClient",
        "AlpacaTrading",
        "OrderRequest",
        "MarketOrderRequest",
        "LimitOrderRequest",
    }
)


def reachable_names(paths: Iterable[Path]) -> set[str]:
    """Every name a module references, by syntax rather than by text.

    Walking the tree means a string literal saying "submit_order is forbidden"
    is not a reference to `submit_order`, while `client.submit_order()` is. That
    distinction is the whole reason this is not a grep.

    Args:
        paths: The Python files to inspect.

    Returns:
        Every identifier, attribute name and imported name referenced.
    """
    found: set[str] = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                found.add(node.id)
            elif isinstance(node, ast.Attribute):
                found.add(node.attr)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    found.update(alias.name.split("."))
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    found.update(node.module.split("."))
                for alias in node.names:
                    found.add(alias.name)
    return found


def audit_package(package: Path | None = None, *, extra: Sequence[Path] = ()) -> dict[str, Any]:
    """Audit the capture package for anything it must not be able to reach.

    Args:
        package: The package directory. Defaults to this one.
        extra: Further files to include in the audit.

    Returns:
        The audit, naming each forbidden class and what was found.

    Raises:
        GuardViolationError: If any forbidden name is reachable.
    """
    root = package or Path(__file__).resolve().parent
    files = sorted({*root.glob("*.py"), *extra})
    names = reachable_names(files)
    findings = {
        "outcome_fields": sorted(names & FORBIDDEN_OUTCOME_NAMES),
        "merged_fundamental_reads": sorted(names & FORBIDDEN_FUNDAMENTAL_NAMES),
        "sector_logic": sorted(names & FORBIDDEN_SECTOR_NAMES),
        "broker_capability": sorted(names & FORBIDDEN_BROKER_NAMES),
    }
    offending = {kind: found for kind, found in findings.items() if found}
    if offending:
        raise GuardViolationError(
            f"the capture path can reach what it must not: {offending}. A decision "
            "that can see an outcome, read a merged statement, apply a sector rule "
            "or place an order is not the frozen candidate."
        )
    return {
        "package": str(root.name),
        "files_audited": len(files),
        "names_referenced": len(names),
        "findings": findings,
        "outcome_isolated": True,
        "fundamentals_are_accession_scoped": True,
        "sector_agnostic": True,
        "broker_requests": 0,
        "method": "AST over Name, Attribute, Import and ImportFrom nodes",
        "why_not_grep": (
            "a text search flags the declaration that forbids an order as if it "
            "were an order; R1.7's first guard failed exactly that way"
        ),
    }
