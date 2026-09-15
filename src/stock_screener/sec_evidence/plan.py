"""Choosing which filings to fetch next, and proving the choice.

Phase 9L.1 measured that the corpus is temporally wrong rather than too small:
six recent filings per security span a median of 100 days against a 55-month
research window, and close 8% of the universe's `UNKNOWN` month-verdicts. Every
qualifying filing inside that window is 269,521 documents. This module decides
which of them are worth fetching, and it never fetches anything.

## What a candidate can and cannot claim

A candidate is a filing we have **not read**. Its filing date is known from the
catalogue; its cover page is not. So every figure here is labelled *potential*,
and a second, deliberately smaller figure — *yield-adjusted* — multiplies it by
the rate at which filings of that form actually produced a usable cover row in
the corpus we did parse. Reporting only the first number would turn a plan into
a promise the evidence cannot keep.

## Coverage is forward-only

A filing supports a month-end `M` when `filing_date <= M` and `M - filing_date
<= 90 days`. Never the reverse. A document cannot describe a month that had not
happened when it was filed, and the single most dangerous thing a planner like
this could do is quietly count backwards to make its numbers look better.

## Determinism

Selection is a greedy set cover with fully specified tie-breaks, so the same
inputs always produce the same ordering and the same digest. A plan nobody can
reproduce is not auditable, and this one is meant to be argued with.
"""

from __future__ import annotations

import csv
import hashlib
import heapq
import json
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from pathlib import Path

log = structlog.get_logger(__name__)

#: The retained carry-forward policy, measured and kept in Phase 9L.1.
CARRY_FORWARD_DAYS = 90

#: The research window currently being reconstructed.
WINDOW_START = date(2022, 1, 1)
WINDOW_END = date(2026, 7, 31)

#: Version stamped on every emitted plan.
PLAN_VERSION = "v1"


class GapClass(StrEnum):
    """Why one month-end lacks authoritative SEC evidence.

    The distinction that matters is between gaps more filings would close and
    gaps they would not. Pooling them would let a plan claim credit for months
    no download can ever reach.
    """

    SOLVABLE = "G1_SOLVABLE"
    """A CIK resolves and catalogue filings exist in a usable window."""

    NO_CIK = "G2_NO_CIK"
    """No filer handle. The SEC has nothing to give, at any budget."""

    AMBIGUOUS_IDENTITY = "G3_AMBIGUOUS"
    """Filings exist at issuer level but cannot be attributed to a security."""

    NO_CANDIDATE = "G4_NO_CANDIDATE"
    """A CIK resolves, but the catalogue holds no filing that could cover it."""

    OTHER = "G5_OTHER"
    """Anything the four above do not describe. Explained per row."""


def month_end(value: date) -> date:
    """The last day of the month containing a date."""
    if value.month == 12:
        return date(value.year, 12, 31)
    return date(value.year, value.month + 1, 1) - timedelta(days=1)


def supported_months(
    filing_date: date, *, horizon_days: int = CARRY_FORWARD_DAYS
) -> tuple[date, ...]:
    """Month-ends a filing may speak for, forward only.

    Args:
        filing_date: When the filing became public.
        horizon_days: How long its evidence carries.

    Returns:
        Month-ends `M` with `filing_date <= M <= filing_date + horizon`. Never a
        month-end before the filing: no backward carry, no interpolation.
    """
    out: list[date] = []
    limit = filing_date + timedelta(days=horizon_days)
    cursor = month_end(filing_date)
    while cursor <= limit:
        if cursor >= filing_date:
            out.append(cursor)
        following = cursor + timedelta(days=1)
        cursor = month_end(following)
    return tuple(out)


@dataclass(frozen=True)
class Gap:
    """One month-end with no authoritative SEC evidence behind it.

    Attributes:
        identity: `(kind, id)` where kind is COMPANY or HISTORICAL.
        cik: The filer handle, when one resolves safely.
        symbol: The ticker at that month.
        month_end: The month-end in question.
        previous_observation: Last SEC observation on or before it, if any.
        days_since: Days since that observation.
        next_observation: First SEC observation after it, if any.
        identity_state: How firmly the symbol attaches to a stored identity.
        universe_state: What `ResearchUniverseMonth` currently records.
        gap_class: Why it is open.
    """

    identity: tuple[str, int]
    cik: int | None
    symbol: str
    month_end: date
    previous_observation: date | None
    days_since: int | None
    next_observation: date | None
    identity_state: str
    universe_state: str
    gap_class: GapClass


@dataclass(frozen=True)
class Candidate:
    """One undownloaded filing, and what it might be worth.

    Attributes:
        cik: The filer whose catalogue named it.
        accession: The submission id — the stable filing identity.
        form: The form type.
        filing_date: When it was filed. Planning metadata only: the acceptance
            timestamp is unknown until the document is fetched.
        estimated_bytes: From the measured size distribution for its form.
        yield_rate: Share of that form's stored documents that produced a
            usable cover row.
        covers: Gaps it could support, as `(identity, month_end)` keys.
    """

    cik: int
    accession: str
    form: str
    filing_date: date
    estimated_bytes: int
    yield_rate: float
    covers: frozenset[tuple[tuple[str, int], date]]

    @property
    def url(self) -> str:
        """Where the filing index lives. The document name needs the catalogue."""
        packed = self.accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{packed}"

    @property
    def key(self) -> tuple[int, str]:
        """The stable filing identity: filer plus submission."""
        return (self.cik, self.accession)


@dataclass(frozen=True)
class PlannedTarget:
    """One selected filing, with the reasoning that selected it."""

    plan_version: str
    rank: int
    cik: int
    accession: str
    form: str
    filing_date: str
    catalogue_source: str
    already_downloaded: bool
    potential_months: int
    yield_adjusted_months: float
    estimated_bytes: int
    first_target_month: str | None
    last_target_month: str | None
    reason: str
    selection_strategy: str


@dataclass(frozen=True)
class Plan:
    """A complete, reproducible selection.

    Attributes:
        strategy: Which rule chose the targets.
        targets: The selection, in rank order.
        covered: Gaps the selection could close.
        potential_months: Their count.
        yield_adjusted_months: The same, discounted by observed form yield.
        estimated_bytes: Storage the plan would consume.
    """

    strategy: str
    targets: tuple[PlannedTarget, ...]
    covered: frozenset[tuple[tuple[str, int], date]]
    potential_months: int
    yield_adjusted_months: float
    estimated_bytes: int

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.strategy}: {len(self.targets):,} filings, "
            f"{self.estimated_bytes / 1024**3:.1f} GiB, "
            f"{self.potential_months:,} potential months "
            f"({self.yield_adjusted_months:,.0f} yield-adjusted)"
        )


def _order_key(
    candidate: Candidate, marginal: int, *, score: str, byte_floor: int = 1
) -> tuple[float, float, float, float, int, str]:
    """The total order greedy selection uses. Every component is deterministic.

    Ties are broken all the way down to `(cik, accession)`, which is unique, so
    no two candidates can ever compare equal. That is what makes the emitted
    plan identical whatever order the pool arrives in — and a plan whose
    ordering depended on input order would not be reproducible by anyone
    checking the work, which is the whole point of emitting a digest.
    """
    if score == "efficiency":
        per_byte = marginal * candidate.yield_rate / max(candidate.estimated_bytes, byte_floor)
        return (
            -per_byte,
            float(-marginal),
            -candidate.yield_rate,
            0.0,
            candidate.cik,
            candidate.accession,
        )
    return (
        float(-marginal),
        -candidate.yield_rate,
        float(candidate.estimated_bytes),
        float(candidate.filing_date.toordinal()),
        candidate.cik,
        candidate.accession,
    )


def _greedy(
    candidates: Sequence[Candidate],
    *,
    strategy: str,
    score: str = "coverage",
    limit: int | None = None,
    min_marginal: int = 1,
    byte_floor: int = 1,
) -> Plan:
    """Select candidates by lazy greedy marginal coverage.

    Lazy rather than naive: recomputing every candidate's marginal gain after
    every selection is quadratic and there are 269,521 of them. The heap holds a
    stale upper bound per candidate; a popped entry is re-scored and only
    accepted when it still beats the next best. That is the standard
    submodular-greedy trick and it changes the result not at all.

    Args:
        candidates: The pool.
        strategy: Label for the emitted plan.
        score: `coverage` ranks by marginal months; `efficiency` by marginal
            months per byte.
        limit: Stop after this many selections.
        min_marginal: Skip candidates contributing fewer than this many new
            months. Excludes zero-benefit filings by construction.
        byte_floor: Divisor guard for the efficiency score.

    Returns:
        The plan.
    """
    covered: set[tuple[tuple[str, int], date]] = set()
    chosen: list[PlannedTarget] = []
    total_bytes = 0
    adjusted = 0.0

    def priority(
        candidate: Candidate, marginal: int
    ) -> tuple[float, float, float, float, int, str]:
        return _order_key(candidate, marginal, score=score, byte_floor=byte_floor)

    heap: list[tuple[tuple[float, float, float, float, int, str], int, int]] = []
    for index, candidate in enumerate(candidates):
        marginal = len(candidate.covers)
        if marginal < min_marginal:
            continue
        heap.append((priority(candidate, marginal), index, marginal))
    heapq.heapify(heap)

    while heap:
        if limit is not None and len(chosen) >= limit:
            break
        _, index, stale = heapq.heappop(heap)
        candidate = candidates[index]
        marginal = len(candidate.covers - covered)
        if marginal < min_marginal:
            continue
        if marginal != stale:
            # The bound moved: re-insert and let it compete again rather than
            # trusting a number that another selection has already invalidated.
            heapq.heappush(heap, (priority(candidate, marginal), index, marginal))
            continue

        gained = candidate.covers - covered
        covered |= gained
        total_bytes += candidate.estimated_bytes
        adjusted += marginal * candidate.yield_rate
        months = sorted(m for _, m in gained)
        chosen.append(
            PlannedTarget(
                plan_version=PLAN_VERSION,
                rank=len(chosen) + 1,
                cik=candidate.cik,
                accession=candidate.accession,
                form=candidate.form,
                filing_date=candidate.filing_date.isoformat(),
                catalogue_source=f"submissions/CIK{candidate.cik:010d}.json",
                already_downloaded=False,
                potential_months=marginal,
                yield_adjusted_months=round(marginal * candidate.yield_rate, 3),
                estimated_bytes=candidate.estimated_bytes,
                first_target_month=months[0].isoformat() if months else None,
                last_target_month=months[-1].isoformat() if months else None,
                reason=(
                    f"covers {marginal} otherwise-unsupported month-end(s); "
                    f"{candidate.form} observed cover yield {candidate.yield_rate:.1%}"
                ),
                selection_strategy=strategy,
            )
        )

    return Plan(
        strategy=strategy,
        targets=tuple(chosen),
        covered=frozenset(covered),
        potential_months=len(covered),
        yield_adjusted_months=adjusted,
        estimated_bytes=total_bytes,
    )


def plan_coverage_first(candidates: Sequence[Candidate], *, limit: int | None = None) -> Plan:
    """Plan A — take whichever filing closes the most open month-ends next."""
    return _greedy(candidates, strategy="COVERAGE_FIRST", score="coverage", limit=limit)


def plan_storage_efficient(
    candidates: Sequence[Candidate], *, limit: int | None = None, min_marginal: int = 2
) -> Plan:
    """Plan B — maximise expected months per byte, with an absolute floor.

    The floor is the whole difficulty. Pure months-per-byte ranks a 30 KB 8-K
    covering one month above a 2 MB 10-K covering four, and a plan built that
    way is thousands of tiny files that never assemble into continuous coverage.
    Requiring at least `min_marginal` new months keeps efficiency honest.
    """
    return _greedy(
        candidates,
        strategy="STORAGE_EFFICIENT",
        score="efficiency",
        limit=limit,
        min_marginal=min_marginal,
    )


def plan_balanced(candidates: Sequence[Candidate], *, limit: int | None = None) -> Plan:
    """Plan C — coverage first, but only where the filing is likely to deliver.

    Interpretable on purpose: rank by marginal months, then by observed form
    yield, then by size. No weighting constants to defend, and every rank is
    explainable in one sentence.
    """
    return _greedy(candidates, strategy="BALANCED", score="coverage", limit=limit, min_marginal=2)


def plan_minimum_cover(candidates: Sequence[Candidate]) -> Plan:
    """The interval-cover plan: fewest filings that leave no coverable month open.

    Same greedy machinery, run to exhaustion with no limit. Because each
    selection re-scores against what is already covered, a filing whose months
    are wholly covered by earlier picks contributes zero and is skipped — which
    is the redundancy guarantee the tests pin.
    """
    return _greedy(candidates, strategy="MINIMUM_COVER", score="coverage", min_marginal=1)


def marginal_curve(plan: Plan, points: Iterable[int]) -> list[dict[str, float | int]]:
    """Walk a plan's ranked targets and report coverage at each size.

    Reads the plan's own order rather than re-planning, so the curve describes
    the plan that would actually be executed.
    """
    rows: list[dict[str, float | int]] = []
    for point in points:
        prefix = plan.targets[:point]
        rows.append(
            {
                "filings": len(prefix),
                "estimated_bytes": sum(t.estimated_bytes for t in prefix),
                "potential_months": sum(t.potential_months for t in prefix),
                "yield_adjusted_months": round(sum(t.yield_adjusted_months for t in prefix), 1),
            }
        )
    return rows


def write_plan(destination: Path, plan: Plan) -> tuple[Path, Path, str]:
    """Write a plan as CSV and JSONL, with a digest over the ordered targets.

    Args:
        destination: Directory beneath the derived subtree.
        plan: What to write.

    Returns:
        `(csv path, jsonl path, sha256)`.
    """
    destination.mkdir(parents=True, exist_ok=True)
    stem = f"expansion_plan_{PLAN_VERSION}"
    csv_path = destination / f"{stem}.csv"
    jsonl_path = destination / f"{stem}.jsonl"
    fields = list(asdict(plan.targets[0]).keys()) if plan.targets else []

    digest = hashlib.sha256()
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for target in plan.targets:
            writer.writerow(asdict(target))
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for target in plan.targets:
            line = json.dumps(asdict(target), sort_keys=True)
            handle.write(line + "\n")
            digest.update(line.encode() + b"\n")

    sha = digest.hexdigest()
    (destination / f"{stem}.sha256").write_text(f"{sha}  {stem}.jsonl\n", encoding="utf-8")
    log.info("expansion plan written", targets=len(plan.targets), sha=sha[:16])
    return csv_path, jsonl_path, sha


@dataclass(frozen=True)
class CikSharing:
    """One filer whose CIK is claimed by more than one tracked security.

    The 9L.1 finding, quantified. `historical_securities` carries
    `UniqueConstraint("cik")`, so a multi-class filer can store the handle on
    exactly one of its securities; the rest keep `cik=None` and become
    unreachable by any CIK-keyed harvest.

    Attributes:
        cik: The filer.
        symbols: Tickers sharing it.
        identities: The stored identities.
        with_cik: How many of them actually carry the handle.
        month_rows: Universe rows belonging to them.
        unknown_rows: How many of those are UNKNOWN.
    """

    cik: int
    symbols: tuple[str, ...]
    identities: tuple[tuple[str, int], ...]
    with_cik: int
    month_rows: int
    unknown_rows: int


@dataclass(frozen=True)
class PlanningInputs:
    """Everything a plan is computed from, gathered once.

    Attributes:
        gaps: Open month-ends by class.
        candidates: Undownloaded filings with their coverage sets.
        form_yield: Observed cover-row yield per form.
        form_bytes: Measured size statistics per form.
        held: `(cik, accession)` already in the corpus.
    """

    gaps: tuple[Gap, ...] = ()
    candidates: tuple[Candidate, ...] = ()
    form_yield: Mapping[str, float] = field(default_factory=dict)
    form_bytes: Mapping[str, Mapping[str, float]] = field(default_factory=dict)
    held: frozenset[tuple[int, str]] = frozenset()

    def by_class(self) -> dict[str, int]:
        """Count gaps per class."""
        counts: dict[str, int] = defaultdict(int)
        for gap in self.gaps:
            counts[str(gap.gap_class)] += 1
        return dict(counts)
