"""What kind of security a filing's cover page describes.

The monthly listing record says a symbol traded on a venue. It cannot say the
symbol was *common stock* — it names warrants, units and preferred lines in the
same column, and Phase 9K measured 90.6% of delisted warrant-shaped tickers
present in it. The universe admits common stock only, so listing evidence alone
cannot decide membership and something has to supply the class.

Since 2019 the SEC cover page does, in inline XBRL: `dei:Security12bTitle`,
`dei:TradingSymbol` and `dei:SecurityExchangeName`, one triple per registered
class, separated by `us-gaap:StatementClassOfStockAxis` when a filer registers
more than one.

## Why the axis is evidence and not the rule

Phase 9I measured the shape of that data and it does not support a rule keyed on
the axis. Only 28.5% of sampled filings were dimensioned at all, and only **9.2%**
used `us-gaap:CommonStockMember`; the rest discriminate with filer-extension
members — `twtr:PreferredStockPurchaseRightsMember`, `snre:AmericanDepositary
SharesMember` — whose names are chosen by the filer and belong to no taxonomy.

So classification reads the **title text** first and treats the member as
corroboration. Where the two disagree the result is `UNKNOWN`, never a guess:
Twitter's final cover page carries common stock *and* preferred stock purchase
rights, and attributing the rights line to `TWTR` would put a non-traded security
into the research universe under a traded symbol.

## What this module refuses to do

It does not classify from ticker shape — a five-letter symbol is a hint about
Nasdaq's numbering conventions, not about what a security is. It does not treat
the first title on a cover page as the common one; order is a rendering
accident. And it never resolves a disagreement by preferring one signal, because
a wrong class here silently admits a warrant to a stock screener.

Nothing here performs I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class SecurityClass(StrEnum):
    """What a registered class of securities is.

    `UNKNOWN` is a real answer and the correct one whenever the title and the
    member disagree, or neither is recognisable.
    """

    COMMON_STOCK = "COMMON_STOCK"
    PREFERRED_STOCK = "PREFERRED_STOCK"
    DEPOSITARY_SHARES = "DEPOSITARY_SHARES"
    ADR_OR_ADS = "ADR_OR_ADS"
    WARRANT = "WARRANT"
    UNIT = "UNIT"
    RIGHT = "RIGHT"
    NOTE_OR_DEBT = "NOTE_OR_DEBT"
    FUND = "FUND"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"


class ClassEvidence(StrEnum):
    """How firmly a class was established."""

    TITLE_AND_MEMBER = "TITLE_AND_MEMBER"
    """The title text and the XBRL member agree. The strongest available."""

    TITLE_ONLY = "TITLE_ONLY"
    """Undimensioned cover page, or a filer-extension member that says nothing.
    The ordinary case — 71.5% of filings carry no dimension at all."""

    MEMBER_ONLY = "MEMBER_ONLY"
    """A recognised member with an unreadable title."""

    CONFLICTING = "CONFLICTING"
    """Title and member name different classes. Resolves to `UNKNOWN`."""

    ABSENT = "ABSENT"
    """No usable class facts on the cover page."""


#: Title patterns, most specific first. Order matters: "units, each consisting of
#: one share of Class A common stock" is a unit and must not match common stock,
#: and "depositary shares each representing one one-thousandth of a share of
#: preferred" is depositary rather than preferred.
_TITLE_RULES: tuple[tuple[SecurityClass, re.Pattern[str]], ...] = (
    (SecurityClass.UNIT, re.compile(r"\bunits?\b", re.I)),
    (
        SecurityClass.RIGHT,
        re.compile(r"purchase rights?\b|\brights? to purchase|subscription rights?", re.I),
    ),
    (SecurityClass.WARRANT, re.compile(r"\bwarrants?\b", re.I)),
    (
        SecurityClass.ADR_OR_ADS,
        re.compile(r"american depositary (shares?|receipts?)|\bADSs?\b|\bADRs?\b", re.I),
    ),
    (SecurityClass.DEPOSITARY_SHARES, re.compile(r"\bdepositary shares?\b", re.I)),
    (
        SecurityClass.NOTE_OR_DEBT,
        re.compile(r"\bnotes? due\b|\bdebentures?\b|\bbonds?\b|senior notes|subordinated", re.I),
    ),
    # "Preferred Securities" and "Preferred Units" are as common as "Preferred
    # Stock" on real cover pages — SIVB's 2017 removal was "7% Cumulative Trust
    # Preferred Securities", and matching only "stock" or "share" would have let
    # it fall through to UNKNOWN.
    (
        SecurityClass.PREFERRED_STOCK,
        re.compile(r"\bpreferred (stock|share|securit|unit|interest)", re.I),
    ),
    (
        SecurityClass.FUND,
        re.compile(r"\bETF\b|exchange[- ]traded fund|\btrust units\b|closed[- ]end fund", re.I),
    ),
    (
        SecurityClass.COMMON_STOCK,
        re.compile(
            r"\bcommon stock\b|\bcommon shares?\b|\bordinary shares?\b"
            r"|\bclass [a-z] common\b|shares of beneficial interest|\bcapital stock\b",
            re.I,
        ),
    ),
)

#: Members from the us-gaap taxonomy that name a class outright. A filer
#: extension is deliberately absent — its name means whatever the filer meant.
_MEMBER_RULES: tuple[tuple[SecurityClass, re.Pattern[str]], ...] = (
    (SecurityClass.COMMON_STOCK, re.compile(r"us-gaap:CommonStockMember$", re.I)),
    (SecurityClass.PREFERRED_STOCK, re.compile(r"us-gaap:PreferredStockMember$", re.I)),
    (SecurityClass.WARRANT, re.compile(r"us-gaap:WarrantMember$", re.I)),
    (SecurityClass.RIGHT, re.compile(r"us-gaap:RightsMember$", re.I)),
)


@dataclass(frozen=True)
class CoverSecurity:
    """One registered class, as one cover page states it.

    Attributes:
        title: `dei:Security12bTitle`, verbatim.
        symbol: `dei:TradingSymbol`, or None where the filing flags that the
            class has none — the ordinary-shares half of an ADS pair.
        exchange: `dei:SecurityExchangeName`, verbatim.
        member: The `StatementClassOfStockAxis` member, where the facts carried
            a dimension.
        no_trading_symbol: Whether `dei:NoTradingSymbolFlag` was set.
    """

    title: str | None
    symbol: str | None
    exchange: str | None
    member: str | None = None
    no_trading_symbol: bool = False


@dataclass(frozen=True)
class ClassVerdict:
    """What one registered class was judged to be, and on what.

    Attributes:
        security_class: The verdict.
        evidence: Which signals produced it.
        title_class: What the title alone said.
        member_class: What the member alone said.
    """

    security_class: SecurityClass
    evidence: ClassEvidence
    title_class: SecurityClass
    member_class: SecurityClass

    @property
    def is_common(self) -> bool:
        """Whether this is the common stock the universe admits."""
        return self.security_class is SecurityClass.COMMON_STOCK


def classify_title(title: str | None) -> SecurityClass:
    """Return what a `Security12bTitle` says the class is.

    Args:
        title: The title text, or None.

    Returns:
        The class, or `UNKNOWN` when nothing matches.
    """
    if not title or not title.strip():
        return SecurityClass.UNKNOWN
    text = " ".join(title.split())
    for security_class, pattern in _TITLE_RULES:
        if pattern.search(text):
            return security_class
    return SecurityClass.UNKNOWN


def classify_member(member: str | None) -> SecurityClass:
    """Return what an XBRL axis member says the class is.

    Only us-gaap members are read. A filer-extension member is returned as
    `UNKNOWN` rather than guessed at from its spelling, because
    `twtr:PreferredStockPurchaseRightsMember` and
    `snre:AmericanDepositarySharesMember` are names their filers invented and
    nothing constrains the next filer to the same convention.
    """
    if not member:
        return SecurityClass.UNKNOWN
    for security_class, pattern in _MEMBER_RULES:
        if pattern.search(member.strip()):
            return security_class
    return SecurityClass.UNKNOWN


def classify_security(security: CoverSecurity) -> ClassVerdict:
    """Judge one registered class from its title and its member.

    The title leads and the member corroborates. Disagreement produces
    `UNKNOWN` — never a preference for one signal, because the cost of a wrong
    class is a warrant entering a common-stock screen.

    Args:
        security: The cover-page facts for one registered class.

    Returns:
        The verdict, carrying both inputs so a disagreement stays inspectable.
    """
    title_class = classify_title(security.title)
    member_class = classify_member(security.member)

    if title_class is not SecurityClass.UNKNOWN and member_class is not SecurityClass.UNKNOWN:
        if title_class is member_class:
            return ClassVerdict(
                title_class, ClassEvidence.TITLE_AND_MEMBER, title_class, member_class
            )
        return ClassVerdict(
            SecurityClass.UNKNOWN, ClassEvidence.CONFLICTING, title_class, member_class
        )
    if title_class is not SecurityClass.UNKNOWN:
        return ClassVerdict(title_class, ClassEvidence.TITLE_ONLY, title_class, member_class)
    if member_class is not SecurityClass.UNKNOWN:
        return ClassVerdict(member_class, ClassEvidence.MEMBER_ONLY, title_class, member_class)
    return ClassVerdict(SecurityClass.UNKNOWN, ClassEvidence.ABSENT, title_class, member_class)


def common_stock_for_symbol(
    securities: Sequence[CoverSecurity], symbol: str
) -> ClassVerdict | None:
    """Return the verdict for the class a cover page attaches to one symbol.

    **The rule that stops a rights line being read as its issuer's stock.** A
    cover page listing common stock under `TWTR` and preferred stock purchase
    rights under no symbol must answer for `TWTR` with the common line only, and
    must answer for the rights line not at all.

    Matching is by symbol, never by position — the first title on a cover page
    is first because of how the table was typeset.

    Args:
        securities: Every registered class the cover page carried.
        symbol: The symbol being asked about.

    Returns:
        The verdict for the class carrying that symbol, or None when no class
        does. Ambiguity — two classes claiming one symbol — returns an `UNKNOWN`
        verdict rather than either of them.
    """
    wanted = symbol.strip().upper()
    matches = [s for s in securities if s.symbol and s.symbol.strip().upper() == wanted]
    if not matches:
        return None
    if len(matches) > 1:
        verdicts = {classify_security(s).security_class for s in matches}
        if len(verdicts) > 1:
            return ClassVerdict(
                SecurityClass.UNKNOWN,
                ClassEvidence.CONFLICTING,
                SecurityClass.UNKNOWN,
                SecurityClass.UNKNOWN,
            )
    return classify_security(matches[0])


def traded_securities(securities: Iterable[CoverSecurity]) -> tuple[CoverSecurity, ...]:
    """Return only the classes that carry a trading symbol.

    The ordinary-shares half of an ADS pair sets `NoTradingSymbolFlag` and has
    no symbol; it is registered but does not trade, and admitting it would count
    one company's listing twice.
    """
    return tuple(s for s in securities if s.symbol and s.symbol.strip() and not s.no_trading_symbol)
