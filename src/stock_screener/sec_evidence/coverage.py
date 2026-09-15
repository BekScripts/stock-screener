"""Measuring what the corpus can actually support, and what it cannot.

Everything here reads the derived index and reports. Nothing writes to
`ResearchUniverseMonth`, nothing fetches, and nothing decides policy — the
carry-forward horizons are *measured* across a range rather than chosen, because
the point of the exercise is to find out whether the horizon already in use is
defensible, and an audit that picked the horizon maximising its own coverage
would be answering a different question.

## A filing is a point, not an interval

The single most important distinction in this module. A cover page states what
was registered on the day the SEC accepted it. It does not say the security was
still listed a month later, and it says nothing at all about the month before.
Carry-forward turns points into spans, which is useful and is also exactly where
a coverage number can be inflated into a fiction — so every carried figure is
reported separately from the direct one, never pooled into a single total.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from itertools import pairwise
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Sequence

log = structlog.get_logger(__name__)

#: Horizons measured for the carry-forward audit. Zero is included because
#: "exact date only" is the honest floor: any coverage above it is an inference.
HORIZONS: tuple[int, ...] = (0, 30, 60, 90, 120, 180)

#: Caps evaluated for the marginal-value curve.
CAPS: tuple[int, ...] = (6, 8, 12, 16, 24)


@dataclass(frozen=True)
class FormValue:
    """What one SEC form actually contributed.

    Attributes:
        form: The form type.
        documents: Stored documents of this form.
        with_symbol: Documents yielding at least one `TradingSymbol`.
        with_title: Documents yielding at least one `Security12bTitle`.
        with_exchange: Documents yielding at least one `SecurityExchangeName`.
        common: Documents yielding at least one common-stock class.
        security_months: Distinct `(symbol, month)` pairs contributed.
    """

    form: str
    documents: int
    with_symbol: int
    with_title: int
    with_exchange: int
    common: int
    security_months: int

    @property
    def yield_rate(self) -> float:
        """Share of documents that produced any usable security row."""
        return self.with_symbol / self.documents if self.documents else 0.0


@dataclass(frozen=True)
class SpanStats:
    """How much dated history one security's stored filings span.

    Attributes:
        cik: The filer.
        filings: Stored qualifying documents.
        earliest: First evidence date.
        latest: Last evidence date.
        span_days: Days between them.
        median_gap: Median days between consecutive observations.
        max_gap: Largest gap.
    """

    cik: int
    filings: int
    earliest: str | None
    latest: str | None
    span_days: int
    median_gap: float
    max_gap: int


def _median(values: Sequence[float]) -> float:
    """Median of a sequence, 0.0 when empty."""
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def span_statistics(db: sqlite3.Connection) -> list[SpanStats]:
    """Measure the dated reach of each filer's stored documents.

    This is the six-filings-per-security question in measurable form: not "did
    we store six" but "how many months of history do six actually pin down, and
    how far apart are they".
    """
    dates: dict[int, list[date]] = defaultdict(list)
    for cik, evidence in db.execute(
        "SELECT cik, evidence_date FROM filings WHERE cik IS NOT NULL AND evidence_date IS NOT NULL"
    ):
        dates[int(cik)].append(date.fromisoformat(evidence))

    stats: list[SpanStats] = []
    for cik, observations in dates.items():
        ordered = sorted(observations)
        gaps = [(later - earlier).days for earlier, later in pairwise(ordered)]
        stats.append(
            SpanStats(
                cik=cik,
                filings=len(ordered),
                earliest=ordered[0].isoformat(),
                latest=ordered[-1].isoformat(),
                span_days=(ordered[-1] - ordered[0]).days,
                median_gap=_median(gaps),
                max_gap=max(gaps) if gaps else 0,
            )
        )
    return stats


def form_value(db: sqlite3.Connection) -> list[FormValue]:
    """Measure which forms carried usable security evidence."""
    documents: dict[str, int] = defaultdict(int)
    for (form,) in db.execute("SELECT COALESCE(form, 'UNKNOWN') FROM filings"):
        documents[str(form)] += 1

    symbol: dict[str, set[str]] = defaultdict(set)
    title: dict[str, set[str]] = defaultdict(set)
    exchange: dict[str, set[str]] = defaultdict(set)
    common: dict[str, set[str]] = defaultdict(set)
    months: dict[str, set[tuple[str, str]]] = defaultdict(set)

    query = """
        SELECT COALESCE(f.form, 'UNKNOWN'), s.url, s.normalized_symbol,
               s.normalized_title, s.normalized_exchange, s.security_class, s.evidence_date
        FROM securities s JOIN filings f ON f.url = s.url
    """
    for form, url, sym, ttl, exch, klass, evidence in db.execute(query):
        form = str(form)
        if sym:
            symbol[form].add(url)
        if ttl:
            title[form].add(url)
        if exch and exch != "UNKNOWN":
            exchange[form].add(url)
        if klass == "COMMON_STOCK":
            common[form].add(url)
            if sym and evidence:
                months[form].add((sym, str(evidence)[:7]))

    return sorted(
        (
            FormValue(
                form=form,
                documents=count,
                with_symbol=len(symbol[form]),
                with_title=len(title[form]),
                with_exchange=len(exchange[form]),
                common=len(common[form]),
                security_months=len(months[form]),
            )
            for form, count in documents.items()
        ),
        key=lambda value: -value.documents,
    )


@dataclass(frozen=True)
class HorizonCoverage:
    """Month-end coverage at one carry-forward horizon.

    Attributes:
        horizon_days: How long a filing's class evidence is allowed to speak for.
        direct: Security-months where a filing landed in that very month.
        carried: Security-months covered only by carrying an earlier filing.
        covered: Distinct security-months with any class evidence.
    """

    horizon_days: int
    direct: int
    carried: int
    covered: int


def carry_forward_coverage(
    db: sqlite3.Connection, horizons: Sequence[int] = HORIZONS
) -> list[HorizonCoverage]:
    """Measure month-end coverage as the carry-forward horizon widens.

    Carrying is forward only. A filing accepted in March may speak for April
    under a 30-day horizon; it may never speak for February, because that would
    be using a document to describe a month before it existed — the
    future-leakage failure this whole layer is built to prevent.

    Args:
        db: The derived index.
        horizons: Day counts to measure.

    Returns:
        One row per horizon, direct and carried kept apart.
    """
    observations: dict[str, list[date]] = defaultdict(list)
    for symbol, evidence in db.execute(
        "SELECT normalized_symbol, evidence_date FROM securities"
        " WHERE security_class = 'COMMON_STOCK' AND normalized_symbol IS NOT NULL"
        " AND evidence_date IS NOT NULL"
    ):
        observations[str(symbol)].append(date.fromisoformat(evidence))

    results: list[HorizonCoverage] = []
    for horizon in horizons:
        direct: set[tuple[str, str]] = set()
        carried: set[tuple[str, str]] = set()
        for symbol, dates in observations.items():
            for observed in dates:
                direct.add((symbol, observed.strftime("%Y-%m")))
                if horizon <= 0:
                    continue
                cursor = observed
                for _ in range(horizon // 28 + 2):
                    cursor = _next_month(cursor)
                    if (cursor - observed).days > horizon:
                        break
                    carried.add((symbol, cursor.strftime("%Y-%m")))
        results.append(
            HorizonCoverage(
                horizon_days=horizon,
                direct=len(direct),
                carried=len(carried - direct),
                covered=len(direct | carried),
            )
        )
    return results


def _next_month(value: date) -> date:
    """The first day of the month after this one."""
    return (
        value.replace(year=value.year + 1, month=1, day=1)
        if value.month == 12
        else value.replace(month=value.month + 1, day=1)
    )


@dataclass(frozen=True)
class EraCoverage:
    """Evidence density split by period.

    Attributes:
        label: The era.
        filings: Stored documents whose evidence date falls in it.
        with_securities: Documents that yielded any registered-class row.
        security_rows: Rows yielded.
        common_rows: Rows classified common stock.
    """

    label: str
    filings: int
    with_securities: int
    security_rows: int
    common_rows: int

    @property
    def yield_rate(self) -> float:
        """Share of documents in this era that produced a usable row."""
        return self.with_securities / self.filings if self.filings else 0.0


def era_coverage(db: sqlite3.Connection, *, boundary: str = "2022-01-01") -> list[EraCoverage]:
    """Split coverage across a boundary, because the corpus is not uniform.

    Inline-XBRL cover facts only became mandatory in stages from 2019, so a
    single averaged coverage figure would hide the fact that recent filings
    answer well and older ones often carry nothing at all. Averaging them is the
    specific way this measurement could mislead.
    """
    # One pass in Python rather than four interpolated queries: the era split is
    # a comparison on a date string, and building SQL predicates by formatting is
    # how a reporting helper grows an injection vector it never needed.
    tallies: dict[str, dict[str, int]] = {
        f"pre-{boundary[:4]}": defaultdict(int),
        f"{boundary[:4]}+": defaultdict(int),
    }
    for evidence, covers, rows_here, common_here in db.execute(
        """
        SELECT f.evidence_date,
               f.cover_securities,
               COUNT(s.id),
               SUM(CASE WHEN s.security_class = 'COMMON_STOCK' THEN 1 ELSE 0 END)
        FROM filings f LEFT JOIN securities s ON s.url = f.url
        WHERE f.evidence_date IS NOT NULL
        GROUP BY f.url
        """
    ):
        era = tallies[f"pre-{boundary[:4]}" if str(evidence) < boundary else f"{boundary[:4]}+"]
        era["filings"] += 1
        era["with_securities"] += 1 if int(covers or 0) > 0 else 0
        era["security_rows"] += int(rows_here or 0)
        era["common_rows"] += int(common_here or 0)

    return [
        EraCoverage(
            label=label,
            filings=counts["filings"],
            with_securities=counts["with_securities"],
            security_rows=counts["security_rows"],
            common_rows=counts["common_rows"],
        )
        for label, counts in tallies.items()
    ]


@dataclass(frozen=True)
class CatalogueOpportunity:
    """What the submissions catalogues describe but the corpus does not hold.

    The distinction this type exists to keep: catalogue metadata proves a filing
    *exists* and names its date. It says nothing about what the cover page of
    that filing contains. So `not_downloaded` is an opportunity to investigate,
    never coverage already earned.

    Attributes:
        qualifying: Filings the catalogues describe in qualifying forms.
        downloaded: Of those, ones the corpus holds as documents.
        not_downloaded: Ones it does not.
        earliest: Earliest catalogue filing date.
        latest: Latest.
        ciks: Filers with a catalogue.
        truncated_ciks: Filers whose catalogue references further history files
            that were never fetched, so even the metadata is incomplete.
        additional_history_files: How many such files are referenced.
    """

    qualifying: int
    downloaded: int
    not_downloaded: int
    earliest: str | None
    latest: str | None
    ciks: int
    truncated_ciks: int
    additional_history_files: int


def catalogue_opportunity(db: sqlite3.Connection) -> CatalogueOpportunity:
    """Measure the filing history the catalogues know about, offline."""
    qualifying, earliest, latest, ciks = db.execute(
        "SELECT COUNT(*), MIN(filing_date), MAX(filing_date), COUNT(DISTINCT cik) FROM catalogue"
    ).fetchone()
    downloaded = db.execute(
        "SELECT COUNT(*) FROM catalogue c WHERE EXISTS"
        " (SELECT 1 FROM filings f WHERE f.accession = c.accession)"
    ).fetchone()[0]
    truncated, blocks = db.execute(
        "SELECT COUNT(*), COALESCE(SUM(older_blocks), 0)"
        " FROM catalogue_stats WHERE older_blocks > 0"
    ).fetchone()
    return CatalogueOpportunity(
        qualifying=int(qualifying),
        downloaded=int(downloaded),
        not_downloaded=int(qualifying) - int(downloaded),
        earliest=earliest,
        latest=latest,
        ciks=int(ciks),
        truncated_ciks=int(truncated),
        additional_history_files=int(blocks),
    )


@dataclass(frozen=True)
class CapEstimate:
    """What raising the per-security filing cap would cost and might buy.

    Attributes:
        cap: Documents per filer.
        documents: Documents the corpus would hold at this cap.
        additional: How many more than it holds now.
        estimated_bytes: Storage for those, from measured mean size.
        reachable_months: Distinct filer-months the *metadata* covers at this
            cap — an upper bound on what could be learned, never a claim about
            cover-page contents we have not read.
    """

    cap: int
    documents: int
    additional: int
    estimated_bytes: int
    reachable_months: int


def cap_curve(
    db: sqlite3.Connection, caps: Sequence[int] = CAPS, *, mean_bytes: float | None = None
) -> list[CapEstimate]:
    """Estimate the marginal value of storing more filings per security.

    Measured from catalogue metadata, which is why the returned months are
    labelled *reachable* rather than covered. We cannot know what a cover page
    we never downloaded says; we can know the filing exists and when it was
    filed, and that bounds the opportunity from above.
    """
    if mean_bytes is None:
        row = db.execute("SELECT AVG(bytes) FROM filings WHERE bytes IS NOT NULL").fetchone()
        mean_bytes = float(row[0] or 0)

    held = int(db.execute("SELECT COUNT(*) FROM filings").fetchone()[0])
    by_cik: dict[int, list[str]] = defaultdict(list)
    for cik, filing_date in db.execute(
        "SELECT cik, filing_date FROM catalogue ORDER BY filing_date DESC"
    ):
        by_cik[int(cik)].append(str(filing_date))

    estimates: list[CapEstimate] = []
    for cap in caps:
        documents = 0
        months: set[tuple[int, str]] = set()
        for cik, dates in by_cik.items():
            kept = dates[:cap]
            documents += len(kept)
            months.update((cik, value[:7]) for value in kept)
        estimates.append(
            CapEstimate(
                cap=cap,
                documents=documents,
                additional=max(0, documents - held),
                estimated_bytes=int(max(0, documents - held) * mean_bytes),
                reachable_months=len(months),
            )
        )

    everything = sum(len(dates) for dates in by_cik.values())
    all_months = {(cik, value[:7]) for cik, dates in by_cik.items() for value in dates}
    estimates.append(
        CapEstimate(
            cap=0,  # 0 reads as "all relevant", reported separately
            documents=everything,
            additional=max(0, everything - held),
            estimated_bytes=int(max(0, everything - held) * mean_bytes),
            reachable_months=len(all_months),
        )
    )
    return estimates


@dataclass(frozen=True)
class ClassBreakdown:
    """Registered-class rows by verdict and by how they were judged."""

    by_class: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    by_exchange: dict[str, int] = field(default_factory=dict)

    @property
    def common(self) -> int:
        """Rows judged common stock."""
        return self.by_class.get("COMMON_STOCK", 0)

    @property
    def uncertain(self) -> int:
        """Rows the evidence could not place."""
        return self.by_class.get("UNKNOWN", 0)

    @property
    def non_common(self) -> int:
        """Rows judged something other than common stock."""
        return sum(v for k, v in self.by_class.items() if k not in {"COMMON_STOCK", "UNKNOWN"})


def class_breakdown(db: sqlite3.Connection) -> ClassBreakdown:
    """Count registered-class rows by verdict, reason and venue."""

    def tally(column: str) -> dict[str, int]:
        return {
            str(k): int(v)
            for k, v in db.execute(
                f"SELECT {column}, COUNT(*) FROM securities GROUP BY {column}"  # noqa: S608
            )
        }

    return ClassBreakdown(
        by_class=tally("security_class"),
        by_reason=tally("classification_reason"),
        by_exchange=tally("normalized_exchange"),
    )
