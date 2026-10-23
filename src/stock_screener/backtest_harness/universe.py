"""The historical research universe of one month, read from frozen evidence.

The question this answers is narrow and it is the one every backtest begins
with: **which securities were, on the evidence that existed by the end of month
M, established common stock listed on a supported venue in M?**

It is answered from two frozen files and never recomputed:

- `pit_coverage_v2.jsonl` holds one row per identity-month of the frozen
  universe — the classification verdict the Phase 9K build reached, whether
  the listing was observed, and, where held evidence already answered the
  month, the acceptance date that answered it.
- `pit_month_outcomes.jsonl` holds what the A3 expansion changed: every month
  that had been open, and for the 155,620 that closed, the acceptance date of
  the common-stock cover page that closed them.

Three states result, and the middle one is the point of the whole evidence
programme:

| Frozen verdict | Post-A3 status | Universe |
| --- | --- | --- |
| `ELIGIBLE` | — | in |
| `NON_COMMON_OR_EXCLUDED` | — | out, on evidence |
| `UNKNOWN` | `COVERED` | in, on A3 evidence |
| `UNKNOWN` | `OPEN` | out, unestablished — *not* a finding |

## One strengthening, never a weakening

Coverage was computed against the **calendar** month end and admits evidence
accepted on it. A decision is taken at the close of the last *trading* session
of the month, which is at or before that, and an acceptance stamped with a date
says nothing about the hour. So this layer requires the establishing filing to
have been accepted **strictly before** the decision session — a conservative
lag, never an early admission — and counts what that removes rather than
quietly keeping it.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING

from stock_screener.backtest_harness.binding import COVERAGE_FILE, OUTCOMES_FILE

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator
    from pathlib import Path

__all__ = [
    "COMPANY",
    "HISTORICAL",
    "PitUniverse",
    "UniverseExclusion",
    "UniverseObservation",
]

COMPANY = "COMPANY"
HISTORICAL = "HISTORICAL"


class UniverseExclusion(StrEnum):
    """Why an identity-month of the frozen universe is not in the study's universe."""

    NON_COMMON_OR_EXCLUDED = "NON_COMMON_OR_EXCLUDED"
    """The frozen verdict establishes a unit, warrant, right, note, preferred
    line or depositary share. A finding, not missing data."""

    CLASS_UNESTABLISHED = "CLASS_UNESTABLISHED"
    """No cover page accepted by the month's end classifies the security. The
    residual the B2.17 audit measured and froze."""

    EVIDENCE_NOT_YET_ACCEPTED = "EVIDENCE_NOT_YET_ACCEPTED"
    """The month is covered, but only by a filing accepted on the decision
    session itself or later. Excluded under the conservative lag."""

    LISTING_UNOBSERVED = "LISTING_UNOBSERVED"
    """The listing record never observed the security on a supported venue that
    month, so no filing can admit it."""


@dataclass(frozen=True)
class UniverseObservation:
    """One security's membership of one month, and what established it.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`. Never collapsed.
        identity_id: The primary key within that class.
        symbol: The symbol the listing record observed that month. A dated
            display attribute, never a key.
        month: The calendar month, `YYYY-MM`.
        source: `FROZEN_VERDICT` for a month the Phase 9K build classified,
            `A3_EVIDENCE` for one the expansion closed.
        evidence_date: The SEC acceptance date of the filing that established
            the classification, where one is recorded.
    """

    kind: str
    identity_id: int
    symbol: str
    month: str
    source: str
    evidence_date: date | None

    @property
    def key(self) -> tuple[str, int]:
        """The identity key: `(kind, identity_id)`."""
        return (self.kind, self.identity_id)


@dataclass
class _Row:
    symbol: str
    universe_state: str
    listing_observed: bool
    status: str
    evidence: str | None


class PitUniverse:
    """Every identity-month of the frozen universe, with what the evidence says.

    Args:
        rows: `{month: {(kind, id): row}}`, as `load` assembles it.
    """

    def __init__(self, rows: dict[str, dict[tuple[str, int], _Row]]) -> None:
        self._rows = rows

    @classmethod
    def load(cls, generation_dir: Path, months: Collection[str] | None = None) -> PitUniverse:
        """Read the frozen coverage and the A3 outcomes into one view.

        Args:
            generation_dir: The evidence generation's plan root.
            months: Keep only these months. None keeps every month; a subset is
                how a worker holds one slice of the calendar rather than all of
                it, and changes no verdict.

        Returns:
            The universe.
        """
        wanted = None if months is None else set(months)
        rows: dict[str, dict[tuple[str, int], _Row]] = defaultdict(dict)
        with (generation_dir / COVERAGE_FILE).open(encoding="utf-8") as handle:
            for line in handle:
                if _skip(line, '"observation_month":"', wanted):
                    continue
                row = json.loads(line)
                month = str(row["observation_month"])[:7]
                if wanted is not None and month not in wanted:
                    continue
                rows[month][(str(row["kind"]), int(row["identity_id"]))] = _Row(
                    symbol=str(row["symbol"]),
                    universe_state=str(row["universe_state"]),
                    listing_observed=bool(row["listing_observed"]),
                    status=str(row["status"]),
                    evidence=row["previous_observation"],
                )
        with (generation_dir / OUTCOMES_FILE).open(encoding="utf-8") as handle:
            for line in handle:
                if _skip(line, '"month_end":"', wanted):
                    continue
                row = json.loads(line)
                if str(row["post_status"]) != "COVERED":
                    continue
                month = str(row["month_end"])[:7]
                if wanted is not None and month not in wanted:
                    continue
                held = rows.get(month, {}).get((str(row["kind"]), int(row["identity_id"])))
                if held is None:
                    continue
                held.status = "COVERED"
                held.evidence = row["evidence_date"]
        return cls(dict(rows))

    def months(self) -> list[str]:
        """Every month the frozen universe holds, in order."""
        return sorted(self._rows)

    def rows_in(self, month: str) -> int:
        """How many identity-months the frozen universe holds for a month."""
        return len(self._rows.get(month, {}))

    def eligible(
        self, month: str, decision: date
    ) -> tuple[list[UniverseObservation], dict[str, int]]:
        """The month's research universe, and why everything else is not in it.

        Args:
            month: The calendar month, `YYYY-MM`.
            decision: The decision session. Evidence must have been accepted
                strictly before it.

        Returns:
            The members in identity order, and a count per exclusion reason.
        """
        members: list[UniverseObservation] = []
        excluded: dict[str, int] = defaultdict(int)
        for (kind, identity_id), row in self._rows.get(month, {}).items():
            if row.universe_state == "ELIGIBLE":
                members.append(
                    UniverseObservation(
                        kind=kind,
                        identity_id=identity_id,
                        symbol=row.symbol,
                        month=month,
                        source="FROZEN_VERDICT",
                        evidence_date=_as_date(row.evidence),
                    )
                )
                continue
            if row.universe_state != "UNKNOWN":
                excluded[str(UniverseExclusion.NON_COMMON_OR_EXCLUDED)] += 1
                continue
            if not row.listing_observed:
                excluded[str(UniverseExclusion.LISTING_UNOBSERVED)] += 1
                continue
            if row.status != "COVERED":
                excluded[str(UniverseExclusion.CLASS_UNESTABLISHED)] += 1
                continue
            accepted = _as_date(row.evidence)
            if accepted is None or accepted >= decision:
                excluded[str(UniverseExclusion.EVIDENCE_NOT_YET_ACCEPTED)] += 1
                continue
            members.append(
                UniverseObservation(
                    kind=kind,
                    identity_id=identity_id,
                    symbol=row.symbol,
                    month=month,
                    source="A3_EVIDENCE",
                    evidence_date=accepted,
                )
            )
        members.sort(key=lambda m: (m.kind, m.identity_id))
        return members, dict(excluded)

    def identities(self) -> Iterator[tuple[str, int]]:
        """Every identity the frozen universe names, once."""
        seen: set[tuple[str, int]] = set()
        for month in self._rows.values():
            for key in month:
                if key not in seen:
                    seen.add(key)
                    yield key


def _skip(line: str, key: str, wanted: set[str] | None) -> bool:
    """Whether a raw line can be discarded without parsing it.

    The frozen files are written with compact separators and sorted keys, so a
    month can be read out of the text directly. Parsing 510,000 objects to keep
    a seventh of them is the difference between a worker starting in seconds
    and starting in minutes, and the fallback is to parse — never to drop.

    Args:
        line: One raw JSON line.
        key: The field the month follows.
        wanted: The months to keep, or None to keep everything.

    Returns:
        True only when the line certainly belongs to another month.
    """
    if wanted is None:
        return False
    at = line.find(key)
    if at < 0:
        return False
    start = at + len(key)
    return line[start : start + 7] not in wanted


def _as_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None
