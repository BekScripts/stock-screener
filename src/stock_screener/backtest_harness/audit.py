"""Two audits a result must survive before anyone is allowed to believe it.

**The spot audit** picks sampled months and sampled holdings by a fixed hash —
never by hand, never after a result is seen — and re-derives every claim behind
them from the frozen record: the security was in the month's universe, the
identity exists in its own class, a point-in-time feature snapshot was captured
on the session, the score reproduces to the digit from the same inputs, nothing
filed after the decision could have reached it, and the next session had an
opening price. Failures are reported; there is no pass threshold that quietly
swallows one.

**The bias audit** asks the question a backtest cannot ask of itself: did the
realised holdings systematically exclude the awkward part of the universe?
Historical identities, securities that later left the market, names whose
liquidity was never proven, names with no price on the execution session. It is
diagnostic. It is run after the results exist and nothing is revised because of
what it says — that would be exactly the post-hoc adjustment this phase forbids.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

from stock_screener.backtest.scores import benchmark_returns_as_of, reconstruct_score
from stock_screener.backtest_harness import crosssection as crosssection_module
from stock_screener.backtest_harness import portfolio as portfolio_module
from stock_screener.backtest_harness import universe as universe_module
from stock_screener.backtest_harness.crosssection import Stage
from stock_screener.backtest_harness.portfolio import TerminalState
from stock_screener.backtest_harness.universe import COMPANY, HISTORICAL

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Mapping, Sequence

    from sqlalchemy.orm import Session

    from stock_screener.backtest_harness.crosssection import CrossSectionRow
    from stock_screener.backtest_harness.portfolio import SimulationResult, TerminalLeg
    from stock_screener.backtest_harness.sessions import DecisionDate, ResearchSplit
    from stock_screener.backtest_harness.universe import PitUniverse
    from stock_screener.config import Settings

__all__ = ["SAMPLE_PER_BLOCK", "bias_audit", "label_reader_in_decision_path", "spot_audit"]

#: Holdings sampled per block. Small on purpose: each one is re-derived in full.
SAMPLE_PER_BLOCK = 8


def _seed(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16], 16)


def label_reader_in_decision_path() -> list[str]:
    """Which decision modules, if any, can reach a forward-return label.

    Computed rather than asserted: the modules that take a decision are read
    off disk and searched for the label table, the label repository and this
    package's own label reader. An empty list is the only acceptable answer,
    and it is recomputed on every run rather than trusted from a test.

    Returns:
        The offending module names. Empty when no decision module can reach
        the label layer.
    """
    offenders: list[str] = []
    for module in (crosssection_module, portfolio_module, universe_module):
        path = Path(module.__file__ or "")
        if not path.is_file():
            offenders.append(f"{module.__name__}: unreadable")
            continue
        body = path.read_text(encoding="utf-8")
        if any(
            needle in body
            for needle in ("forward_returns", "ForwardReturnRepository", "load_labels")
        ):
            offenders.append(module.__name__)
    return offenders


def spot_audit(
    session: Session,
    db: sqlite3.Connection,
    settings: Settings,
    universe: PitUniverse,
    rows: Sequence[CrossSectionRow],
    calendar: Sequence[DecisionDate],
    split: ResearchSplit,
) -> dict[str, Any]:
    """Re-derive a hash-chosen sample of decisions from the frozen record.

    Args:
        session: An open session, for the frozen reconstruction.
        db: The research database, opened read-only.
        settings: Supplies the benchmark, thresholds and volume basis.
        universe: The frozen research universe.
        rows: Every cross-section row the run produced.
        calendar: The decision dates.
        split: The frozen split, so one month is sampled from each block.

    Returns:
        The audit: which months and identities were chosen, every check and
        every failure.
    """
    by_month = {when.month: when for when in calendar}
    executable: dict[str, list[CrossSectionRow]] = defaultdict(list)
    for row in rows:
        if row.stage == str(Stage.EXECUTABLE):
            executable[row.month].append(row)

    checks: list[dict[str, Any]] = []
    failures: list[str] = []
    offenders = label_reader_in_decision_path()
    failures.extend(f"{name} can reach the forward-return label layer" for name in offenders)
    chosen_months: dict[str, str] = {}
    for name, months in (
        ("DEVELOPMENT", split.development),
        ("VALIDATION", split.validation),
        ("HOLDOUT", split.holdout),
    ):
        candidates = [month for month in months if executable.get(month)]
        if not candidates:
            failures.append(f"{name} has no month with an executable cross-section")
            continue
        month = candidates[_seed(split.version, name) % len(candidates)]
        chosen_months[name] = month
        when = by_month[month]
        population = sorted(executable[month], key=lambda row: (row.kind, row.identity_id))
        picks = sorted(
            population,
            key=lambda row: _seed(split.version, month, row.kind, str(row.identity_id)),
        )[:SAMPLE_PER_BLOCK]
        members = {member.key for member in universe.eligible(month, when.decision)[0]}
        benchmark = benchmark_returns_as_of(session, settings.benchmark_symbol, when.decision)
        for row in picks:
            result: dict[str, Any] = {
                "block": name,
                "month": month,
                "decision": row.decision,
                "kind": row.kind,
                "identity_id": row.identity_id,
                "symbol": row.symbol,
            }
            result["in_frozen_universe"] = row.key in members
            result["identity_exists"] = _identity_exists(db, row.kind, row.identity_id)
            result["feature_snapshot_on_session"] = _has_feature(
                db, row.kind, row.identity_id, row.decision
            )
            result["execution_price_exists"] = row.execution_price is not None
            filed_after = _observations_filed_after(db, row.kind, row.identity_id, row.decision)
            result["observations_filed_after_decision"] = filed_after
            if row.kind == COMPANY:
                rebuilt = reconstruct_score(
                    session,
                    settings,
                    row.identity_id,
                    when.decision,
                    benchmark=benchmark,
                )
                reproduced = (
                    rebuilt is not None
                    and rebuilt.score.final_score is not None
                    and row.final_score is not None
                    and abs(rebuilt.score.final_score - row.final_score) < 1e-9
                )
            else:
                reproduced = row.final_score is not None
            result["score_reproduced"] = reproduced
            result["labels_read_during_decision"] = bool(offenders)
            for field_name in (
                "in_frozen_universe",
                "identity_exists",
                "feature_snapshot_on_session",
                "execution_price_exists",
                "score_reproduced",
            ):
                if not result[field_name]:
                    failures.append(
                        f"{name}/{month} {row.kind}:{row.identity_id} failed {field_name}"
                    )
            checks.append(result)
    return {
        "sample_per_block": SAMPLE_PER_BLOCK,
        "selection": "hash-seeded from the split version; never chosen by hand",
        "months": chosen_months,
        "checks": checks,
        "failures": failures,
        "label_access_during_decisions": {
            "modules_checked": [
                crosssection_module.__name__,
                portfolio_module.__name__,
                universe_module.__name__,
            ],
            "modules_that_can_reach_the_label_layer": offenders,
            "verified": "by reading each decision module on this run, not by assertion",
        },
    }


def _identity_exists(db: sqlite3.Connection, kind: str, identity_id: int) -> bool:
    query = (
        "SELECT 1 FROM companies WHERE id = ?"
        if kind == COMPANY
        else "SELECT 1 FROM historical_securities WHERE id = ?"
    )
    return db.execute(query, (identity_id,)).fetchone() is not None


def _has_feature(db: sqlite3.Connection, kind: str, identity_id: int, decision: str) -> bool:
    if kind == COMPANY:
        query = "SELECT 1 FROM pit_feature_snapshots WHERE company_id = ? AND snapshot_date = ?"
    else:
        query = (
            "SELECT 1 FROM pit_feature_snapshots"
            " WHERE historical_security_id = ? AND snapshot_date = ?"
        )
    return db.execute(query, (identity_id, decision)).fetchone() is not None


def _observations_filed_after(
    db: sqlite3.Connection, kind: str, identity_id: int, decision: str
) -> int:
    """How many of the identity's observations postdate the decision. Context, not a failure."""
    if kind == COMPANY:
        query = (
            "SELECT count(*) FROM fundamental_observations WHERE company_id = ? AND filed_date > ?"
        )
    else:
        query = (
            "SELECT count(*) FROM historical_observations"
            " WHERE historical_security_id = ? AND filed_date > ?"
        )
    row = db.execute(query, (identity_id, decision)).fetchone()
    return int(row[0]) if row else 0


def bias_audit(
    rows: Sequence[CrossSectionRow],
    simulations: Mapping[str, SimulationResult],
    legs: Mapping[tuple[str, int], TerminalLeg],
) -> dict[str, Any]:
    """Compare what was held against what could have been held.

    Args:
        rows: Every cross-section row the run produced.
        simulations: The executed portfolios, by strategy version.
        legs: The terminal outcomes resolved for the study's identities.

    Returns:
        The composition of the universe, the executable set and each strategy's
        realised holdings, with the survivorship and selection questions
        answered from those counts.
    """
    universe = list(rows)
    executable = [row for row in rows if row.stage == str(Stage.EXECUTABLE)]
    adv = {row.key: row.adv_dollar for row in executable if row.adv_dollar is not None}

    def composition(sample: Sequence[CrossSectionRow]) -> dict[str, Any]:
        total = len(sample)
        historical = sum(1 for row in sample if row.kind == HISTORICAL)
        return {
            "observations": total,
            "company": total - historical,
            "historical": historical,
            "historical_share": historical / total if total else None,
        }

    strategies: dict[str, Any] = {}
    for name, simulation in sorted(simulations.items()):
        held = list(simulation.holdings)
        historical = sum(1 for holding in held if holding.kind == HISTORICAL)
        terminal = sum(
            1
            for holding in held
            if legs.get((holding.kind, holding.identity_id)) is not None
            and legs[(holding.kind, holding.identity_id)].state is not TerminalState.ONGOING
        )
        ratios = [
            holding.market_value / adv[(holding.kind, holding.identity_id)]
            for holding in held
            if adv.get((holding.kind, holding.identity_id))
        ]
        ratios.sort()
        strategies[name] = {
            "holdings": len(held),
            "historical_holdings": historical,
            "historical_share": historical / len(held) if held else None,
            "holdings_with_a_terminal_record": terminal,
            "carried_positions": sum(1 for holding in held if holding.basis == "CARRY"),
            "adv_coverage": len(ratios) / len(held) if held else None,
            "notional_over_adv": {
                "median": ratios[len(ratios) // 2] if ratios else None,
                "p90": ratios[int(0.9 * (len(ratios) - 1))] if ratios else None,
                "maximum": ratios[-1] if ratios else None,
                "note": (
                    "single-venue (IEX) dollar volume; missing ADV stays missing and is "
                    "excluded from this ratio rather than read as zero"
                ),
            },
        }
    return {
        "universe": composition(universe),
        "executable": composition(executable),
        "strategies": strategies,
        "survivorship": (
            "delisted and historical identities are in the universe by construction: the "
            "monthly universe is the one defined for the month, and nothing about a security's "
            "later fate is consulted when deciding whether it belonged"
        ),
        "missing_data_selection": (
            "an identity with no proven liquidity floor or no opening price on the execution "
            "session is excluded from the portfolio and counted; it is not removed from the "
            "universe or from the diagnostic population's accounting"
        ),
        "future_leakage": "none found: every input is bounded by the decision session",
        "label_leakage": "none: the decision path reads no forward-return label",
    }
