"""Rendering rankings and score explanations.

Three outputs, three audiences. The table is for skimming the top of a ranking
in a terminal; the CSV is for the manual review the specification calls the most
important step of the project, which happens in a spreadsheet; the explanation
is the answer to "why did this company rank here", printed for one company.

The explanation is rendered from the stored breakdown by validating it back into
a `CompanyScore`, so the CLI reads the same structure the API serves and neither
parses a formatted string.

Missing values render as `-` in the table and as an **empty cell** in the CSV,
never as `0`. A blank is excluded from a spreadsheet average; a zero drags it
down, which would corrupt exactly the review the export exists to support.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from domain import CompanyScore, MetricUnit
from stock_screener.scanning.report import format_money, format_percent, format_points

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from domain import ComponentScore, SubScore
    from stock_screener.scoring.rankings import RankingRow, ScoreDetail

log = structlog.get_logger(__name__)

_MISSING = "-"

_TABLE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Rank", 6),
    ("Ticker", 8),
    ("Score", 7),
    ("Raw", 7),
    ("Risk", 7),
    ("Growth", 8),
    ("Quality", 9),
    ("Valuation", 11),
    ("Momentum", 10),
    ("Rev Growth", 12),
    ("Mkt Cap", 10),
    ("30D", 8),
)

CSV_COLUMNS: tuple[str, ...] = (
    "rank",
    "ticker",
    "company",
    "sector",
    "scoring_status",
    "final_score",
    "raw_score",
    "growth_score",
    "quality_score",
    "valuation_score",
    "momentum_score",
    "risk_penalty",
    "risk_level",
    "score_category",
    "data_coverage",
    "market_cap",
    "revenue_growth",
    "growth_acceleration",
    "enterprise_value",
    "valuation_basis",
    "market_cap_source",
    "volume_basis",
    "ranking_state",
    "score_change_7d",
    "score_change_30d",
    "score_change_window_days",
    "warnings",
)


def format_score(value: float | None) -> str:
    """Format a score to one decimal place, or `-` when there is none."""
    return _MISSING if value is None else f"{value:.1f}"


def format_change(value: float | None) -> str:
    """Format a score change with an explicit sign, or `-` when unknown.

    A company with no earlier snapshot has an unknown change, not a change of
    zero, and `-` is the difference between "we have not seen it before" and "it
    has not moved".
    """
    return _MISSING if value is None else f"{value:+.1f}"


def format_observed(value: float | None, unit: MetricUnit) -> str:
    """Format an observed metric according to how it should be read.

    Args:
        value: The metric, or None when it was unavailable.
        unit: What kind of quantity it is.

    Returns:
        A formatted string — `35.0%`, `+15.4pp`, `3.2x`, `4`, `7.5 months` or a
        currency amount — or `-` when the metric is missing.
    """
    if value is None:
        return _MISSING
    if unit is MetricUnit.POINTS:
        return format_points(value)
    if unit is MetricUnit.MULTIPLE:
        return f"{value:.1f}x"
    if unit is MetricUnit.COUNT:
        return f"{value:.0f}"
    if unit is MetricUnit.MONTHS:
        return f"{value:.1f} months"
    if unit is MetricUnit.MONEY:
        return format_money(value)
    return format_percent(value)


def format_rankings_table(rows: Sequence[RankingRow]) -> str:
    """Render ranking rows as a fixed-width console table.

    Args:
        rows: The rows to render, in ranking order.

    Returns:
        The table as a single string, without a trailing newline. An empty input
        returns a header and a note rather than a bare header, so an empty
        ranking does not look like a rendering bug.
    """
    header = "".join(name.ljust(width) for name, width in _TABLE_COLUMNS).rstrip()
    divider = "-" * sum(width for _, width in _TABLE_COLUMNS)

    if not rows:
        return f"{header}\n{divider}\nNo companies matched."

    lines = [header, divider]
    for row in rows:
        cells = (
            str(row.rank),
            row.ticker,
            format_score(row.final_score),
            format_score(row.raw_score),
            format_score(row.risk_penalty),
            format_score(row.growth_score),
            format_score(row.quality_score),
            format_score(row.valuation_score),
            format_score(row.momentum_score),
            format_percent(row.revenue_growth_yoy),
            format_money(row.market_cap),
            format_change(row.score_change_30d),
        )
        lines.append(
            "".join(
                cell.ljust(width) for cell, (_, width) in zip(cells, _TABLE_COLUMNS, strict=True)
            ).rstrip()
        )
    return "\n".join(lines)


def write_rankings_csv(rows: Iterable[RankingRow], path: Path) -> int:
    """Write ranking rows to a CSV file, one row per company.

    Numbers are written raw rather than formatted — a spreadsheet needs `0.425`,
    not `42.5%`, to sort or average a column.

    Args:
        rows: The rows to write, in ranking order.
        path: Destination file. Parent directories are created if needed.

    Returns:
        How many data rows were written, excluding the header.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_row(row))
            written += 1

    return written


def _csv_row(row: RankingRow) -> dict[str, object]:
    """Flatten one ranking row into the CSV column layout."""
    values: dict[str, object] = {
        "rank": row.rank,
        "ticker": row.ticker,
        "company": row.name,
        "sector": row.sector or "",
        "scoring_status": row.scoring_status,
        "final_score": row.final_score,
        "raw_score": row.raw_score,
        "growth_score": row.growth_score,
        "quality_score": row.quality_score,
        "valuation_score": row.valuation_score,
        "momentum_score": row.momentum_score,
        "risk_penalty": row.risk_penalty,
        "risk_level": row.risk_level or "",
        "score_category": row.score_category or "",
        "data_coverage": row.data_coverage,
        "market_cap": row.market_cap,
        "revenue_growth": row.revenue_growth_yoy,
        "growth_acceleration": row.revenue_growth_acceleration,
        "enterprise_value": row.enterprise_value,
        "valuation_basis": row.valuation_basis or "",
        "market_cap_source": row.market_cap_source or "",
        "volume_basis": row.volume_basis or "",
        "ranking_state": row.ranking_state,
        "score_change_7d": row.score_change_7d,
        "score_change_30d": row.score_change_30d,
        "score_change_window_days": row.score_change_window_days,
        "warnings": "|".join(row.warnings),
    }
    return {key: "" if value is None else value for key, value in values.items()}


def format_explanation(detail: ScoreDetail) -> str:
    """Render one company's score as a human-readable breakdown.

    Args:
        detail: The stored score and its breakdown.

    Returns:
        A multi-line explanation: the headline numbers, then every component
        with the points each metric earned and the value it earned them on.
        Metrics that could not be calculated are listed as unavailable rather
        than shown as zero.
    """
    lines = [
        f"{detail.ticker} — {detail.name}",
        f"Scored {detail.score_date} under {detail.score_version}",
        f"Status: {detail.scoring_status}",
    ]

    score = _parse_breakdown(detail)
    if score is None:
        lines.append("")
        lines.append("No breakdown was stored for this score.")
        return "\n".join(lines)

    if score.final_score is not None:
        risk = score.risk
        lines.append(
            f"Final score: {score.final_score:.1f} / 100   "
            f"raw {format_score(score.raw_score)}   "
            f"risk {format_score(risk.total_penalty if risk else None)}"
            f"{f' ({risk.level.value})' if risk else ''}"
        )
        lines.append(
            f"Category: {score.category.value if score.category else _MISSING}   "
            f"Data coverage: {format_percent(score.data_coverage)}   "
            f"Valuation basis: {score.valuation_basis.value}"
        )
        lines.append(
            f"Change: 7d {format_change(detail.score_change_7d)}   "
            f"30d {format_change(detail.score_change_30d)}"
        )

    for component in score.components:
        lines.append("")
        lines.extend(_component_lines(component))

    if score.risk is not None:
        lines.append("")
        lines.extend(_risk_lines(score))

    if score.warnings:
        lines.append("")
        lines.append("Warnings: " + ", ".join(warning.value for warning in score.warnings))

    return "\n".join(lines)


def _parse_breakdown(detail: ScoreDetail) -> CompanyScore | None:
    """Validate a stored breakdown back into a score, or None if it cannot be.

    A breakdown written by an older version of the models may no longer
    validate. That is a reason to print less, not to fail: the snapshot's own
    columns still carry the score.
    """
    if not detail.breakdown:
        return None
    try:
        return CompanyScore.model_validate(detail.breakdown)
    except ValidationError:
        log.warning("stored breakdown could not be read", ticker=detail.ticker)
        return None


def _component_lines(component: ComponentScore) -> list[str]:
    """Render one component and its sub-scores."""
    headline = (
        f"{component.name.title()}: {format_score(component.score)} / "
        f"{component.max_points:.0f}   [{component.status.value}]"
    )
    lines = [headline]
    if component.redistributed:
        lines.append(
            f"  weight redistributed across {format_percent(component.coverage)} "
            "of the component's metrics"
        )
    lines.extend(f"  {_subscore_line(sub)}" for sub in component.subscores)
    return lines


def _subscore_line(sub: SubScore) -> str:
    """Render one metric's contribution."""
    points = (
        f"{sub.points:.1f} / {sub.max_points:.0f}"
        if sub.points is not None
        else f"unavailable / {sub.max_points:.0f}"
    )
    observed = format_observed(sub.observed, sub.unit)
    note = f"  ({sub.note})" if sub.note else ""
    return f"{sub.name:<26} {points:>18}   {observed}{note}"


def _risk_lines(score: CompanyScore) -> list[str]:
    """Render the risk penalties and what they were based on."""
    risk = score.risk
    if risk is None:  # pragma: no cover — callers check first
        return []

    return [
        f"Risk: {format_score(risk.total_penalty)}   [{risk.level.value}]   "
        f"assessed {format_percent(risk.coverage)}",
        f"  {'dilution':<26} {format_score(risk.dilution_penalty):>18}   "
        f"{format_percent(risk.share_count_growth_yoy)}",
        f"  {'cash runway':<26} {format_score(risk.runway_penalty):>18}   "
        f"{format_observed(risk.cash_runway_months, MetricUnit.MONTHS)}",
        f"  {'balance sheet':<26} {format_score(risk.balance_sheet_penalty):>18}   "
        f"{format_percent(risk.net_debt_to_market_cap)}",
        f"  {'liquidity':<26} {format_score(risk.liquidity_penalty):>18}",
    ]
