"""Reading the security-level facts off an SEC filing's inline-XBRL cover page.

`filing_xbrl` reads the *numeric* facts out of a raw XBRL instance. This reads
something different and from a different document: the `ix:nonNumeric` facts
embedded in the filing's own HTML, which since 2019 carry one
`(Security12bTitle, TradingSymbol, SecurityExchangeName)` triple per registered
class. `filing_xbrl` cannot be extended to do it — it keeps only facts that have
a `unitRef` and parse as a number, and a ticker is neither.

**Dimensions are what make this security-level rather than issuer-level.** A
filer registering more than one class separates them with
`us-gaap:StatementClassOfStockAxis`, and the member on each fact's context says
which class it belongs to. Twitter's final 8-K carries `TWTR` under
`us-gaap:CommonStockMember` and a preferred-stock-purchase-rights line under
`twtr:PreferredStockPurchaseRightsMember` with no symbol at all. Reading the
facts without their contexts would pool the two.

Most filings carry no dimension, because most filers register one class. Both
shapes are returned the same way, with `member` left None for the undimensioned
case; `domain.security_class` decides what the facts mean.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from domain import CoverSecurity

if TYPE_CHECKING:
    from collections.abc import Mapping

log = structlog.get_logger(__name__)

#: The cover-page concepts this reader keeps.
_CONCEPTS = ("Security12bTitle", "TradingSymbol", "SecurityExchangeName", "NoTradingSymbolFlag")

_FACT = re.compile(
    r"<ix:nonNumeric([^>]*?)name=\"dei:("
    + "|".join(_CONCEPTS)
    + r")\"([^>]*)>(.*?)</ix:nonNumeric>",
    re.S | re.I,
)
_CONTEXT = re.compile(r"<(?:xbrli:)?context id=\"([^\"]+)\"(.*?)</(?:xbrli:)?context>", re.S | re.I)
_MEMBER = re.compile(r"dimension=\"([^\"]+)\"[^>]*>\s*([^<]+)", re.S)
_CONTEXT_REF = re.compile(r"contextRef=\"([^\"]+)\"")
_TAGS = re.compile(r"<[^>]+>")

#: The axis that separates one registered class from another. A context carrying
#: it names a class; a context without it speaks for the whole filing.
CLASS_AXIS = "StatementClassOfStockAxis"


@dataclass(frozen=True)
class CoverPage:
    """Every registered class one filing's cover page names.

    Attributes:
        securities: One entry per registered class, in document order — which is
            a typesetting artefact and must not be read as precedence.
        dimensioned: Whether any cover fact carried a class dimension.
        ambiguous: Whether some class was surrendered because two of them shared
            one context and could not be told apart. A page can carry usable
            securities and still be ambiguous about others.
    """

    securities: tuple[CoverSecurity, ...]
    dimensioned: bool
    ambiguous: bool = False

    @property
    def multi_security(self) -> bool:
        """Whether the cover page registers more than one class."""
        return len(self.securities) > 1


def parse_cover_page(document: str) -> CoverPage:
    """Extract the security-level cover facts from a filing's inline XBRL.

    Args:
        document: The filing's primary document, as fetched.

    Returns:
        The registered classes. Empty when the filing predates inline XBRL or
        carries no cover facts — which is not an error and is the expected
        result for anything before 2019.
    """
    contexts = _class_members(document)
    grouped: dict[str | None, dict[str, list[str]]] = {}
    order: list[str | None] = []
    for prefix, concept, suffix, raw in _FACT.findall(document):
        reference = _CONTEXT_REF.search(prefix + suffix)
        member = contexts.get(reference.group(1)) if reference else None
        if member not in grouped:
            grouped[member] = {}
            order.append(member)
        value = " ".join(html.unescape(_TAGS.sub("", raw)).strip().split())
        values = grouped[member].setdefault(concept, [])
        if value not in values:
            values.append(value)

    securities: list[CoverSecurity] = []
    ambiguous = False
    for member in order:
        facts = grouped[member]
        if not facts:
            continue
        # More than one distinct value for a concept inside one group means two
        # registered classes shared a context — an undimensioned cover page with
        # multiple securities on it. Keeping the first would pair one class's
        # title with another's symbol, which is the exact mis-attribution this
        # layer exists to prevent, so the group is surrendered as ambiguous
        # rather than resolved by document order.
        if any(len(v) > 1 for v in facts.values()):
            ambiguous = True
            continue
        raw_flag = _first(facts, "NoTradingSymbolFlag") or ""
        flag = raw_flag.strip().lower() in {"true", "1", "yes"}
        securities.append(
            CoverSecurity(
                title=_first(facts, "Security12bTitle"),
                symbol=_first(facts, "TradingSymbol"),
                exchange=_first(facts, "SecurityExchangeName"),
                member=member,
                no_trading_symbol=flag,
            )
        )
    page = CoverPage(
        securities=tuple(securities),
        dimensioned=any(m is not None for m in order),
        ambiguous=ambiguous,
    )
    log.debug("parsed cover page", securities=len(page.securities), dimensioned=page.dimensioned)
    return page


def _first(facts: Mapping[str, list[str]], concept: str, default: str | None = None) -> str | None:
    """Return a group's single value for a concept, or the default when absent."""
    values = facts.get(concept)
    return values[0] if values else default


def _class_members(document: str) -> Mapping[str, str]:
    """Map each context id to its class member, for contexts that carry one."""
    members: dict[str, str] = {}
    for context_id, body in _CONTEXT.findall(document):
        for dimension, member in _MEMBER.findall(body):
            if CLASS_AXIS in dimension:
                members[context_id] = member.strip()
    return members
