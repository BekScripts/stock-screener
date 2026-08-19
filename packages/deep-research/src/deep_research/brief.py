"""What a deep research model is given: one company, refreshed, plus the world.

A `DeepResearchBrief` is the deep-research counterpart of `research.ResearchBrief`
and differs from it in three ways that matter.

**It carries current external evidence.** Phase 3's world ends at the SEC. This
brief adds published material from outside — under the `W.` namespace, never
mixed with the deterministic ids, and never permitted to alter the score it sits
beside.

**It is on-demand, not selected.** A Phase 3 brief records *why the screener
picked this company*; a deep brief exists because somebody typed a ticker, so
there is no `SelectionReason` here and no pretending there was one.

**It says how current it is.** A report that presents itself as an investigation
of a company today has to be checkable against when the data behind it was
actually refreshed, which is what `DataFreshness` records.

Everything else is reused from `research` outright — the score breakdown, the
metric facts, the reported periods, the filing references and excerpts. Those
models already say the right things about missing values and provenance, and a
second copy of them would be a second thing to keep correct.

Nothing here performs I/O. Refreshing a company, recomputing its score,
collecting external evidence and assembling the result are application concerns.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime  # noqa: TC003 — pydantic needs the runtime symbols
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deep_research.evidence import (
    ExternalEvidence,  # noqa: TC001 — pydantic needs the runtime symbol
)
from domain import normalise_ticker
from research import (
    EnrichmentFacts,
    FilingReference,
    FilingText,
    MetricFact,
    RankingState,
    ReportedPeriod,
    ScoreEvidence,
    ScorePoint,
)

DEEP_RESEARCH_V1 = "DEEP_RESEARCH_V1"
"""The first deep research contract: which inputs are supplied, and what may come back."""

CURRENT_DEEP_CONTRACT_VERSION = DEEP_RESEARCH_V1
"""The version every new deep brief and report is stamped with.

Separate from `research.CURRENT_CONTRACT_VERSION` and moving independently of it.
Changing which inputs a deep brief supplies, which sections exist, or how a claim
must be evidenced means a new identifier here — never an edit to an existing one,
and never a change to the Phase 3 constant.
"""


class _Frozen(BaseModel):
    """Base for immutable value objects."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DataFreshness(_Frozen):
    """How current the deterministic half of a brief is, and how it got that way.

    Deep research promises an investigation of a company *now*, which is a
    promise about data as much as about prose. This records what that promise
    rests on, so a report written over three-month-old prices is legible as one
    rather than reading exactly like a report written this morning.

    The fields fall into two groups, and the split is load-bearing.

    **What the evidence is.** `price_as_of`, `fundamentals_through`,
    `filings_through` and `excerpts_through` are properties of the stored data.
    Two briefs that disagree on any of them are built on different evidence, so
    these are hashed into the deterministic fingerprint like everything else.

    **How it was obtained.** `refreshed_at`, `refreshed`, `reused` and `stale`
    describe the run rather than the data. Whether a figure was fetched this
    morning or was already stored, and whether an optional provider answered,
    changes nothing about what the figure *is* — so all four are excluded from
    every fingerprint. Hashing them would mean a company whose data has not
    moved gets a new cache key every time a provider has a bad afternoon, and
    the cache would buy nothing.

    Attributes:
        price_as_of: The date of the most recent price behind the brief.
        fundamentals_through: The end of the most recent reported period.
        filings_through: The filing date of the most recent stored filing.
        excerpts_through: The filing date of the most recent extracted text.
        refreshed_at: When preparation last ran for this company, as an aware
            UTC datetime.
        refreshed: Stages that fetched new data on this run.
        reused: Stages already current, left untouched. Naming them is what
            makes "nothing changed" distinguishable from "nothing was tried".
        stale: What could not be brought up to date, in plain words. A provider
            that returned `429` leaves a note here rather than leaving the brief
            silently describing older data as current.
    """

    price_as_of: date | None = None
    fundamentals_through: date | None = None
    filings_through: date | None = None
    excerpts_through: date | None = None
    refreshed_at: datetime | None = None
    refreshed: tuple[str, ...] = ()
    reused: tuple[str, ...] = ()
    stale: tuple[str, ...] = ()


class MarketRanking(_Frozen):
    """Where the company sat in the ranking this brief describes.

    Not separately citable, and deliberately so. A rank is a property of the
    market on one day rather than a fact about the company, and a claim about
    why the score is what it is should cite the score lines that produced it. The
    numbers are here so a report can state the position honestly, not so it can
    rest an argument on it.

    Attributes:
        rank: Position in the ranking, 1 being the highest score.
        universe_size: How many scored companies the rank is out of. A rank
            without it says nothing.
        percentile: Position as a proportion, 0-1, where 1 is the top.
        ranking_state: Whether enrichment verified the inputs behind the score.
            Echoed from the score so the two can be checked against each other.
    """

    rank: int | None = Field(default=None, ge=1)
    universe_size: int | None = Field(default=None, ge=0)
    percentile: float | None = Field(default=None, ge=0, le=1)
    ranking_state: RankingState = RankingState.PRELIMINARY


class DeepResearchBrief(_Frozen):
    """Everything one deep research report is allowed to be based on.

    Attributes:
        contract_version: The deep contract this brief was assembled under.
        ticker: The company, canonicalised.
        name: Registered company name.
        as_of: The score date this brief describes. Every deterministic figure
            here belongs to this date, and external evidence is read against it.
        assembled_at: When the brief was built, as an aware UTC datetime.
            Excluded from every fingerprint: assembling the same evidence twice
            is the cache hit, not a new brief.
        score: The stored CompounderScore breakdown, read-only. Reused from the
            Phase 3 contract unchanged, because a deep report explains exactly
            the same score.
        sector: Sector classification, when known.
        industry: Industry classification, when known.
        exchange: Listing exchange, when known.
        facts: Derived metrics, each addressable and each possibly unknown.
        quarters: Reported periods, oldest first.
        score_history: Earlier scores under the same version.
        filings: Filing metadata — that a document exists, and where.
        excerpts: Text extracted from filings. The only evidence in the brief
            about what a filing *says*.
        external: Current external evidence, under the `W.` namespace. Empty is
            normal and legal: collection has not been built yet, and a company
            nobody has written about recently is a real case.
        enrichment: Vendor-supplied fields, when the metered pass reached this
            company.
        ranking: Where the company sat in the ranking, when it was ranked.
        freshness: How current the deterministic half is.
    """

    contract_version: str = CURRENT_DEEP_CONTRACT_VERSION
    ticker: str
    name: str
    as_of: date
    assembled_at: datetime
    score: ScoreEvidence
    sector: str | None = None
    industry: str | None = None
    exchange: str | None = None
    facts: tuple[MetricFact, ...] = ()
    quarters: tuple[ReportedPeriod, ...] = ()
    score_history: tuple[ScorePoint, ...] = ()
    filings: tuple[FilingReference, ...] = ()
    excerpts: tuple[FilingText, ...] = ()
    external: tuple[ExternalEvidence, ...] = ()
    enrichment: EnrichmentFacts | None = None
    ranking: MarketRanking | None = None
    freshness: DataFreshness = DataFreshness()

    @model_validator(mode="after")
    def _normalise_ticker(self) -> Self:
        """Canonicalise the ticker so a brief always matches the company row."""
        canonical = normalise_ticker(self.ticker)
        if canonical != self.ticker:
            object.__setattr__(self, "ticker", canonical)
        return self

    @model_validator(mode="after")
    def _check_assembled_at(self) -> Self:
        """Reject a naive assembly timestamp."""
        if self.assembled_at.tzinfo is None:
            raise ValueError("assembled_at must be timezone-aware")
        return self

    @model_validator(mode="after")
    def _check_external_ids_are_unique(self) -> Self:
        """Reject two external items answering to one id.

        An id is a citation target, so a duplicate makes a claim's evidence
        ambiguous and leaves the external fingerprint hashing a set that cannot
        be reconstructed from what a reader is shown.

        The namespace itself is not rechecked here. `ExternalEvidence` rejects a
        non-`W.` id on construction and pydantic revalidates each item on its way
        into this field, so a second check would be unreachable — and an
        unreachable guard reads as protection without being any.
        """
        seen: set[str] = set()
        for item in self.external:
            if item.evidence_id in seen:
                raise ValueError(f"duplicate external evidence id: {item.evidence_id!r}")
            seen.add(item.evidence_id)
        return self

    @property
    def all_facts(self) -> tuple[MetricFact, ...]:
        """Derived and vendor-supplied facts together, in that order."""
        supplied = self.enrichment.facts if self.enrichment is not None else ()
        return (*self.facts, *supplied)

    @property
    def deterministic_ids(self) -> frozenset[str]:
        """Every citable id that this system calculated or the company filed.

        The `S.`, `M.`, `F.`, `D.`, `X.` and `E.` namespaces — everything a Phase
        3 brief would have offered.

        Returns:
            The citable deterministic set.
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
    def external_ids(self) -> frozenset[str]:
        """Ids of the current external evidence supplied.

        Returns:
            The citable `W.` set. Empty when no collection has run.
        """
        return frozenset(item.evidence_id for item in self.external)

    @property
    def evidence_ids(self) -> frozenset[str]:
        """Every id a claim in a deep report may cite.

        Returns:
            The deterministic and external sets together. A citation outside it
            does not resolve, and an unresolvable citation is invented evidence
            rather than weak evidence.
        """
        return self.deterministic_ids | self.external_ids

    @property
    def extractable_ids(self) -> frozenset[str]:
        """Ids of the filing text a claim about filing content may rest on.

        Carries the Phase 3 rule forward unchanged: `D.` proves a filing exists,
        `X.` quotes what it says, and only the second can support a claim about
        contents.

        Returns:
            The citable ids that carry filing text.
        """
        return frozenset(excerpt.id for excerpt in self.excerpts)

    @property
    def unknowns(self) -> tuple[str, ...]:
        """Ids of everything the brief was expected to carry but could not.

        Computed rather than declared, so it cannot drift from the facts it
        describes. The model is told what is missing instead of having to infer
        it from a gap — which is what makes `UNKNOWN` a usable answer rather than
        an admission of not having looked.

        Returns:
            Ids of unavailable facts and unscored sub-scores, in a stable order.
        """
        return (
            *(fact.id for fact in self.all_facts if not fact.known),
            *(item.id for item in self.score.items if item.points is None),
        )

    def deterministic_fingerprint(self) -> str:
        """Return a hash of everything in this brief that the system produced.

        The score, the metrics, the periods, the filings and the extracted text —
        the half of the brief that a rescan or a rescore would change, and that
        no amount of news can. Kept separate from the external hash so a later
        refresh can tell "the fundamentals moved" from "somebody published
        something", which are different reasons to spend a model call.

        `assembled_at` is excluded, and so is every process field on
        `freshness` — when preparation ran, which stages fetched, which were
        reused, and which failed. All of them record how the evidence was
        obtained rather than what it is, and hashing them would mean a company
        whose data has not moved got a new cache key whenever an optional
        provider had a bad afternoon.

        The freshness *dates* are kept. `price_as_of` moving is a real change in
        the evidence even when every figure derived from it happens to round the
        same way.

        Returns:
            A hex SHA-256 digest.
        """
        payload = self.model_dump(
            mode="json",
            exclude={
                "external": True,
                "assembled_at": True,
                "freshness": {"refreshed_at", "refreshed", "reused", "stale"},
            },
        )
        return _digest(payload)

    def external_fingerprint(self) -> str:
        """Return a hash of the current external evidence supplied.

        Sorted by id before hashing, because a collector's ordering is an
        accident of which request finished first and must not read as a change
        in the evidence. Each item's `retrieved_at` is dropped for the same
        reason: fetching the same article tomorrow is not new evidence.

        Returns:
            A hex SHA-256 digest. A stable digest over the empty set when no
            external evidence was supplied.
        """
        items = sorted(
            (item.model_dump(mode="json", exclude={"retrieved_at"}) for item in self.external),
            key=lambda item: str(item["evidence_id"]),
        )
        return _digest(items)

    def evidence_fingerprint(self) -> str:
        """Return the cache key: both halves of the evidence, together.

        A report may be reused when — and only when — this matches, because it is
        the only value that changes for either reason a report goes stale.
        Composed from the two digests rather than hashing the brief again, so the
        three values cannot disagree about what they cover.

        Returns:
            A hex SHA-256 digest over the deterministic and external digests.
        """
        return _digest([self.deterministic_fingerprint(), self.external_fingerprint()])


def _digest(payload: object) -> str:
    """Return a stable SHA-256 over a JSON-serialisable payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
