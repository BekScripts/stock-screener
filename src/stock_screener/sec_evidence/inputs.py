"""Assembling the A3 planner's inputs from frozen state, and freezing them.

`plan.PlanningInputs` names everything Plan A3 is computed from — gaps,
candidates, form yield, form sizes, the held set — and nothing in the
repository ever constructed one: the Phase 9L assembly ran from working files
that were lost with the drive. This module is that assembly, rebuilt from the
contracts that did survive, and it changes none of them. `plan.py` is not
imported for anything but its types and `supported_months`; `reachability`,
`join` and the derived index are used exactly as they are.

## Which timestamp controls what

Two dated rules live in this codebase, and this module uses each where the
code that owns it does — never a third.

- **Classifying a universe month** (`ResearchUniverseMonth`, Phase 9K.7):
  a filing may classify a month only if it was accepted by the month's
  *first day* and within `CLASS_CARRY_FORWARD_DAYS` of it. That table is
  read here as frozen input and never recomputed.
- **Planning** (`plan.py`, Phase 9L): a filing supports a *month-end* `M`
  when `date <= M` and `M - date <= CARRY_FORWARD_DAYS`, forward only. Held
  evidence is dated by the filing's SEC acceptance date (`evidence_date`,
  `ACCEPTED` for every V2 filing); an undownloaded candidate is dated by
  its catalogue filing date, because its acceptance stamp is planning
  metadata the planner deliberately does not rely on. Gaps, "already
  covered" and every candidate's coverage set use this one rule, so the
  pool the planner ranks and the gaps it closes are measured identically.

The two rules differ by up to one month at a boundary. That difference is
reported as a diagnostic (`covered_under_classification_rule`) and is not
reconciled here: reconciling it would change the planner.

## Class-aware credit, unchanged

`reachability.build_reachability` decides, per issuer, which tracked
identities a filing may be credited with, from cover rows that already
joined `EXACT` to an identity through `join.join_symbol` — the same dated,
occupancy-aware join the listing layer uses, so a reused ticker can never
credit a later occupant with an earlier one's months. Issuer-level evidence
never credits every security under a CIK: `MULTI_CLASS_UNASSIGNED` and
`IDENTITY_AMBIGUOUS` credit nothing, and an identity the record shows as
non-common is excluded.

## The package is the contract

Every component is written in a frozen order with a digest, and the package
digest binds them together with the digests of every source they were
computed from. `load_planning_inputs` rebuilds the `PlanningInputs` from the
files alone and refuses a package whose sources have moved, so Plan A3 can be
regenerated from what was frozen and never from what happens to be on disk.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy import select

from data_access.models import ResearchUniverseMonth
from domain import SecurityClass
from stock_screener.sec_evidence.join import JoinCache, JoinState, join_symbol
from stock_screener.sec_evidence.plan import (
    CARRY_FORWARD_DAYS,
    PLAN_VERSION,
    WINDOW_END,
    WINDOW_START,
    Candidate,
    Gap,
    GapClass,
    PlanningInputs,
    month_end,
    supported_months,
)
from stock_screener.sec_evidence.reachability import (
    Identity,
    IdentityClass,
    IssuerReach,
    Reach,
    ReachabilityModel,
    build_reachability,
)
from stock_screener.sec_evidence.targets import held_from_manifest

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from pathlib import Path

    from sqlalchemy.orm import Session

    from stock_screener.sec_evidence.manifest import Manifest
    from stock_screener.sec_evidence.mirror import ControlMirror, MirrorRecord

log = structlog.get_logger(__name__)

__all__ = [
    "A3_INPUTS_COMPLETE_MARKER",
    "A3_INPUTS_MANIFEST",
    "A3_INPUTS_VERSION",
    "COMPANY",
    "COMPONENTS",
    "HISTORICAL",
    "AssembledInputs",
    "CoverageRow",
    "CoverageStatus",
    "InputsBindings",
    "Observation",
    "PackageError",
    "ReachRow",
    "ResolvedIdentity",
    "assemble_planning_inputs",
    "component_digests",
    "dashed_accession",
    "load_planning_inputs",
    "load_resolution",
    "mirror_package",
    "package_files",
    "package_sha256",
    "read_bindings",
    "write_package",
]

#: Bumped when the package layout, a row schema, or an assembly rule changes.
A3_INPUTS_VERSION = "B2.13/1"

#: The identity kinds, spelled as `plan.Gap.identity` spells them.
COMPANY = "COMPANY"
HISTORICAL = "HISTORICAL"

#: Package files, in the order they are digested.
COMPONENTS = (
    "pit_coverage_v2.jsonl",
    "reachability_v2.jsonl",
    "coverage_gaps_v2.jsonl",
    "candidates_v2.jsonl",
    "held_v2.jsonl",
    "form_yield_v2.json",
    "form_bytes_v2.json",
    "conflict_report_v2.json",
)
A3_INPUTS_MANIFEST = "planning_inputs_manifest.json"
A3_INPUTS_COMPLETE_MARKER = "a3_inputs_complete.json"

#: The reason the frozen resolution gives for a delisted symbol the current
#: ticker map names but that policy declined to bridge (B2.10: 135 of them).
DECLINED_MAP_REASON = "the current ticker map names today's holder of the symbol, not this one"


class PackageError(RuntimeError):
    """A frozen package cannot be trusted: a digest or a binding disagrees."""


class CoverageStatus(StrEnum):
    """One universe month's standing for planning. Mutually exclusive."""

    DECIDED = "DECIDED"
    """The frozen verdict is not `UNKNOWN`; there is nothing to plan for."""

    COVERED = "COVERED"
    """`UNKNOWN`, but held V2 evidence already supports it under the planning rule."""

    OPEN = "OPEN"
    """`UNKNOWN` and unsupported: a gap, classed by what could close it."""


@dataclass(frozen=True)
class InputsBindings:
    """Every source a package was computed from, as digests and counts.

    Attributes:
        generation_id: The evidence generation.
        database_path: The frozen PIT database, as named by the operator.
        database_sha256: Its digest.
        universe_count: Identities in its production universe.
        universe_sha256: The universe digest.
        resolution_sha256: The frozen filer resolution's digest.
        manifest_sha256: The V2 evidence manifest's digest.
        index_sha256: The V2 derived index file's digest.
        fingerprints: The index's semantic fingerprints by table.
        repo_commit: The checked-out commit.
        inputs_version: `A3_INPUTS_VERSION`.
        plan_version: `plan.PLAN_VERSION`.
        carry_forward_days: `plan.CARRY_FORWARD_DAYS`.
        window_start: `plan.WINDOW_START`, ISO.
        window_end: `plan.WINDOW_END`, ISO.
    """

    generation_id: str
    database_path: str
    database_sha256: str
    universe_count: int
    universe_sha256: str
    resolution_sha256: str
    manifest_sha256: str
    index_sha256: str
    fingerprints: Mapping[str, str]
    repo_commit: str | None
    inputs_version: str = A3_INPUTS_VERSION
    plan_version: str = PLAN_VERSION
    carry_forward_days: int = CARRY_FORWARD_DAYS
    window_start: str = WINDOW_START.isoformat()
    window_end: str = WINDOW_END.isoformat()

    def as_dict(self) -> dict[str, object]:
        """The bindings as plain JSON-ready values."""
        out = asdict(self)
        out["fingerprints"] = dict(sorted(self.fingerprints.items()))
        return out

    def differences(self, other: InputsBindings) -> list[str]:
        """Which bindings disagree, by name. Empty means the same sources."""
        mine, theirs = self.as_dict(), other.as_dict()
        return sorted(
            key
            for key in mine
            if key not in {"database_path", "repo_commit"} and mine[key] != theirs[key]
        )


@dataclass(frozen=True)
class Observation:
    """One cover row from the V2 index, attached to a stored identity or to nothing.

    Attributes:
        cik: The filer whose document carried the row.
        accession: The filing.
        evidence_date: The SEC acceptance date the row is evidence as of.
        symbol: The normalised symbol the cover page named.
        security_class: The class the row was judged to be.
        join_state: How firmly the symbol attached to an identity on that date.
        identity: The identity it attached to, when `EXACT`.
        reason: The join's own explanation.
    """

    cik: int
    accession: str
    evidence_date: date
    symbol: str
    security_class: str
    join_state: str
    identity: Identity | None
    reason: str


@dataclass(frozen=True)
class CoverageRow:
    """One frozen universe month, measured against held V2 evidence.

    Attributes:
        kind: `COMPANY` or `HISTORICAL`.
        identity_id: The security's id in its table.
        cik: The frozen resolution's CIK for it, if any.
        symbol: The symbol the month recorded.
        observation_month: The month, as its first day.
        month_end: The planner's key date for the month.
        universe_state: What `ResearchUniverseMonth` recorded.
        universe_class: The class it recorded.
        frozen_accession: The filing the frozen verdict cited, if any.
        frozen_as_of: When that filing was accepted, if any.
        listing_observed: Whether the month had a listing observation.
        identity_state: The join state of the symbol at the month-end.
        reach: The issuer's reach kind, or `NO_CIK`.
        status: `DECIDED`, `COVERED` or `OPEN`.
        gap_class: The gap class when `OPEN`.
        previous_observation: The newest common-stock observation on or before
            the month-end that joined to this identity.
        days_since: Days from it to the month-end.
        next_observation: The first such observation after the month-end.
        source_cik: The filer of the previous observation.
        source_accession: Its accession.
        reason: Why the status and class.
    """

    kind: str
    identity_id: int
    cik: int | None
    symbol: str
    observation_month: str
    month_end: str
    universe_state: str
    universe_class: str
    frozen_accession: str | None
    frozen_as_of: str | None
    listing_observed: bool
    identity_state: str
    reach: str
    status: str
    gap_class: str | None
    previous_observation: str | None
    days_since: int | None
    next_observation: str | None
    source_cik: int | None
    source_accession: str | None
    reason: str

    @property
    def identity(self) -> Identity:
        """The planner's identity key."""
        return (self.kind, self.identity_id)


@dataclass(frozen=True)
class ReachRow:
    """One tracked identity under one issuer, and whether its filings are credited.

    Attributes:
        cik: The issuer.
        kind: The identity kind.
        identity_id: The identity's id.
        symbol: Its symbol, from the frozen resolution.
        reach: The issuer's `Reach` kind.
        identity_class: The identity's `IdentityClass`.
        credited: Whether a filing from this issuer may support this identity.
        tracked: How many identities the issuer has in the universe.
        common_observations: Common-stock cover rows that joined to it, from
            this issuer's documents.
        other_observations: Non-common rows that joined to it.
        reason: One sentence.
    """

    cik: int
    kind: str
    identity_id: int
    symbol: str
    reach: str
    identity_class: str
    credited: bool
    tracked: int
    common_observations: int
    other_observations: int
    reason: str


@dataclass
class AssembledInputs:
    """Everything one assembly produced, before it is written.

    Attributes:
        inputs: The planner's inputs.
        coverage: One row per frozen universe month.
        reach: One row per tracked identity under a resolved issuer.
        observations: Every cover row's join outcome.
        form_bytes: Size statistics per form, for the package.
        conflicts: The frozen-verdict versus V2-evidence diagnostic.
        summary: Counts a report prints.
        timings: Seconds per stage.
    """

    inputs: PlanningInputs
    coverage: list[CoverageRow]
    reach: list[ReachRow]
    observations: list[Observation]
    form_bytes: dict[str, dict[str, float | int]]
    conflicts: dict[str, object]
    summary: dict[str, object] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Sources
# ----------------------------------------------------------------------------


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class ResolvedIdentity:
    """One frozen resolution row, reduced to what assembly needs."""

    identity: Identity
    symbol: str
    bucket: str
    cik: int | None
    source: str
    reason: str


def load_resolution(path: Path) -> tuple[list[ResolvedIdentity], str]:
    """Read the frozen filer resolution and prove it against its companion.

    Args:
        path: `filer_resolution_v1.jsonl`; its `.sha256` companion must sit
            beside it.

    Returns:
        The rows, and the file's digest.

    Raises:
        PackageError: If the companion is missing or the file no longer
            hashes to it.
    """
    companion = path.with_suffix(".sha256")
    if not companion.is_file():
        raise PackageError(f"{path} has no .sha256 companion; the resolution is not frozen")
    actual = _sha256_of(path)
    expected = companion.read_text(encoding="utf-8").split()[0]
    if actual != expected:
        raise PackageError(f"{path} hashes to {actual[:16]}…, its companion says {expected[:16]}…")
    rows: list[ResolvedIdentity] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("company_id") is not None:
                identity: Identity = (COMPANY, int(raw["company_id"]))
            elif raw.get("historical_security_id") is not None:
                identity = (HISTORICAL, int(raw["historical_security_id"]))
            else:
                raise PackageError(f"resolution row without an identity: {raw}")
            rows.append(
                ResolvedIdentity(
                    identity=identity,
                    symbol=str(raw["symbol"]).upper(),
                    bucket=str(raw["bucket"]),
                    cik=int(raw["cik"]) if raw.get("cik") is not None else None,
                    source=str(raw["source"]),
                    reason=str(raw.get("reason", "")),
                )
            )
    return rows, actual


def _observations(
    index: sqlite3.Connection, session: Session, cache: JoinCache
) -> list[Observation]:
    """Attach every symbol-bearing cover row in the index to an identity, or to nothing."""
    rows: list[Observation] = []
    for cik, accession, evidence, symbol, klass in index.execute(
        "SELECT cik, accession, evidence_date, normalized_symbol, security_class"
        " FROM securities WHERE normalized_symbol IS NOT NULL AND evidence_date IS NOT NULL"
        " AND cik IS NOT NULL AND accession IS NOT NULL"
        " ORDER BY cik, accession, normalized_symbol, security_class, evidence_date"
    ):
        on = date.fromisoformat(str(evidence))
        joined = join_symbol(session, str(symbol), on, cache)
        identity: Identity | None = None
        if joined.state is JoinState.EXACT:
            if joined.company_id is not None:
                identity = (COMPANY, int(joined.company_id))
            elif joined.historical_security_id is not None:
                identity = (HISTORICAL, int(joined.historical_security_id))
        rows.append(
            Observation(
                cik=int(cik),
                accession=str(accession),
                evidence_date=on,
                symbol=str(symbol),
                security_class=str(klass),
                join_state=str(joined.state),
                identity=identity,
                reason=joined.reason,
            )
        )
    return rows


def _universe_rows(session: Session) -> list[ResearchUniverseMonth]:
    """The frozen universe, in the package's order: kind, id, month."""
    rows = list(session.execute(select(ResearchUniverseMonth)).scalars())
    rows.sort(key=lambda r: (*_row_identity(r), r.observation_month))
    return rows


def _row_identity(row: ResearchUniverseMonth) -> Identity:
    if row.company_id is not None:
        return (COMPANY, int(row.company_id))
    return (HISTORICAL, int(row.historical_security_id or 0))


def _join_identity(joined_company: int | None, joined_historical: int | None) -> Identity | None:
    if joined_company is not None:
        return (COMPANY, int(joined_company))
    if joined_historical is not None:
        return (HISTORICAL, int(joined_historical))
    return None


# ----------------------------------------------------------------------------
# Assembly
# ----------------------------------------------------------------------------


def _form_statistics(
    index: sqlite3.Connection,
) -> tuple[dict[str, float], dict[str, dict[str, float | int]]]:
    """Per-form yield and size, measured over the held documents.

    Yield is the share of a form's stored documents that produced at least
    one cover row classified common stock *with* a trading symbol — a row
    the identity join could attach. Sizes are the stored documents' bytes.
    """
    documents: dict[str, list[int]] = defaultdict(list)
    for form, size in index.execute(
        "SELECT COALESCE(form, 'UNKNOWN'), bytes FROM filings ORDER BY url"
    ):
        documents[str(form)].append(int(size or 0))
    usable: dict[str, int] = defaultdict(int)
    for form, count in index.execute(
        "SELECT COALESCE(f.form, 'UNKNOWN'), COUNT(*) FROM filings f WHERE EXISTS ("
        " SELECT 1 FROM securities s WHERE s.url = f.url AND s.security_class = 'COMMON_STOCK'"
        " AND s.normalized_symbol IS NOT NULL) GROUP BY 1"
    ):
        usable[str(form)] = int(count)
    form_yield: dict[str, float] = {}
    form_bytes: dict[str, dict[str, float | int]] = {}
    for form in sorted(documents):
        sizes = sorted(documents[form])
        count = len(sizes)
        form_yield[form] = usable[form] / count if count else 0.0
        middle = count // 2
        median = sizes[middle] if count % 2 else (sizes[middle - 1] + sizes[middle]) / 2
        form_bytes[form] = {
            "count": count,
            "usable_common": usable[form],
            "total": sum(sizes),
            "mean": sum(sizes) / count if count else 0.0,
            "median": median,
            "min": sizes[0] if sizes else 0,
            "max": sizes[-1] if sizes else 0,
        }
    return form_yield, form_bytes


def _month_ends_in_window() -> frozenset[date]:
    ends: set[date] = set()
    cursor = WINDOW_START
    while cursor <= WINDOW_END:
        ends.add(month_end(cursor))
        cursor = month_end(cursor) + timedelta(days=1)
    return frozenset(ends)


def assemble_planning_inputs(
    session: Session,
    index: sqlite3.Connection,
    manifest: Manifest,
    resolution: Sequence[ResolvedIdentity],
    *,
    progress: Callable[[str], None] | None = None,
) -> AssembledInputs:
    """Build the planner's inputs from the frozen universe, the index and the manifest.

    Args:
        session: A read-only session on the frozen PIT database.
        index: The V2 derived index, read-only.
        manifest: The V2 evidence manifest, for the held set.
        resolution: The frozen filer resolution.
        progress: Receives one line per stage.

    Returns:
        Everything the package will hold, plus the summary and timings.
    """
    say = progress or (lambda line: None)
    timings: dict[str, float] = {}
    started = time.perf_counter()

    # --- identities and issuers -------------------------------------------
    by_identity = {resolved.identity: resolved for resolved in resolution}
    tracked: dict[int, set[Identity]] = defaultdict(set)
    for resolved in resolution:
        if resolved.cik is not None:
            tracked[resolved.cik].add(resolved.identity)

    # --- observations: every cover row joined to an identity ----------------
    stage = time.perf_counter()
    cache = JoinCache.load(session)
    observations = _observations(index, session, cache)
    timings["observations"] = time.perf_counter() - stage
    say(
        f"observations: {len(observations)} cover rows joined in {timings['observations']:.1f}s; "
        f"states {_tally(o.join_state for o in observations)}"
    )

    # --- the frozen universe, and each month's own join ---------------------
    stage = time.perf_counter()
    universe = _universe_rows(session)
    month_join: dict[tuple[Identity, date], tuple[str, Identity | None, str]] = {}
    unsafe: set[Identity] = set()
    for row in universe:
        identity = _row_identity(row)
        end = month_end(row.observation_month)
        joined = join_symbol(session, row.symbol, end, cache)
        target = _join_identity(joined.company_id, joined.historical_security_id)
        if joined.state is JoinState.EXACT and target == identity:
            state = str(JoinState.EXACT)
        elif joined.state is JoinState.EXACT:
            # The record attributes the symbol to another security on this
            # date: the universe row and the join disagree, which is exactly
            # the reuse case, and the conservative reading is "unsafe".
            state = "OTHER_OCCUPANT"
            unsafe.add(identity)
        else:
            state = str(joined.state)
            if joined.state is JoinState.AMBIGUOUS:
                unsafe.add(identity)
        month_join[(identity, end)] = (state, target, joined.reason)
    timings["universe_join"] = time.perf_counter() - stage
    say(
        f"universe: {len(universe)} months over {len({_row_identity(r) for r in universe})} "
        f"identities joined in {timings['universe_join']:.1f}s; {len(unsafe)} identities unsafe"
    )

    # --- reachability -------------------------------------------------------
    stage = time.perf_counter()
    common_pairs = sorted(
        {
            (o.cik, o.identity)
            for o in observations
            if o.identity is not None and o.security_class == str(SecurityClass.COMMON_STOCK)
        }
    )
    other_pairs = sorted(
        {
            (o.cik, o.identity)
            for o in observations
            if o.identity is not None and o.security_class != str(SecurityClass.COMMON_STOCK)
        }
    )
    model = build_reachability(
        {cik: sorted(ids) for cik, ids in sorted(tracked.items())},
        common_pairs,
        other_pairs,
        sorted(unsafe),
    )
    reach_rows = _reach_rows(model, tracked, by_identity, common_pairs, other_pairs)
    timings["reachability"] = time.perf_counter() - stage
    say(f"reachability: {model.counts()} issuers; classes {model.class_counts()}")

    # --- held evidence per identity ----------------------------------------
    stage = time.perf_counter()
    common_dates: dict[Identity, list[tuple[date, int, str]]] = defaultdict(list)
    for o in observations:
        if o.identity is not None and o.security_class == str(SecurityClass.COMMON_STOCK):
            common_dates[o.identity].append((o.evidence_date, o.cik, o.accession))
    for dates in common_dates.values():
        dates.sort()
    held = held_from_manifest(manifest)
    window_ends = _month_ends_in_window()

    # --- candidates: every catalogue filing that could support a window month ----
    catalogue: dict[int, list[tuple[str, str, date]]] = defaultdict(list)
    catalogue_total = 0
    for cik, accession, form, filed in index.execute(
        "SELECT cik, accession, form, filing_date FROM catalogue ORDER BY cik, accession"
    ):
        catalogue_total += 1
        filed_on = date.fromisoformat(str(filed))
        if not (set(supported_months(filed_on)) & window_ends):
            continue
        catalogue[int(cik)].append((str(accession), str(form), filed_on))
    # Which month-ends each issuer's catalogue could reach, for gap classing.
    reachable_months: dict[int, set[date]] = {}
    for cik, entries in catalogue.items():
        months: set[date] = set()
        for accession, _form, filed_on in entries:
            if (cik, accession) in held:
                continue
            months.update(supported_months(filed_on))
        reachable_months[cik] = months
    timings["held_and_catalogue"] = time.perf_counter() - stage

    # --- coverage and gaps --------------------------------------------------
    stage = time.perf_counter()
    coverage: list[CoverageRow] = []
    gaps: list[Gap] = []
    open_keys: dict[tuple[Identity, date], GapClass] = {}
    classification_covered = 0
    for row in universe:
        identity = _row_identity(row)
        known = by_identity.get(identity)
        cik = known.cik if known else None
        end = month_end(row.observation_month)
        state, _target, join_reason = month_join[(identity, end)]
        reach = model.by_cik.get(cik) if cik is not None else None
        reach_kind = str(reach.kind) if reach is not None else str(Reach.NO_CIK)
        previous, days_since, following, source = _nearest(common_dates.get(identity, ()), end)
        listing_observed = row.venue is not None
        frozen_state = str(row.state)

        if frozen_state != "UNKNOWN":
            status, gap_class, reason = (
                CoverageStatus.DECIDED,
                None,
                f"frozen verdict {frozen_state}",
            )
        elif not listing_observed:
            # No filing can admit a month the listing record never observed;
            # held evidence here is real but changes nothing, so the month is
            # open and non-actionable rather than "covered".
            status = CoverageStatus.OPEN
            gap_class, reason = _classify_gap(
                cik, reach, identity, state, listing_observed, end, reachable_months, join_reason
            )
        elif days_since is not None and days_since <= CARRY_FORWARD_DAYS:
            status, gap_class = CoverageStatus.COVERED, None
            reason = (
                f"held common-stock evidence {previous} ({days_since} days before the month-end)"
            )
        else:
            status = CoverageStatus.OPEN
            gap_class, reason = _classify_gap(
                cik, reach, identity, state, listing_observed, end, reachable_months, join_reason
            )
        # The classification rule, for the diagnostic only: accepted by the
        # month's first day and within the bound of it.
        if frozen_state == "UNKNOWN" and listing_observed:
            start = row.observation_month
            for observed, _cik, _acc in common_dates.get(identity, ()):
                if observed <= start and (start - observed).days <= CARRY_FORWARD_DAYS:
                    classification_covered += 1
                    break

        coverage.append(
            CoverageRow(
                kind=identity[0],
                identity_id=identity[1],
                cik=cik,
                symbol=row.symbol,
                observation_month=row.observation_month.isoformat(),
                month_end=end.isoformat(),
                universe_state=frozen_state,
                universe_class=str(row.security_class),
                frozen_accession=row.class_source_accession,
                frozen_as_of=(
                    row.class_evidence_as_of.isoformat() if row.class_evidence_as_of else None
                ),
                listing_observed=listing_observed,
                identity_state=state,
                reach=reach_kind,
                status=str(status),
                gap_class=str(gap_class) if gap_class is not None else None,
                previous_observation=previous.isoformat() if previous else None,
                days_since=days_since,
                next_observation=following.isoformat() if following else None,
                source_cik=source[0] if source else None,
                source_accession=source[1] if source else None,
                reason=reason,
            )
        )
        if status is CoverageStatus.OPEN and gap_class is not None:
            open_keys[(identity, end)] = gap_class
            gaps.append(
                Gap(
                    identity=identity,
                    cik=cik,
                    symbol=row.symbol,
                    month_end=end,
                    previous_observation=previous,
                    days_since=days_since,
                    next_observation=following,
                    identity_state=state,
                    universe_state=frozen_state,
                    gap_class=gap_class,
                )
            )
    timings["coverage"] = time.perf_counter() - stage
    say(f"coverage: {len(coverage)} months in {timings['coverage']:.1f}s; {len(gaps)} open")

    # --- candidates with their coverage sets --------------------------------
    stage = time.perf_counter()
    form_yield, form_bytes = _form_statistics(index)
    solvable = {key for key, klass in open_keys.items() if klass is GapClass.SOLVABLE}
    candidates: list[Candidate] = []
    held_excluded = 0
    unmeasured_forms: set[str] = set()
    for cik in sorted(catalogue):
        credited = model.credited(cik)
        for accession, form, filed_on in sorted(catalogue[cik]):
            if (cik, accession) in held:
                held_excluded += 1
                continue
            if form not in form_bytes:
                unmeasured_forms.add(form)
            covers = frozenset(
                (identity, end)
                for identity in credited
                for end in supported_months(filed_on)
                if (identity, end) in solvable
            )
            candidates.append(
                Candidate(
                    cik=cik,
                    accession=accession,
                    form=form,
                    filing_date=filed_on,
                    estimated_bytes=round(float(form_bytes.get(form, {}).get("mean", 0))),
                    yield_rate=form_yield.get(form, 0.0),
                    covers=covers,
                )
            )
    timings["candidates"] = time.perf_counter() - stage
    say(
        f"candidates: {len(candidates)} from {sum(len(v) for v in catalogue.values())} window "
        f"filings ({held_excluded} held) in {timings['candidates']:.1f}s"
    )

    inputs = PlanningInputs(
        gaps=tuple(gaps),
        candidates=tuple(candidates),
        form_yield=form_yield,
        form_bytes=form_bytes,
        held=held,
    )
    conflicts = _conflict_report(coverage, observations, index)
    timings["total"] = time.perf_counter() - started

    summary = _summary(
        coverage,
        gaps,
        candidates,
        reach_rows,
        observations,
        held,
        catalogue_total,
        held_excluded,
        classification_covered,
        unmeasured_forms,
        resolution,
        model,
    )
    return AssembledInputs(
        inputs=inputs,
        coverage=coverage,
        reach=reach_rows,
        observations=observations,
        form_bytes=form_bytes,
        conflicts=conflicts,
        summary=summary,
        timings=timings,
    )


def _nearest(
    dates: Sequence[tuple[date, int, str]], end: date
) -> tuple[date | None, int | None, date | None, tuple[int, str] | None]:
    """The newest observation on or before a month-end, and the first after it."""
    previous: tuple[date, int, str] | None = None
    following: date | None = None
    for observed, cik, accession in dates:
        if observed <= end:
            previous = (observed, cik, accession)
        elif following is None:
            following = observed
    if previous is None:
        return None, None, following, None
    return previous[0], (end - previous[0]).days, following, (previous[1], previous[2])


def _classify_gap(
    cik: int | None,
    reach: IssuerReach | None,
    identity: Identity,
    identity_state: str,
    listing_observed: bool,
    end: date,
    reachable_months: Mapping[int, set[date]],
    join_reason: str,
) -> tuple[GapClass, str]:
    """Why one open month-end is open, in the planner's own classes."""
    if cik is None:
        return GapClass.NO_CIK, "no resolved CIK under the frozen resolution"
    if not listing_observed:
        return GapClass.OTHER, "no listing observation this month; SEC evidence cannot admit it"
    if identity_state != str(JoinState.EXACT):
        return (
            GapClass.AMBIGUOUS_IDENTITY,
            f"symbol does not attach to this identity: {join_reason}",
        )
    if reach is None:
        return GapClass.NO_CIK, "resolved CIK tracks no identity"
    kind, credited = reach.kind, reach.credited
    if kind is Reach.IDENTITY_AMBIGUOUS:
        return GapClass.AMBIGUOUS_IDENTITY, "issuer reach IDENTITY_AMBIGUOUS"
    if identity not in credited:
        if kind is Reach.MULTI_CLASS_UNASSIGNED:
            return GapClass.OTHER, "issuer reach MULTI_CLASS_UNASSIGNED: no identity credited"
        # Filings exist at issuer level and cannot be attributed to this
        # security: the record shows it non-common, or has not classed it.
        return (
            GapClass.AMBIGUOUS_IDENTITY,
            f"identity not credited under {kind}: known non-common or unclassed",
        )
    if end in reachable_months.get(cik, set()):
        return GapClass.SOLVABLE, f"issuer reach {kind}; an unheld catalogue filing reaches it"
    return GapClass.NO_CANDIDATE, f"issuer reach {kind}; no unheld catalogue filing reaches it"


def _reach_rows(
    model: ReachabilityModel,
    tracked: Mapping[int, set[Identity]],
    by_identity: Mapping[Identity, ResolvedIdentity],
    common_pairs: Sequence[tuple[int, Identity]],
    other_pairs: Sequence[tuple[int, Identity]],
) -> list[ReachRow]:
    common_count: dict[tuple[int, Identity], int] = defaultdict(int)
    for pair in common_pairs:
        common_count[pair] += 1
    other_count: dict[tuple[int, Identity], int] = defaultdict(int)
    for pair in other_pairs:
        other_count[pair] += 1
    rows: list[ReachRow] = []
    for cik in sorted(tracked):
        reach = model.by_cik[cik]
        for identity in sorted(tracked[cik]):
            klass = model.identity_class.get(identity, IdentityClass.UNKNOWN_CLASS)
            credited = identity in reach.credited
            rows.append(
                ReachRow(
                    cik=cik,
                    kind=identity[0],
                    identity_id=identity[1],
                    symbol=by_identity[identity].symbol,
                    reach=str(reach.kind),
                    identity_class=str(klass),
                    credited=credited,
                    tracked=len(reach.tracked),
                    common_observations=common_count.get((cik, identity), 0),
                    other_observations=other_count.get((cik, identity), 0),
                    reason=(
                        f"{reach.kind}: {'credited' if credited else 'not credited'}; "
                        f"identity {klass} among {len(reach.tracked)} tracked"
                    ),
                )
            )
    return rows


def dashed_accession(value: str) -> str:
    """The SEC's dashed accession form. The frozen universe stores the 18-digit form."""
    text = value.strip()
    if len(text) == 18 and text.isdigit():
        return f"{text[:10]}-{text[10:12]}-{text[12:]}"
    return text


def _conflict_report(
    coverage: Sequence[CoverageRow],
    observations: Sequence[Observation],
    index: sqlite3.Connection,
) -> dict[str, object]:
    """Where the frozen verdicts and the V2 evidence disagree, classified. Diagnostic.

    The frozen table's `class_source_accession` is compared in the dashed
    form the index uses; the table itself is never touched.
    """
    held_accessions = {str(a) for (a,) in index.execute("SELECT DISTINCT accession FROM filings")}
    catalogued = {str(a) for (a,) in index.execute("SELECT DISTINCT accession FROM catalogue")}
    by_accession: dict[str, set[Identity]] = defaultdict(set)
    dates: dict[str, set[date]] = defaultdict(set)
    for o in observations:
        dates[o.accession].add(o.evidence_date)
        if o.identity is not None:
            by_accession[o.accession].add(o.identity)
    tally: dict[str, int] = defaultdict(int)
    timestamp_disagreements = 0
    for row in coverage:
        if row.frozen_accession is None:
            continue
        accession = dashed_accession(row.frozen_accession)
        if accession not in held_accessions:
            tally[
                "frozen_accession_catalogued_not_held"
                if accession in catalogued
                else "frozen_accession_not_in_v2"
            ] += 1
            continue
        if row.identity in by_accession.get(accession, set()):
            tally["frozen_accession_joins_in_v2"] += 1
        else:
            tally["frozen_accession_held_but_no_join"] += 1
        if (
            row.frozen_as_of is not None
            and dates.get(accession)
            and date.fromisoformat(row.frozen_as_of) not in dates[accession]
        ):
            timestamp_disagreements += 1
    unknown_covered = sum(
        1
        for row in coverage
        if row.universe_state == "UNKNOWN" and row.status == str(CoverageStatus.COVERED)
    )
    decided_uncovered = sum(
        1 for row in coverage if row.universe_state != "UNKNOWN" and row.days_since is None
    )
    return {
        "rows_with_frozen_accession": sum(1 for r in coverage if r.frozen_accession),
        "frozen_accession_classes": dict(sorted(tally.items())),
        "frozen_as_of_vs_v2_evidence_date_disagreements": timestamp_disagreements,
        "frozen_unknown_covered_by_v2_planning_rule": unknown_covered,
        "frozen_decided_without_v2_common_observation": decided_uncovered,
    }


def _tally(values: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for value in values:
        out[value] += 1
    return dict(sorted(out.items()))


def _summary(
    coverage: Sequence[CoverageRow],
    gaps: Sequence[Gap],
    candidates: Sequence[Candidate],
    reach_rows: Sequence[ReachRow],
    observations: Sequence[Observation],
    held: frozenset[tuple[int, str]],
    catalogue_total: int,
    held_excluded: int,
    classification_covered: int,
    unmeasured_forms: set[str],
    resolution: Sequence[ResolvedIdentity],
    model: ReachabilityModel,
) -> dict[str, object]:
    status = _tally(row.status for row in coverage)
    declined = {r.identity for r in resolution if DECLINED_MAP_REASON in r.reason}
    unresolved = {r.identity for r in resolution if r.cik is None}
    declined_rows = [r for r in coverage if r.identity in declined]
    unresolved_rows = [r for r in coverage if r.identity in unresolved]
    covers_total = sum(len(c.covers) for c in candidates)
    return {
        "universe_rows": len(coverage),
        "universe_identities": len({row.identity for row in coverage}),
        "universe_months": len({row.observation_month for row in coverage}),
        "status": status,
        "gap_classes": _tally(str(g.gap_class) for g in gaps),
        "identity_states": _tally(row.identity_state for row in coverage),
        "reach_by_month": _tally(row.reach for row in coverage),
        "listing_unobserved_unknown": sum(
            1 for r in coverage if r.universe_state == "UNKNOWN" and not r.listing_observed
        ),
        "covered_under_classification_rule": classification_covered,
        "observations": len(observations),
        "observation_join_states": _tally(o.join_state for o in observations),
        "common_observations_joined": sum(
            1
            for o in observations
            if o.identity is not None and o.security_class == str(SecurityClass.COMMON_STOCK)
        ),
        "issuer_reach": model.counts(),
        "identity_classes": model.class_counts(),
        "reach_rows": len(reach_rows),
        "credited_identities": sum(1 for r in reach_rows if r.credited),
        "catalogue_rows": catalogue_total,
        "candidates": len(candidates),
        "candidates_with_cover": sum(1 for c in candidates if c.covers),
        "candidate_cover_keys": covers_total,
        "candidate_forms": _tally(c.form for c in candidates),
        "held_filings": len(held),
        "held_excluded_from_candidates": held_excluded,
        "unmeasured_forms": sorted(unmeasured_forms),
        "solvable_months": sum(1 for g in gaps if g.gap_class is GapClass.SOLVABLE),
        "raw_unknown_floor": sum(1 for g in gaps if g.gap_class is not GapClass.SOLVABLE),
        "declined_map_identities": len(declined),
        "declined_map_universe_rows": len(declined_rows),
        "declined_map_open_rows": sum(
            1 for r in declined_rows if r.status == str(CoverageStatus.OPEN)
        ),
        "unresolved_identities": len(unresolved),
        "unresolved_universe_rows": len(unresolved_rows),
        "unresolved_open_rows": sum(
            1 for r in unresolved_rows if r.status == str(CoverageStatus.OPEN)
        ),
    }


# ----------------------------------------------------------------------------
# The package
# ----------------------------------------------------------------------------


def _line(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        for row in rows:
            line = _line(row) + "\n"
            handle.write(line)
            digest.update(line.encode("utf-8"))
    temporary.replace(path)
    sha = digest.hexdigest()
    path.with_suffix(".sha256").write_text(f"{sha}  {path.name}\n", encoding="utf-8")
    return sha


def _write_json(path: Path, payload: Mapping[str, object], *, companion: bool = True) -> str:
    text = json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if companion:
        path.with_suffix(".sha256").write_text(f"{sha}  {path.name}\n", encoding="utf-8")
    return sha


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0]


def package_files() -> list[str]:
    """Every file a complete package holds, in a fixed order.

    Components, their digests, the JSONL summaries, the manifest and the marker.
    """
    names: list[str] = []
    for name in COMPONENTS:
        names.extend([name, f"{_stem(name)}.sha256"])
        if name.endswith(".jsonl"):
            names.append(f"{_stem(name)}.summary.json")
    names.extend(
        [
            A3_INPUTS_MANIFEST,
            f"{_stem(A3_INPUTS_MANIFEST)}.sha256",
            A3_INPUTS_COMPLETE_MARKER,
            f"{_stem(A3_INPUTS_COMPLETE_MARKER)}.sha256",
        ]
    )
    return names


def _gap_record(gap: Gap) -> dict[str, object]:
    return {
        "kind": gap.identity[0],
        "identity_id": gap.identity[1],
        "cik": gap.cik,
        "symbol": gap.symbol,
        "month_end": gap.month_end.isoformat(),
        "previous_observation": (
            gap.previous_observation.isoformat() if gap.previous_observation else None
        ),
        "days_since": gap.days_since,
        "next_observation": gap.next_observation.isoformat() if gap.next_observation else None,
        "identity_state": gap.identity_state,
        "universe_state": gap.universe_state,
        "gap_class": str(gap.gap_class),
    }


def _candidate_record(candidate: Candidate) -> dict[str, object]:
    return {
        "cik": candidate.cik,
        "accession": candidate.accession,
        "form": candidate.form,
        "filing_date": candidate.filing_date.isoformat(),
        "estimated_bytes": candidate.estimated_bytes,
        "yield_rate": candidate.yield_rate,
        "covers": sorted(
            [kind, identity_id, end.isoformat()] for (kind, identity_id), end in candidate.covers
        ),
    }


def package_sha256(digests: Mapping[str, str]) -> str:
    """The package digest: SHA-256 over `name  sha256` lines in component order."""
    digest = hashlib.sha256()
    for name in COMPONENTS:
        digest.update(f"{digests[name]}  {name}\n".encode())
    return digest.hexdigest()


def write_package(
    directory: Path, assembled: AssembledInputs, bindings: InputsBindings
) -> dict[str, object]:
    """Write every component, the manifest and the completion marker.

    Args:
        directory: The package directory; created if absent.
        assembled: What `assemble_planning_inputs` produced.
        bindings: Every source the assembly read.

    Returns:
        The manifest that was written.
    """
    directory.mkdir(parents=True, exist_ok=True)
    inputs = assembled.inputs
    digests: dict[str, str] = {}
    digests["pit_coverage_v2.jsonl"] = _write_jsonl(
        directory / "pit_coverage_v2.jsonl", (asdict(row) for row in assembled.coverage)
    )
    digests["reachability_v2.jsonl"] = _write_jsonl(
        directory / "reachability_v2.jsonl", (asdict(row) for row in assembled.reach)
    )
    digests["coverage_gaps_v2.jsonl"] = _write_jsonl(
        directory / "coverage_gaps_v2.jsonl", (_gap_record(gap) for gap in inputs.gaps)
    )
    digests["candidates_v2.jsonl"] = _write_jsonl(
        directory / "candidates_v2.jsonl", (_candidate_record(c) for c in inputs.candidates)
    )
    digests["held_v2.jsonl"] = _write_jsonl(
        directory / "held_v2.jsonl",
        ({"cik": cik, "accession": accession} for cik, accession in sorted(inputs.held)),
    )
    digests["form_yield_v2.json"] = _write_json(
        directory / "form_yield_v2.json", dict(sorted(inputs.form_yield.items()))
    )
    digests["form_bytes_v2.json"] = _write_json(
        directory / "form_bytes_v2.json",
        {form: dict(stats) for form, stats in sorted(assembled.form_bytes.items())},
    )
    digests["conflict_report_v2.json"] = _write_json(
        directory / "conflict_report_v2.json", assembled.conflicts
    )
    summaries: dict[str, dict[str, object]] = {
        "pit_coverage_v2.jsonl": {
            "status": assembled.summary.get("status"),
            "gap_classes": assembled.summary.get("gap_classes"),
            "identity_states": assembled.summary.get("identity_states"),
            "unique_identities": assembled.summary.get("universe_identities"),
            "unique_months": assembled.summary.get("universe_months"),
            "source_accessions": len(
                {row.source_accession for row in assembled.coverage if row.source_accession}
            ),
        },
        "reachability_v2.jsonl": {
            "issuer_reach": assembled.summary.get("issuer_reach"),
            "identity_classes": assembled.summary.get("identity_classes"),
            "credited": assembled.summary.get("credited_identities"),
        },
        "coverage_gaps_v2.jsonl": {"gap_classes": assembled.summary.get("gap_classes")},
        "candidates_v2.jsonl": {
            "forms": assembled.summary.get("candidate_forms"),
            "with_cover": assembled.summary.get("candidates_with_cover"),
            "cover_keys": assembled.summary.get("candidate_cover_keys"),
        },
        "held_v2.jsonl": {"identity": "(cik, accession)"},
    }
    for name, facts in summaries.items():
        _write_json(
            directory / f"{_stem(name)}.summary.json",
            {"file": name, "sha256": digests[name], "rows": _count_rows(directory / name), **facts},
            companion=False,
        )
    package = package_sha256(digests)
    manifest: dict[str, object] = {
        "inputs_version": A3_INPUTS_VERSION,
        "bindings": bindings.as_dict(),
        "components": {name: digests[name] for name in COMPONENTS},
        "package_sha256": package,
        "counts": {
            "gaps": len(inputs.gaps),
            "candidates": len(inputs.candidates),
            "held": len(inputs.held),
            "coverage_rows": len(assembled.coverage),
            "reach_rows": len(assembled.reach),
            "forms": len(inputs.form_yield),
        },
        "summary": assembled.summary,
        "timings": {k: round(v, 3) for k, v in assembled.timings.items()},
    }
    _write_json(directory / A3_INPUTS_MANIFEST, manifest)
    _write_json(
        directory / A3_INPUTS_COMPLETE_MARKER,
        {
            "stage": "A3_INPUTS_COMPLETE",
            "inputs_version": A3_INPUTS_VERSION,
            "bindings": bindings.as_dict(),
            "components": {name: digests[name] for name in COMPONENTS},
            "package_sha256": package,
            "counts": manifest["counts"],
        },
    )
    log.info("a3 inputs package written", directory=str(directory), package=package[:16])
    return manifest


def _count_rows(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def component_digests(directory: Path) -> dict[str, str]:
    """Recompute every component's digest from the files, in component order."""
    digests: dict[str, str] = {}
    for name in COMPONENTS:
        path = directory / name
        if not path.is_file():
            raise PackageError(f"package component missing: {path}")
        digests[name] = _sha256_of(path)
    return digests


def read_bindings(directory: Path) -> InputsBindings:
    """The bindings a package records, from its manifest."""
    manifest: dict[str, Any] = json.loads(
        (directory / A3_INPUTS_MANIFEST).read_text(encoding="utf-8")
    )
    return InputsBindings(**dict(manifest["bindings"]))


def load_planning_inputs(
    directory: Path, *, expect: InputsBindings | None = None
) -> tuple[PlanningInputs, dict[str, Any]]:
    """Rebuild the planner's inputs from a frozen package, refusing a moved source.

    Args:
        directory: The package directory.
        expect: The bindings the caller has proven against the live sources.
            When given, any difference refuses the package.

    Returns:
        The inputs, and the manifest they were read under.

    Raises:
        PackageError: If a component is missing or hashes differently from
            the manifest, if the package digest disagrees, or if `expect`
            names other sources.
    """
    manifest_path = directory / A3_INPUTS_MANIFEST
    if not manifest_path.is_file():
        raise PackageError(f"no package manifest at {manifest_path}")
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    recorded: dict[str, str] = dict(manifest["components"])
    actual = component_digests(directory)
    drift = sorted(name for name in COMPONENTS if recorded.get(name) != actual[name])
    if drift:
        raise PackageError(f"package components changed since they were frozen: {drift}")
    if package_sha256(actual) != manifest["package_sha256"]:
        raise PackageError("the package digest does not bind these components")
    if manifest.get("inputs_version") != A3_INPUTS_VERSION:
        raise PackageError(
            f"package version {manifest.get('inputs_version')!r} is not {A3_INPUTS_VERSION!r}"
        )
    bindings = InputsBindings(**dict(manifest["bindings"]))
    if expect is not None:
        differences = expect.differences(bindings)
        if differences:
            raise PackageError(f"the package was frozen against other sources: {differences}")

    gaps: list[Gap] = []
    with (directory / "coverage_gaps_v2.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            gaps.append(
                Gap(
                    identity=(str(raw["kind"]), int(raw["identity_id"])),
                    cik=int(raw["cik"]) if raw["cik"] is not None else None,
                    symbol=str(raw["symbol"]),
                    month_end=date.fromisoformat(raw["month_end"]),
                    previous_observation=(
                        date.fromisoformat(raw["previous_observation"])
                        if raw["previous_observation"]
                        else None
                    ),
                    days_since=int(raw["days_since"]) if raw["days_since"] is not None else None,
                    next_observation=(
                        date.fromisoformat(raw["next_observation"])
                        if raw["next_observation"]
                        else None
                    ),
                    identity_state=str(raw["identity_state"]),
                    universe_state=str(raw["universe_state"]),
                    gap_class=GapClass(str(raw["gap_class"])),
                )
            )
    candidates: list[Candidate] = []
    with (directory / "candidates_v2.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            candidates.append(
                Candidate(
                    cik=int(raw["cik"]),
                    accession=str(raw["accession"]),
                    form=str(raw["form"]),
                    filing_date=date.fromisoformat(raw["filing_date"]),
                    estimated_bytes=int(raw["estimated_bytes"]),
                    yield_rate=float(raw["yield_rate"]),
                    covers=frozenset(
                        ((str(kind), int(identity_id)), date.fromisoformat(end))
                        for kind, identity_id, end in raw["covers"]
                    ),
                )
            )
    held: set[tuple[int, str]] = set()
    with (directory / "held_v2.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            raw = json.loads(line)
            held.add((int(raw["cik"]), str(raw["accession"])))
    form_yield = {
        str(k): float(v)
        for k, v in json.loads(
            (directory / "form_yield_v2.json").read_text(encoding="utf-8")
        ).items()
    }
    form_bytes = {
        str(k): {str(n): v for n, v in stats.items()}
        for k, stats in json.loads(
            (directory / "form_bytes_v2.json").read_text(encoding="utf-8")
        ).items()
    }
    inputs = PlanningInputs(
        gaps=tuple(gaps),
        candidates=tuple(candidates),
        form_yield=form_yield,
        form_bytes=form_bytes,
        held=frozenset(held),
    )
    return inputs, manifest


# ----------------------------------------------------------------------------
# The mirror
# ----------------------------------------------------------------------------

#: Everything the package holds is mirrored: the components, their digests,
#: the manifest and the marker. Rebuildable, but B2.14 must not have to.
_MIRROR_KIND = "a3-inputs"


def mirror_package(
    mirror: ControlMirror, directory: Path, *, generation_id: str
) -> list[MirrorRecord]:
    """Mirror the whole package as immutable snapshots and read each file back.

    Args:
        mirror: The control mirror for the generation.
        directory: The package directory.
        generation_id: Recorded in each descriptor.

    Returns:
        One record per mirrored file.

    Raises:
        PackageError: If a mirrored file's newest snapshot is not the local file.
    """
    records = []
    for name in package_files():
        local = directory / name
        if not local.is_file():
            raise PackageError(f"{local} does not exist; nothing to mirror")
        kind = f"{_MIRROR_KIND}/{name.replace('.', '-')}"
        record, _ = mirror.mirror_snapshot(local, kind, generation_id=generation_id)
        newest = mirror.newest_snapshot(kind)
        if newest is None or newest[0].get("sha256") != record.sha256:
            raise PackageError(f"the newest mirrored snapshot of {name} is not the local file")
        records.append(record)
    return records
