"""The decision calendar, and the chronological split frozen before any result.

Two things are settled here and nowhere else.

**When a decision happens.** A monthly research decision is taken at the close
of the last benchmark session of the month, and is executed at the open of the
first benchmark session strictly after it — `CLOSE_SIGNAL_NEXT_SESSION_OPEN`.
The benchmark's calendar is the market calendar for the whole study, because it
is the one series that is present on every session.

**Which months are usable at all.** Not the calendar's months: the months the
frozen record can answer for *both* identity classes. Point-in-time features
and signals were captured for historical securities through one date and for
companies past it, and a study that ran past that boundary would silently drop
every delisted name from its universe half way through — survivorship arriving
by the back door, as a pipeline horizon rather than a market event. So the last
usable decision month is derived from feature readiness across both classes,
not assumed from the calendar.

The split is chronological, non-overlapping and written before performance is
computed. Its boundaries are the recommended ones except where readiness moves
them, and the reason is recorded beside the boundary it moved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from itertools import pairwise
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence
    from pathlib import Path

__all__ = [
    "DEVELOPMENT_END",
    "EXECUTION_RULE",
    "SPLIT_FILE",
    "SPLIT_VERSION",
    "VALIDATION_END",
    "CalendarError",
    "DecisionDate",
    "ResearchSplit",
    "SplitName",
    "build_split",
    "decision_calendar",
    "load_split",
    "write_split",
]

#: The one execution rule this phase supports, named in every run manifest.
EXECUTION_RULE = "CLOSE_SIGNAL_NEXT_SESSION_OPEN"

#: The frozen split's identity.
SPLIT_VERSION = "RESEARCH_SPLIT_V1"
SPLIT_FILE = "research_split_v1.json"

#: The recommended boundaries, in months. Moved only by data availability, and
#: only with the reason recorded on the split.
DEVELOPMENT_END = "2023-12"
VALIDATION_END = "2024-12"


class CalendarError(RuntimeError):
    """The frozen record cannot supply a calendar the study can stand on."""


class SplitName(StrEnum):
    """Which chronological block a decision month belongs to."""

    DEVELOPMENT = "DEVELOPMENT"
    VALIDATION = "VALIDATION"
    HOLDOUT = "HOLDOUT"


@dataclass(frozen=True)
class DecisionDate:
    """One monthly decision, and the session that executes it.

    Attributes:
        month: The calendar month, `YYYY-MM`.
        decision: The last benchmark session of the month. The signal is taken
            at its close.
        execution: The first benchmark session strictly after `decision`.
        company_features: Company feature snapshots captured on `decision`.
        historical_features: Historical-security feature snapshots on it.
    """

    month: str
    decision: date
    execution: date
    company_features: int
    historical_features: int


def decision_calendar(db: sqlite3.Connection) -> list[DecisionDate]:
    """Derive every usable monthly decision date from the frozen record.

    A month is usable when the universe holds it, the benchmark has a session
    in it, that session has a successor to execute at, and point-in-time
    features were captured on it for **both** identity classes. The last
    condition is what stops the study running past the horizon at which
    historical securities stop being observed.

    Args:
        db: The research database, opened read-only.

    Returns:
        The decision dates, in order.

    Raises:
        CalendarError: If no month survives, which would mean the dataset is
            not the one the contract describes.
    """
    sessions = [
        date.fromisoformat(str(row[0]))
        for row in db.execute("SELECT date FROM benchmark_prices ORDER BY date")
    ]
    if not sessions:
        raise CalendarError("the benchmark series is empty; there is no market calendar")
    universe_months = {
        str(row[0])[:7]
        for row in db.execute("SELECT DISTINCT observation_month FROM research_universe_months")
    }
    last_of_month: dict[str, date] = {}
    for session in sessions:
        last_of_month[session.isoformat()[:7]] = session
    successor = dict(pairwise(sessions))

    readiness: dict[str, tuple[int, int]] = {}
    for raw_date, companies, historicals in db.execute(
        "SELECT snapshot_date,"
        " sum(CASE WHEN company_id IS NOT NULL THEN 1 ELSE 0 END),"
        " sum(CASE WHEN historical_security_id IS NOT NULL THEN 1 ELSE 0 END)"
        " FROM pit_feature_snapshots GROUP BY snapshot_date"
    ):
        readiness[str(raw_date)] = (int(companies or 0), int(historicals or 0))

    out: list[DecisionDate] = []
    for month in sorted(universe_months):
        found = last_of_month.get(month)
        if found is None or found not in successor:
            continue
        session = found
        companies, historicals = readiness.get(session.isoformat(), (0, 0))
        if companies <= 0 or historicals <= 0:
            continue
        out.append(
            DecisionDate(
                month=month,
                decision=session,
                execution=successor[session],
                company_features=companies,
                historical_features=historicals,
            )
        )
    if not out:
        raise CalendarError("no month is ready for both identity classes; refusing to run")
    return out


@dataclass(frozen=True)
class ResearchSplit:
    """Three chronological, non-overlapping blocks of decision months.

    Attributes:
        version: The split's identity, recorded on every run.
        development: Months in the development block, in order.
        validation: Months in the validation block.
        holdout: Months in the final holdout.
        boundary_reasons: Why a boundary sits where it does, per block, when
            the frozen record moved it from the recommended one.
    """

    version: str
    development: tuple[str, ...]
    validation: tuple[str, ...]
    holdout: tuple[str, ...]
    boundary_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Refuse a split whose blocks overlap or run out of order."""
        blocks = (self.development, self.validation, self.holdout)
        seen: set[str] = set()
        for block in blocks:
            overlap = seen & set(block)
            if overlap:
                raise CalendarError(f"split blocks overlap on {sorted(overlap)}")
            seen |= set(block)
        ordered = [*self.development, *self.validation, *self.holdout]
        if ordered != sorted(ordered):
            raise CalendarError("split blocks are not chronological")

    def assign(self, month: str) -> SplitName | None:
        """Which block a month belongs to, or None when it belongs to none."""
        if month in self.development:
            return SplitName.DEVELOPMENT
        if month in self.validation:
            return SplitName.VALIDATION
        if month in self.holdout:
            return SplitName.HOLDOUT
        return None

    def months(self) -> tuple[str, ...]:
        """Every month the split covers, in order."""
        return (*self.development, *self.validation, *self.holdout)


def build_split(dates: Sequence[DecisionDate]) -> ResearchSplit:
    """Cut the decision calendar into development, validation and holdout.

    The recommended boundaries are used unchanged; only the ends move, and only
    because the frozen record ends where it ends. Nothing about returns is
    consulted — the input is a list of dates.

    Args:
        dates: Every usable decision date, in order.

    Returns:
        The split.

    Raises:
        CalendarError: If any block would be empty.
    """
    months = [d.month for d in dates]
    development = tuple(m for m in months if m <= DEVELOPMENT_END)
    validation = tuple(m for m in months if DEVELOPMENT_END < m <= VALIDATION_END)
    holdout = tuple(m for m in months if m > VALIDATION_END)
    reasons: list[str] = []
    if months and months[0] > "2022-01":
        reasons.append(
            f"development begins {months[0]}: the frozen universe's first month is {months[0]}"
        )
    if holdout:
        reasons.append(
            f"holdout ends {holdout[-1]}: point-in-time features were captured for both "
            "identity classes through that month and no further"
        )
    for name, block in (
        ("development", development),
        ("validation", validation),
        ("holdout", holdout),
    ):
        if not block:
            raise CalendarError(f"the {name} block is empty; the split cannot be frozen")
    return ResearchSplit(
        version=SPLIT_VERSION,
        development=development,
        validation=validation,
        holdout=holdout,
        boundary_reasons=tuple(reasons),
    )


def split_document(split: ResearchSplit, dates: Sequence[DecisionDate]) -> dict[str, object]:
    """The split as it is written to disk: months, sessions and the reasons.

    Args:
        split: The split.
        dates: The decision dates it was cut from.

    Returns:
        A deterministic, sorted-key document.
    """
    by_month = {d.month: d for d in dates}

    def block(months: tuple[str, ...]) -> dict[str, object]:
        return {
            "months": list(months),
            "count": len(months),
            "first_decision": by_month[months[0]].decision.isoformat(),
            "last_decision": by_month[months[-1]].decision.isoformat(),
            "first_execution": by_month[months[0]].execution.isoformat(),
            "last_execution": by_month[months[-1]].execution.isoformat(),
        }

    return {
        "version": split.version,
        "execution_rule": EXECUTION_RULE,
        "signal_timing": "close of the last benchmark session of the month",
        "blocks": {
            "DEVELOPMENT": block(split.development),
            "VALIDATION": block(split.validation),
            "HOLDOUT": block(split.holdout),
        },
        "boundary_reasons": list(split.boundary_reasons),
        "overlap": "NONE",
        "decision_dates": [
            {
                "month": d.month,
                "decision": d.decision.isoformat(),
                "execution": d.execution.isoformat(),
                "split": str(split.assign(d.month)),
            }
            for d in dates
        ],
    }


def write_split(directory: Path, split: ResearchSplit, dates: Sequence[DecisionDate]) -> Path:
    """Write the split deterministically and return its path.

    Args:
        directory: The research output root.
        split: The split to freeze.
        dates: The decision dates it was cut from.

    Returns:
        The written file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / SPLIT_FILE
    path.write_text(
        json.dumps(split_document(split, dates), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def load_split(path: Path) -> ResearchSplit:
    """Read a frozen split back, refusing one that does not validate.

    Args:
        path: The split file.

    Returns:
        The split.

    Raises:
        CalendarError: If the file is absent or does not describe three blocks.
    """
    if not path.is_file():
        raise CalendarError(f"no frozen split at {path}")
    document = json.loads(path.read_text(encoding="utf-8"))
    blocks = document.get("blocks") or {}
    try:
        return ResearchSplit(
            version=str(document["version"]),
            development=tuple(blocks["DEVELOPMENT"]["months"]),
            validation=tuple(blocks["VALIDATION"]["months"]),
            holdout=tuple(blocks["HOLDOUT"]["months"]),
            boundary_reasons=tuple(document.get("boundary_reasons") or ()),
        )
    except KeyError as exc:
        raise CalendarError(f"the split at {path} is not a three-block split: {exc}") from exc
