"""Reading facts straight out of one filing's XBRL instance.

The SEC publishes two views of the same numbers. `companyfacts` is the tidy one:
every fact a filer has ever tagged, aggregated across filings, deduplicated, and
already shaped the way the rest of this adapter expects. It is the primary source
and stays that way.

It is also, sometimes, behind. TSM filed its FY2025 20-F on 16 April 2026 with a
complete inline-XBRL instance, and four months later `companyfacts` still served
nothing newer than FY2024 — so a company with published annual statements scored
on figures a full reporting cycle old. The filing itself had the facts the whole
time.

This module reads that filing. It is deliberately **not** a second fundamentals
engine: it produces the same nested structure `companyfacts` returns —
`{namespace: {concept: {"units": {unit: [facts]}}}}` — and hands it to exactly the
same concept chains, unit selection, cadence classification and duplicate
resolution that the primary path uses. Adding a source should not mean adding a
second set of opinions about what a number means.

**Dimensions are the hazard.** `companyfacts` serves facts that apply to the whole
filing entity. A raw instance also carries every segment a filer tagged — revenue
by geography, by product line, by subsidiary — under the same concept, the same
date and the same unit, distinguishable only by the context they point at. Fed in
as equivalents they would compete with the consolidated figure and the winner
would be arbitrary. So a fact whose context carries any segment is refused here,
and segments are never summed to reconstruct a total the filer did not state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

import structlog

from api_clients.errors import ProviderDataError

log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from collections.abc import Mapping

XBRL_INSTANCE_NS = "http://www.xbrl.org/2003/instance"
XBRLDI_NS = "http://xbrl.org/2006/xbrldi"

#: Namespaces whose concepts the normalizer understands. Anything else — a
#: filer's own extension namespace, the SEC's country or currency taxonomies —
#: is parsed and then dropped, because a concept chain that has never heard of a
#: tag cannot say what it means and guessing is how a segment becomes a total.
_NORMALISED_NAMESPACES = frozenset({"us-gaap", "ifrs-full", "dei"})

#: Prefixes stripped from a unit measure. `iso4217:TWD` is the currency `TWD`,
#: and `xbrli:shares` is `shares` — the same keys `companyfacts` uses, so the
#: existing unit selection needs no special case for this source.
_UNIT_PREFIXES = ("iso4217:", "xbrli:", "utr:")


def parse_filing_instance(
    xml: str,
    *,
    accession: str,
    form: str,
    filed: str,
) -> dict[str, dict[str, Any]]:
    """Return one filing instance as a company-facts shaped document.

    Args:
        xml: The instance document.
        accession: The filing's accession number, stamped onto every fact so a
            figure can be traced back to the document it came from.
        form: The filing form, recorded the way `companyfacts` records it.
        filed: The filing date, which is what the duplicate resolution ranks on.

    Returns:
        `{namespace: {concept: {"units": {unit: [fact, ...]}}}}`, holding only
        facts from recognised taxonomies whose context carries no dimensions.
        An instance with nothing usable returns `{}` rather than raising.

    Raises:
        ProviderDataError: If the document is not parseable XML.
    """
    try:
        root = ElementTree.fromstring(xml)  # noqa: S314 — SEC-published, and see below
    except ElementTree.ParseError as exc:
        raise ProviderDataError(f"filing instance is not valid XML: {exc}") from exc

    contexts = _parse_contexts(root)
    units = _parse_units(root)

    facts: dict[str, dict[str, Any]] = {}
    skipped_dimensional = 0
    for element in root:
        namespace, concept = _split_tag(element.tag)
        if namespace not in _NORMALISED_NAMESPACES or not element.text:
            continue

        context_id = element.get("contextRef")
        period = contexts.get(context_id or "")
        if period is None:
            skipped_dimensional += 1
            continue

        unit = units.get(element.get("unitRef") or "")
        value = _as_number(element.text)
        if unit is None or value is None:
            continue

        fact: dict[str, Any] = {"val": value, "end": period[1], "filed": filed, "form": form}
        if period[0] is not None:
            fact["start"] = period[0]
        fact["accn"] = accession

        concepts = facts.setdefault(namespace, {})
        entry = concepts.setdefault(concept, {"units": {}})
        entry["units"].setdefault(unit, []).append(fact)

    log.debug(
        "parsed filing instance",
        accession=accession,
        namespaces=sorted(facts),
        concepts=sum(len(c) for c in facts.values()),
        dimensional_facts_skipped=skipped_dimensional,
    )
    return facts


def _parse_contexts(root: ElementTree.Element) -> dict[str, tuple[str | None, str]]:
    """Return each usable context as `(start, end)`, dropping dimensional ones.

    A context with a `segment` or `scenario` scopes its facts to part of the
    business — one geography, one product line, one subsidiary. Those are real
    figures and none of them is the company's revenue. `companyfacts` filters
    them out before anyone sees them; this restores the same property, which is
    the single most important thing this parser does.

    An instant context reports `(None, instant)`, matching how `companyfacts`
    presents a balance-sheet fact: an `end` with no `start`.
    """
    contexts: dict[str, tuple[str | None, str]] = {}
    for context in root.findall(f"{{{XBRL_INSTANCE_NS}}}context"):
        identifier = context.get("id")
        if identifier is None:
            continue
        if _has_dimensions(context):
            continue

        period = context.find(f"{{{XBRL_INSTANCE_NS}}}period")
        if period is None:
            continue

        instant = period.findtext(f"{{{XBRL_INSTANCE_NS}}}instant")
        if instant:
            contexts[identifier] = (None, instant.strip())
            continue

        start = period.findtext(f"{{{XBRL_INSTANCE_NS}}}startDate")
        end = period.findtext(f"{{{XBRL_INSTANCE_NS}}}endDate")
        if start and end:
            contexts[identifier] = (start.strip(), end.strip())
    return contexts


def _has_dimensions(context: ElementTree.Element) -> bool:
    """Whether a context scopes its facts to a segment rather than the entity."""
    for holder in ("segment", "scenario"):
        element = context.find(f".//{{{XBRL_INSTANCE_NS}}}{holder}")
        if element is not None and len(element):
            return True
    return context.find(f".//{{{XBRLDI_NS}}}explicitMember") is not None


def _parse_units(root: ElementTree.Element) -> dict[str, str]:
    """Return each unit as the key `companyfacts` would use for it.

    Only single-measure units. A divided unit — earnings per share, a rate — is
    not money or a share count, and no normalized field reads one.
    """
    units: dict[str, str] = {}
    for unit in root.findall(f"{{{XBRL_INSTANCE_NS}}}unit"):
        identifier = unit.get("id")
        if identifier is None:
            continue
        if unit.find(f"{{{XBRL_INSTANCE_NS}}}divide") is not None:
            continue
        measures = unit.findall(f"{{{XBRL_INSTANCE_NS}}}measure")
        if len(measures) != 1 or not measures[0].text:
            continue
        units[identifier] = _strip_prefix(measures[0].text.strip())
    return units


def _strip_prefix(measure: str) -> str:
    """Turn `iso4217:TWD` into `TWD`, matching the company-facts unit keys."""
    for prefix in _UNIT_PREFIXES:
        if measure.startswith(prefix):
            return measure[len(prefix) :]
    return measure.split(":")[-1]


def _split_tag(tag: str) -> tuple[str, str]:
    """Return the taxonomy prefix and concept name for a fully qualified tag."""
    if not tag.startswith("{"):
        return "", tag
    namespace, _, concept = tag[1:].partition("}")
    return _namespace_prefix(namespace), concept


def _namespace_prefix(namespace: str) -> str:
    """Map a namespace URI to the short prefix the concept chains use."""
    if "ifrs" in namespace:
        return "ifrs-full"
    if "us-gaap" in namespace or "fasb.org/us-gaap" in namespace:
        return "us-gaap"
    if "/dei/" in namespace or namespace.endswith("/dei"):
        return "dei"
    return namespace.rsplit("/", 1)[-1]


def _as_number(text: str) -> float | None:
    """Parse a fact's value, returning None for anything non-numeric."""
    try:
        return float(text.strip().replace(",", ""))
    except ValueError:
        return None


def merge_instance_facts(
    primary: Mapping[str, Any], instance: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """Combine company-facts with instance facts under one document shape.

    Both sides keep every fact they hold; nothing is dropped or overwritten
    here. Which fact wins for a period is decided further down by the existing
    resolution — most recently filed — and the caller decides which *periods*
    the instance is allowed to contribute at all.

    Args:
        primary: The company-facts document's `facts` block.
        instance: Facts parsed from a filing instance.

    Returns:
        A new document with the two merged. Neither input is modified.
    """
    merged: dict[str, dict[str, Any]] = {}
    for source in (primary, instance):
        for namespace, concepts in source.items():
            if not isinstance(concepts, dict):
                continue
            target = merged.setdefault(namespace, {})
            for concept, body in concepts.items():
                units = body.get("units") if isinstance(body, dict) else None
                if not isinstance(units, dict):
                    continue
                entry = target.setdefault(concept, {"units": {}})
                for unit, entries in units.items():
                    if isinstance(entries, list):
                        entry["units"].setdefault(unit, []).extend(entries)
    return merged
