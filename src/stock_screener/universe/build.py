"""Building the monthly research universe from the audited free evidence.

The chain, walked in the one order that is defensible:

    identity → listing observation → supported venue → security class → month

Nothing later in that chain can admit a security that failed something earlier,
and prices are not in it at all. `domain.classify_month` holds the rule; this
module supplies it with evidence and records what it says.

## What "PIT-valid" means here, concretely

A class observation may describe a month only if the filing's **SEC acceptance
date** is at or before that month, and only if the filing falls inside the same
unbroken run of listing observations — the occupant's tenure. That second test is
`stock_screener.listing.class_for_observation`, and it is what stops Beyond,
Inc.'s 2025 cover page classifying Bed Bath & Beyond's 2022 months under the
symbol they share.

## Every month is written, including the ones that answer nothing

A security observed in January and March gets a February row saying `UNKNOWN`.
Omitting it would make the accounting balance by losing the question, and an
audit that cannot count its own blind spots is the failure this whole sequence
was built to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import delete, select

from data_access.models import (
    ListingObservation,
    ResearchUniverseMonth,
)
from domain import (
    SUPPORTED_FROM,
    ListingEvidenceState,
    Venue,
    classify_month,
    month_start,
)
from stock_screener.listing import class_for_observation
from stock_screener.listing.attribution import RESOLVED_COMPANY, RESOLVED_HISTORICAL

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import date

    from sqlalchemy.orm import Session

    from stock_screener.listing import FilingCover

log = structlog.get_logger(__name__)

#: Attribution states that name exactly one security. `REUSED` and `AMBIGUOUS`
#: are deliberately absent: a symbol two issuers held resolves to neither
#: without a date-aware assignment this layer does not perform.
ATTRIBUTED = frozenset({RESOLVED_COMPANY, RESOLVED_HISTORICAL})


@dataclass(frozen=True)
class BuildReport:
    """What one build produced.

    Attributes:
        rows: Monthly verdicts written.
        by_state: Counts per `UniverseState`.
        securities: Distinct securities represented.
        months: Distinct months covered.
        unattributed: Observations skipped because no single security owns them.
    """

    rows: int = 0
    by_state: dict[str, int] = field(default_factory=dict)
    securities: int = 0
    months: int = 0
    unattributed: int = 0

    def summary(self) -> str:
        """One line for a report."""
        parts = ", ".join(f"{k}={v}" for k, v in sorted(self.by_state.items()))
        return (
            f"{self.rows} monthly verdicts over {self.securities} securities "
            f"and {self.months} months — {parts}; {self.unattributed} unattributed"
        )


def build_universe(
    session: Session,
    covers: Mapping[tuple[str, int], Sequence[FilingCover]],
    *,
    supported_from: date = SUPPORTED_FROM,
) -> BuildReport:
    """Write one monthly verdict per attributed security per observed month.

    Deterministic: the same observations and covers produce the same rows, and a
    second run replaces rather than duplicates.

    Args:
        session: An open session.
        covers: Cover pages held per security, keyed `(kind, identity)` where
            kind is `COMPANY` or `HISTORICAL`.
        supported_from: The first month the universe covers.

    Returns:
        What was written.
    """
    session.execute(delete(ResearchUniverseMonth))

    observations: dict[tuple[str, int], list[ListingObservation]] = {}
    symbols: dict[tuple[str, int], str] = {}
    unattributed = 0
    for row in session.execute(select(ListingObservation)).scalars():
        if row.identity_state not in ATTRIBUTED:
            unattributed += 1
            continue
        identity = row.company_id if row.company_id is not None else row.historical_security_id
        if identity is None:
            unattributed += 1
            continue
        kind = RESOLVED_COMPANY if row.company_id is not None else RESOLVED_HISTORICAL
        key: tuple[str, int] = (kind, identity)
        observations.setdefault(key, []).append(row)
        symbols[key] = row.symbol

    written = 0
    states: dict[str, int] = {}
    months: set[date] = set()
    for key, rows in observations.items():
        kind, identity = key
        venue_of = {r.observation_month: r for r in rows}
        # Months, not venues, are the unit. A security transferring exchanges is
        # observed on both in the handover month — 66 such months exist in the
        # record — and both NASDAQ and NYSE are supported, so membership is
        # unaffected and only the label is ambiguous. Iterating per venue would
        # instead try to write two rows for one security-month.
        venues_by_month: dict[date, set[Venue]] = {}
        for r in rows:
            for candidate in (Venue.NASDAQ, Venue.NYSE):
                if r.venue == str(candidate):
                    venues_by_month.setdefault(r.observation_month, set()).add(candidate)
        if not venues_by_month:
            continue
        first_month, last_month = min(venues_by_month), max(venues_by_month)
        span = [(first_month, last_month)]

        for month in _months_between(first_month, last_month):
            seen_venues = sorted(venues_by_month.get(month, ()), key=str)
            seen = bool(seen_venues)
            observation = venue_of.get(month) if seen else None
            verdict, used = class_for_observation(covers.get(key, ()), symbols[key], month, span)
            age = (month - used.accepted).days if used else None
            decision = classify_month(
                listing_state=(
                    ListingEvidenceState.MARKET_SHARE_OBSERVED
                    if seen
                    else ListingEvidenceState.UNKNOWN
                ),
                venue=seen_venues[0] if seen else None,
                security_class=verdict.security_class,
                class_age_days=age,
                month=month,
                supported_from=supported_from,
            )
            reason = decision.reason
            if len(seen_venues) > 1:
                reason = (
                    f"{reason}; observed on {', '.join(str(v) for v in seen_venues)} "
                    "in the same month — a venue transfer, both supported"
                )
            session.add(
                ResearchUniverseMonth(
                    company_id=identity if kind == RESOLVED_COMPANY else None,
                    historical_security_id=(identity if kind == RESOLVED_HISTORICAL else None),
                    symbol=symbols[key],
                    observation_month=month,
                    venue=str(seen_venues[0]) if seen else None,
                    state=str(decision.state),
                    security_class=str(decision.security_class),
                    class_evidence_mode=str(decision.class_mode),
                    class_age_days=decision.class_age_days,
                    class_source_accession=used.accession if used else None,
                    class_evidence_as_of=used.accepted if used else None,
                    listing_source_file_id=(observation.source_file_id if observation else None),
                    reason=reason,
                )
            )
            states[str(decision.state)] = states.get(str(decision.state), 0) + 1
            months.add(month)
            written += 1
    session.flush()
    report = BuildReport(
        rows=written,
        by_state=states,
        securities=len(observations),
        months=len(months),
        unattributed=unattributed,
    )
    log.info("research universe build", summary=report.summary())
    return report


def _months_between(first: date, last: date) -> list[date]:
    """Every month-start from first to last inclusive."""
    out: list[date] = []
    cursor = month_start(first)
    stop = month_start(last)
    while cursor <= stop:
        out.append(cursor)
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    return out


def covers_from_classifications(
    classifications: dict[tuple[str, int], Sequence[FilingCover]],
) -> dict[tuple[str, int], Sequence[FilingCover]]:
    """Pass cover pages through unchanged.

    A seam rather than a transformation: the builder takes evidence it is given
    and never fetches, so a test supplies fixtures and a run supplies the
    harvested corpus through the same door.
    """
    return classifications
