"""Turn search results into `W.` evidence, rejecting most of them.

The middle stage of deep research: deterministic preparation has run, synthesis
has not, and this decides what current material is worth putting in front of a
model. It performs the only network access on the external path — brief assembly
stays a pure read — so the architecture is preparation, then collection, then
zero-network assembly.

The job is mostly refusal. A search for a large company returns the same earnings
story from twenty outlets, a dozen aggregator reposts, and a handful of genuinely
different events; a collector that passed all of it through would produce a brief
where the loudest story crowded out every other fact about the business. So five
filters run in order, each cheap and each deterministic:

1. **Tier.** An unrecognised domain is rejected, not demoted. `sources` decides.
2. **Window.** Older than the configured window unless the vendor gave no date.
3. **SEC overlap.** A filing already carried as `X.` text is not re-collected as
   a web copy. `X.` is authoritative for filings and stays that way.
4. **Duplicates.** By canonical URL first, then by event: two headlines about the
   same announcement keep the better-tiered source and drop the other.
5. **Caps.** At most `max_per_domain` from any one publisher, and
   `max_items` overall, filled best-tier-first.

Nothing here interprets. An item is stored with its title, its publisher, its
date and a bounded excerpt of what the source said — never a summary, never a
paraphrase, and never a claim. Interpretation is the synthesis layer's job and
doing any of it here would launder the collector's opinion into evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import structlog

from api_clients import ProviderError
from deep_research import (
    MAX_EXCERPT_CHARS,
    ExternalEvidence,
    ExternalSourceType,
    SourceTier,
    external_evidence_id,
)
from stock_screener.deep_research.sources import (
    canonical_url,
    is_denied,
    registrable_domain,
    source_type_for,
    tier_for,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from datetime import date

    from api_clients import ExternalResearchProvider
    from deep_research import DeepResearchBrief
    from domain import ExternalSearchResult
    from stock_screener.config import Settings

log = structlog.get_logger(__name__)

MIN_EXCERPT_CHARS = 80
"""Shortest excerpt worth keeping.

A twenty-character snippet is a headline fragment. It cannot support a claim, and
an evidence item that cannot support a claim is a citation with nothing behind
it — which is precisely the failure the `W.` namespace exists to make visible.
"""

MAX_MARKET_REACTION_ITEMS = 2
"""How many market-reaction pieces one evidence set may carry.

Two, because the first is context a reader wants — the market moved, and here is
a reputable account of it — and the second guards against the first being an
outlier. Live MU collection produced seven before this cap, all about one
quarter, crowding out a $250bn investment commitment and a strategic supply
agreement that appeared once each.
"""

_TIER_ORDER: dict[SourceTier, int] = {
    SourceTier.TIER_1_PRIMARY: 0,
    SourceTier.TIER_2_REPUTABLE: 1,
    SourceTier.TIER_3_SUPPORTING: 2,
}

_ACCESSION = re.compile(r"\b(\d{10})-?(\d{2})-?(\d{6})\b")

_EDGAR_FIRST_YEAR = 93
"""EDGAR's first year, as its accession numbers write it. The century pivot."""

_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "the",
        "for",
        "of",
        "to",
        "in",
        "on",
        "at",
        "by",
        "with",
        "its",
        "it",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "from",
        "that",
        "this",
        "after",
        "amid",
        "over",
        "into",
        "up",
        "down",
        "new",
        "says",
        "said",
        "inc",
        "corp",
        "corporation",
        "company",
        "co",
        "ltd",
        "plc",
        "nasdaq",
        "nyse",
    }
)

_WORD = re.compile(r"[a-z0-9]+")

_ROUNDUP = re.compile(
    r"\b("
    r"stock market today|market wrap|markets? (?:close|open|today)|"
    r"(?:pre|post)-?market movers|stocks to watch|biggest movers|"
    r"midday movers|after-?hours movers|market roundup|movers and shakers|"
    r"what to watch in the market"
    r")\b",
    re.IGNORECASE,
)
"""Headline shapes that mark a daily market column rather than a company story.

Matched on the headline only. A phrase list rather than a heuristic about how
many tickers appear, because counting tickers would also reject a genuine story
about two companies signing an agreement — which is exactly the kind of evidence
worth having.
"""

_EVENT_OVERLAP = 0.7
"""How much two headlines must share before they are called the same event.

Tuned against real duplicate flooding: an earnings story carried by six outlets
produces headlines that agree on the ticker, the quarter and the verb, which lands
well above this. Two genuinely different events about one company — a buyback and
a CEO departure — share almost nothing but the company name, which the stopword
list removes. Set lower and distinct events start collapsing; higher and the
sixth rewrite of one story survives.
"""


class EventClass(StrEnum):
    """What kind of thing an accepted item reports.

    A coarse label derived from the headline, existing for one purpose: to stop a
    single quarter's coverage filling the evidence set. Seven stories saying the
    market reacted to one earnings print are seven citations pointing at one
    fact, and a synthesis model handed them will weight that fact seven times.

    Not a taxonomy of the news, and not used for anything else. Deliberately
    keyword-driven rather than semantic — no embeddings, no model, no clustering.
    A misclassification costs at most one slot in a fifteen-item set.
    """

    PRIMARY_DEVELOPMENT = "PRIMARY_DEVELOPMENT"
    """Something the company did or announced: a partnership, a product, a
    contract, a lawsuit, a regulatory clearance."""

    EARNINGS = "EARNINGS"
    """Reported results, guidance or an outlook."""

    COMPANY_ACTION = "COMPANY_ACTION"
    """Capital allocation or leadership: investment, buyback, dividend,
    financing, an executive change."""

    INDUSTRY = "INDUSTRY"
    """The market the company sells into, rather than the company."""

    MARKET_REACTION = "MARKET_REACTION"
    """How the share price or the analyst community responded. Legitimate, and
    capped: it is the class most likely to arrive many times about one event, and
    the least likely to say anything the deterministic layers do not."""


class Rejection(StrEnum):
    """Why a search result did not become evidence.

    Counted rather than discarded silently, because the shape of the rejections
    is how a person judges whether the collector is working. A run that accepted
    three items and rejected forty as duplicates is healthy; one that rejected
    forty as untrusted means the allowlist is wrong for that company's sector.
    """

    DENIED_SOURCE = "DENIED_SOURCE"
    """A social platform, forum or aggregator. Named in the denylist."""

    UNTRUSTED_SOURCE = "UNTRUSTED_SOURCE"
    """Not in any tier. Unrecognised domains are refused, not demoted."""

    OUT_OF_WINDOW = "OUT_OF_WINDOW"
    """Published before the recency window."""

    MISSING_DATE = "MISSING_DATE"
    """Regulator material whose date could not be established.

    Only `SEC` results are held to this. Deep research is about what is true now,
    and an undated filing copy is not evidence of currency — it is evidence that
    nobody knows. Treating it as recent is the one reading that is certainly
    wrong, and the safe refusal costs nothing: a filing worth citing is already
    carried as `D.` metadata and `X.` text, where it has a real filed date."""

    SEC_DUPLICATE = "SEC_DUPLICATE"
    """The same filing is already carried as `X.` text, which is authoritative."""

    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    """A different outlet's version of a story already accepted."""

    OFF_TOPIC = "OFF_TOPIC"
    """The headline is not about this company.

    A search for a company returns adjacent industry stories that mention it once
    in passing — a drone-maker's export ban, a rival's antitrust probe. They come
    from impeccable sources and read as evidence, which is what makes them
    dangerous: a synthesis model handed an article about somebody else will
    happily reason about it. Requiring the company in the *headline* is the
    cheapest deterministic answer to "is this actually about them"."""

    MARKET_ROUNDUP = "MARKET_ROUNDUP"
    """A daily market wrap that lists the company among several tickers.

    Passes the off-topic check — the company genuinely is in the headline — while
    being about the market rather than the business. "Stock Market Today: Dow
    Rises As GM Surges; Marvell, Micron, Sandisk Rally" says nothing about Micron
    that a reader could research, and two of them in one evidence set displace two
    real events."""

    THIN_EXCERPT = "THIN_EXCERPT"
    """No quotable text, or too little of it to support a claim."""

    LANDING_PAGE = "LANDING_PAGE"
    """A section index or home page rather than an account of an event.

    A company's home page and its "quarterly results" archive are real pages on
    real primary domains, and both describe the company in general terms — which
    the deterministic layers and the filings already do, better and with dates.
    Collecting them fills the evidence set with context nobody needed at the
    expense of the events it exists to carry."""

    MALFORMED = "MALFORMED"
    """Missing a URL, a title or a publisher, or failed contract validation."""

    DOMAIN_CAP = "DOMAIN_CAP"
    """One publisher had already contributed its maximum."""

    ITEM_CAP = "ITEM_CAP"
    """The evidence set was already full."""

    REACTION_CAP = "REACTION_CAP"
    """Enough market-reaction pieces were already accepted.

    Not a judgement that the story is bad — the ones dropped are usually from
    excellent publishers. It is a judgement that the eighth account of how a
    share price moved after one quarter displaces a fact nobody else reported."""


@dataclass(frozen=True, slots=True)
class RejectedResult:
    """One result that did not make it, and why.

    Attributes:
        url: The result's URL, for tracing a specific decision.
        title: Its headline.
        reason: Which filter rejected it.
        detail: Extra context, such as the item it duplicated.
    """

    url: str
    title: str
    reason: Rejection
    detail: str = ""


@dataclass(frozen=True, slots=True)
class CollectionReport:
    """What one collection run searched, kept and refused.

    Attributes:
        ticker: The company searched for.
        queries: The queries issued, in order.
        raw: How many results the provider returned in total, across every query.
        unique: How many distinct documents those were, after canonical-URL
            deduplication. Every rejection count is over this number, not `raw` —
            four queries returning one denied aggregator is one rejection, not
            four, and the difference is what makes the report diagnostic.
        evidence: What survived, best tier first.
        rejected: Everything that did not, with a reason each.
        failures: Provider errors, as readable text. A search that failed is
            degradation, never a reason to abandon a brief.
    """

    ticker: str
    queries: tuple[str, ...] = ()
    raw: int = 0
    unique: int = 0
    evidence: tuple[ExternalEvidence, ...] = ()
    rejected: tuple[RejectedResult, ...] = ()
    failures: tuple[str, ...] = field(default_factory=tuple)

    @property
    def duplicates(self) -> int:
        """How many results were the same document arriving again."""
        return max(0, self.raw - self.unique)

    def counts(self) -> dict[str, int]:
        """Return how many results each rejection reason accounted for."""
        found: dict[str, int] = {}
        for item in self.rejected:
            found[item.reason.value] = found.get(item.reason.value, 0) + 1
        return found

    def by_tier(self) -> dict[SourceTier, int]:
        """Return how many accepted items came from each tier."""
        found = dict.fromkeys(_TIER_ORDER, 0)
        for item in self.evidence:
            found[item.tier] += 1
        return found

    def summary(self) -> str:
        """Return a one-line human-readable summary."""
        tiers = " ".join(
            f"t{index + 1}={self.by_tier()[tier]}" for tier, index in _TIER_ORDER.items()
        )
        return (
            f"ticker={self.ticker} raw={self.raw} unique={self.unique} "
            f"accepted={len(self.evidence)} rejected={len(self.rejected)} {tiers}"
        )


_CORPORATE_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "co",
        "company",
        "ltd",
        "limited",
        "plc",
        "lp",
        "llc",
        "holdings",
        "group",
        "the",
    }
)
"""Words that identify a legal wrapper rather than the business.

Dropped from both the search anchor and the company-domain guess: `"Micron
Technology"` is a better search phrase than `"Micron Technology, Inc."`, and
`acaciaresearch.com` is a better domain guess than `acaciaresearchcorporation.com`.
"""


def search_anchor(name: str, ticker: str) -> str:
    """Return the phrase every query is anchored on.

    The registered name in quotes with corporate suffixes dropped, followed by
    the ticker in parentheses: `"Micron Technology" (MU)`, `"Acacia Research"
    (ACTG)`. The quoted name leads, which is the fix for the ACTG failure — the
    bare symbol is also a DNA base sequence, and a search led by it returned
    whey-protein papers and biofuel newsletters.

    The ticker stays as parenthesised context rather than being dropped, and
    three shapes were measured live before settling on this one. A name-only
    anchor cost recall badly: `"Micron Technology"` returned 18 results across
    four queries where this shape returned 80. Shortening to `"Micron"` restored
    the count but pulled in general technology news — a judge's ruling about an
    N64 game, among others. And a *bare* trailing ticker was worst of all:
    `"Acacia Research" ACTG quarterly results` answered with twenty general
    earnings stories, none about Acacia, because the loose token dragged the
    query toward finance coverage at large.

    Recall is the scarce resource here and precision is cheap: `_is_off_topic`
    rejects anything whose headline does not name the company, so a wide query
    costs a few rejections while a narrow one costs evidence that does not exist.

    Args:
        name: The stored canonical company name.
        ticker: The symbol, as secondary context.

    Returns:
        The anchor phrase.
    """
    words = [
        word
        for word in re.split(r"[^A-Za-z0-9&]+", name)
        if word and word.lower() not in _CORPORATE_SUFFIXES
    ]
    if not words:
        return f'"{name.strip()}" ({ticker})'

    return f'"{" ".join(words)}" ({ticker})'


def build_queries(ticker: str, name: str) -> tuple[str, ...]:
    """Return the searches to run for one company.

    Four narrow queries rather than one broad one. A single "latest news" search
    returns whatever the vendor's ranking favours — in practice, several versions
    of the loudest story — while asking separately about results, products,
    capital and legal matters surfaces different *events*, which is what the
    evidence set is short of.

    Every query is anchored on the quoted company name with the ticker as
    parenthesised context — see `search_anchor` for the three shapes measured
    against live results before settling on that one.

    Deliberately not a query per section of the report. Sections are a synthesis
    concern; the collector gathers what happened, and the model decides which
    section a fact belongs in.

    Args:
        ticker: The symbol, normalised.
        name: The registered company name.

    Returns:
        The queries, in the order they should be issued.
    """
    anchor = search_anchor(name, ticker)
    return (
        f"{anchor} quarterly results earnings guidance",
        f"{anchor} product launch contract customer partnership",
        f"{anchor} acquisition financing buyback dividend executive change",
        f"{anchor} regulatory legal investigation industry outlook",
    )


def _company_domains(name: str) -> frozenset[str]:
    """Guess the company's own hosts from its registered name.

    Tries progressively longer prefixes of the name with corporate suffixes
    removed, because a one-word guess is wrong for most companies: "Acacia
    Research Corporation" lives at `acaciaresearch.com`, not `acacia.com`, and
    the single-word version silently failed to recognise its investor-relations
    site as primary.

    A deliberately weak heuristic, and safe because of where it is used: being
    wrong can only fail to promote a genuine investor-relations page to tier 1.
    It can never admit a source the allowlist would otherwise reject, because
    `tier_for` consults the denylist first and refuses anything unrecognised —
    so a wrong guess costs a tier, never a standard.
    """
    words = [
        word for word in _WORD.findall(name.lower()) if word and word not in _CORPORATE_SUFFIXES
    ]
    if not words:
        return frozenset()

    hosts: set[str] = set()
    for count in range(1, min(len(words), 3) + 1):
        slug = "".join(words[:count])
        if len(slug) < 3:
            continue
        hosts |= {f"{slug}.com", f"investor.{slug}.com", f"investors.{slug}.com", f"ir.{slug}.com"}
    return frozenset(hosts)


def _event_key(title: str) -> frozenset[str]:
    """Return the meaningful words in a headline, for same-event comparison."""
    return frozenset(_WORD.findall(title.lower())) - _STOPWORDS


def _same_event(one: frozenset[str], other: frozenset[str]) -> bool:
    """Whether two headlines describe the same announcement."""
    if not one or not other:
        return False
    overlap = len(one & other) / min(len(one), len(other))
    return overlap >= _EVENT_OVERLAP


def _sec_accessions(brief: DeepResearchBrief) -> frozenset[str]:
    """Return the accessions already carried as filing evidence, digits only."""
    filed = (reference.accession for reference in brief.filings)
    quoted = (excerpt.accession for excerpt in brief.excerpts)
    return frozenset(re.sub(r"\D", "", accession) for accession in (*filed, *quoted))


def _mentions_stored_filing(url: str, title: str, accessions: frozenset[str]) -> bool:
    """Whether a result is a web copy of a filing the brief already quotes.

    Matched on the accession number, which appears in every SEC URL and in most
    filing-aggregator links. Deliberately narrow: it catches the identical
    document, and lets through commentary *about* a filing, which is a different
    thing and often worth having.
    """
    if not accessions:
        return False
    for match in _ACCESSION.finditer(f"{url} {title}"):
        if "".join(match.groups()) in accessions:
            return True
    return False


_DEVELOPMENT = re.compile(
    r"\b("
    r"announce\w*|unveil\w*|launch\w*|introduc\w*|debut\w*|"
    r"acquir\w*|acquisition|merge\w*|merger|divest\w*|spin[- ]off|"
    r"sign\w*|partner\w*|partnership|agreement|deal with|contract|award\w*|"
    r"win[s]?\b|secur\w*|expand\w*|open\w*|build\w*|"
    r"settle\w*|settlement|sue[sd]?|lawsuit|litigation|patent|"
    r"approv\w*|clearance|authoris\w*|authoriz\w*|recall\w*|"
    r"appoint\w*|name[sd]? .{0,20}(?:chief|ceo|cfo|president)|resign\w*|step down|"
    r"breach|outage|strike"
    r")\b",
    re.IGNORECASE,
)
"""Verbs that mark something the company actually did."""

_CAPITAL_ACTION = re.compile(
    r"\b("
    r"buyback|repurchas\w*|dividend|special dividend|"
    r"invest(?:ment|ments|ing|s|ed)\b|capex|capital expenditure|commit[s]?\b|committed|"
    r"offering|convertible|notes?\b|financing|refinanc\w*|credit facility|"
    r"raise[sd]? \$|stake|"
    r"chief executive|chief financial|board of directors|independent director"
    r")\b",
    re.IGNORECASE,
)
"""Capital allocation and leadership — a company action rather than a product one."""

_EARNINGS = re.compile(
    r"\b("
    r"earnings|results|quarter\w*|q[1-4]\b|fiscal|guidance|forecast\w*|outlook|"
    r"revenue|profit|margin|eps|beats?|miss(?:es|ed)?|reports?|reported"
    r")\b",
    re.IGNORECASE,
)

_REACTION = re.compile(
    r"\b("
    r"(?:share|shares|stock|stocks)\s+"
    r"(?:rise|rises|rose|fall|falls|fell|jump|jumps|drop|drops|dive|dives|"
    r"spike|spikes|slide|slides|surge|surges|sink|sinks|climb|climbs|"
    r"plunge|plunges|soar|soars|tumble|tumbles|rally|rallies|gain|gains|"
    r"slip|slips|sag|sags|pop|pops|get|gets|hit|hits)|"
    r"(?:stock|shares)(?:'s)?\s+(?:pain|slump|woes|rout|struggles|surge|slide)|"
    r"pre-?market|after-?hours|intraday|premarket trading|"
    r"price[- ]target|target (?:hike|raised|cut|lowered)|"
    r"upgrade[sd]?|downgrade[sd]?|initiat\w+ coverage|analyst[s]? (?:say|see|expect)|"
    r"sell-?off|rout|overbought|oversold|record high|52-week|"
    r"investors? (?:await|bet|brace|shrug|cheer|fear)|nervous investors|"
    r"ahead of earnings|what to expect from"
    r")\b",
    re.IGNORECASE,
)
"""Headline shapes whose subject is the share price or the analyst view."""

_PRICE_SUBJECT = re.compile(r"\b(stock|stocks|shares)\b", re.IGNORECASE)

_INDUSTRY_WORDS = re.compile(
    r"\b(industry|sector|market outlook|demand|supply|shortage|glut|tariff\w*|"
    r"export control\w*|competitor\w*|rival\w*)\b",
    re.IGNORECASE,
)


def classify_event(title: str) -> EventClass:
    """Return what kind of thing a headline reports.

    Development verbs win over everything: a headline that says the company
    signed an agreement is about the agreement, whatever else it mentions.
    Reaction language is checked next, before earnings, because "Micron
    Earnings: Shares Spike 13%" is a story about the share price that happens to
    name the quarter — and treating it as earnings coverage is precisely how one
    print ends up cited seven times.

    A bare mention of "stock" or "shares" with no earnings or development
    content falls to `MARKET_REACTION` as well, which catches the idiomatic
    headlines no keyword list will ever enumerate.

    Args:
        title: The headline.

    Returns:
        The class. `PRIMARY_DEVELOPMENT` is the default, so an unrecognised
        company-specific story is never silently capped.
    """
    if _DEVELOPMENT.search(title):
        return EventClass.PRIMARY_DEVELOPMENT
    if _CAPITAL_ACTION.search(title):
        return EventClass.COMPANY_ACTION
    if _REACTION.search(title):
        return EventClass.MARKET_REACTION

    earnings = _EARNINGS.search(title)
    if _PRICE_SUBJECT.search(title) and not earnings:
        return EventClass.MARKET_REACTION
    if earnings:
        return EventClass.EARNINGS
    if _INDUSTRY_WORDS.search(title):
        return EventClass.INDUSTRY
    return EventClass.PRIMARY_DEVELOPMENT


def _subject_tokens(ticker: str, name: str) -> frozenset[str]:
    """Return the words a headline must contain to be about this company.

    The ticker, plus the distinctive words of the registered name with corporate
    suffixes removed — `micron`, `technology` for Micron Technology, Inc. Any one
    of them in the headline is enough.

    Args:
        ticker: The symbol, normalised.
        name: The registered company name.

    Returns:
        The acceptable tokens, lower case.
    """
    words = {
        word
        for word in _WORD.findall(name.lower())
        if word not in _CORPORATE_SUFFIXES and len(word) > 2
    }
    return frozenset({ticker.lower(), *words})


def _is_off_topic(title: str, subjects: frozenset[str]) -> bool:
    """Whether a headline fails to name the company it was returned for.

    Args:
        title: The result's headline.
        subjects: Tokens from `_subject_tokens`.

    Returns:
        True when none of the company's words appear in the headline.
    """
    return not (frozenset(_WORD.findall(title.lower())) & subjects)


def _accession_year(url: str, title: str) -> int | None:
    """Return the filing year encoded in an accession number, when one is present.

    The SEC's accession format is `NNNNNNNNNN-YY-NNNNNN`, where the middle pair is
    the year the filing was accepted. That is metadata rather than inference — the
    registrar assigns it — so reading it is safe in a way that guessing a date
    from page text would not be.

    It yields a year and nothing finer, which is why it can only ever *exclude*.
    A year strictly before the window began proves the item is stale; a current
    year proves nothing about the day.

    Args:
        url: The result's URL.
        title: The result's title, which sometimes carries the accession instead.

    Returns:
        The four-digit year, or None when no accession is present.
    """
    match = _ACCESSION.search(f"{url} {title}")
    if match is None:
        return None
    year = int(match.group(2))
    # EDGAR's two-digit year has no century, and the archive starts in 1993 — so
    # `93`-`99` are the 1990s and everything else is the 2000s. Reading `99` as
    # 2099 would place a 1999 filing comfortably inside any recency window, which
    # is the exact failure this function exists to prevent.
    return (1900 + year) if year >= _EDGAR_FIRST_YEAR else (2000 + year)


_MIN_ARTICLE_PATH_SEGMENTS = 2


def _is_landing_page(result: ExternalSearchResult) -> bool:
    """Whether a result looks like a section index rather than a specific event.

    Two signals together, and both are needed. An article normally lives at a
    deeper path than a section — `/news/2026/micron-raises-guidance` rather than
    `/news` — and a real story usually carries a publication date. A result with
    a shallow path *and* no date is a landing page in every case seen so far:
    a company home page, an investor-relations archive, a ticker overview.

    Requiring both keeps the rule narrow. A dated story at a shallow path is
    still a story, and a deep undated path is still specific enough to cite.

    Args:
        result: The candidate.

    Returns:
        True when the result names a section rather than an event.
    """
    if result.published_at is not None:
        return False
    path = urlsplit(canonical_url(result.url)).path.strip("/")
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) < _MIN_ARTICLE_PATH_SEGMENTS


def _excerpt(result: ExternalSearchResult) -> str:
    """Return the source's own words, bounded and never rewritten."""
    text = " ".join(result.snippet.split())
    return text[:MAX_EXCERPT_CHARS]


def collect_external_evidence(
    provider: ExternalResearchProvider,
    settings: Settings,
    brief: DeepResearchBrief,
    *,
    now: datetime | None = None,
) -> CollectionReport:
    """Search for current material about a company and keep what is worth citing.

    The only network access on the external path. Everything it returns is a
    validated `ExternalEvidence`; everything it refused is in the report with a
    reason, so a thin result set can be diagnosed rather than guessed at.

    A provider failure is recorded and the run continues with whatever other
    queries succeeded. External evidence is optional by design — a brief with none
    is a legal brief — so nothing here raises.

    Args:
        provider: The search provider.
        settings: Supplies the window, the caps and the result count.
        brief: The prepared brief. Read for the company's identity, its `as_of`,
            and the filings already carried as `X.` evidence.
        now: Treat this as the current time. Injected so tests do not depend on
            when they run.

    Returns:
        What was searched, what was kept, and why everything else was not.
    """
    retrieved_at = now or datetime.now(UTC)
    cutoff = retrieved_at.date() - timedelta(days=settings.external_research_window_days)
    domains = _company_domains(brief.name)
    accessions = _sec_accessions(brief)

    queries = build_queries(brief.ticker, brief.name)
    results: list[ExternalSearchResult] = []
    failures: list[str] = []

    for query in queries:
        try:
            results.extend(
                provider.search(
                    query,
                    since=cutoff,
                    limit=settings.external_research_max_results,
                )
            )
        except ProviderError as error:
            failures.append(f"{query!r}: {error}")
            log.warning("external search failed", ticker=brief.ticker, query=query)

    accepted, rejected, unique = _select(
        results,
        settings=settings,
        brief=brief,
        cutoff=cutoff,
        domains=domains,
        accessions=accessions,
        retrieved_at=retrieved_at,
    )

    report = CollectionReport(
        ticker=brief.ticker,
        queries=queries,
        raw=len(results),
        unique=unique,
        evidence=tuple(accepted),
        rejected=tuple(rejected),
        failures=tuple(failures),
    )
    log.info("external evidence collected", summary=report.summary())
    return report


def _select(
    results: Sequence[ExternalSearchResult],
    *,
    settings: Settings,
    brief: DeepResearchBrief,
    cutoff: date,
    domains: frozenset[str],
    accessions: frozenset[str],
    retrieved_at: datetime,
) -> tuple[list[ExternalEvidence], list[RejectedResult], int]:
    """Run every filter over the raw results and return what survived.

    Deduplication by canonical URL happens **first**, before any policy filter.
    Four queries about one company return the same article several times, and
    filtering each copy separately made the rejection counts a multiple of the
    truth — one denied aggregator read as four. Collapsing first means every
    later count is a count of distinct documents, which is what makes the report
    diagnostic rather than decorative.

    Order after that is cheapest-and-most-decisive first: who published it, when,
    whether it is about this company, whether it is a page about an event at all,
    whether we already have it as a filing, and whether it says enough to cite.
    Event deduplication and the caps run last, over what is left.

    Returns:
        The accepted evidence, the rejections over *unique* results, and how many
        unique results there were.
    """
    subjects = _subject_tokens(brief.ticker, brief.name)
    rejected: list[RejectedResult] = []

    def refuse(result: ExternalSearchResult, reason: Rejection, detail: str = "") -> None:
        rejected.append(RejectedResult(result.url, result.title, reason, detail))

    # 1. Canonicalise and collapse duplicates. A dropped copy is not a rejection;
    #    it is the same document arriving twice, and it is counted as such.
    unique: dict[str, ExternalSearchResult] = {}
    for result in results:
        unique.setdefault(canonical_url(result.url), result)

    candidates: list[tuple[SourceTier, ExternalSearchResult]] = []

    for result in unique.values():
        # 2. Source policy.
        if is_denied(result.url):
            refuse(result, Rejection.DENIED_SOURCE, registrable_domain(result.url))
            continue

        tier = tier_for(result.url, company_domains=domains)
        if tier is None:
            refuse(result, Rejection.UNTRUSTED_SOURCE, registrable_domain(result.url))
            continue

        source_type = source_type_for(result.url, company_domains=domains)

        # 3. Date and window policy.
        if result.published_at is not None:
            if result.published_at < cutoff:
                refuse(result, Rejection.OUT_OF_WINDOW, result.published_at.isoformat())
                continue
        elif source_type is ExternalSourceType.SEC:
            year = _accession_year(result.url, result.title)
            if year is not None and year < cutoff.year:
                refuse(result, Rejection.OUT_OF_WINDOW, f"accession year {year}")
            else:
                refuse(result, Rejection.MISSING_DATE, "no publication date on SEC material")
            continue

        # 4. Is it about this company at all?
        if _is_off_topic(result.title, subjects):
            refuse(result, Rejection.OFF_TOPIC, "the company is not named in the headline")
            continue

        # 5. Market columns that merely list the company.
        if _ROUNDUP.search(result.title):
            refuse(result, Rejection.MARKET_ROUNDUP, "a market column, not a company story")
            continue

        # 6. Landing-page policy.
        if _is_landing_page(result):
            refuse(result, Rejection.LANDING_PAGE, "no dated article path")
            continue

        # 7. Already carried as filing evidence.
        if _mentions_stored_filing(result.url, result.title, accessions):
            refuse(result, Rejection.SEC_DUPLICATE, "already carried as X. filing text")
            continue

        # 8. Enough text to support a claim.
        if len(" ".join(result.snippet.split())) < MIN_EXCERPT_CHARS:
            refuse(result, Rejection.THIN_EXCERPT, f"{len(result.snippet)} characters")
            continue

        candidates.append((tier, result))

    # Best tier first, then newest, then title — fully deterministic, and
    # independent of the order the provider happened to return.
    candidates.sort(
        key=lambda pair: (
            _TIER_ORDER[pair[0]],
            -(pair[1].published_at.toordinal() if pair[1].published_at else 0),
            pair[1].title.lower(),
        )
    )

    accepted: list[ExternalEvidence] = []
    events: list[frozenset[str]] = []
    per_domain: dict[str, int] = {}
    reactions = 0

    for tier, result in candidates:
        # 9. Event deduplication.
        key = _event_key(result.title)
        if any(_same_event(key, seen) for seen in events):
            refuse(result, Rejection.DUPLICATE_EVENT, "same story already accepted")
            continue

        # 10. Caps.
        if len(accepted) >= settings.external_research_max_items:
            refuse(result, Rejection.ITEM_CAP, f"already at {len(accepted)}")
            continue

        host = registrable_domain(result.url)
        if per_domain.get(host, 0) >= settings.external_research_max_per_domain:
            refuse(result, Rejection.DOMAIN_CAP, host)
            continue

        # Candidates are already sorted best-tier-then-newest, so the reaction
        # pieces that survive the cap are the best-sourced and most recent ones
        # rather than whichever the provider happened to rank first.
        event = classify_event(result.title)
        if event is EventClass.MARKET_REACTION:
            if reactions >= MAX_MARKET_REACTION_ITEMS:
                refuse(
                    result,
                    Rejection.REACTION_CAP,
                    f"already carrying {reactions} market-reaction items",
                )
                continue
            reactions += 1

        evidence = _as_evidence(result, tier, brief, domains, retrieved_at)
        if evidence is None:
            refuse(result, Rejection.MALFORMED, "failed contract validation")
            continue

        accepted.append(evidence)
        events.append(key)
        per_domain[host] = per_domain.get(host, 0) + 1

    return accepted, rejected, len(unique)


def _as_evidence(
    result: ExternalSearchResult,
    tier: SourceTier,
    brief: DeepResearchBrief,
    domains: frozenset[str],
    retrieved_at: datetime,
) -> ExternalEvidence | None:
    """Build one validated evidence item, or None when the result is malformed.

    Validation is the contract's own — `ExternalEvidence` rejects an empty title,
    an empty excerpt, a naive timestamp or an id outside the `W.` namespace. This
    catches the rejection and reports it rather than repairing anything: a
    collector that invented a publisher to satisfy a required field would be
    fabricating exactly the metadata a reader relies on.

    The id is derived from the canonical URL, so the same article collected
    tomorrow keeps the same handle and the external fingerprint does not move.
    """
    canonical = canonical_url(result.url)
    publisher = result.publisher.strip() or registrable_domain(result.url)
    source_type = source_type_for(result.url, company_domains=domains)

    try:
        return ExternalEvidence(
            evidence_id=external_evidence_id(source_type, canonical),
            source_type=source_type,
            tier=tier,
            title=" ".join(result.title.split()),
            publisher=publisher,
            url=canonical,
            excerpt=_excerpt(result),
            retrieved_at=retrieved_at,
            published_at=result.published_at,
            ticker=brief.ticker,
        )
    except ValueError as error:
        log.warning("external evidence rejected", url=result.url, error=str(error)[:200])
        return None


def evidence_from(reports: Iterable[CollectionReport]) -> tuple[ExternalEvidence, ...]:
    """Flatten several collection reports into one evidence set."""
    return tuple(item for report in reports for item in report.evidence)
