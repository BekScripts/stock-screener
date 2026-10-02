"""The prospective fundamentals source: reported values, bound to an accession.

R1.11 found no producer for `score_inputs`. The deeper reason was that no source
carried a reported figure at all. The universe is a broker asset list, the market
snapshot is daily bars, and `sec_availability` records only *when* each filing
became public — accession, form, acceptance timestamp. A CompounderScore cannot
be built from any of that.

This adapter supplies what was missing, under the rule the frozen contract
already states: company evidence is admissible when it was "accepted strictly
before the decision instant". Observations are therefore joined to the
acceptance snapshot by accession and admitted only when that accession was
public at the instant. The join is the point — it is what makes a reported value
provably available rather than merely true.

`get_filing_observations` is the right read and `get_financial_statements` is
not, which is why the frozen `NON_PIT_SAFE_OPERATIONS` refuses the second and
not the first. The merged view resolves every period to its most recently filed
value and so destroys the record of when each became knowable; the unresolved
view keeps one observation per concept, per period, per filing. A restatement
appears twice, and only the copy already public at the instant is admitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from stock_screener.prospective_source.adapters import NORMALIZER_VERSION, SourceResult
from stock_screener.prospective_source.envelope import (
    Completeness,
    SnapshotEnvelope,
    StaleSourceError,
    TemporalClass,
    content_digest,
    request_fingerprint,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from .identity import ProspectiveIdentity

log = structlog.get_logger(__name__)

__all__ = [
    "OBSERVATION_EXCLUSIONS",
    "admitted_accessions_by_ticker",
    "fetch_filing_observations",
]

#: Why a reported observation may not take part in a decision.
OBSERVATION_EXCLUSIONS: dict[str, str] = {
    "NO_ACCESSION": (
        "the observation carries no accession, so it cannot be joined to an "
        "acceptance timestamp and cannot prove it was public at the instant"
    ),
    "NO_FILED_DATE": (
        "the observation carries no filed date. `period_end` is not an "
        "availability date, so this cannot participate point-in-time"
    ),
    "ACCESSION_NOT_PUBLIC_AT_INSTANT": (
        "the filing carrying this value was accepted after the decision instant, "
        "or was not public at it. This is the late-information case"
    ),
    "FILER_NOT_IN_ACCEPTANCE_SNAPSHOT": (
        "no acceptance record covers this filer, so no observation of theirs can "
        "be proven available. The filer is incomplete, not silently admitted"
    ),
}


def admitted_accessions_by_ticker(
    acceptance_records: Sequence[Mapping[str, Any]],
) -> dict[str, set[str]]:
    """Group the acceptance snapshot's admitted accessions by filer.

    Args:
        acceptance_records: The `sec_availability` snapshot's admitted records,
            each already bounded to `accepted_at <= decision instant`.

    Returns:
        `{ticker: {accession, ...}}`, upper-cased tickers.
    """
    grouped: dict[str, set[str]] = {}
    for row in acceptance_records:
        ticker, accession = row.get("ticker"), row.get("accession")
        if ticker and accession:
            grouped.setdefault(str(ticker).upper(), set()).add(str(accession))
    return grouped


def fetch_filing_observations(
    source: Any,
    identities: Sequence[ProspectiveIdentity],
    *,
    decision_instant: datetime,
    admitted: Mapping[str, set[str]],
) -> SourceResult:
    """Snapshot every reported value that was public at the decision instant.

    Args:
        source: A fundamentals source exposing `get_filing_observations`.
        identities: The filers to read, already resolved to research identities.
        decision_instant: The cutoff. Must be aware. Used as `semantic_as_of`,
            because the admissibility of every record here is decided by event
            time rather than by when the bytes arrived.
        admitted: `{ticker: {accession}}` from the acceptance snapshot — the only
            accessions whose values may take part.

    Returns:
        The snapshot, one record per admitted observation.

    Raises:
        StaleSourceError: If the instant is naive, or no filer yielded a single
            admissible observation — a cross-section with no fundamentals is not
            a thin cross-section, it is an unservable decision.
    """
    if decision_instant.tzinfo is None:
        raise StaleSourceError("the decision instant must be timezone-aware")
    retrieved = datetime.now(UTC)
    records: list[dict[str, Any]] = []
    excluded: dict[str, int] = dict.fromkeys(OBSERVATION_EXCLUSIONS, 0)
    unavailable: list[str] = []
    covered: list[str] = []
    for identity in identities:
        public = admitted.get(identity.symbol)
        if not public:
            excluded["FILER_NOT_IN_ACCEPTANCE_SNAPSHOT"] += 1
            unavailable.append(f"{identity.symbol}: no acceptance record")
            continue
        try:
            observations = source.get_filing_observations(identity.symbol)
        except Exception as exc:
            unavailable.append(f"{identity.symbol}: {type(exc).__name__}")
            continue
        kept = 0
        for observation in observations:
            if observation.accession is None:
                excluded["NO_ACCESSION"] += 1
                continue
            if observation.filed_date is None:
                excluded["NO_FILED_DATE"] += 1
                continue
            if str(observation.accession) not in public:
                excluded["ACCESSION_NOT_PUBLIC_AT_INSTANT"] += 1
                continue
            records.append(
                {
                    "kind": identity.kind,
                    "identity_id": identity.identity_id,
                    "symbol": identity.symbol,
                    "taxonomy": observation.taxonomy,
                    "concept": observation.concept,
                    "value": observation.value,
                    "unit": observation.unit,
                    "period_end": observation.period_end.isoformat(),
                    "period_start": (
                        observation.period_start.isoformat() if observation.period_start else None
                    ),
                    "filed_date": observation.filed_date.isoformat(),
                    "accession": observation.accession,
                    "form": observation.form,
                    "fiscal_year": observation.fiscal_year,
                    "fiscal_period": observation.fiscal_period,
                    "frame": observation.frame,
                    "source": observation.source,
                }
            )
            kept += 1
        if kept:
            covered.append(identity.symbol)
    if not records:
        raise StaleSourceError(
            "no filer yielded an observation that was public at the decision "
            "instant; COMPOUNDER_V1_2 cannot be constructed from an empty "
            "fundamentals snapshot"
        )
    records.sort(
        key=lambda row: (
            row["kind"],
            row["identity_id"],
            row["taxonomy"],
            row["concept"],
            row["period_end"],
            row["accession"] or "",
        )
    )
    filed = [row["filed_date"] for row in records]
    payload = {"observations": records, "excluded": excluded}
    envelope = SnapshotEnvelope(
        source_type="filing_observations",
        source_name=type(source).__name__,
        source_version="EDGAR reported facts, unresolved",
        operation="get_filing_observations",
        retrieved_at=retrieved,
        semantic_as_of=decision_instant,
        content_sha256=content_digest(payload),
        request=request_fingerprint(
            type(source).__name__,
            "get_filing_observations",
            {
                "identities": [f"{i.kind}:{i.identity_id}" for i in identities],
                "cutoff": decision_instant.isoformat(),
            },
        ),
        normalizer_version=NORMALIZER_VERSION,
        record_count=len(records),
        completeness=Completeness.INCOMPLETE if unavailable else Completeness.COMPLETE,
        temporal_class=TemporalClass.EVENT_TIME,
        decision_session=decision_instant.date().isoformat(),
        min_source_timestamp=min(filed) if filed else None,
        max_source_timestamp=max(filed) if filed else None,
        ambiguity=unavailable,
        notes=[
            f"{len(covered)} of {len(identities)} filers yielded an admissible observation",
            "admissibility is by accession: a value participates only when the filing "
            "carrying it was accepted at or before the decision instant",
            "the unresolved view is read deliberately; the merged view would resolve "
            "each period to its latest filed value and lose the availability date",
            f"excluded: {excluded}",
        ],
    )
    log.info(
        "prospective.observations",
        admitted=len(records),
        filers=len(covered),
        unavailable=len(unavailable),
    )
    return SourceResult(envelope=envelope, payload=payload, records=records)
