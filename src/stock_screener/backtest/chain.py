"""The whole historical chain for one decision date, with nothing dropped silently.

Phase 10 audited the layers and found them individually correct and collectively
disconnected: membership existed but nothing read it, and a delisted security
could reach an outcome but never a market signal. This is the single path that
runs the chain end to end for **both identity kinds**, and the one property it
guarantees above all others is that *every* security is accounted for.

    identity → membership → PIT fundamentals → score → features → signal → outcome

A security that does not reach the end of that chain leaves with a named reason.
There is no branch here that simply skips one — which matters because the failure
this whole phase exists to prevent is not an error, it is a silence. A survivor-
only population produced by a filter nobody wrote looks exactly like a correct
one, and the only defence is an accounting that has to balance.

## The reasons are the existing vocabulary

`ExclusionReason` is a scoring concept and is deliberately not reused: a security
can be excluded from a *research decision* for reasons the score never considers
— it may have no membership at all. The reasons here name chain stages, and each
maps to the layer that could not answer.

## What this module does not do

It applies no strategy, ranks nothing, and calibrates nothing. It answers "could
this security have been decided about on this date, and if not, why not" — the
question that has to be answerable before any strategy question is worth asking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

import structlog
from sqlalchemy import select

from data_access import (
    CompanyRepository,
    ForwardReturnRepository,
    HistoricalSecurityRepository,
)
from data_access.models import (
    FundamentalObservation,
    HistoricalObservation,
    HistoricalPrice,
    PitFeatureSnapshot,
    PitSignalSnapshot,
    PriceHistory,
)
from domain import CURRENT_SIGNAL_VERSION, MembershipConfidence
from stock_screener.backtest.membership import COMPANY, HISTORICAL, members_as_of

if TYPE_CHECKING:
    from datetime import date

    from sqlalchemy import ColumnElement
    from sqlalchemy.orm import InstrumentedAttribute, Session

log = structlog.get_logger(__name__)

#: The feature definitions the chain reads.
FEATURE_VERSION = "MARKET_FEATURES_V1"


class ChainStage(StrEnum):
    """Why a security did not reach a research decision on a date.

    Ordered as the chain is walked, so the reason names the *first* stage that
    could not answer. A security missing both fundamentals and prices is reported
    against fundamentals, because that is where it stopped.
    """

    NO_MEMBERSHIP = "NO_MEMBERSHIP"
    """The record places the security outside the universe on this date — a dated
    exit, or a first appearance after it."""

    UNKNOWN_MEMBERSHIP = "UNKNOWN_MEMBERSHIP"
    """The record cannot place it either way. Explicitly not the same as
    `NO_MEMBERSHIP`, and never silently merged with it."""

    NO_PIT_DATA = "NO_PIT_DATA"
    """No filing had been made by the decision date."""

    NO_PRICE = "NO_PRICE"
    """No price bar on or before the decision date."""

    NO_MARKET_FEATURES = "NO_MARKET_FEATURES"
    """Prices exist but no feature row was computed for the date."""

    NO_SIGNAL = "NO_SIGNAL"
    """Features exist but no signal was derived from them."""

    NO_OUTCOME = "NO_OUTCOME"
    """The chain held to the decision, and no forward return was measured."""

    ELIGIBLE = "ELIGIBLE"
    """Not an exclusion. The security reached the end of the chain."""


@dataclass(frozen=True)
class ChainVerdict:
    """One security's progress through the chain on one date.

    Attributes:
        identity_kind: `COMPANY` or `HISTORICAL`.
        identity_id: The primary key within that kind, never comparable across.
        ticker: The symbol, for reading.
        stage: Where it stopped, or `ELIGIBLE`.
        membership_confidence: How its membership was established, when it has
            one.
        has_fundamentals: A filing existed by the date.
        has_price: A bar existed on or before the date.
        has_features: A feature row exists for the date.
        has_signal: A signal row exists for the date.
        has_outcome: A forward return exists for the date.
    """

    identity_kind: str
    identity_id: int
    ticker: str
    stage: ChainStage
    membership_confidence: str | None = None
    has_fundamentals: bool = False
    has_price: bool = False
    has_features: bool = False
    has_signal: bool = False
    has_outcome: bool = False

    @property
    def eligible(self) -> bool:
        """Whether the security reached a research decision."""
        return self.stage is ChainStage.ELIGIBLE


@dataclass(frozen=True)
class ChainAccounting:
    """Every security on a date, classified, with the totals reconciled.

    Attributes:
        as_of: The decision date.
        verdicts: One entry per security considered, of both identity kinds.
    """

    as_of: date
    verdicts: tuple[ChainVerdict, ...] = field(default_factory=tuple)

    @property
    def total(self) -> int:
        """Securities considered."""
        return len(self.verdicts)

    @property
    def eligible(self) -> tuple[ChainVerdict, ...]:
        """Securities that reached a decision."""
        return tuple(v for v in self.verdicts if v.eligible)

    @property
    def excluded(self) -> tuple[ChainVerdict, ...]:
        """Securities that did not."""
        return tuple(v for v in self.verdicts if not v.eligible)

    @property
    def unexplained(self) -> int:
        """Securities neither eligible nor carrying a reason.

        **Must be zero.** It is computed rather than asserted so that a run can
        report a non-zero value instead of hiding it: the accounting exists to
        balance, and a silent hole in it is the exact defect this phase closes.
        """
        return sum(1 for v in self.verdicts if not v.eligible and v.stage is ChainStage.ELIGIBLE)

    def _count(self, kind: str, *, eligible: bool | None = None) -> int:
        return sum(
            1
            for v in self.verdicts
            if v.identity_kind == kind and (eligible is None or v.eligible is eligible)
        )

    @property
    def company_decisions(self) -> int:
        """Live-company securities considered."""
        return self._count(COMPANY)

    @property
    def historical_decisions(self) -> int:
        """Delisted securities considered."""
        return self._count(HISTORICAL)

    @property
    def eligible_historical(self) -> int:
        """Delisted securities that reached a decision — the survivorship number."""
        return self._count(HISTORICAL, eligible=True)

    def by_reason(self) -> tuple[tuple[str, int], ...]:
        """Exclusion counts by stage, most common first."""
        counts: dict[str, int] = {}
        for verdict in self.excluded:
            counts[str(verdict.stage)] = counts.get(str(verdict.stage), 0) + 1
        return tuple(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def summary(self) -> str:
        """One line for a report."""
        return (
            f"{self.as_of}: {self.total} securities — {len(self.eligible)} eligible "
            f"({self.eligible_historical} delisted), {len(self.excluded)} excluded, "
            f"{self.unexplained} unexplained"
        )


def _ids_with(
    session: Session,
    column: InstrumentedAttribute[int | None],
    *,
    where: ColumnElement[bool],
) -> set[int]:
    """Return the distinct non-null ids a filtered column holds."""
    rows = session.execute(select(column).where(where).distinct())
    return {value for (value,) in rows if value is not None}


def chain_accounting(session: Session, as_of: date) -> ChainAccounting:
    """Walk every security through the chain for one date, accounting for all of them.

    Both identity kinds, one pass, no flag. A security that stops short of a
    decision carries the stage that stopped it, so the totals reconcile exactly:
    `eligible + excluded == total`, and `unexplained` is zero by construction
    rather than by hope.

    Args:
        session: An open session.
        as_of: The decision date.

    Returns:
        The classified verdicts.
    """
    universe = members_as_of(session, as_of)
    member = {(kind, identity): verdict for kind, identity, verdict in universe.members}
    unknown = set(universe.unknown)

    companies = {c.id: c.ticker for c in CompanyRepository(session).list_all()}
    securities = {s.id: s.primary_ticker for s in HistoricalSecurityRepository(session).list_all()}

    filed_c = _ids_with(
        session, FundamentalObservation.company_id, where=FundamentalObservation.filed_date <= as_of
    )
    filed_h = _ids_with(
        session,
        HistoricalObservation.historical_security_id,
        where=HistoricalObservation.filed_date <= as_of,
    )
    priced_c = _ids_with(session, PriceHistory.company_id, where=PriceHistory.date <= as_of)
    priced_h = _ids_with(
        session, HistoricalPrice.historical_security_id, where=HistoricalPrice.date <= as_of
    )
    feat_c = _ids_with(
        session,
        PitFeatureSnapshot.company_id,
        where=(PitFeatureSnapshot.snapshot_date == as_of),
    )
    feat_h = _ids_with(
        session,
        PitFeatureSnapshot.historical_security_id,
        where=(PitFeatureSnapshot.snapshot_date == as_of),
    )
    sig_c = _ids_with(
        session,
        PitSignalSnapshot.company_id,
        where=(PitSignalSnapshot.snapshot_date == as_of)
        & (PitSignalSnapshot.signal_version == CURRENT_SIGNAL_VERSION),
    )
    sig_h = _ids_with(
        session,
        PitSignalSnapshot.historical_security_id,
        where=(PitSignalSnapshot.snapshot_date == as_of)
        & (PitSignalSnapshot.signal_version == CURRENT_SIGNAL_VERSION),
    )
    outcomes = ForwardReturnRepository(session).for_observation_date(as_of)
    out_c = {row.company_id for row in outcomes if row.company_id is not None}
    out_h = {
        row.historical_security_id for row in outcomes if row.historical_security_id is not None
    }

    verdicts: list[ChainVerdict] = []
    for kind, identities, filed, priced, feats, sigs, outs in (
        (COMPANY, companies, filed_c, priced_c, feat_c, sig_c, out_c),
        (HISTORICAL, securities, filed_h, priced_h, feat_h, sig_h, out_h),
    ):
        for identity, ticker in identities.items():
            verdicts.append(
                _verdict(
                    kind,
                    identity,
                    ticker,
                    membership=member.get((kind, identity)),
                    is_unknown=(kind, identity) in unknown,
                    has_fundamentals=identity in filed,
                    has_price=identity in priced,
                    has_features=identity in feats,
                    has_signal=identity in sigs,
                    has_outcome=identity in outs,
                )
            )

    accounting = ChainAccounting(as_of=as_of, verdicts=tuple(verdicts))
    log.info(
        "chain accounting",
        as_of=str(as_of),
        total=accounting.total,
        eligible=len(accounting.eligible),
        eligible_historical=accounting.eligible_historical,
        unexplained=accounting.unexplained,
    )
    return accounting


def _verdict(
    kind: str,
    identity: int,
    ticker: str,
    *,
    membership: object,
    is_unknown: bool,
    has_fundamentals: bool,
    has_price: bool,
    has_features: bool,
    has_signal: bool,
    has_outcome: bool,
) -> ChainVerdict:
    """Classify one security against the chain, naming the first stage that failed."""
    confidence: str | None = None
    if membership is not None:
        confidence = str(getattr(membership, "confidence", MembershipConfidence.INFERRED))

    if membership is None:
        stage = ChainStage.UNKNOWN_MEMBERSHIP if is_unknown else ChainStage.NO_MEMBERSHIP
    elif not has_price:
        stage = ChainStage.NO_PRICE
    elif not has_fundamentals:
        stage = ChainStage.NO_PIT_DATA
    elif not has_features:
        stage = ChainStage.NO_MARKET_FEATURES
    elif not has_signal:
        stage = ChainStage.NO_SIGNAL
    elif not has_outcome:
        stage = ChainStage.NO_OUTCOME
    else:
        stage = ChainStage.ELIGIBLE

    return ChainVerdict(
        identity_kind=kind,
        identity_id=identity,
        ticker=ticker,
        stage=stage,
        membership_confidence=confidence,
        has_fundamentals=has_fundamentals,
        has_price=has_price,
        has_features=has_features,
        has_signal=has_signal,
        has_outcome=has_outcome,
    )
