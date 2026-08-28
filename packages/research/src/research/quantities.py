"""Reading numbers out of prose, and deciding whether the evidence contains them.

Extracted from `research.validation` so the deep-research contract can apply the
**same** rule rather than a second, weaker one. A grounding check that differed
between the two layers would be a grounding check nobody could reason about, and
the weaker of the pair would be the one that mattered — it would be guarding the
report that reads the wider evidence.

Nothing here knows what a brief is. The caller supplies the quantities a claim is
allowed to draw on, which is what lets the two layers scope them differently:
Phase 3 pools structured figures brief-wide and scopes filing text to the
excerpts a claim cites, and deep research does the same while additionally
scoping external figures to the sources a claim cites.

The parsing is deliberately literal. A figure is supported when some supplied
value, rendered in the unit the claim used and rounded to the precision the claim
used, is the number written. No arithmetic is performed and none is inferred: a
model that computed a ratio the pipeline never calculated has fabricated it,
however correct the arithmetic happened to be.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from domain import MetricUnit
from research.brief import Quantity

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


FORM_TYPES = re.compile(r"\b\d{1,2}-[A-Z]{1,2}\b")
"""Filing form types — `10-K`, `8-K`, `20-F`. Stripped before scanning for
figures, since the digits in them are part of a name, not a quantity."""

NUMBER = re.compile(
    r"(?P<currency>\$)?\s*"
    r"(?P<digits>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<suffix>%|pp|percentage points|bps|x|×|"  # noqa: RUF001 — both multiplication signs
    r"thousand|million|billion|trillion|[kKmMbB](?![\w-]))?",
    re.IGNORECASE,
)

MAGNITUDES: dict[str, float] = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "billion": 1e9,
    "trillion": 1e12,
}

ADVICE = re.compile(
    r"(?<![\w-])("
    r"buy|sell|short the stock|price target|target price|fair value target|"
    r"overweight|underweight|outperform rating|accumulate|"
    r"position siz(?:e|ing)|stop loss|take profits?|"
    r"recommend (?:buying|selling|holding)"
    r")(?![\w-])",
    re.IGNORECASE,
)
"""Phrases that turn a research note into a recommendation.

Deliberately narrow. `undervalued`, `expensive` and `overvalued` are valuation
interpretation and stay legal; `buy` and `price target` are instructions and do
not. Hyphen boundaries keep `sell-through`, `cross-sell` and `buy-side` out of
the match, since those describe a business rather than an action to take.
"""

LITERAL = re.compile(r"\d+(?:\.\d+)?")

SCORE_SCALE = re.compile(
    r"(?P<score>\d+(?:\.\d+)?)\s*(?:/|\s+(?:out\s+of|of)\s+)(?P<scale>100)\b"
    r"(?!\s*(?:%|x\b|×|million|billion|thousand|bn\b|m\b|k\b))",  # noqa: RUF001
    re.IGNORECASE,
)
"""A CompounderScore written against its own scale: "72.63 of 100", "72.63/100".

The scale is the one number a claim may contain that no brief supplies, because
it is a property of the formula rather than of the company. Recognising it needs
the numerator too — a bare 100 says nothing, but a 100 standing under a score the
brief actually recorded is the denominator of a fact, not a figure about a
business. Anything else keeping company with a 100 — millions, percentages,
multiples, counts — is excluded by the lookahead and stays subject to the
ordinary rule.
"""

TOLERANCE = 1e-9


class Marker(StrEnum):
    """What unit a figure in prose was written in."""

    BARE = auto()
    PERCENT = auto()
    POINTS = auto()
    MULTIPLE = auto()
    MONEY = auto()
    UNSUPPORTED = auto()


@dataclass(frozen=True, slots=True)
class Figure:
    """One number as it appears in a claim, with how it was written."""

    value: float
    decimals: int
    marker: Marker
    scale: float


def find_advice(text: str) -> str | None:
    """Return the first recommendation phrase in a claim, if any.

    Args:
        text: The claim's text.

    Returns:
        The matched phrase, or None when the claim recommends nothing.
    """
    found = ADVICE.search(text)
    return found.group(0) if found is not None else None


def parse_figures(text: str) -> tuple[Figure, ...]:
    """Return every quantity written in a claim."""
    scrubbed = FORM_TYPES.sub(" ", text)

    figures: list[Figure] = []
    for match in NUMBER.finditer(scrubbed):
        digits = match.group("digits").replace(",", "")
        suffix = (match.group("suffix") or "").strip().lower()
        _, _, fraction = digits.partition(".")

        marker, scale = _classify(suffix, currency=match.group("currency") is not None)
        figures.append(
            Figure(value=float(digits), decimals=len(fraction), marker=marker, scale=scale)
        )
    return tuple(figures)


def _classify(suffix: str, *, currency: bool) -> tuple[Marker, float]:
    """Return the unit and magnitude a written suffix implies."""
    if suffix == "%":
        return Marker.PERCENT, 1.0
    if suffix in {"pp", "percentage points"}:
        return Marker.POINTS, 1.0
    if suffix in {"x", "×"}:  # noqa: RUF001 — a model may write either multiplication sign
        return Marker.MULTIPLE, 1.0
    if suffix == "bps":
        return Marker.UNSUPPORTED, 1.0

    scale = MAGNITUDES.get(suffix, 1.0)
    return (Marker.MONEY if currency else Marker.BARE), scale


def renderings(quantity: Quantity, figure: Figure) -> tuple[float, ...]:
    """Return the ways a brief quantity could legitimately have been written.

    Only renderings compatible with how the figure was written count. A number
    the model marked `%` must come from a percentage in the brief — otherwise a
    claim could borrow an unrelated integer and dress it up as a rate.
    """
    magnitude = abs(quantity.value) / figure.scale
    unit = quantity.unit

    if figure.marker is Marker.PERCENT:
        return (magnitude * 100,) if unit is MetricUnit.PERCENT else ()
    if figure.marker is Marker.POINTS:
        return (magnitude * 100,) if unit is MetricUnit.POINTS else ()
    if figure.marker is Marker.MULTIPLE:
        return (magnitude,) if unit is MetricUnit.MULTIPLE else ()
    if figure.marker is Marker.MONEY:
        return (magnitude,) if unit in {MetricUnit.MONEY, MetricUnit.COUNT} else ()
    if figure.marker is Marker.BARE:
        if unit in {MetricUnit.PERCENT, MetricUnit.POINTS}:
            return (magnitude, magnitude * 100)
        return (magnitude,)
    return ()


def rounds_to(candidate: float, figure: Figure) -> bool:
    """Whether a brief value, rounded as written, is the figure in the claim."""
    return math.isclose(round(candidate, figure.decimals), figure.value, abs_tol=TOLERANCE)


def unsupported_figures(
    text: str,
    *,
    quantities: Sequence[Quantity],
    literals: Iterable[float] = (),
    scale_values: Sequence[float] = (),
) -> tuple[str, ...]:
    """Return every number in a claim that the supplied evidence does not contain.

    The rule that does the real work against fabrication, and against the subtler
    failure of a model quietly computing a ratio the pipeline never calculated. A
    figure is supported when some supplied quantity, rendered in the unit the
    claim used and rounded to the precision the claim used, is the number written.

    Scoping is the caller's decision and the reason this function takes values
    rather than a brief. A figure quoted inside one filing excerpt, or one news
    article, is evidence only for a claim that cites *that* item — pooling them
    would let any quoted number legitimise any claim.

    Args:
        text: The claim's text.
        quantities: Every value this particular claim may draw on, already
            scoped by the caller.
        literals: Bare digit sequences the claim may restate — dates, counts,
            share counts — drawn from structured fields only. Prose is
            deliberately excluded: it is full of incidental integers, and
            admitting them brief-wide would undo the scoping above.
        scale_values: Scores a bare `100` may legitimately stand under, as in
            "72.6 out of 100". The scale belongs to the formula rather than to
            the company, so no brief supplies it.

    Returns:
        The unsupported figures as written, in the order they appear.
    """
    allowed = set(literals)
    unsupported: list[str] = []
    for figure in parse_figures(mask_score_scale(text, scale_values)):
        bare = figure.marker is Marker.BARE and figure.scale == 1.0
        if bare and any(rounds_to(literal, figure) for literal in allowed):
            continue
        supported = any(
            rounds_to(rendering, figure)
            for quantity in quantities
            for rendering in renderings(quantity, figure)
        )
        if not supported:
            unsupported.append(written(figure))
    return tuple(unsupported)


def quoted_quantities(text: str) -> tuple[Quantity, ...]:
    """Return the numbers written in a passage of supplied evidence.

    Filing text and news excerpts are parsed with the same machinery a claim is,
    so `4.750%` in a note offering is a percentage, `$1,000,000,000` is money and
    `2031` is a bare year — and a claim may only restate one in a unit it could
    legitimately have been written in. A quoted figure is evidence in exactly the
    way a metric is; what it is not is evidence for a sentence that cites
    something else.

    Args:
        text: The excerpt's text.

    Returns:
        Every number it states, as quantities.
    """
    return tuple(as_quantity(figure) for figure in parse_figures(text))


def as_quantity(figure: Figure) -> Quantity:
    """Read a figure written in prose as a quantity in this codebase's units.

    The inverse of `_renderings`: percentages become decimal proportions, scaled
    magnitudes are multiplied out, and a plain integer becomes a count. Going
    through `Quantity` rather than comparing raw numbers is what keeps the unit
    rules identical for a figure from a filing and a figure from a metric.
    """
    value = figure.value * figure.scale
    if figure.marker is Marker.PERCENT:
        return Quantity(value=value / 100, unit=MetricUnit.PERCENT)
    if figure.marker is Marker.POINTS:
        return Quantity(value=value / 100, unit=MetricUnit.POINTS)
    if figure.marker is Marker.MULTIPLE:
        return Quantity(value=value, unit=MetricUnit.MULTIPLE)
    if figure.marker is Marker.MONEY:
        return Quantity(value=value, unit=MetricUnit.MONEY)
    return Quantity(value=value, unit=MetricUnit.COUNT)


def mask_score_scale(text: str, scale_values: Sequence[float]) -> str:
    """Blank out the `100` in a score written against its own scale.

    Narrow on purpose, in both directions. The numerator is left in the text and
    checked like any other figure, so a score the evidence never recorded is
    still rejected — and rejected together with its denominator, since a scale is
    only a scale when it scales something real.

    Args:
        text: The claim's text.
        scale_values: The scores a 100 may stand under.

    Returns:
        The text with qualifying scale denominators replaced by spaces.
    """
    if not scale_values:
        return text

    masked = list(text)
    for match in SCORE_SCALE.finditer(text):
        number = match.group("score")
        _, _, fraction = number.partition(".")
        numerator = Figure(
            value=float(number), decimals=len(fraction), marker=Marker.BARE, scale=1.0
        )
        if not any(rounds_to(score, numerator) for score in scale_values):
            continue
        start, end = match.span("scale")
        masked[start:end] = " " * (end - start)
    return "".join(masked)


def written(figure: Figure) -> str:
    """Render a figure roughly as it appeared, for an issue message."""
    suffix = {
        Marker.PERCENT: "%",
        Marker.POINTS: "pp",
        Marker.MULTIPLE: "x",
        Marker.MONEY: " (currency)",
        Marker.UNSUPPORTED: " (unsupported unit)",
        Marker.BARE: "",
    }[figure.marker]
    scale = "" if figure.scale == 1.0 else f" x{figure.scale:g}"
    return f"{figure.value:g}{suffix}{scale}"
