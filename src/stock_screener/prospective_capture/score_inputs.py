"""Turning frozen prospective snapshots into COMPOUNDER_V1_2's actual inputs.

This is the producer R1.11 found missing. It takes the sealed snapshots and
builds, per identity, exactly what the frozen score consumes: a profile, the
financial periods behind it, the price history, and the derived metrics. It
performs no I/O, which is what makes it replayable from the sealed artifacts and
what satisfies the rule that nothing strategy-relevant is fetched once scoring
has begun.

Every transformation here is the shared one. `periods_from_observations` is the
same function the historical reconstruction calls, and `build_company_metrics`
is the same metric engine. Nothing in this module reimplements a formula; if it
did, the prospective and historical streams could disagree and no test would
catch it.

Two degradations are recorded rather than hidden, because the prospective plane
has no source for either and improvising one would be worse than declaring it:
no FX rate resolves, so a foreign filer's currency-sensitive ratios are
unavailable exactly as they are when the live path cannot resolve a rate; and
the market capitalisation's share basis is corrected only by the corporate
actions the prospective window actually observed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

import structlog

from api_clients import periods_from_observations
from domain import (
    DEFAULT_LIQUIDITY_WINDOW,
    CompanyProfile,
    CorporateActionKind,
    CorporateActionRecord,
    FilingObservation,
    PriceBar,
    VolumeBasis,
    as_traded_price,
    build_company_metrics,
    latest_close,
)
from stock_screener.prospective_source.envelope import content_digest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from domain import CompanyMetrics, FinancialPeriod

    from .identity import ProspectiveIdentity

log = structlog.get_logger(__name__)

__all__ = [
    "KNOWN_DEGRADATIONS",
    "SCORE_INPUT_ARTIFACT_ID",
    "ScoreInput",
    "ScoreInputSet",
    "build_score_inputs",
]

#: The artifact's identity, recorded in the decision packet's provenance.
SCORE_INPUT_ARTIFACT_ID = "PROSPECTIVE_SCORE_INPUTS_V1"

#: The cover-page share count's concept, which dates that figure. Matching the
#: historical reconstruction exactly, because a different anchor would value the
#: company differently across a split.
_COVER_PAGE_SHARES = "EntityCommonStockSharesOutstanding"

#: What this producer cannot supply, stated rather than silently absorbed.
KNOWN_DEGRADATIONS: dict[str, str] = {
    "FX_UNAVAILABLE": (
        "the prospective plane designates no FX source, so a foreign filer's "
        "currency-sensitive ratios are unavailable. This is the documented "
        "degradation of the live path when no rate resolves, not a new policy: "
        "missing stays missing and is never filled with an unconverted figure."
    ),
    "SHARE_BASIS_CORRECTED_FROM_OBSERVED_ACTIONS_ONLY": (
        "bars are adjusted, so recovering the price the share count was stated "
        "against needs the split factors since that date. Only the corporate "
        "actions the prospective window retrieved are applied; a split outside "
        "that window is not corrected for."
    ),
}


def _to_observation(row: Mapping[str, Any]) -> FilingObservation:
    """Rebuild a domain observation from its snapshot record."""
    return FilingObservation(
        taxonomy=str(row["taxonomy"]),
        concept=str(row["concept"]),
        value=float(row["value"]),
        unit=str(row["unit"]),
        period_end=date.fromisoformat(str(row["period_end"])),
        period_start=(
            date.fromisoformat(str(row["period_start"])) if row.get("period_start") else None
        ),
        filed_date=date.fromisoformat(str(row["filed_date"])),
        accession=row.get("accession"),
        form=row.get("form"),
        fiscal_year=row.get("fiscal_year"),
        fiscal_period=row.get("fiscal_period"),
        frame=row.get("frame"),
        source=str(row.get("source") or "unknown"),
    )


def _to_bar(row: Mapping[str, Any]) -> PriceBar:
    """Rebuild a domain price bar from its snapshot record."""
    return PriceBar(
        date=date.fromisoformat(str(row["session"])),
        open=float(row["open"]),
        high=float(row["high"]),
        low=float(row["low"]),
        close=float(row["close"]),
        volume=float(row["volume"]),
    )


def _to_action(row: Mapping[str, Any]) -> CorporateActionRecord:
    """Rebuild a domain corporate action from its snapshot record."""
    return CorporateActionRecord(
        symbol=str(row["symbol"]),
        kind=CorporateActionKind(str(row["kind"])),
        ex_date=date.fromisoformat(str(row["ex_date"])),
        split_factor=row.get("split_factor"),
        cash_amount=row.get("cash_amount"),
        currency=row.get("currency"),
        source=str(row.get("source") or "prospective"),
    )


def _share_count_date(observations: Sequence[FilingObservation]) -> date | None:
    """The instant the newest available cover-page share count describes.

    Its own `period_end`, not the filing date — a count stated as of 17 July and
    filed on 31 July is a statement about 17 July, and a split between the two is
    not in it.

    Args:
        observations: The admissible observations for one identity.

    Returns:
        The period end of the newest cover-page count, or None if none was filed.
    """
    dated = [
        observation.period_end
        for observation in observations
        if observation.concept == _COVER_PAGE_SHARES
    ]
    return max(dated) if dated else None


@dataclass(frozen=True)
class ScoreInput:
    """One identity's inputs to the frozen score, with the provenance to replay it.

    Attributes:
        kind: `COMPANY`.
        identity_id: The research identity.
        symbol: Display only.
        profile: The classification and listing attributes the score reads.
        metrics: The derived metrics, or None when nothing had been filed.
        periods: The financial periods behind the metrics.
        provenance: Everything needed to answer which inputs produced this.
    """

    kind: str
    identity_id: int
    symbol: str
    profile: CompanyProfile
    metrics: CompanyMetrics | None
    periods: tuple[FinancialPeriod, ...]
    provenance: dict[str, Any]

    @property
    def key(self) -> tuple[str, int]:
        """The identity, as the harness addresses it."""
        return (self.kind, self.identity_id)


@dataclass
class ScoreInputSet:
    """Every identity's score inputs for one decision, as a sealable artifact.

    Attributes:
        decision_session: The session these inputs describe.
        decision_instant: The instant they are bounded to.
        inputs: One entry per identity, in identity order.
        source_fingerprints: The snapshots these were built from.
        degradations: Which known degradations applied, and to how many.
    """

    decision_session: str
    decision_instant: str
    inputs: list[ScoreInput] = field(default_factory=list)
    source_fingerprints: dict[str, str] = field(default_factory=dict)
    degradations: dict[str, int] = field(default_factory=dict)

    @property
    def scorable(self) -> int:
        """How many identities produced metrics the score can read."""
        return sum(1 for row in self.inputs if row.metrics is not None)

    def body(self) -> dict[str, Any]:
        """The fingerprinted body: inputs and bindings, no run metadata.

        Returns:
            A JSON-safe mapping that is identical for identical inputs, so the
            fingerprint is a property of the decision rather than of the run.
        """
        return {
            "artifact_id": SCORE_INPUT_ARTIFACT_ID,
            "decision_session": self.decision_session,
            "decision_instant": self.decision_instant,
            "score_version": "COMPOUNDER_V1_2",
            "source_fingerprints": dict(sorted(self.source_fingerprints.items())),
            "identities": [
                {
                    "kind": row.kind,
                    "identity_id": row.identity_id,
                    "symbol": row.symbol,
                    "has_metrics": row.metrics is not None,
                    "provenance": row.provenance,
                }
                for row in self.inputs
            ],
        }

    def fingerprint(self) -> str:
        """The artifact's deterministic digest."""
        return content_digest(self.body())

    def as_json(self) -> dict[str, Any]:
        """Return the artifact with its fingerprint and counts."""
        return {
            **self.body(),
            "fingerprint": self.fingerprint(),
            "identity_count": len(self.inputs),
            "scorable_count": self.scorable,
            "degradations": dict(sorted(self.degradations.items())),
            "degradation_meanings": dict(KNOWN_DEGRADATIONS),
            "built_without_network": True,
            "missing_is_not_zero": True,
        }


def build_score_inputs(
    identities: Sequence[ProspectiveIdentity],
    *,
    observation_records: Sequence[Mapping[str, Any]],
    bar_records: Sequence[Mapping[str, Any]],
    action_records: Sequence[Mapping[str, Any]] = (),
    decision_session: date,
    decision_instant: str,
    source_fingerprints: Mapping[str, str],
    bar_volume_basis: VolumeBasis = VolumeBasis.UNKNOWN,
    liquidity_window: int = DEFAULT_LIQUIDITY_WINDOW,
) -> ScoreInputSet:
    """Build every identity's score inputs from the sealed snapshots.

    Args:
        identities: The resolved identities to build for.
        observation_records: The admissible filing observations.
        bar_records: The admissible daily bars.
        action_records: Corporate actions observed in the window, for the share
            basis correction.
        decision_session: The session being decided.
        decision_instant: The instant, ISO with offset, recorded in provenance.
        source_fingerprints: `{source: digest}` these inputs derive from.
        bar_volume_basis: What the bars' volume represents.
        liquidity_window: Sessions to average dollar volume over.

    Returns:
        The artifact. Identities with nothing filed are present and carry no
        metrics, because an absent identity and an unscorable one are different
        facts and the funnel must show both.
    """
    by_identity: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for row in observation_records:
        by_identity.setdefault((str(row["kind"]), int(row["identity_id"])), []).append(row)
    bars_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in bar_records:
        bars_by_symbol.setdefault(str(row["ticker"]).upper(), []).append(row)
    actions_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for row in action_records:
        actions_by_symbol.setdefault(str(row["symbol"]).upper(), []).append(row)

    built = ScoreInputSet(
        decision_session=decision_session.isoformat(),
        decision_instant=decision_instant,
        source_fingerprints=dict(source_fingerprints),
    )
    degradations: dict[str, int] = dict.fromkeys(KNOWN_DEGRADATIONS, 0)
    for identity in identities:
        raw = by_identity.get(identity.key, [])
        observations = [_to_observation(row) for row in raw]
        periods = periods_from_observations(observations)
        bars = sorted(
            (_to_bar(row) for row in bars_by_symbol.get(identity.symbol, [])),
            key=lambda bar: bar.date,
        )
        actions = [_to_action(row) for row in actions_by_symbol.get(identity.symbol, [])]
        # The profile's market capitalisation is never carried: a current quote
        # against a current share count answers a different question than the
        # decision instant asks. The metric engine rebuilds it point-in-time.
        profile = CompanyProfile(
            ticker=identity.symbol,
            name=identity.name or identity.symbol,
            exchange=identity.exchange,
            sector=identity.sector,
            industry=identity.industry,
            market_cap=None,
            quote_currency=identity.quote_currency,
        )
        metrics: CompanyMetrics | None = None
        unusable: str | None = None
        share_basis = None
        count_anchor = _share_count_date(observations)
        if not periods:
            unusable = "NOTHING_FILED_BY_DECISION_INSTANT"
        elif not bars:
            unusable = "NO_PRICE_HISTORY_THROUGH_DECISION_SESSION"
        else:
            anchor = count_anchor or decision_session
            share_basis = as_traded_price(latest_close(bars), actions, anchor)
            if not actions:
                degradations["SHARE_BASIS_CORRECTED_FROM_OBSERVED_ACTIONS_ONLY"] += 1
            degradations["FX_UNAVAILABLE"] += 1
            metrics = build_company_metrics(
                profile,
                periods,
                bars,
                liquidity_window=liquidity_window,
                bar_volume_basis=bar_volume_basis,
                fx=None,
                share_basis_close=share_basis,
            )
        sessions = [bar.date.isoformat() for bar in bars]
        built.inputs.append(
            ScoreInput(
                kind=identity.kind,
                identity_id=identity.identity_id,
                symbol=identity.symbol,
                profile=profile,
                metrics=metrics,
                periods=tuple(periods),
                provenance={
                    "decision_instant": decision_instant,
                    "accessions": sorted(
                        {str(row["accession"]) for row in raw if row.get("accession")}
                    ),
                    "observations_admitted": len(raw),
                    "period_ends": [period.period_end.isoformat() for period in periods],
                    "periods_used": len(periods),
                    "price_sessions": len(sessions),
                    "first_session": sessions[0] if sessions else None,
                    "last_session": sessions[-1] if sessions else None,
                    "share_basis_close": share_basis,
                    "share_count_anchor": (count_anchor.isoformat() if count_anchor else None),
                    "corporate_actions_applied": len(actions),
                    "fx": None,
                    "unusable_reason": unusable,
                    "score_version": "COMPOUNDER_V1_2",
                },
            )
        )
    built.inputs.sort(key=lambda row: (row.kind, row.identity_id))
    built.degradations = degradations
    log.info(
        "prospective.score_inputs",
        identities=len(built.inputs),
        scorable=built.scorable,
        fingerprint=built.fingerprint()[:16],
    )
    return built
