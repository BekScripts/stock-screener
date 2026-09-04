"""What a security *is*, kept separate from what it is currently called.

`survivorship` answers who was in the universe on a past date. That question is
only answerable if the thing being asked about has a stable identity, and every
handle the free record offers is a *label* rather than an identity:

- a **ticker** is a lease. `FB` became `META` with no change of company; `BBBY`
  passed from Bed Bath & Beyond to Beyond, Inc. on 2025-09-02, twenty-eight months
  after the first one's equity was extinguished. Joining on it splices two
  companies into one history.
- a **CUSIP** is a dated identifier of an *issue*, not a permanent identifier of
  a lineage. A reverse split retires one CUSIP and mints another while the ticker,
  the issuer and the economics carry straight through. Keying on it splits one
  company's history in two.
- a **CIK** is stable and is the right identity when it exists, but the free
  record establishes one for only a minority of delisted symbols. A schema that
  requires it cannot record the majority of the names it exists to record.

So none of the three is the key. The key is an opaque internal `security_id`, and
ticker, CUSIP and venue hang off it as **dated intervals**: this symbol, this
CUSIP, on this venue, valid from this date to that one, on this source's word.
The three rules that follow are the whole design:

1. A ticker change **preserves** `security_id` — the old interval closes and a
   new one opens against the same security.
2. A ticker reused by another issuer takes a **different** `security_id` — two
   securities, two interval sets, no join between them.
3. A reverse split that mints a new CUSIP **preserves** `security_id`, for the
   same reason as (1): the identifier changed, the security did not.

Nothing here performs I/O, reads a clock, or reaches configuration. It is given
intervals and evidence and returns a verdict — including, often, the verdict that
the evidence does not support one. That refusal is the point. A resolver that
always answered would be manufacturing identity, and a manufactured identity is
worse than an absent one: it is an absent one that a study cannot see.

## On evidence, and on its absence

The SEC's fails-to-deliver file is the only free, dated, CUSIP-bearing record
that spans two decades of U.S. equities, so it is the identity evidence this
module is built to read. It has one asymmetry that must never be forgotten:

**A positive match is evidence. An absence is not.** A security appears in the
file only when a delivery failed on that settlement date, and before 2008-09-16
only when the failing balance also cleared a reporting threshold. A name absent
from the whole file may have traded for twenty years without a single failed
delivery. `existence_evidence` therefore returns `UNPROVEN`, never `ABSENT`, and
there is no member of `EvidenceVerdict` that would let a caller record otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

#: The date the SEC stopped applying a reporting threshold to the fails-to-deliver
#: file. Before it, only a security whose failing balance reached the threshold of
#: the day was published at all, so an absence over any window starting earlier is
#: doubly uninformative — it may mean no failure, or a failure too small to print.
FTD_THRESHOLD_LIFTED = date(2008, 9, 16)

#: The most days two issuers' evidence windows may share under one symbol and
#: still be read as a handover rather than a concurrent claim. One: a CUSIP
#: transition effective on a settlement date puts the outgoing issue's last
#: published failure and the incoming issue's first on that same date, so a
#: sequence shows up as contact. See `IssuerSpan.concurrent_with`.
_HANDOVER_CONTACT_DAYS = 1

#: How many leading characters of a CUSIP identify the *issuer* rather than the
#: issue. This is the CUSIP standard, not a heuristic: characters 1-6 are the
#: issuer number, 7-8 the issue, 9 the check digit. Two CUSIPs sharing a base
#: are two issues of one issuer; two that differ in it are two issuers, and no
#: lineage may ever be drawn between them.
CUSIP_ISSUER_CHARS = 6

#: The full length of a CUSIP or its CINS foreign equivalent.
CUSIP_LENGTH = 9

#: One day, for closing an interval the day before its successor opens. Named so the
#: arithmetic in `succeed_interval` reads as the rule rather than as a bare literal.
_ONE_DAY = timedelta(days=1)

#: Character values for the CUSIP check digit: digits are worth their face value,
#: letters 10-35, and the three permitted symbols follow them.
_CUSIP_ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ*@#"


class IdentityConfidence(StrEnum):
    """How an identifier interval came to be believed.

    Stored beside every interval because the three are not interchangeable. An
    `OBSERVED` interval can be checked against the file that produced it; an
    `INFERRED` one rests on a rule this module applied; an `ASSERTED` one rests
    on a person's reading of evidence no feed carries. A study that wants only
    the first must be able to ask for it.
    """

    OBSERVED = "OBSERVED"
    """A source recorded this pairing on these dates. The strongest kind: the
    interval's endpoints are settlement dates that actually appear in a file."""

    INFERRED = "INFERRED"
    """Derived by a rule stated in this module — the CUSIP issuer base, an
    interval closed where the next one opens. Reproducible from the observed
    evidence, but not itself observed."""

    ASSERTED = "ASSERTED"
    """Established from evidence outside the feeds and supplied by a caller — a
    filed corporate action, an exchange notice. The only route by which a
    lineage the data cannot show may be recorded at all."""


class LineageKind(StrEnum):
    """Why an identifier interval opened, which is how lineage is recorded.

    A security's history is its ordered intervals, and this says what happened at
    each boundary. It is deliberately on the *interval* rather than in a table of
    its own: a lineage row that could disagree with the intervals it describes
    would be a second source of truth about the same fact.
    """

    INITIAL = "INITIAL"
    """The earliest interval known for this security. Asserts nothing about the
    time before it — the security may well have traded under this identifier for
    years before the record begins."""

    TICKER_CHANGE = "TICKER_CHANGE"
    """The symbol changed and the CUSIP did not. One security throughout."""

    CUSIP_CHANGE = "CUSIP_CHANGE"
    """The CUSIP changed within one issuer. The security carries through; only
    the issue identifier was retired and reminted."""

    REVERSE_SPLIT = "REVERSE_SPLIT"
    """A CUSIP change caused by a reverse split. Recorded separately from
    `CUSIP_CHANGE` because it is the case that most tempts a reader to treat the
    new CUSIP as a new security, and it is emphatically not one."""

    FORWARD_SPLIT = "FORWARD_SPLIT"
    """A CUSIP change caused by a forward split. Rare — most forward splits keep
    the CUSIP — and recorded for the same reason as its reverse."""

    REORGANISATION = "REORGANISATION"
    """A re-incorporation, redomicile or holding-company insertion that minted a
    new CUSIP over unchanged economics. Only ever `ASSERTED`: no free feed states
    it, and inferring it from a CUSIP change would be a guess."""

    VENUE_CHANGE = "VENUE_CHANGE"
    """The security moved exchange keeping its ticker and CUSIP."""


class ResolutionStatus(StrEnum):
    """The outcome of asking which security an identifier named on a date."""

    RESOLVED = "RESOLVED"
    """Exactly one security's intervals cover the identifier on the date."""

    AMBIGUOUS = "AMBIGUOUS"
    """More than one security's intervals cover it. The candidates are returned;
    a single answer is not, because the record does not contain one."""

    UNRESOLVED = "UNRESOLVED"
    """No interval covers it. The identifier may have been in use and unrecorded;
    this says the record cannot place it, never that it did not exist."""


class EvidenceVerdict(StrEnum):
    """What the fails-to-deliver record establishes about a security's existence.

    Two members, and the missing third is the design. There is no `ABSENT`,
    because the file cannot support one: a security appears in it only when a
    delivery failed, so silence is silence about deliveries, not about listing.
    """

    PRESENT = "PRESENT"
    """At least one dated row names this security. It existed and it traded."""

    UNPROVEN = "UNPROVEN"
    """No row names it. Nothing follows about whether it existed."""


class SymbolStatus(StrEnum):
    """What a *symbol* — as opposed to a security — resolves to across all time.

    A symbol is not an identity, so this is a statement about a lease's history
    rather than about a company. `REUSED` is the interesting member: it is not a
    failure, it is the correct answer for `BBBY`, and it is resolvable the moment
    a date accompanies the symbol.
    """

    RESOLVED = "RESOLVED"
    """One issuer held the symbol throughout the evidence."""

    REUSED = "REUSED"
    """Several issuers held it over windows that do not overlap. Each is its own
    security; the symbol alone names none of them, and the symbol *with a date*
    names exactly one."""

    AMBIGUOUS = "AMBIGUOUS"
    """Several issuers' windows overlap, or the evidence disagrees with itself.
    No security may be minted from it without corroboration."""

    UNRESOLVED = "UNRESOLVED"
    """No evidence carries the symbol at all."""


def cusip_issuer_base(cusip: str) -> str:
    """Return the six characters of a CUSIP that identify its issuer.

    Args:
        cusip: A CUSIP or CINS, of any case.

    Returns:
        The upper-cased issuer base. A string shorter than the base is returned
        upper-cased and whole rather than padded, so a malformed value groups
        only with itself.
    """
    return cusip.strip().upper()[:CUSIP_ISSUER_CHARS]


def is_valid_cusip(cusip: str) -> bool:
    """Check a CUSIP against its own check digit.

    The modulus-10 "double-add-double" the CUSIP standard defines, which CINS
    foreign identifiers share. Used to separate a real identifier from a
    malformed cell, never to correct one: a value that fails is recorded as
    having failed, and no repaired version of it is invented.

    Args:
        cusip: The candidate identifier.

    Returns:
        True when the value is nine permitted characters whose check digit
        agrees with the other eight.
    """
    value = cusip.strip().upper()
    if len(value) != CUSIP_LENGTH:
        return False
    total = 0
    for position, character in enumerate(value[:-1]):
        index = _CUSIP_ALPHABET.find(character)
        if index < 0:
            return False
        if position % 2:
            index *= 2
        total += index // 10 + index % 10
    if not value[-1].isdigit():
        return False
    return (10 - total % 10) % 10 == int(value[-1])


@dataclass(frozen=True)
class FtdIdentityEvidence:
    """One distinct identity triple the fails-to-deliver record carries.

    Not a raw row. The file prints one line per security per settlement date, and
    the identity content of a million such lines is a handful of triples; this is
    one of them, with the window over which it was seen and the provenance needed
    to find the lines again.

    Attributes:
        cusip: The identifier as printed, upper-cased.
        symbol: The trading symbol as printed, upper-cased.
        issuer_name: The description field, which names the issuer.
        first_settlement: Earliest settlement date carrying the triple.
        last_settlement: Latest settlement date carrying it.
        observations: How many rows across the whole record carried it.
        source_file: The archive member the triple was first seen in.
        evidence_hash: A digest of the triple, so a stored row can be matched
            back to the evidence that produced it.
    """

    cusip: str
    symbol: str
    issuer_name: str
    first_settlement: date
    last_settlement: date
    observations: int
    source_file: str
    evidence_hash: str

    @property
    def issuer_base(self) -> str:
        """The CUSIP's issuer field — what makes two issues one issuer."""
        return cusip_issuer_base(self.cusip)


@dataclass(frozen=True)
class IdentifierInterval:
    """One identifier's claim on one security over one closed date range.

    The intervals of a `security_id` are its identity. Ranges are **inclusive at
    both ends**: an identifier valid through 2023-05-01 and replaced on 2023-05-02
    is stored as `valid_to=2023-05-01` on the first and `valid_from=2023-05-02` on
    the second, so no date is covered twice and none falls between them.

    Attributes:
        security_id: The stable internal identity. Never the CUSIP, never the
            ticker — see the module docstring.
        cusip: The issue identifier over this range, or None when the interval
            records a ticker whose CUSIP the record does not establish.
        ticker: The trading symbol over this range, or None.
        venue: The exchange over this range, or None where unestablished.
        valid_from: First date the claim holds, inclusive.
        valid_to: Last date it holds, inclusive. None means still open — not
            "unknown", and never a stand-in for one.
        source: What established the interval.
        confidence: How it came to be believed.
        lineage: Why the interval opened.
    """

    security_id: int
    cusip: str | None
    ticker: str | None
    venue: str | None
    valid_from: date
    valid_to: date | None
    source: str
    confidence: IdentityConfidence = IdentityConfidence.OBSERVED
    lineage: LineageKind = LineageKind.INITIAL

    def covers(self, on: date) -> bool:
        """Whether this interval's claim holds on a date.

        Args:
            on: The date to test.

        Returns:
            True when `on` falls within the inclusive range.
        """
        if on < self.valid_from:
            return False
        return self.valid_to is None or on <= self.valid_to

    def matches_cusip(self, cusip: str) -> bool:
        """Whether this interval carries a CUSIP, compared case-insensitively."""
        return self.cusip is not None and self.cusip.upper() == cusip.strip().upper()

    def matches_ticker(self, ticker: str) -> bool:
        """Whether this interval carries a ticker, compared case-insensitively."""
        return self.ticker is not None and self.ticker.upper() == ticker.strip().upper()


@dataclass(frozen=True)
class IdentityResolution:
    """The answer to "which security did this identifier name on this date".

    Attributes:
        status: Whether one security, several, or none.
        security_id: The single answer when `status` is `RESOLVED`, else None.
            Never populated for an ambiguous query — a caller that wants a guess
            has to write the guess itself, visibly.
        candidates: Every security whose intervals covered the query, sorted. One
            entry when resolved, several when ambiguous, empty when unresolved.
        reason: A short explanation, for an audit line.
    """

    status: ResolutionStatus
    security_id: int | None
    candidates: tuple[int, ...]
    reason: str

    @property
    def resolved(self) -> bool:
        """True only when exactly one security was named."""
        return self.status is ResolutionStatus.RESOLVED


def _resolve(
    matched: Iterable[IdentifierInterval],
    *,
    label: str,
    as_of: date,
) -> IdentityResolution:
    """Turn the intervals covering a query into a resolution verdict."""
    candidates = sorted({interval.security_id for interval in matched})
    if not candidates:
        return IdentityResolution(
            status=ResolutionStatus.UNRESOLVED,
            security_id=None,
            candidates=(),
            reason=f"no interval covers {label} on {as_of}",
        )
    if len(candidates) > 1:
        return IdentityResolution(
            status=ResolutionStatus.AMBIGUOUS,
            security_id=None,
            candidates=tuple(candidates),
            reason=(
                f"{label} on {as_of} is covered by {len(candidates)} securities "
                f"({', '.join(str(c) for c in candidates)}); the record names no single one"
            ),
        )
    return IdentityResolution(
        status=ResolutionStatus.RESOLVED,
        security_id=candidates[0],
        candidates=(candidates[0],),
        reason=f"{label} on {as_of} names security {candidates[0]}",
    )


def resolve_by_ticker(
    intervals: Iterable[IdentifierInterval], ticker: str, as_of: date
) -> IdentityResolution:
    """Resolve a trading symbol to a security, as of a date.

    The date is not optional and has no default, because a symbol without one is
    not a question this module can answer: `BBBY` on 2022-06-30 and `BBBY` on
    2025-09-30 name different companies, and a signature that let a caller omit
    the date would let them ask the unanswerable version by accident.

    Args:
        intervals: Every identifier interval to search — typically one security's
            or the whole record's.
        ticker: The symbol, of any case.
        as_of: The date the symbol is being read on.

    Returns:
        The verdict, `AMBIGUOUS` rather than a choice where the record holds two.
    """
    matched = [i for i in intervals if i.matches_ticker(ticker) and i.covers(as_of)]
    return _resolve(matched, label=f"ticker {ticker.strip().upper()}", as_of=as_of)


def resolve_by_cusip(
    intervals: Iterable[IdentifierInterval], cusip: str, as_of: date
) -> IdentityResolution:
    """Resolve a CUSIP to a security, as of a date.

    Dated for the same reason `resolve_by_ticker` is: a CUSIP retired at a
    reverse split and later reassigned by its agent is not a permanent handle
    either, and an interval that has closed must stop answering.

    Args:
        intervals: Every identifier interval to search.
        cusip: The identifier, of any case.
        as_of: The date the identifier is being read on.

    Returns:
        The verdict.
    """
    matched = [i for i in intervals if i.matches_cusip(cusip) and i.covers(as_of)]
    return _resolve(matched, label=f"cusip {cusip.strip().upper()}", as_of=as_of)


@dataclass(frozen=True)
class ExistenceEvidence:
    """What the record establishes about whether a security ever traded.

    Attributes:
        verdict: `PRESENT` or `UNPROVEN`. There is no third value; see
            `EvidenceVerdict`.
        observations: How many dated rows named it.
        first_seen: Earliest settlement date naming it, or None.
        last_seen: Latest settlement date naming it, or None.
        threshold_censored: True when the window asked about begins before the
            SEC lifted the reporting threshold, so an absence is uninformative
            for a second, independent reason.
        detail: The sentence an audit should print.
    """

    verdict: EvidenceVerdict
    observations: int
    first_seen: date | None
    last_seen: date | None
    threshold_censored: bool
    detail: str

    @property
    def proves_existence(self) -> bool:
        """True only for a positive match. Never true for an absence."""
        return self.verdict is EvidenceVerdict.PRESENT


def existence_evidence(
    evidence: Sequence[FtdIdentityEvidence],
    *,
    window_start: date | None = None,
) -> ExistenceEvidence:
    """Judge what fails-to-deliver rows establish about a security's existence.

    The asymmetry this function exists to preserve: a match proves the security
    traded on a date; no match proves nothing at all. The `UNPROVEN` verdict is
    therefore the *only* thing an empty sequence can produce, and no argument to
    this function can turn it into a claim of non-existence.

    Args:
        evidence: Every identity triple found for the security or symbol.
        window_start: The earliest date the caller searched. When it precedes
            `FTD_THRESHOLD_LIFTED` the result is flagged as threshold-censored,
            because the file of that era printed only failures large enough to
            report.

    Returns:
        The verdict and the reason for it.
    """
    censored = window_start is not None and window_start < FTD_THRESHOLD_LIFTED
    if not evidence:
        detail = (
            "no fails-to-deliver row names this security; the record establishes "
            "nothing about whether it existed"
        )
        if censored:
            detail += (
                f" — and the window begins before {FTD_THRESHOLD_LIFTED}, when the file "
                "carried only failures above the reporting threshold"
            )
        return ExistenceEvidence(
            verdict=EvidenceVerdict.UNPROVEN,
            observations=0,
            first_seen=None,
            last_seen=None,
            threshold_censored=censored,
            detail=detail,
        )

    first = min(item.first_settlement for item in evidence)
    last = max(item.last_settlement for item in evidence)
    total = sum(item.observations for item in evidence)
    return ExistenceEvidence(
        verdict=EvidenceVerdict.PRESENT,
        observations=total,
        first_seen=first,
        last_seen=last,
        threshold_censored=censored,
        detail=f"{total} fails-to-deliver rows name it between {first} and {last}",
    )


@dataclass(frozen=True)
class IssuerSpan:
    """One issuer's claim on a symbol, over the window the evidence shows.

    Attributes:
        issuer_base: The CUSIP issuer field shared by every issue in the span.
        cusips: Every full CUSIP seen for this issuer under the symbol, sorted.
        names: Every issuer name printed for it, sorted. More than one is
            ordinary — the file abbreviates, and companies rename.
        first_settlement: Earliest date the issuer was seen under the symbol.
        last_settlement: Latest date it was seen.
        observations: Total rows behind the span.
    """

    issuer_base: str
    cusips: tuple[str, ...]
    names: tuple[str, ...]
    first_settlement: date
    last_settlement: date
    observations: int

    def overlaps(self, other: IssuerSpan) -> bool:
        """Whether two issuers' windows under one symbol intersect at all."""
        return (
            self.first_settlement <= other.last_settlement
            and other.first_settlement <= self.last_settlement
        )

    def intersection_days(self, other: IssuerSpan) -> int:
        """How many days two issuers' windows share. Zero when they are disjoint."""
        start = max(self.first_settlement, other.first_settlement)
        end = min(self.last_settlement, other.last_settlement)
        return (end - start).days + 1 if end >= start else 0

    def concurrent_with(self, other: IssuerSpan) -> bool:
        """Whether two issuers held the symbol *at the same time*.

        Deliberately not `overlaps`. These windows are **evidence spans** — the
        first and last settlement dates a failure was published under — not
        validity intervals, and a symbol changing hands leaves its two occupants
        touching rather than separated. A CUSIP transition taking effect on a
        settlement date puts the outgoing issue's last reported failure and the
        incoming issue's first on that *same* date, so the windows share exactly
        one day while the securities never traded concurrently for any of it.

        Treating that contact as concurrency was a real defect. `SGEN` is the
        worked case: Seattle Genetics' evidence runs to 2020-10-09 and Seagen's
        begins on 2020-10-09, and the symbol was called `AMBIGUOUS` for its whole
        history — so a $43bn acquisition was dropped from the delisted record
        entirely. The inconsistency is what gives it away: an occupant arriving
        the *next* day left the spans disjoint and resolved normally, and across
        the registry 79 handovers separated by one day resolved while 10
        separated by none did not. Nothing distinguishes those two populations.

        So contact at a single shared day is a handover, and anything longer is
        a genuine concurrent claim that no date can separate. The threshold is
        not a tuned parameter: the four real overlaps in the registry intersect
        by 835, 1,585, 2,131 and 4,236 days, and no evidence sits between one day
        and eight hundred.

        Args:
            other: The other issuer's span under the same symbol.

        Returns:
            True when the windows share more than a single boundary day.
        """
        return self.intersection_days(other) > _HANDOVER_CONTACT_DAYS


@dataclass(frozen=True)
class SymbolVerdict:
    """What the evidence says a symbol named, across the whole record.

    Attributes:
        symbol: The symbol judged.
        status: Whether one issuer held it, several in sequence, several at once,
            or none that the record shows.
        spans: One entry per issuer, earliest first.
        detail: The sentence an audit should print.
    """

    symbol: str
    status: SymbolStatus
    spans: tuple[IssuerSpan, ...]
    detail: str

    @property
    def is_reuse(self) -> bool:
        """True when more than one issuer held the symbol in sequence."""
        return self.status is SymbolStatus.REUSED

    @property
    def cusip_change_spans(self) -> tuple[IssuerSpan, ...]:
        """The issuers that carried more than one CUSIP under this symbol.

        A CUSIP change within one issuer — the reverse-split shape. Reported
        rather than acted on: the lineage between two issues of one issuer is
        recorded, but which corporate action caused it is not inferable from
        this file and is never guessed.
        """
        return tuple(span for span in self.spans if len(span.cusips) > 1)


def _span_from(evidence: Sequence[FtdIdentityEvidence]) -> IssuerSpan:
    """Fold one issuer's evidence under a symbol into a span."""
    return IssuerSpan(
        issuer_base=evidence[0].issuer_base,
        cusips=tuple(sorted({item.cusip for item in evidence})),
        names=tuple(sorted({item.issuer_name for item in evidence})),
        first_settlement=min(item.first_settlement for item in evidence),
        last_settlement=max(item.last_settlement for item in evidence),
        observations=sum(item.observations for item in evidence),
    )


def classify_symbol(symbol: str, evidence: Sequence[FtdIdentityEvidence]) -> SymbolVerdict:
    """Decide what a symbol named, grouping its evidence by issuer.

    The grouping rule is the CUSIP standard rather than a heuristic. Two issues
    sharing the six-character issuer base belong to one issuer, so a symbol that
    carried both carried one company's securities; two issues that differ in it
    belong to two issuers, and no lineage may ever be drawn across that boundary.
    That is what stops `BBBY`'s two occupants from being spliced.

    Given more than one issuer, the windows decide:

    - **disjoint windows, or windows touching at a single day**, are `REUSED` — a
      lease that changed hands, resolvable the moment a date accompanies the
      symbol. `IssuerSpan.concurrent_with` says why contact is a handover rather
      than an overlap, and why reading it as one lost `SGEN` entirely;
    - **windows overlapping by more than a day** are `AMBIGUOUS` — the two cannot
      be told apart by date, so the symbol resolves to neither and no security is
      minted from it.

    Note what `REUSED` does *not* assert. The spans stay separate issuers and
    become separate securities; the verdict only says the symbol's occupants can
    be ordered, so a date names one. Recognising two adjacent issuer bases as one
    continuing security would need issuer-identity evidence — a CIK — that the
    fails record does not carry, and is never inferred from adjacency here.

    Args:
        symbol: The symbol being judged, of any case.
        evidence: Every identity triple carrying that symbol.

    Returns:
        The verdict, with one span per issuer.
    """
    normalised = symbol.strip().upper()
    if not evidence:
        return SymbolVerdict(
            symbol=normalised,
            status=SymbolStatus.UNRESOLVED,
            spans=(),
            detail=f"no evidence carries {normalised}",
        )

    grouped: dict[str, list[FtdIdentityEvidence]] = {}
    for item in evidence:
        grouped.setdefault(item.issuer_base, []).append(item)
    spans = tuple(sorted((_span_from(rows) for rows in grouped.values()), key=_span_order))

    if len(spans) == 1:
        span = spans[0]
        extra = "" if len(span.cusips) == 1 else f", across {len(span.cusips)} CUSIPs"
        return SymbolVerdict(
            symbol=normalised,
            status=SymbolStatus.RESOLVED,
            spans=spans,
            detail=(
                f"one issuer ({span.issuer_base}) held {normalised} from "
                f"{span.first_settlement} to {span.last_settlement}{extra}"
            ),
        )

    concurrent = [
        (left, right)
        for index, left in enumerate(spans)
        for right in spans[index + 1 :]
        if left.concurrent_with(right)
    ]
    if concurrent:
        left, right = concurrent[0]
        return SymbolVerdict(
            symbol=normalised,
            status=SymbolStatus.AMBIGUOUS,
            spans=spans,
            detail=(
                f"{len(spans)} issuers carry {normalised} over overlapping windows, sharing "
                f"{left.intersection_days(right)} days "
                f"({left.issuer_base} {left.first_settlement}..{left.last_settlement} and "
                f"{right.issuer_base} {right.first_settlement}..{right.last_settlement}); "
                "no date separates them"
            ),
        )
    return SymbolVerdict(
        symbol=normalised,
        status=SymbolStatus.REUSED,
        spans=spans,
        detail=(
            f"{len(spans)} issuers held {normalised} in sequence, "
            + ", ".join(f"{s.issuer_base} {s.first_settlement}..{s.last_settlement}" for s in spans)
            + "; the symbol names one only with a date"
        ),
    )


def _span_order(span: IssuerSpan) -> tuple[date, str]:
    """Sort spans earliest first, breaking ties on the issuer base."""
    return (span.first_settlement, span.issuer_base)


def intervals_for_evidence(
    security_id: int,
    symbol: str,
    evidence: Sequence[FtdIdentityEvidence],
    *,
    source: str,
    venue: str | None = None,
) -> tuple[IdentifierInterval, ...]:
    """Build one security's identifier intervals from the evidence for one issuer.

    One interval per CUSIP, and every endpoint is a settlement date that actually
    appears in the record. No boundary is interpolated: where one issuer carried
    two issues, each interval spans exactly the dates its own CUSIP was seen on,
    even where those windows abut awkwardly or overlap. Dividing the span evenly
    between them would read as tidier and would be invented, and an invented date
    is a fabricated identity claim wearing a plausible face.

    A second CUSIP under one issuer opens a `CUSIP_CHANGE` interval at `INFERRED`
    confidence. The dates are observed; what is inferred is the claim that the
    two issues are one security, which rests on the CUSIP issuer base. It is
    deliberately **not** called a reverse split — the fails file records no
    corporate action, and naming one would be a guess. A caller holding split
    evidence records `REVERSE_SPLIT` through `succeed_interval`, and either way
    `security_id` is the same on both sides, which is the property that matters.

    Args:
        security_id: The stable identity every interval hangs off.
        symbol: The trading symbol over the whole span.
        evidence: One issuer's identity triples under that symbol. Every entry
            must share a CUSIP issuer base; a caller gets such a group from
            `classify_symbol`.
        source: What established the intervals, for the `source` column.
        venue: The exchange, when a source establishes one. None otherwise —
            never a default, which would assert a listing the record lacks.

    Returns:
        The intervals, earliest first.

    Raises:
        ValueError: If the evidence is empty, or spans more than one issuer, in
            which case the intervals would attribute two issuers' issues to one
            security — the exact splice this module exists to prevent.
    """
    if not evidence:
        message = f"no evidence for {symbol}; no interval can be built from it"
        raise ValueError(message)
    bases = {item.issuer_base for item in evidence}
    if len(bases) > 1:
        message = (
            f"evidence for {symbol} spans {len(bases)} CUSIP issuers ({', '.join(sorted(bases))}); "
            "one security cannot be built across them"
        )
        raise ValueError(message)

    normalised = symbol.strip().upper()
    by_cusip: dict[str, list[FtdIdentityEvidence]] = {}
    for item in evidence:
        by_cusip.setdefault(item.cusip, []).append(item)

    windows = sorted(
        (
            (
                cusip,
                min(item.first_settlement for item in rows),
                max(item.last_settlement for item in rows),
            )
            for cusip, rows in by_cusip.items()
        ),
        key=lambda window: (window[1], window[0]),
    )
    return tuple(
        IdentifierInterval(
            security_id=security_id,
            cusip=cusip,
            ticker=normalised,
            venue=venue,
            valid_from=first,
            valid_to=last,
            source=source,
            confidence=(IdentityConfidence.OBSERVED if index == 0 else IdentityConfidence.INFERRED),
            lineage=(LineageKind.INITIAL if index == 0 else LineageKind.CUSIP_CHANGE),
        )
        for index, (cusip, first, last) in enumerate(windows)
    )


def succeed_interval(
    previous: IdentifierInterval,
    *,
    effective_date: date,
    cusip: str | None = None,
    ticker: str | None = None,
    venue: str | None = None,
    lineage: LineageKind,
    source: str,
    confidence: IdentityConfidence = IdentityConfidence.ASSERTED,
) -> tuple[IdentifierInterval, IdentifierInterval]:
    """Close an interval and open its successor against the *same* security.

    The single operation every lineage rule in this module reduces to. A ticker
    change, a CUSIP change and a reverse split are the same shape — one label
    stops applying on a date and another starts — and they differ only in
    `lineage`. Because both intervals carry `previous.security_id`, none of the
    three can split a security in two, and because the caller cannot pass a
    different id, none of them can splice two securities into one.

    The closed interval ends the day *before* `effective_date`, so the two ranges
    abut without overlapping and the retired identifier stops resolving the
    moment the new one starts.

    Args:
        previous: The interval being superseded.
        effective_date: First date the successor holds. Must be after the
            predecessor opened.
        cusip: The successor's CUSIP. Defaults to the predecessor's, which is
            what a pure ticker change wants.
        ticker: The successor's symbol. Defaults to the predecessor's, which is
            what a pure CUSIP change wants.
        venue: The successor's venue. Defaults to the predecessor's.
        lineage: Why the successor opened.
        source: What established the change.
        confidence: How it is known. Defaults to `ASSERTED`, because a lineage
            link is normally a caller's reading of evidence no feed carries.

    Returns:
        The closed predecessor and the open successor, in that order.

    Raises:
        ValueError: If `effective_date` is not after the predecessor's start,
            which would produce an interval covering no dates at all.
    """
    if effective_date <= previous.valid_from:
        message = (
            f"effective_date {effective_date} is not after the interval it supersedes "
            f"({previous.valid_from}); the closed interval would cover no dates"
        )
        raise ValueError(message)

    closed = IdentifierInterval(
        security_id=previous.security_id,
        cusip=previous.cusip,
        ticker=previous.ticker,
        venue=previous.venue,
        valid_from=previous.valid_from,
        valid_to=effective_date - _ONE_DAY,
        source=previous.source,
        confidence=previous.confidence,
        lineage=previous.lineage,
    )
    successor = IdentifierInterval(
        security_id=previous.security_id,
        cusip=previous.cusip if cusip is None else cusip.strip().upper(),
        ticker=previous.ticker if ticker is None else ticker.strip().upper(),
        venue=previous.venue if venue is None else venue,
        valid_from=effective_date,
        valid_to=None,
        source=source,
        confidence=confidence,
        lineage=lineage,
    )
    return closed, successor


def overlapping_intervals(
    intervals: Sequence[IdentifierInterval],
) -> tuple[tuple[IdentifierInterval, IdentifierInterval], ...]:
    """Find pairs of intervals that claim one identifier for two securities at once.

    The invariant the storage layer has to hold: an identifier may name only one
    security on any given date. A pair returned here is a contradiction in the
    record — two issuers claiming one CUSIP or one ticker over dates that
    intersect — and must be reported rather than resolved by preference.

    Args:
        intervals: The intervals to check, any order.

    Returns:
        Every conflicting pair, each reported once.
    """
    conflicts: list[tuple[IdentifierInterval, IdentifierInterval]] = []
    for index, left in enumerate(intervals):
        for right in intervals[index + 1 :]:
            if left.security_id == right.security_id:
                continue
            shares_cusip = (
                left.cusip is not None and right.cusip is not None and left.cusip == right.cusip
            )
            shares_ticker = (
                left.ticker is not None and right.ticker is not None and left.ticker == right.ticker
            )
            if not (shares_cusip or shares_ticker):
                continue
            left_end = left.valid_to or date.max
            right_end = right.valid_to or date.max
            if left.valid_from <= right_end and right.valid_from <= left_end:
                conflicts.append((left, right))
    return tuple(conflicts)
