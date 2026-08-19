"""Which evidence supports which claim, and in which section.

These are the rules the deep validator will apply, written as pure functions so
they can be exercised against a hand-built brief with no model, no transport and
no database. The validator itself — dropping claims, composing confidence,
assembling a report — is a later step; what is settled here is the part that must
not change underneath it.

Four rules, and the reason each exists.

1. **A basis may only cite its own kind of evidence.** `DETERMINISTIC` restates
   what this system calculated, `EXTRACTED` quotes a filing, `EXTERNAL` cites a
   published source, `INTERPRETATION` reasons over any of them. A claim citing
   the wrong namespace is not weakly evidenced; it is evidenced by something that
   cannot support it.
2. **`D.` proves a filing exists; `X.` quotes what it says.** Carried forward
   from Phase 3 unchanged. An accession number attached to a remembered fact
   about the business launders recall into evidence, and that is the single
   easiest way for a grounded research tool to become an ungrounded one.
3. **`UNKNOWN` cites nothing.** A claim that says the evidence does not answer
   the question, while pointing at evidence, is either mislabelled or decorating
   itself. Both readings are wrong and the distinction is not worth guessing at.
4. **The score is explained deterministically or not at all.** The sections that
   account for the CompounderScore accept `DETERMINISTIC` and `UNKNOWN` only, so
   no `W.` id can appear anywhere in the explanation of a number that was
   computed before any external source was read. This is the structural half of
   "web never changes the score"; the other halves are that the brief's score is
   read-only and that the report has no score field to overwrite.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from deep_research.evidence import is_external
from deep_research.report import DeepBasis, DeepSection
from research import EvidenceKind, evidence_kind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from deep_research.brief import DeepResearchBrief
    from deep_research.report import DeepClaim

_DETERMINISTIC_KINDS = frozenset(
    {
        EvidenceKind.SCORE,
        EvidenceKind.METRIC,
        EvidenceKind.STATEMENT,
        EvidenceKind.ENRICHMENT,
    }
)
"""The namespaces holding a figure this system produced or a vendor supplied."""

_ALLOWED_KINDS: dict[DeepBasis, frozenset[EvidenceKind]] = {
    DeepBasis.DETERMINISTIC: _DETERMINISTIC_KINDS,
    DeepBasis.EXTRACTED: frozenset({EvidenceKind.EXCERPT, EvidenceKind.FILING}),
    DeepBasis.EXTERNAL: frozenset(),
    DeepBasis.INTERPRETATION: frozenset(EvidenceKind),
    DeepBasis.UNKNOWN: frozenset(),
}
"""Deterministic namespaces each basis may cite.

`EXTERNAL` maps to the empty set because its evidence is not in
`research.EvidenceKind` at all — that is the point of keeping `W.` out of the
Phase 3 enum. `allows_external` answers for it instead, and both are consulted
together by `unsupported_citations`.
"""

_EXTERNAL_BASES = frozenset({DeepBasis.EXTERNAL, DeepBasis.INTERPRETATION})
"""Bases that may rest on a `W.` source.

`INTERPRETATION` is included because reasoning over the news is exactly what a
deep report is for. `DETERMINISTIC` is excluded because a calculated figure did
not come from an article, and `EXTRACTED` because a filing did not either.
"""

_ALLOWED_BASES: dict[DeepSection, frozenset[DeepBasis]] = {
    DeepSection.COMPANY_OVERVIEW: frozenset({DeepBasis.EXTRACTED, DeepBasis.EXTERNAL}),
    DeepSection.CURRENT_SNAPSHOT: frozenset({DeepBasis.DETERMINISTIC}),
    DeepSection.WHY_THE_ALGORITHM_LIKES_IT: frozenset({DeepBasis.DETERMINISTIC}),
    DeepSection.GROWTH_QUALITY: frozenset(
        {DeepBasis.DETERMINISTIC, DeepBasis.EXTRACTED, DeepBasis.INTERPRETATION}
    ),
    DeepSection.FINANCIAL_QUALITY: frozenset({DeepBasis.DETERMINISTIC, DeepBasis.INTERPRETATION}),
    DeepSection.VALUATION: frozenset({DeepBasis.DETERMINISTIC, DeepBasis.INTERPRETATION}),
    DeepSection.LATEST_EARNINGS: frozenset(
        {DeepBasis.DETERMINISTIC, DeepBasis.EXTRACTED, DeepBasis.EXTERNAL}
    ),
    DeepSection.RECENT_DEVELOPMENTS: frozenset({DeepBasis.EXTRACTED, DeepBasis.EXTERNAL}),
    DeepSection.COMPETITIVE_POSITION: frozenset(
        {DeepBasis.EXTRACTED, DeepBasis.EXTERNAL, DeepBasis.INTERPRETATION}
    ),
    DeepSection.CATALYSTS: frozenset(
        {DeepBasis.EXTRACTED, DeepBasis.EXTERNAL, DeepBasis.INTERPRETATION}
    ),
    DeepSection.MAJOR_RISKS: frozenset(
        {
            DeepBasis.DETERMINISTIC,
            DeepBasis.EXTRACTED,
            DeepBasis.EXTERNAL,
            DeepBasis.INTERPRETATION,
        }
    ),
    DeepSection.BULL_CASE: frozenset({DeepBasis.INTERPRETATION}),
    DeepSection.BEAR_CASE: frozenset({DeepBasis.INTERPRETATION}),
    DeepSection.THESIS_BREAKERS: frozenset({DeepBasis.INTERPRETATION}),
    DeepSection.WHAT_THE_MARKET_MAY_BE_MISSING: frozenset({DeepBasis.INTERPRETATION}),
    DeepSection.WHAT_TO_WATCH_NEXT: frozenset({DeepBasis.INTERPRETATION}),
    DeepSection.RESEARCH_CONCLUSION: frozenset({DeepBasis.INTERPRETATION}),
}
"""Which bases each section accepts, before `UNKNOWN` is added to every one.

`UNKNOWN` is universally allowed and deliberately absent from this table: it is
the answer a section falls back to, so forbidding it anywhere would make an
unanswerable section unrepresentable.

`CURRENT_SNAPSHOT` and `WHY_THE_ALGORITHM_LIKES_IT` are the load-bearing rows.
Both describe the deterministic layer, both accept `DETERMINISTIC` only, and
neither can therefore contain a sentence resting on a news story.
"""


def allowed_bases(section: DeepSection) -> frozenset[DeepBasis]:
    """Return every basis a section accepts.

    Args:
        section: The section to look up.

    Returns:
        The permitted bases, always including `UNKNOWN`.
    """
    return _ALLOWED_BASES[section] | {DeepBasis.UNKNOWN}


def allowed_kinds(basis: DeepBasis) -> frozenset[EvidenceKind]:
    """Return the deterministic namespaces a basis may cite.

    Args:
        basis: The basis to look up.

    Returns:
        The permitted kinds. Empty for `EXTERNAL` and `UNKNOWN`, neither of
        which rests on deterministic evidence — use `allows_external` for the
        first and `requires_evidence` for the second.
    """
    return _ALLOWED_KINDS[basis]


def allows_external(basis: DeepBasis) -> bool:
    """Whether a basis may rest on a `W.` source.

    Args:
        basis: The basis to look up.

    Returns:
        True for `EXTERNAL` and `INTERPRETATION`, False otherwise.
    """
    return basis in _EXTERNAL_BASES


def requires_evidence(basis: DeepBasis) -> bool:
    """Whether a claim on this basis must cite at least one resolvable id.

    Args:
        basis: The basis to look up.

    Returns:
        True for everything except `UNKNOWN`, which must cite nothing.
    """
    return basis is not DeepBasis.UNKNOWN


def supports(basis: DeepBasis, evidence_id: str) -> bool:
    """Whether one evidence id can support a claim on one basis.

    The single rule the rest of the module composes. Answers only the namespace
    question — whether the id resolves in a given brief is `unsupported_citations`'
    concern, and the two are separate because an id can be real and still be the
    wrong kind of thing.

    Args:
        basis: The claim's basis.
        evidence_id: A citation as written by the model.

    Returns:
        True when the id's namespace is one this basis may rest on.
    """
    if is_external(evidence_id):
        return allows_external(basis)
    kind = evidence_kind(evidence_id)
    return kind is not None and kind in allowed_kinds(basis)


def unsupported_citations(claim: DeepClaim, brief: DeepResearchBrief) -> tuple[str, ...]:
    """Return the ids a claim cites that its basis cannot rest on.

    Args:
        claim: The claim to check.
        brief: The evidence it was written from. Only used to distinguish an id
            that does not resolve from one that resolves to the wrong namespace;
            both are returned, because both leave the claim unsupported.

    Returns:
        The offending ids, in the order the claim cites them.
    """
    return tuple(
        cited
        for cited in claim.evidence
        if cited not in brief.evidence_ids or not supports(claim.basis, cited)
    )


def cites_filing_text(evidence: Sequence[str], brief: DeepResearchBrief) -> bool:
    """Whether any cited id quotes a filing rather than merely naming one.

    The Phase 3 distinction, carried forward: a `D.` accession resolves, proves
    the filing exists, and says nothing whatever about its contents. Only an `X.`
    excerpt carries words.

    Args:
        evidence: The ids the claim cites.
        brief: The evidence supplying the extractable set.

    Returns:
        True when at least one cited id carries readable filing text.
    """
    return bool(set(evidence) & brief.extractable_ids)


def claim_problems(
    claim: DeepClaim, section: DeepSection, brief: DeepResearchBrief
) -> tuple[str, ...]:
    """Return every provenance rule one claim breaks, as readable reasons.

    The whole provenance check in one call, for the later validator and for
    tests. Deliberately returns prose rather than issue codes: this module states
    the rules, and mapping a broken rule onto a `DeepIssueCode` and a dropped
    claim is the validator's job.

    Args:
        claim: The claim to check.
        section: The section it was assigned to.
        brief: The evidence it was written from.

    Returns:
        One string per broken rule, empty when the claim's provenance is sound.
        Says nothing about the claim's numbers, its length or its tone.
    """
    problems: list[str] = []

    if claim.basis not in allowed_bases(section):
        problems.append(f"{claim.basis.value} is not permitted in {section.value}")

    unresolved = tuple(cited for cited in claim.evidence if cited not in brief.evidence_ids)
    if unresolved:
        problems.append(f"cited ids are not in the brief: {', '.join(unresolved)}")

    resolved = tuple(cited for cited in claim.evidence if cited in brief.evidence_ids)
    mismatched = tuple(cited for cited in resolved if not supports(claim.basis, cited))
    if mismatched:
        problems.append(
            f"{claim.basis.value} cannot rest on: {', '.join(mismatched)}",
        )

    if claim.basis is DeepBasis.UNKNOWN and claim.evidence:
        problems.append("an UNKNOWN claim must cite nothing")

    if requires_evidence(claim.basis) and not resolved:
        problems.append(f"a {claim.basis.value} claim must cite what it rests on")

    if (
        claim.basis is DeepBasis.EXTRACTED
        and resolved
        and not mismatched
        and not cites_filing_text(resolved, brief)
    ):
        problems.append(
            "the cited filings carry metadata only; an accession proves a filing exists, "
            "not what it says — cite an X. excerpt"
        )

    if (
        section is DeepSection.WHY_THE_ALGORITHM_LIKES_IT
        and claim.basis is not DeepBasis.UNKNOWN
        and not any(evidence_kind(cited) is EvidenceKind.SCORE for cited in resolved)
    ):
        problems.append("this section must cite the score breakdown")

    return tuple(problems)
