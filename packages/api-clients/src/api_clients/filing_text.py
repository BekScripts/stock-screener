"""Turning a filed SEC document into a few quotable sections.

Deterministic throughout. Headings are located by pattern, the text between them
is taken verbatim, and nothing is summarised, rewritten or inferred — an excerpt
a reader cannot find in the original by searching for the same words would be
worse than no excerpt at all.

Two rules shape everything here.

**Missing beats wrong.** A heading that cannot be located confidently, or whose
body is too short to be the real section, yields nothing. A research report that
answers `UNKNOWN` is honest; one quoting the table of contents as a business
description is not, and nothing downstream could tell the difference.

**The table of contents is the enemy.** Every 10-K and 10-Q names its items
twice — once in the index at the front, once where the section actually begins —
and the naive first match returns a page-number list. Candidates are therefore
scored by the length of the body that follows them, and the longest wins.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

MAX_SECTION_CHARS = 4000
"""Characters kept per extracted section.

Enough for the opening of a business description or the substance of an 8-K item,
and far short of a full risk-factor chapter. What reaches a model is bounded
again, and lower, when a brief is assembled — this bound is about what is worth
storing, not what is worth sending.
"""

MIN_SECTION_CHARS = 400
"""Shortest body accepted as a real section.

Below this a match is almost always a table-of-contents line or a cross-reference
such as "see Item 1A. Risk Factors", and quoting either as the section itself is
the failure this module exists to avoid.
"""

BUSINESS = "business"
RISK_FACTORS = "risk_factors"
MDA = "mda"

_TEN_K_SECTIONS = (BUSINESS, RISK_FACTORS, MDA)
_TEN_Q_SECTIONS = (MDA, RISK_FACTORS)

_EIGHT_K_PRIORITY = ("1.01", "2.01", "2.02", "5.02", "7.01", "8.01")
"""8-K items worth reading, in the order a researcher would want them.

Material agreements, completed acquisitions, results of operations, officer
changes, regulation FD disclosures and other events. The rest — auditor changes,
delistings, amendments to bylaws — are real but rarely what a research brief is
short of.
"""

_PUNCTUATION = str.maketrans(
    {
        "\u2019": "'",  # right single quotation mark, as in MANAGEMENT-APOSTROPHE-S
        "\u2018": "'",  # left single quotation mark
        "\u201b": "'",  # single high-reversed-9 quotation mark
        "\u02bc": "'",  # modifier letter apostrophe
        "\u2013": "-",  # en dash, as in ITEM 2 (dash) MD&A
        "\u2014": "-",  # em dash, the separator Dell files
        "\u2015": "-",  # horizontal bar
        "\u00a0": " ",  # non-breaking space
    }
)
"""Typographic characters folded to ASCII before a heading is matched.

Every replacement is one character for one character, which is the property the
rest of this module depends on: offsets into the folded text are offsets into the
original, so a heading can be *found* in the normalised copy while the body is
*taken* from the document as filed. Nothing stored is ever rewritten.

This is not cosmetic. Dell files its MD&A heading with an em dash and a
typographic apostrophe, and a pattern written with the ASCII apostrophe matched
no filing from any company at all: MD&A was silently unavailable everywhere.
"""

# After folding, filers still separate an item number from its title with a full
# stop, a colon or a hyphen, and sometimes with nothing at all.
_SEPARATOR = r"\s*[.:\-]?\s*"

_ITEM_PATTERNS: dict[str, re.Pattern[str]] = {
    BUSINESS: re.compile(rf"item\s*1{_SEPARATOR}business\b", re.IGNORECASE),
    RISK_FACTORS: re.compile(rf"item\s*1a{_SEPARATOR}risk\s+factors\b", re.IGNORECASE),
    MDA: re.compile(
        rf"item\s*(?:7|2){_SEPARATOR}management'?s?\s+discussion\b",
        re.IGNORECASE,
    ),
}

_EIGHT_K_ITEM = re.compile(rf"item\s*(\d\.\d\d){_SEPARATOR}", re.IGNORECASE)

_ANY_ITEM = re.compile(r"item\s*\d+\.?\d*[a-z]?\s*[.:\-]", re.IGNORECASE)

_SIGNATURE = re.compile(r"^\s*signatures?\s*$", re.IGNORECASE | re.MULTILINE)

_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6", "li", "hr"}
)
_SKIP_TAGS = frozenset({"script", "style", "title"})

_SENTENCE_END = re.compile(r"[.!?][\"')\]]?\s")


@dataclass(frozen=True, slots=True)
class ExtractedSection:
    """One located section: its slug and the text found under it."""

    section: str
    text: str


class _Text(HTMLParser):
    """Collect readable text, dropping markup and inline-XBRL scaffolding."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Open a tag, suppressing content that is markup rather than prose."""
        name = _bare(tag)
        if name in _SKIP_TAGS or tag.startswith("ix:"):
            self._skipping += 1
        elif name in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        """Close a tag, ending any suppression it started."""
        name = _bare(tag)
        if (name in _SKIP_TAGS or tag.startswith("ix:")) and self._skipping:
            self._skipping -= 1
        elif name in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        """Keep a run of text unless it sits inside a suppressed element."""
        if not self._skipping:
            self.parts.append(data)


def _bare(tag: str) -> str:
    """Return a tag name without its namespace prefix."""
    _, _, name = tag.rpartition(":")
    return name


def clean_filing_text(document: str) -> str:
    """Return the readable text of a filed document.

    Deterministic and lossy in one direction only: markup, scripts and inline
    XBRL scaffolding go, words stay in the order they were filed. Running this
    twice on the same document always gives the same string, which is what lets
    an extraction be cached and compared.

    Args:
        document: The filing as served — HTML, XHTML with inline XBRL, or plain
            text.

    Returns:
        The text, with blocks separated by newlines and runs of whitespace
        collapsed.
    """
    parser = _Text()
    parser.feed(document)
    parser.close()

    text = "".join(parser.parts)
    text = text.replace("\xa0", " ").replace("​", "")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def extract_sections(form: str, text: str) -> tuple[ExtractedSection, ...]:
    """Return the sections worth quoting from one filing's text.

    Headings are located in a punctuation-folded copy of the document and bodies
    are taken from the document itself, so a filer's choice of apostrophe or dash
    cannot hide a section while the text quoted stays exactly as filed.

    Args:
        form: The filing type. Anything other than `10-K`, `10-Q` or `8-K`
            yields nothing — there is no generic fallback, because guessing at
            an unfamiliar form is how a cover page becomes a business summary.
        text: The cleaned document text.

    Returns:
        The sections found, in the order this form should be read. A form whose
        headings cannot be located returns an empty tuple.
    """
    normalised = form.strip().upper().removesuffix("/A")
    headings = text.translate(_PUNCTUATION)
    if normalised == "8-K":
        return _eight_k_items(text, headings)
    if normalised == "10-K":
        return _named_sections(text, headings, _TEN_K_SECTIONS)
    if normalised == "10-Q":
        return _named_sections(text, headings, _TEN_Q_SECTIONS)
    return ()


def _named_sections(
    text: str, headings: str, wanted: tuple[str, ...]
) -> tuple[ExtractedSection, ...]:
    """Extract the periodic-report items a 10-K or 10-Q should carry."""
    found: list[ExtractedSection] = []
    for section in wanted:
        body = _best_body(text, headings, _ITEM_PATTERNS[section])
        if body is not None:
            found.append(ExtractedSection(section=section, text=body))
    return tuple(found)


def _eight_k_items(text: str, headings: str) -> tuple[ExtractedSection, ...]:
    """Extract an 8-K's substantive items, most research-relevant first."""
    bodies: dict[str, str] = {}
    for match in _EIGHT_K_ITEM.finditer(headings):
        number = match.group(1)
        body = _body_after(text, headings, match.end())
        if body is None:
            continue
        # An 8-K names each item twice when it carries a cover index. The longer
        # body is the real one.
        if len(body) > len(bodies.get(number, "")):
            bodies[number] = body

    ordered = sorted(
        bodies,
        key=lambda number: (
            _EIGHT_K_PRIORITY.index(number)
            if number in _EIGHT_K_PRIORITY
            else len(_EIGHT_K_PRIORITY),
            number,
        ),
    )
    return tuple(
        ExtractedSection(section=f"item_{number}", text=bodies[number]) for number in ordered
    )


def _best_body(text: str, headings: str, heading: re.Pattern[str]) -> str | None:
    """Return the longest body following any occurrence of a heading.

    The index at the front of a filing repeats every heading, so the first match
    is usually a page-number line. Taking the longest body reaches the section
    itself without needing to know how the filer laid out its contents page.
    """
    best: str | None = None
    for match in heading.finditer(headings):
        body = _body_after(text, headings, match.end())
        if body is not None and (best is None or len(body) > len(best)):
            best = body
    return best


_HEADING_TAIL_CHARS = 120


def _body_after(text: str, headings: str, start: int) -> str | None:
    """Return the text from one heading up to the next, bounded and trimmed.

    The rest of the heading's own line is skipped first: filers write "Item 2.
    Management's Discussion and Analysis of Financial Condition and Results of
    Operations", and a body beginning "and Analysis of Financial Condition" reads
    like a sentence fragment the model then has to guess the start of. Only a
    short remainder is skipped, so an item written inline with its text does not
    lose a paragraph.

    Returns None when what follows is too short to be a real section — a
    contents-page entry, or a cross-reference to an item discussed elsewhere.
    """
    break_at = text.find("\n", start)
    if break_at != -1 and break_at - start <= _HEADING_TAIL_CHARS:
        start = break_at + 1

    following = text[start:]
    end = len(following)

    # A match at position zero is a real boundary rather than the heading itself:
    # the heading's own line was already skipped, so an item name here means the
    # "section" was a contents-page entry with nothing under it. Boundaries are
    # searched in the folded copy, whose offsets are the document's own.
    next_item = _ANY_ITEM.search(headings[start:])
    if next_item is not None:
        end = min(end, next_item.start())

    signature = _SIGNATURE.search(headings[start:])
    if signature is not None:
        end = min(end, signature.start())

    body = following[:end].strip(" \n.:—–-")  # noqa: RUF001 — filers use both dashes
    if len(body) < MIN_SECTION_CHARS:
        return None
    return _truncate(body, MAX_SECTION_CHARS)


def _truncate(text: str, limit: int) -> str:
    """Cut text to a bound, preferring the last sentence that fits.

    A mid-sentence cut invites a reader — or a model — to complete the thought,
    which is exactly the inference this whole layer exists to prevent.
    """
    if len(text) <= limit:
        return text

    window = text[:limit]
    ends = list(_SENTENCE_END.finditer(window))
    if ends and ends[-1].end() > limit // 2:
        return window[: ends[-1].end()].strip()
    return window.rsplit(" ", 1)[0].strip()
