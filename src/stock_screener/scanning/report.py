"""Rendering scan results for a terminal and for a spreadsheet.

Two audiences, two formats. The console table is for a human skimming the top of
a scan; the CSV is for the manual review the specification calls the most
important step of the project, which happens in a spreadsheet.

Missing values render as `-` in the table and as an **empty cell** in the CSV.
Neither renders as `0`. A blank cell in a spreadsheet is excluded from an
average; a zero silently drags it down, which would corrupt exactly the review
the export exists to support.
"""

from __future__ import annotations

import csv
from typing import TYPE_CHECKING

from domain import VolumeBasis

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from pathlib import Path

    from stock_screener.scanning.scanner import ScanRow

_MISSING = "-"

_THOUSAND = 1_000.0
_MILLION = 1_000_000.0
_BILLION = 1_000_000_000.0
_TRILLION = 1_000_000_000_000.0

_TABLE_COLUMNS: tuple[tuple[str, int], ...] = (
    ("Ticker", 8),
    ("Market Cap", 12),
    ("Price", 9),
    ("Rev Growth", 12),
    ("Acceleration", 14),
    ("Gross Margin", 14),
    ("FCF Margin", 12),
    ("Net Cash", 11),
    ("ADV", 12),
)

CSV_COLUMNS: tuple[str, ...] = (
    "ticker",
    "name",
    "exchange",
    "sector",
    "industry",
    "eligible",
    "exclusion_reasons",
    "warnings",
    "price",
    "market_cap",
    "market_cap_source",
    "calculated_market_cap",
    "market_cap_discrepancy",
    "average_dollar_volume_20d",
    "liquidity_basis",
    "trading_days_used",
    "revenue_growth_yoy",
    "previous_revenue_growth_yoy",
    "revenue_growth_acceleration",
    "ttm_revenue",
    "ttm_revenue_growth",
    "revenue_cagr_3y",
    "gross_margin",
    "gross_margin_change",
    "gross_profit_growth_yoy",
    "operating_margin",
    "operating_margin_change",
    "fcf_margin",
    "fcf_margin_change",
    "ttm_free_cash_flow",
    "cash",
    "debt",
    "net_cash",
    "enterprise_value",
    "share_count_growth_yoy",
    "return_6m",
    "return_12m",
    "high_52w",
    "low_52w",
    "distance_from_52w_high",
)


def format_money(value: float | None) -> str:
    """Format a currency amount with a magnitude suffix.

    Args:
        value: The amount, or None when unavailable.

    Returns:
        A string like `$1.2B`, or `-` when the value is missing. Negative
        amounts keep their sign: `-$40.0M`.
    """
    if value is None:
        return _MISSING

    sign = "-" if value < 0 else ""
    magnitude = abs(value)
    for threshold, suffix in (
        (_TRILLION, "T"),
        (_BILLION, "B"),
        (_MILLION, "M"),
        (_THOUSAND, "K"),
    ):
        if magnitude >= threshold:
            return f"{sign}${magnitude / threshold:.1f}{suffix}"
    return f"{sign}${magnitude:.2f}"


def format_percent(value: float | None) -> str:
    """Format a decimal proportion as a percentage.

    Args:
        value: A decimal, where 0.35 means 35%. None when unavailable.

    Returns:
        A string like `35.0%`, or `-` when the value is missing.
    """
    return _MISSING if value is None else f"{value * 100:.1f}%"


def format_points(value: float | None) -> str:
    """Format a decimal difference as signed percentage points.

    Growth acceleration is a difference between two rates, not a rate, so it is
    rendered `+13.0pp` rather than `13.0%`. Conflating the two is the confusion
    the Phase 1 specification asks for a final review against.

    Args:
        value: A decimal difference, where 0.13 means 13 percentage points.

    Returns:
        A string like `+13.0pp`, or `-` when the value is missing.
    """
    return _MISSING if value is None else f"{value * 100:+.1f}pp"


def format_price(value: float | None) -> str:
    """Format a share price to two decimal places, or `-` when unavailable."""
    return _MISSING if value is None else f"${value:,.2f}"


def format_liquidity(value: float | None, basis: VolumeBasis) -> str:
    """Format average dollar volume, marking a figure that is not whole-market.

    A partial figure is suffixed with `*`. Printing it bare would invite the
    reader to compare it with the configured threshold, which is precisely the
    comparison that is invalid — a single-exchange feed carries a few percent of
    real volume.

    Args:
        value: The dollar volume, or None when unavailable.
        basis: What the figure represents.

    Returns:
        A formatted amount, suffixed `*` when partial, or `-` when missing.
    """
    if value is None:
        return _MISSING
    suffix = "*" if basis is not VolumeBasis.CONSOLIDATED else ""
    return f"{format_money(value)}{suffix}"


def format_table(rows: Sequence[ScanRow]) -> str:
    """Render scan rows as a fixed-width console table.

    Args:
        rows: The rows to render, in the order they should appear.

    Returns:
        The table as a single string, without a trailing newline. An empty input
        returns a header and a note rather than a bare header, so an empty scan
        does not look like a rendering bug.
    """
    header = "".join(name.ljust(width) for name, width in _TABLE_COLUMNS).rstrip()
    divider = "-" * sum(width for _, width in _TABLE_COLUMNS)

    if not rows:
        return f"{header}\n{divider}\nNo companies matched."

    partial = any(row.metrics.liquidity_basis is not VolumeBasis.CONSOLIDATED for row in rows)

    lines = [header, divider]
    for row in rows:
        metrics = row.metrics
        cells = (
            row.ticker,
            format_money(metrics.market_cap),
            format_price(metrics.price),
            format_percent(metrics.revenue_growth_yoy),
            format_points(metrics.revenue_growth_acceleration),
            format_percent(metrics.gross_margin),
            format_percent(metrics.fcf_margin),
            format_money(metrics.net_cash),
            format_liquidity(metrics.average_dollar_volume_20d, metrics.liquidity_basis),
        )
        lines.append(
            "".join(
                cell.ljust(width) for cell, (_, width) in zip(cells, _TABLE_COLUMNS, strict=True)
            ).rstrip()
        )

    if partial:
        lines.append("")
        lines.append(
            "* single-exchange volume only — not comparable with the liquidity "
            "threshold, which was not applied to these rows."
        )
    return "\n".join(lines)


def write_csv(rows: Iterable[ScanRow], path: Path) -> int:
    """Write scan rows to a CSV file, one row per company.

    Numbers are written raw rather than formatted — a spreadsheet needs
    `0.425`, not `42.5%`, to be able to sort or average the column.

    Args:
        rows: The rows to write.
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


def _csv_row(row: ScanRow) -> dict[str, object]:
    """Flatten one scan row into the CSV column layout.

    None values become empty cells: a spreadsheet skips a blank in an average but
    counts a zero, so writing 0 for "not reported" would quietly distort the
    manual review.
    """
    metrics = row.metrics
    profile = row.profile
    record: dict[str, object] = {
        "ticker": profile.ticker,
        "name": profile.name,
        "exchange": profile.exchange or "",
        "sector": profile.sector or "",
        "industry": profile.industry or "",
        "eligible": row.eligible,
        "exclusion_reasons": "|".join(reason.value for reason in row.eligibility.reasons),
        "warnings": "|".join(warning.value for warning in row.eligibility.warnings),
    }

    for column in CSV_COLUMNS:
        if column in record:
            continue
        value = getattr(metrics, column, None)
        # The basis is an enum; export its name so a spreadsheet can filter on
        # it rather than silently trusting the number beside it.
        record[column] = "" if value is None else getattr(value, "value", value)

    return record
