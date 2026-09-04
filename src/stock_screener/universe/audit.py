"""What the research universe covers, and what it knowably does not.

Two rules shape every figure here. **Censored is not unknown** — a security whose
venue the source cannot report is a measured blind spot, while one the evidence
cannot place at all is an open question, and pooling them would let the second
hide inside the first. **Nothing disappears** — every state is counted, including
`UNKNOWN`, because an audit that reports only its successes cannot detect the
bias this layer exists to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import func, select

from data_access.models import Company, ResearchUniverseMonth
from domain import UniverseState

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy.orm import InstrumentedAttribute, Session

log = structlog.get_logger(__name__)

#: Exchange spellings this project stores for the venue the free source cannot
#: report. Counted, never mapped to NYSE.
CENSORED_VENUE_SPELLINGS = frozenset({"AMEX", "NYSE AMERICAN", "NYSEAMERICAN"})


@dataclass(frozen=True)
class MonthCount:
    """One month's universe.

    Attributes:
        month: The observation month.
        eligible: Securities the evidence admits.
        unknown: Securities the evidence cannot place.
        censored: Securities knowably outside the universe.
    """

    month: date
    eligible: int
    unknown: int
    censored: int


@dataclass(frozen=True)
class UniverseAudit:
    """The coverage report.

    Attributes:
        by_state: Monthly verdicts per `UniverseState`.
        by_class: Verdicts per security class.
        by_evidence: Verdicts per class-evidence mode.
        months: Per-month counts, earliest first.
        live_eligible: Distinct live companies ever eligible.
        delisted_eligible: Distinct delisted securities ever eligible.
        live_censored_venue: Live companies stored on the censored venue.
    """

    by_state: dict[str, int] = field(default_factory=dict)
    by_class: dict[str, int] = field(default_factory=dict)
    by_evidence: dict[str, int] = field(default_factory=dict)
    months: tuple[MonthCount, ...] = field(default_factory=tuple)
    live_eligible: int = 0
    delisted_eligible: int = 0
    live_censored_venue: int = 0

    @property
    def total(self) -> int:
        """Monthly verdicts written."""
        return sum(self.by_state.values())

    def lines(self) -> tuple[str, ...]:
        """The report, one line each."""
        first = self.months[0].month if self.months else None
        last = self.months[-1].month if self.months else None
        return (
            f"monthly verdicts: {self.total} over {len(self.months)} months ({first}..{last})",
            "state: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_state.items())),
            "class: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_class.items())),
            "class evidence: " + ", ".join(f"{k}={v}" for k, v in sorted(self.by_evidence.items())),
            f"survivorship: {self.live_eligible} live securities ever eligible, "
            f"{self.delisted_eligible} delisted securities ever eligible",
            f"censored venue (NYSE American) live companies: {self.live_censored_venue}",
        )


def build_universe_audit(session: Session) -> UniverseAudit:
    """Measure the universe as written.

    Args:
        session: An open session.

    Returns:
        The counts. Live and delisted are reported separately and never pooled —
        a combined figure would hide exactly the survivorship question.
    """

    def counts(column: InstrumentedAttribute[str]) -> dict[str, int]:
        rows = session.execute(select(column, func.count()).group_by(column)).all()
        return {str(k): int(v) for k, v in rows}

    by_state = counts(ResearchUniverseMonth.state)
    by_class = counts(ResearchUniverseMonth.security_class)
    by_evidence = counts(ResearchUniverseMonth.class_evidence_mode)

    months: list[MonthCount] = []
    for (month,) in session.execute(
        select(ResearchUniverseMonth.observation_month)
        .distinct()
        .order_by(ResearchUniverseMonth.observation_month)
    ):
        rows = session.execute(
            select(ResearchUniverseMonth.state, func.count())
            .where(ResearchUniverseMonth.observation_month == month)
            .group_by(ResearchUniverseMonth.state)
        ).all()
        tally = {str(k): int(v) for k, v in rows}
        months.append(
            MonthCount(
                month=month,
                eligible=tally.get(str(UniverseState.ELIGIBLE), 0),
                unknown=tally.get(str(UniverseState.UNKNOWN), 0),
                censored=sum(v for k, v in tally.items() if UniverseState(k).censored),
            )
        )

    live = session.execute(
        select(func.count(func.distinct(ResearchUniverseMonth.company_id))).where(
            (ResearchUniverseMonth.state == str(UniverseState.ELIGIBLE))
            & (ResearchUniverseMonth.company_id.isnot(None))
        )
    ).scalar_one()
    delisted = session.execute(
        select(func.count(func.distinct(ResearchUniverseMonth.historical_security_id))).where(
            (ResearchUniverseMonth.state == str(UniverseState.ELIGIBLE))
            & (ResearchUniverseMonth.historical_security_id.isnot(None))
        )
    ).scalar_one()
    censored = sum(
        1
        for (exchange,) in session.execute(select(Company.exchange))
        if (exchange or "").upper() in CENSORED_VENUE_SPELLINGS
    )

    audit = UniverseAudit(
        by_state=by_state,
        by_class=by_class,
        by_evidence=by_evidence,
        months=tuple(months),
        live_eligible=int(live),
        delisted_eligible=int(delisted),
        live_censored_venue=censored,
    )
    log.info("research universe audit", lines=audit.lines())
    return audit


def eligible_for_month(session: Session, month: date) -> tuple[ResearchUniverseMonth, ...]:
    """Return the securities a strategy may consider for one month.

    **The downstream contract.** A caller receives only `ELIGIBLE` rows; there is
    no best-effort mode and no flag that widens it. An `UNKNOWN` security stays
    unknown, and a censored one is never quietly promoted.
    """
    rows = session.execute(
        select(ResearchUniverseMonth).where(
            (ResearchUniverseMonth.observation_month == month.replace(day=1))
            & (ResearchUniverseMonth.state == str(UniverseState.ELIGIBLE))
        )
    ).scalars()
    return tuple(rows)
