"""Who is worth citing, and how two links are recognised as the same thing.

Collection policy, deliberately kept out of both the contract package and the
provider adapter. `deep_research` states what a `SourceTier` means and refuses to
name publishers; an adapter has no basis for judging one. This module is where the
judgement lives, in one file, as data — so changing who counts as reputable is a
reviewable diff rather than an argument spread across a codebase.

Three rules, and the reason each exists.

**A source is excluded unless it is recognised.** The default for an unknown
domain is rejection, not `TIER_3_SUPPORTING`. An allowlist that fails open is an
allowlist in name only, and the failure mode — an SEO content farm cited in a
research report — is exactly what the tiers exist to prevent.

**Social platforms are named anyway.** They would be rejected by the default
already, so the denylist is redundant by construction. It is here so that the
rejection is *visible* in the collection report rather than indistinguishable
from an unrecognised trade journal, and so that anyone widening the allowlist
trips over the list of things V1 will not cite.

**A URL is identity.** Two links to one article must collide, so tracking
parameters, fragments, `www.`, `amp` suffixes and trailing slashes are stripped
before comparison. Getting this wrong shows up as five copies of one earnings
story crowding out four real events.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlsplit, urlunsplit

from deep_research import ExternalSourceType, SourceTier

TIER_1_DOMAINS: frozenset[str] = frozenset(
    {
        "sec.gov",
        "investor.gov",
        "businesswire.com",
        "prnewswire.com",
        "globenewswire.com",
        "accesswire.com",
        "newsfilecorp.com",
        "prweb.com",
    }
)
"""Wire services and regulators — the company or the filer speaking directly.

A press release distributed through Business Wire is the company's own words, not
a report about them. That makes it primary in the sense the tiers mean: closest to
the facts, and least mediated. It does **not** make it true, and nothing here
suggests otherwise — a company's own account of its prospects is exactly the kind
of claim a reader should discount, which is why the basis on such a claim is
`EXTERNAL` and names the publisher.

Company investor-relations hosts cannot be enumerated — there is one per company —
so they are recognised per company by `tier_for`, against the ticker's own domain.
"""

TIER_2_DOMAINS: frozenset[str] = frozenset(
    {
        "reuters.com",
        "apnews.com",
        "bloomberg.com",
        "wsj.com",
        "ft.com",
        "cnbc.com",
        "barrons.com",
        "forbes.com",
        "fortune.com",
        "economist.com",
        "nytimes.com",
        "washingtonpost.com",
        "marketwatch.com",
        "investors.com",
        "axios.com",
        "bbc.com",
        "bbc.co.uk",
        "theguardian.com",
        "npr.org",
        "cnn.com",
        "nbcnews.com",
        "cbsnews.com",
        "abcnews.go.com",
        "usatoday.com",
        "latimes.com",
        "politico.com",
        "businessinsider.com",
    }
)
"""News organisations with an editorial process and a corrections policy."""

TIER_3_DOMAINS: frozenset[str] = frozenset(
    {
        "tomshardware.com",
        "anandtech.com",
        "eetimes.com",
        "semianalysis.com",
        "theregister.com",
        "arstechnica.com",
        "techcrunch.com",
        "theverge.com",
        "zdnet.com",
        "lightreading.com",
        "fiercebiotech.com",
        "fiercepharma.com",
        "statnews.com",
        "endpts.com",
        "biopharmadive.com",
        "utilitydive.com",
        "supplychaindive.com",
        "retaildive.com",
        "healthcaredive.com",
        "oilprice.com",
        "rigzone.com",
        "hartenergy.com",
        "worldoil.com",
        "spglobal.com",
        "ratings.moodys.com",
        "law360.com",
        "reuters.legal",
        "ieee.org",
        "digitimes.com",
        "trendforce.com",
    }
)
"""Trade and industry publications: strong on a market, weakest on one company."""

DENIED_DOMAINS: frozenset[str] = frozenset(
    {
        "reddit.com",
        "old.reddit.com",
        "x.com",
        "twitter.com",
        "stocktwits.com",
        "facebook.com",
        "instagram.com",
        "tiktok.com",
        "youtube.com",
        "quora.com",
        "medium.com",
        "substack.com",
        "seekingalpha.com",
        "fool.com",
        "investorplace.com",
        "zacks.com",
        "benzinga.com",
        "marketbeat.com",
        "simplywall.st",
        "tipranks.com",
        "gurufocus.com",
        "wallstreetzen.com",
        "stocktitan.net",
        "nasdaq.com",
        "finance.yahoo.com",
        "insidermonkey.com",
        "247wallst.com",
        "barchart.com",
    }
)
"""Named refusals for V1: social platforms, forums, and aggregator/SEO finance sites.

Redundant against the allowlist default, and kept anyway so a rejection reads as a
decision rather than as an omission. Two entries deserve their reasoning:

`seekingalpha.com` and `fool.com` publish real analysis alongside a great deal of
unvetted contributor content, and V1 has no way to tell the two apart from a URL.

`nasdaq.com` and `finance.yahoo.com` mostly re-host wire copy under their own
domain, so admitting them would mean the same press release arriving twice under
two publishers — the duplicate-event flooding the caps exist to prevent.
"""

_TRACKING_PREFIXES = ("utm_", "mc_", "pk_")
_TRACKING_KEYS = frozenset(
    {"fbclid", "gclid", "igshid", "ref", "ref_src", "source", "cmpid", "smid", "guccounter"}
)


def registrable_domain(url: str) -> str:
    """Return the comparable host for a URL: lower case, no `www.`, no port.

    Not a public-suffix implementation, and does not need to be. It normalises
    enough that two links to one outlet agree, and subdomains are preserved
    because `investor.acme.com` and `acme.com` are meaningfully different things
    to a tier decision.

    Args:
        url: Any absolute URL.

    Returns:
        The host, or an empty string when the URL has none.
    """
    host = urlsplit(url).hostname or ""
    return host.removeprefix("www.").lower()


def _matches(host: str, domains: frozenset[str]) -> bool:
    """Whether a host is one of these domains, or a subdomain of one."""
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_denied(url: str) -> bool:
    """Whether a URL is from a source V1 refuses to cite.

    Args:
        url: The result's URL.

    Returns:
        True for social platforms, forums and aggregator finance sites.
    """
    return _matches(registrable_domain(url), DENIED_DOMAINS)


def tier_for(url: str, *, company_domains: frozenset[str] = frozenset()) -> SourceTier | None:
    """Return the tier a URL qualifies for, or None when it does not qualify.

    Args:
        url: The result's URL.
        company_domains: Hosts belonging to the company itself — its own site and
            investor-relations subdomain. Recognised as primary because a company
            publishing on its own site is the company speaking.

    Returns:
        The tier, or None for a denied or unrecognised source. None is the
        default on purpose: an allowlist that admitted the unknown would not be
        one.
    """
    host = registrable_domain(url)
    if not host or _matches(host, DENIED_DOMAINS):
        return None
    if company_domains and _matches(host, company_domains):
        return SourceTier.TIER_1_PRIMARY
    if _matches(host, TIER_1_DOMAINS):
        return SourceTier.TIER_1_PRIMARY
    if _matches(host, TIER_2_DOMAINS):
        return SourceTier.TIER_2_REPUTABLE
    if _matches(host, TIER_3_DOMAINS):
        return SourceTier.TIER_3_SUPPORTING
    return None


def source_type_for(
    url: str, *, company_domains: frozenset[str] = frozenset()
) -> ExternalSourceType:
    """Return what kind of source a URL is.

    Args:
        url: The result's URL.
        company_domains: Hosts belonging to the company itself.

    Returns:
        `SEC` for regulator material, `COMPANY` for the company's own site or a
        wire release, `INDUSTRY` for trade press, `NEWS` otherwise.
    """
    host = registrable_domain(url)
    if _matches(host, frozenset({"sec.gov", "investor.gov"})):
        return ExternalSourceType.SEC
    if company_domains and _matches(host, company_domains):
        return ExternalSourceType.COMPANY
    if _matches(host, TIER_1_DOMAINS):
        return ExternalSourceType.COMPANY
    if _matches(host, TIER_3_DOMAINS):
        return ExternalSourceType.INDUSTRY
    return ExternalSourceType.NEWS


def canonical_url(url: str) -> str:
    """Return a URL reduced to what identifies the document.

    Strips the scheme's variability, `www.`, tracking parameters, fragments, AMP
    suffixes and a trailing slash, then sorts what query parameters remain. Two
    links that survive to the same string are the same article, which is what the
    duplicate filter and the evidence id both depend on.

    Args:
        url: Any absolute URL.

    Returns:
        The canonical form, or the input stripped when it cannot be parsed.
    """
    try:
        parts = urlsplit(url)
    except ValueError:  # pragma: no cover — urlsplit is extremely permissive
        return url.strip()

    host = registrable_domain(url)
    if not host:
        return url.strip()

    path = parts.path.rstrip("/")
    for suffix in ("/amp", ".amp", "/amp.html"):
        path = path.removesuffix(suffix)

    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_KEYS and not key.lower().startswith(_TRACKING_PREFIXES)
    )
    query = "&".join(f"{key}={value}" for key, value in kept)

    return urlunsplit(("https", host, path, query, ""))
