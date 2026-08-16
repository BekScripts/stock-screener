"""What the model is given: one company's evidence, assembled and addressable.

A brief is the model's entire world. Anything not in it does not exist for the
report that comes back, including whatever the model believes it already knows
about the company — that recall is untraceable, and a claim nobody can trace is
indistinguishable from an invention.

Three properties make the rest of the contract enforceable.

**Everything citable has an id.** Every score line, metric, reported period and
filing carries a stable identifier with a one-letter namespace, so a claim can
point at the exact thing it rests on and the validator can check the pointer
resolves. Ids are the join between what was supplied and what was asserted.

**Missing stays missing.** A metric the data could not support is `None` here and
renders as the literal `unknown` in the prompt, never blank and never `0.0`.
`unknowns` lists them explicitly, so the model is told what it lacks rather than
left to notice an absence.

**Provenance travels.** Whether a market capitalisation was quoted or multiplied
out, whether liquidity was consolidated or single-exchange, whether the row has
been through enrichment — all of it is in the brief, because a report that
describes an estimate as a measurement is wrong in a way no amount of fluent
prose reveals.

Nothing here performs I/O. Assembling a brief from the database is the
application's job; this package only says what a well-formed one looks like.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date  # noqa: TC003 — pydantic needs the runtime symbol
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from domain import (
    MarketCapSource,
    MetricUnit,
    RiskLevel,
    ScoreCategory,
    ScoreWarning,
    ScoringStatus,
    ValuationBasis,
    VolumeBasis,
    normalise_ticker,
)

RESEARCH_V1 = "RESEARCH_V1"
"""The first research contract: which inputs are supplied and what may be returned."""

CURRENT_CONTRACT_VERSION = RESEARCH_V1
"""The version every new brief and report is stamped with.

Reports produced under different contract versions are not comparable, for the
same reason score snapshots are not compared across `score_version`: the change
would be in the rules, not in the company. Changing which inputs are supplied,
which sections are required, or how a claim must be evidenced means a new
identifier here — never an edit to an existing one.
"""


class EvidenceKind(StrEnum):
    """The namespace an evidence id belongs to.

    The kind is carried in the id's prefix rather than beside it, so a claim that
    cites `S.growth.revenue_growth` is self-describing in the prompt and in the
    stored report.
    """

    SCORE = "SCORE"
    """A line of the stored CompounderScore breakdown, or a point of its history."""

    METRIC = "METRIC"
    """A Phase 1 derived metric."""

    STATEMENT = "STATEMENT"
    """A reported financial period."""

    FILING = "FILING"
    """An SEC filing, as metadata: it was filed, on a date, at a URL."""

    EXCERPT = "EXCERPT"
    """Text actually extracted from a filing. The only evidence of what it says."""

    ENRICHMENT = "ENRICHMENT"
    """A field supplied by the optional metered provider."""


EVIDENCE_PREFIXES: dict[EvidenceKind, str] = {
    EvidenceKind.SCORE: "S.",
    EvidenceKind.METRIC: "M.",
    EvidenceKind.STATEMENT: "F.",
    EvidenceKind.FILING: "D.",
    EvidenceKind.EXCERPT: "X.",
    EvidenceKind.ENRICHMENT: "E.",
}
"""The prefix that identifies each namespace. One letter, so prompts stay short."""

_KIND_BY_PREFIX = {prefix: kind for kind, prefix in EVIDENCE_PREFIXES.items()}


def evidence_kind(evidence_id: str) -> EvidenceKind | None:
    """Return the namespace an evidence id belongs to.

    Args:
        evidence_id: A citation as written by the model, e.g. `M.fcf_margin`.

    Returns:
        The matching kind, or None when the id carries no recognised prefix —
        which is itself a finding, not something to guess at.
    """
    return _KIND_BY_PREFIX.get(evidence_id[:2])


class SelectionReason(StrEnum):
    """Why this company qualified for AI research.

    Recorded because it is the honest answer to "why am I reading about this
    company", and because the three sets are worth judging separately once there
    are enough reports to judge.
    """

    TOP_RANKED = "TOP_RANKED"
    """Among the highest final scores in the latest ranking."""

    SCORE_MOVER = "SCORE_MOVER"
    """Score improved materially over the change window."""

    HIDDEN_GEM = "HIDDEN_GEM"
    """Passed the hidden-gems filter: small, growing, already scoring well."""


class RankingState(StrEnum):
    """Whether the score behind this brief has been through candidate enrichment.

    Mirrors the `PRELIMINARY` / `FINAL` constants `data-access` stores on a score
    snapshot. Duplicated rather than imported so this package stays independent
    of persistence; the two must not drift.
    """

    PRELIMINARY = "PRELIMINARY"
    """Scored on free data only. Market cap may be calculated, liquidity unverified."""

    FINAL = "FINAL"
    """A vendor verified the market capitalisation and consolidated volume."""


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


def _require_prefix(value: str, kind: EvidenceKind) -> str:
    """Return the id unchanged, or raise when it is not in the expected namespace."""
    prefix = EVIDENCE_PREFIXES[kind]
    if not value.startswith(prefix):
        raise ValueError(f"{kind.value} id must start with {prefix!r}: {value!r}")
    if len(value) <= len(prefix):
        raise ValueError(f"{kind.value} id needs something after {prefix!r}: {value!r}")
    return value


class Quantity(_Frozen):
    """One number the brief contains, with the unit it should be read in.

    The numeric whitelist compares every figure a claim states against these. A
    unit of None means the number has no natural rendering other than itself —
    score points, a count of days — and so may only be quoted bare.

    Attributes:
        value: The number as the brief holds it. A percentage is a decimal
            proportion here, exactly as it is everywhere else in this codebase.
        unit: How the number may legitimately be rendered in prose.
    """

    value: float
    unit: MetricUnit | None = None


class MetricFact(_Frozen):
    """One derived or vendor-supplied figure, addressable by id.

    Attributes:
        id: Citation handle, e.g. `M.revenue_growth_yoy`. Must carry the prefix
            matching `kind`.
        label: Human-readable name, rendered into the prompt beside the value.
        value: The figure, or None when the data could not support it. None is
            not zero and must never be presented as zero.
        unit: How to read `value`.
        kind: Which namespace the id belongs to — `METRIC` for a figure this
            system derived, `ENRICHMENT` for one a metered provider supplied.
        source: The provider or process the figure came from.
        as_of: The date the figure describes, when it has one.
    """

    id: str
    label: str
    value: float | None
    unit: MetricUnit = MetricUnit.PERCENT
    kind: EvidenceKind = EvidenceKind.METRIC
    source: str = "derived"
    as_of: date | None = None

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id whose prefix disagrees with the fact's kind."""
        _require_prefix(self.id, self.kind)
        return self

    @property
    def known(self) -> bool:
        """Whether the figure exists at all."""
        return self.value is not None


class ScoreItem(_Frozen):
    """One line of the stored CompounderScore breakdown.

    Components, sub-scores and risk penalties are all carried in this one shape.
    They differ in what they mean, not in how they are cited, and flattening them
    keeps the prompt readable and every line addressable.

    A sub-score with `points is None` is a metric the score could not use. It is
    supplied deliberately: the report should be able to say what the score does
    not know, and it cannot do that if the gaps are filtered out on the way in.

    Attributes:
        id: Citation handle — `S.growth`, `S.growth.revenue_growth`,
            `S.risk.dilution`.
        label: Human-readable name for the prompt.
        points: Points earned, or None when the metric was unavailable. Negative
            for a risk penalty.
        max_points: The most the line can contribute, when it has a ceiling.
        observed: The metric the points were calculated from.
        unit: How to read `observed`.
        note: The scoring engine's own explanation, where it recorded one.
    """

    id: str
    label: str
    points: float | None = None
    max_points: float | None = None
    observed: float | None = None
    unit: MetricUnit | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id outside the score namespace."""
        _require_prefix(self.id, EvidenceKind.SCORE)
        return self


class ScorePoint(_Frozen):
    """One earlier score, for stating how the ranking has moved.

    Attributes:
        id: Citation handle, `S.history.<iso date>`.
        score_date: The day the score describes.
        final_score: The risk-adjusted score on that day, under the same version.
    """

    id: str
    score_date: date
    final_score: float | None = None

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id that does not match the date it describes."""
        expected = f"S.history.{self.score_date.isoformat()}"
        if self.id != expected:
            raise ValueError(f"score point id must be {expected!r}, not {self.id!r}")
        return self


class ScoreEvidence(_Frozen):
    """The stored breakdown, verbatim, as read-only input.

    This is the one part of the brief the report may restate but must never
    dispute numerically. It is supplied whole — including the version it was
    computed under — so a report can be checked against the exact rules that
    produced the number it explains.

    Attributes:
        score_version: The formula the score came from. Echoed onto the report.
        score_date: The day the score describes.
        scoring_status: Whether there is a number, and why not when there is not.
        final_score: The risk-adjusted score.
        raw_score: Before risk penalties.
        risk_penalty: Zero or negative, or None when risk was not assessed.
        risk_level: The risk label.
        category: The research-priority band.
        data_coverage: Share of scoring metrics that were available, 0-1. Bounds
            how confident any report about this company is allowed to be.
        ranking_state: Whether enrichment verified the inputs behind the score.
        valuation_basis: Which multiple the valuation component used. A company
            scored on the price-to-sales fallback must not be written about as
            though its balance sheet was taken into account.
        market_cap_source: Whether the size behind every ratio was quoted by a
            provider or multiplied out from filings and a price.
        liquidity_basis: What the volume figure behind the score represented.
        items: Every component, sub-score and risk line, each addressable.
        warnings: Caveats the scoring engine recorded about the data.
    """

    score_version: str
    score_date: date
    scoring_status: ScoringStatus
    final_score: float | None = None
    raw_score: float | None = None
    risk_penalty: float | None = None
    risk_level: RiskLevel | None = None
    category: ScoreCategory | None = None
    data_coverage: float | None = Field(default=None, ge=0, le=1)
    ranking_state: RankingState = RankingState.PRELIMINARY
    valuation_basis: ValuationBasis = ValuationBasis.NOT_AVAILABLE
    market_cap_source: MarketCapSource = MarketCapSource.UNKNOWN
    liquidity_basis: VolumeBasis = VolumeBasis.UNKNOWN
    items: tuple[ScoreItem, ...] = ()
    warnings: tuple[ScoreWarning, ...] = ()


class ReportedPeriod(_Frozen):
    """One quarter of reported fundamentals, as filed.

    Attributes:
        id: Citation handle, `F.<iso period end>`.
        period_end: Last day of the reporting period.
        revenue: Total revenue for the period.
        gross_profit: Revenue less cost of revenue.
        operating_income: Income from operations.
        free_cash_flow: Operating cash flow less capital expenditure.
        cash: Cash, equivalents and short-term investments.
        total_debt: Short- plus long-term debt.
        shares_outstanding: Weighted-average diluted share count.
        gross_profit_basis: Which concept produced `gross_profit` — carried so a
            derived figure is never read as a reported one.
        source: The provider the period came from.
    """

    id: str
    period_end: date
    revenue: float | None = None
    gross_profit: float | None = None
    operating_income: float | None = None
    free_cash_flow: float | None = None
    cash: float | None = None
    total_debt: float | None = None
    shares_outstanding: float | None = None
    gross_profit_basis: str | None = None
    source: str = "unknown"

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id that does not match the period it describes."""
        expected = f"F.{self.period_end.isoformat()}"
        if self.id != expected:
            raise ValueError(f"period id must be {expected!r}, not {self.id!r}")
        return self


class FilingReference(_Frozen):
    """One SEC filing, as metadata.

    Metadata only, by construction: there is no field here for the document's
    text. What a filing says lives in `FilingText`, under its own `X.` id, so
    the two can never be confused by a claim, a validator or a reader.

    Attributes:
        id: Citation handle, `D.<accession>`.
        form: Filing type, e.g. `10-K`, `10-Q`, `8-K`.
        filed: The date the filing was submitted.
        period_end: The period it covers, when it covers one.
        accession: The SEC accession number, which identifies the filing.
        url: Where the filing can be read.
    """

    id: str
    form: str
    filed: date
    accession: str
    period_end: date | None = None
    url: str = ""

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id that does not match the accession it describes."""
        expected = f"D.{self.accession}"
        if self.id != expected:
            raise ValueError(f"filing id must be {expected!r}, not {self.id!r}")
        return self


class FilingText(_Frozen):
    """Verbatim text from one section of one filing.

    The evidence `FilingReference` deliberately is not. A reference proves a
    document exists; this quotes it, and the split is carried in the citation
    handle so a claim's grounding is legible without looking anything up: `D.`
    means "a filing exists", `X.` means "here is what it says".

    Attributes:
        id: Citation handle, `X.<accession>.<section>`.
        accession: The filing the text came from.
        form: Filing type, e.g. `10-K`, `10-Q`, `8-K`.
        filed: The date the filing was submitted.
        section: Which part of the filing — `business`, `risk_factors`, `mda`,
            or `item_2.02` for an 8-K item.
        text: The extracted text, verbatim.
        url: Where the document can be read.
    """

    id: str
    accession: str
    form: str
    filed: date
    section: str
    text: str = Field(min_length=1)
    url: str = ""

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id that does not match the filing and section it quotes."""
        expected = f"X.{self.accession}.{self.section}"
        if self.id != expected:
            raise ValueError(f"filing text id must be {expected!r}, not {self.id!r}")
        return self


class EnrichmentFacts(_Frozen):
    """Fields the optional metered provider supplied, flagged as vendor-supplied.

    Present only when the enrichment pass reached this company. Absent is
    normal — a quota is finite and the ranking does not wait for it.

    Attributes:
        provider: Which vendor supplied the fields.
        retrieved: When they were fetched.
        facts: The fields, each with an `E.` id.
    """

    provider: str
    retrieved: date
    facts: tuple[MetricFact, ...] = ()

    @model_validator(mode="after")
    def _check_kinds(self) -> Self:
        """Reject a fact that is not in the enrichment namespace."""
        for fact in self.facts:
            if fact.kind is not EvidenceKind.ENRICHMENT:
                raise ValueError(f"enrichment fact {fact.id!r} must have kind ENRICHMENT")
        return self


class ResearchBrief(_Frozen):
    """Everything one company's research report is allowed to be based on.

    Attributes:
        contract_version: The contract this brief was assembled under.
        ticker: The company, canonicalised.
        name: Registered company name.
        sector: Sector classification, when known.
        industry: Industry classification, when known.
        exchange: Listing exchange, when known.
        as_of: The score date this brief describes.
        selection: Why the company qualified for research.
        score: The stored breakdown, read-only.
        facts: Derived metrics, each addressable and each possibly unknown.
        quarters: Reported periods, oldest first.
        score_history: Earlier scores under the same version.
        filings: Filing metadata — that a document exists, and where. Empty is
            normal for a company that has not filed recently.
        excerpts: Text extracted from some of those filings. The only evidence
            in the brief about what a filing *says*; empty when extraction has
            not run for this company, which is why a section resting on one then
            answers `UNKNOWN`.
        enrichment: Vendor-supplied fields, when the pass reached this company.
    """

    contract_version: str = CURRENT_CONTRACT_VERSION
    ticker: str
    name: str
    as_of: date
    selection: SelectionReason
    score: ScoreEvidence
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    facts: tuple[MetricFact, ...] = ()
    quarters: tuple[ReportedPeriod, ...] = ()
    score_history: tuple[ScorePoint, ...] = ()
    filings: tuple[FilingReference, ...] = ()
    excerpts: tuple[FilingText, ...] = ()
    enrichment: EnrichmentFacts | None = None

    @model_validator(mode="after")
    def _normalise_ticker(self) -> Self:
        """Canonicalise the ticker so a brief always matches the company row."""
        canonical = normalise_ticker(self.ticker)
        if canonical != self.ticker:
            object.__setattr__(self, "ticker", canonical)
        return self

    @property
    def all_facts(self) -> tuple[MetricFact, ...]:
        """Derived and vendor-supplied facts together, in that order."""
        supplied = self.enrichment.facts if self.enrichment is not None else ()
        return (*self.facts, *supplied)

    @property
    def evidence_ids(self) -> frozenset[str]:
        """Every id a claim in a report about this company may cite.

        Returns:
            The complete citable set. A citation outside it does not resolve, and
            an unresolvable citation is treated as no citation at all.
        """
        return frozenset(
            {
                *(fact.id for fact in self.all_facts),
                *(item.id for item in self.score.items),
                *(point.id for point in self.score_history),
                *(period.id for period in self.quarters),
                *(filing.id for filing in self.filings),
                *(excerpt.id for excerpt in self.excerpts),
            }
        )

    @property
    def extractable_ids(self) -> frozenset[str]:
        """Ids of the text a claim about filing content may rest on.

        The distinction this property exists to draw: a filing reference proves
        a filing **exists**, and nothing whatever about what it says. Only an
        `X.` entry carries words, so only an `X.` entry can support a claim about
        content — and a company with no extracted text still answers `UNKNOWN`
        rather than filling the gap from a model's recollection.

        Returns:
            The citable ids that carry readable text.
        """
        return frozenset(excerpt.id for excerpt in self.excerpts)

    @property
    def unknowns(self) -> tuple[str, ...]:
        """Ids of everything the brief was expected to carry but could not.

        Computed rather than declared, so it cannot drift from the facts it
        describes, and rendered into the prompt so the model is told what is
        missing instead of having to infer it from a gap.

        Returns:
            Ids of unavailable facts and unscored sub-scores, in a stable order.
        """
        return (
            *(fact.id for fact in self.all_facts if not fact.known),
            *(item.id for item in self.score.items if item.points is None),
        )

    @property
    def quantities(self) -> tuple[Quantity, ...]:
        """Every number the brief states, with the unit it may be rendered in.

        This is the whitelist the numeric check compares claims against. A figure
        that is not here has not been supplied, and a report stating it would be
        doing arithmetic the deterministic layers did not do.

        How much evidence there is counts as evidence: the number of quarters,
        filings and earlier scores supplied is included, so a report may say it
        read four quarters when four were given — and may not say eight.

        Returns:
            Every numeric value in the brief, deduplicated on value and unit.
        """
        found: list[Quantity] = [
            Quantity(value=float(len(supplied)), unit=MetricUnit.COUNT)
            for supplied in (self.quarters, self.filings, self.score_history, self.facts)
        ]

        for fact in self.all_facts:
            if fact.value is not None:
                found.append(Quantity(value=fact.value, unit=fact.unit))

        score = self.score
        for plain in (score.final_score, score.raw_score, score.risk_penalty):
            if plain is not None:
                found.append(Quantity(value=plain))
        if score.data_coverage is not None:
            found.append(Quantity(value=score.data_coverage, unit=MetricUnit.PERCENT))

        for item in score.items:
            for points in (item.points, item.max_points):
                if points is not None:
                    found.append(Quantity(value=points))
            if item.observed is not None:
                found.append(Quantity(value=item.observed, unit=item.unit))

        for point in self.score_history:
            if point.final_score is not None:
                found.append(Quantity(value=point.final_score))

        for period in self.quarters:
            money = (
                period.revenue,
                period.gross_profit,
                period.operating_income,
                period.free_cash_flow,
                period.cash,
                period.total_debt,
            )
            found.extend(Quantity(value=v, unit=MetricUnit.MONEY) for v in money if v is not None)
            if period.shares_outstanding is not None:
                found.append(Quantity(value=period.shares_outstanding, unit=MetricUnit.COUNT))

        return tuple(dict.fromkeys(found))

    def fingerprint(self) -> str:
        """Return a stable hash of everything this brief contains.

        The cache key. A report is regenerated when — and only when — the
        evidence behind it changed, which is what keeps a daily run from paying
        for the same reading of the same facts.

        Returns:
            A hex SHA-256 digest over the brief's canonical JSON form.
        """
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
