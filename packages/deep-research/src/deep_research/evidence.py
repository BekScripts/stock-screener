"""Current external evidence: the `W.` namespace, and what may enter it.

Phase 3 gave a model five kinds of evidence, all of them produced inside this
system or filed with the SEC. Deep research adds a sixth that is unlike the rest:
material published by other people, at other times, that nobody here computed
and nobody here can recompute. It gets its own namespace so that difference
survives into the stored report.

`W.` is deliberately **not** added to `research.EvidenceKind`. That enum belongs
to a frozen contract, and leaving it alone buys a guarantee for free:
`research.evidence_kind("W.…")` returns None, so a web citation can never be
read as deterministic evidence by the Phase 3 validator, whatever else changes.

Two properties make the namespace safe to reason about.

**An id is derived from the URL, not from the order things were fetched.** The
same article yields the same id on every run, which is what lets a fingerprint
over external evidence mean "the sources changed" rather than "we looked again".

**A tier is a claim about the source, not about the story.** It records how
close the publisher is to the facts — the filer and the company itself, then
established news organisations, then credible trade press. Nothing below that is
representable: there is no tier for social media, forums or blogs, so V1 cannot
carry one even by mistake.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime  # noqa: TC003 — pydantic needs the runtime symbols
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

EXTERNAL_PREFIX = "W."
"""The prefix identifying current external evidence.

One letter, like every namespace in `research.EVIDENCE_PREFIXES`, so a claim's
grounding is legible in the prompt and in the stored report without a lookup.
`W` for web: the material came from outside, and outside is where it can be
checked.
"""

MAX_EXCERPT_CHARS = 2000
"""How much of a source one evidence item may carry.

A bound, not a summary target. The excerpt is the part of the page that supports
a claim, quoted verbatim; a whole article — let alone a whole page of navigation
and advertising — would swamp the deterministic evidence it sits beside and make
the brief's size a function of somebody else's CMS.
"""

_ID_DIGEST_CHARS = 12


class ExternalSourceType(StrEnum):
    """What kind of source an external item came from.

    Coarse on purpose. The distinction that matters downstream is who is
    speaking — the company, the regulator's archive, the press, or the trade —
    and a finer taxonomy would be guesswork applied to a URL.
    """

    COMPANY = "COMPANY"
    """Investor relations, an official release, a transcript the company posted."""

    SEC = "SEC"
    """Material reached through the SEC, but outside the deterministic filing
    pipeline — a filing index page, a document not covered by extraction."""

    NEWS = "NEWS"
    """A news organisation reporting on the company."""

    INDUSTRY = "INDUSTRY"
    """Trade press or industry analysis covering the market the company sells
    into."""


class SourceTier(StrEnum):
    """How close the publisher sits to the facts it reports.

    Read as provenance rather than as quality. A tier-3 trade journal may be more
    accurate about a niche market than a tier-1 press release is about its own
    company's prospects; what the tier records is how many hands the fact passed
    through, which is what a reader needs in order to discount it.
    """

    TIER_1_PRIMARY = "TIER_1_PRIMARY"
    """The source itself: SEC material, company investor relations, official
    company releases."""

    TIER_2_REPUTABLE = "TIER_2_REPUTABLE"
    """A major news organisation with an editorial process and a corrections
    policy."""

    TIER_3_SUPPORTING = "TIER_3_SUPPORTING"
    """Credible industry and trade publications. Useful for market context,
    weakest for company specifics."""


def external_evidence_id(source_type: ExternalSourceType, url: str) -> str:
    """Return the stable citation handle for one external source.

    Derived from the URL alone, so re-collecting the same article on a later day
    produces the same id. That stability is what makes `external_fingerprint`
    mean something: a changed fingerprint says the set of sources changed, not
    that the collector ran again.

    SHA-256 truncated rather than a full digest — the id appears in every
    citation in the prompt, and sixty-four hex characters of collision margin is
    not worth the tokens for a set of sources numbering in the tens.

    Args:
        source_type: The kind of source, rendered into the id so a citation is
            readable without resolving it.
        url: The canonical URL of the source. Compared verbatim, so a caller
            that wants two spellings of one page to collide must normalise
            before calling.

    Returns:
        A handle of the form `W.news.7f3a1c9e2b04`.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:_ID_DIGEST_CHARS]
    return f"{EXTERNAL_PREFIX}{source_type.value.lower()}.{digest}"


def is_external(evidence_id: str) -> bool:
    """Whether an id belongs to the current-external namespace.

    Args:
        evidence_id: A citation as written by the model.

    Returns:
        True for a well-formed `W.` id. A bare `W.` carries no source and is not
        one.
    """
    return evidence_id.startswith(EXTERNAL_PREFIX) and len(evidence_id) > len(EXTERNAL_PREFIX)


class ExternalEvidence(BaseModel):
    """One piece of current, externally published evidence.

    The only model in this package describing something nobody here produced.
    Everything else in a deep brief is a figure this system calculated or a
    document the company filed; this is a third party's account, and every field
    exists so a reader can judge it as one.

    Attributes:
        evidence_id: Citation handle, `W.<type>.<digest>`. Build it with
            `external_evidence_id` so it stays stable across collections.
        source_type: What kind of source this is.
        tier: How close the publisher sits to the facts.
        title: The headline or document title, as published.
        publisher: Who published it. Free text — no publisher list is hard-coded
            here, because a domain allowlist is a collection policy and belongs
            with the collector, not in the contract it feeds.
        url: Where it can be read. The identity of the source, and the reason
            `evidence_id` is derived from it.
        excerpt: The passage supporting whatever will be claimed from it,
            verbatim and bounded by `MAX_EXCERPT_CHARS`. Never a summary: a
            paraphrase written before the model sees it is an unattributed
            interpretation wearing the clothes of a quotation.
        published_at: When the source was published, when it says.
        occurred_at: When the event described happened, where that differs from
            publication — a Monday article about a Friday announcement.
        retrieved_at: When this system fetched it, as an aware UTC datetime.
            Deliberately excluded from every fingerprint: it records when we
            looked, not what the source says, and hashing it would mean no
            collection ever hit the cache.
        ticker: The company this item is about, when it is about one. Market and
            industry context legitimately has none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    source_type: ExternalSourceType
    tier: SourceTier
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    url: str = Field(min_length=1)
    excerpt: str = Field(min_length=1, max_length=MAX_EXCERPT_CHARS)
    retrieved_at: datetime
    published_at: date | None = None
    occurred_at: date | None = None
    ticker: str | None = None

    @model_validator(mode="after")
    def _check_id(self) -> Self:
        """Reject an id outside the external namespace."""
        if not is_external(self.evidence_id):
            raise ValueError(
                f"external evidence id must start with {EXTERNAL_PREFIX!r} and name a "
                f"source: {self.evidence_id!r}"
            )
        return self

    @model_validator(mode="after")
    def _check_retrieved_at(self) -> Self:
        """Reject a naive retrieval timestamp.

        Staleness is the whole point of this evidence class, and an offset-naive
        datetime compared against an aware one raises rather than answering. The
        failure belongs here, where a collector wrote the wrong thing, not in a
        renderer six steps later.
        """
        if self.retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")
        return self

    @property
    def dated(self) -> date | None:
        """The date this evidence describes, preferring the event over the report.

        Returns:
            `occurred_at` when the item records one, otherwise `published_at`,
            or None when the source is undated — which is itself worth knowing
            about a source being offered as current.
        """
        return self.occurred_at or self.published_at

    def age_in_days(self, on: date) -> int | None:
        """Return how old this evidence was on a given date.

        Args:
            on: The date to measure against, normally a brief's `as_of`.

        Returns:
            Whole days between `dated` and `on`, negative for a source dated
            after it, or None when the source carries no date at all.
        """
        when = self.dated
        return None if when is None else (on - when).days
