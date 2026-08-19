---
type: reference
title: Phase 6 — on-demand deep research
---

# Phase 6 — on-demand deep research

Phases 1 to 5 built a screener: it scans the market, scores it, ranks it,
explains the top of that ranking with grounded AI research, and runs itself from
a dashboard. Every part of it is market-wide and scheduled.

Phase 6 is the other shape. Somebody types a ticker and asks for one company to
be investigated properly — against its current fundamentals, its filings, and
what has since been published about it.

**Phase 6A** is the contract and the table. **Phase 6B** is single-stock
preparation. **Phase 6C** is external evidence collection — the `W.` namespace,
filled. None of them calls a synthesis model or builds a UI.

## A separate layer, not a replacement

```
                Deterministic Data
                        |
                        v
              CompounderScore V1.1
                        |
            +-----------+-----------+
            |                       |
      SEC evidence          current external evidence
       (D. / X.)                    (W.)
            |                       |
            +-----------+-----------+
                        |
                        v
                  Deep Research
                        |
                        v
          grounded research report
```

Everything above the fork is unchanged. The scan, the metric engine, the score,
the risk penalties, the ranking views and Phase 3's `ResearchReport` all behave
exactly as they did.

## The five guarantees

### Web and news never change the CompounderScore

The score is computed by `domain` from deterministic data before any external
source is read, and nothing downstream may revise it. Three mechanisms hold the
line, and all three would have to be dismantled to break it:

- `research.EvidenceKind` has no `W.` member, so an external id is unresolvable
  to the deterministic layer rather than merely discouraged there.
- The sections that account for the score — `current_snapshot` and
  `why_the_algorithm_likes_it` — accept `DETERMINISTIC` and `UNKNOWN` claims
  only.
- `DeepResearchReport` has no score, rating, category or component field.

### Deep Research issues no investment instructions

There is no BUY, SELL or HOLD, no price target, no fair value and no position
size anywhere in the contract. `research_conclusion` states how strong the
evidence is and how interesting the company is to research further — a statement
about the research, not about what anyone should do.

### Provenance is retained

Every claim carries a `basis` and the ids it rests on, and the namespace of an id
says where it came from:

| Prefix | Evidence |
| --- | --- |
| `S.` | a line of the stored score breakdown |
| `M.` | a derived metric |
| `F.` | a reported financial period |
| `D.` | filing metadata — that a document exists |
| `X.` | text extracted from a filing — what it says |
| `E.` | a field the metered provider supplied |
| `W.` | **new:** current external evidence |

A stored report carries the external items its claims cite, so a `W.` citation
still resolves to a title, publisher, URL and date long after the brief is gone.

### Stale and current evidence are distinguishable

`DataFreshness` records when the company's data was last refreshed, the date of
the most recent price, the last reported period, and anything the refresh could
not bring up to date. Each external item carries `published_at`, `occurred_at`
and `retrieved_at`, and reports its age against a brief's `as_of`.

### It is on-demand, per ticker — never market-wide

There is no `SelectionReason` on a deep brief. A Phase 3 brief records why the
screener picked a company; a deep brief exists because somebody asked about one.
Nothing here runs across the universe.

## What a deep report answers

Seventeen sections, in reading order: `company_overview`, `current_snapshot`,
`why_the_algorithm_likes_it`, `growth_quality`, `financial_quality`,
`valuation`, `latest_earnings`, `recent_developments`, `competitive_position`,
`catalysts`, `major_risks`, `bull_case`, `bear_case`, `thesis_breakers`,
`what_the_market_may_be_missing`, `what_to_watch_next`, `research_conclusion`.

## The claim model

| Basis | May cite | Means |
| --- | --- | --- |
| `DETERMINISTIC` | `S.` `M.` `F.` `E.` | restates a figure this system produced |
| `EXTRACTED` | `X.` (a `D.` alone is insufficient) | quotes a filing |
| `EXTERNAL` | `W.` | reports what a named source published |
| `INTERPRETATION` | any resolvable id | the model's reasoning |
| `UNKNOWN` | nothing | the evidence does not answer this |

`UNKNOWN` is legal in every section and is what a section falls back to.

## External evidence

| Source type | Is |
| --- | --- |
| `COMPANY` | investor relations, official releases, posted transcripts |
| `SEC` | SEC material outside the deterministic filing pipeline |
| `NEWS` | a news organisation reporting on the company |
| `INDUSTRY` | trade press covering the market it sells into |

| Tier | Is |
| --- | --- |
| `TIER_1_PRIMARY` | SEC, company IR, official releases |
| `TIER_2_REPUTABLE` | major news organisations |
| `TIER_3_SUPPORTING` | credible industry and trade publications |

There is no tier below the third, so social media, forums and blogs are
unrepresentable in V1. No publisher is hard-coded into the contract; assigning a
tier is the collector's job.

An item's id is derived from its URL — `W.news.7f3a1c9e2b04` — so re-collecting
the same article yields the same id.

## Fingerprints and caching

Three hashes, because there are two different reasons a report goes stale:

| Fingerprint | Covers | Moves when |
| --- | --- | --- |
| `deterministic_fingerprint` | score, metrics, periods, filings, excerpts, enrichment | the company's data is rescanned or rescored |
| `external_fingerprint` | the external items | a source is added, removed or changed |
| `evidence_fingerprint` | both digests | either of the above — the cache key |

**Process metadata is never hashed.** `assembled_at`, each item's
`retrieved_at`, and `DataFreshness`'s `refreshed_at`, `refreshed`, `reused` and
`stale` all record how the evidence was obtained rather than what it is. Hashing
them would hand an unchanged company a new cache key every time an optional
provider had a bad afternoon, and the cache would buy nothing exactly when it
matters. The freshness *dates* are hashed: `price_as_of` moving is a real change
in the evidence. External items are sorted by id before hashing, so collection
order is not evidence.

## Storage

`deep_research_reports` (migration `0013`) stores one row per validated report.

**It appends; it never overwrites.** `research_reports` upserts on its cache key,
which is right for a report explaining a stored snapshot. A deep report is a
dated investigation, and the record of how a thesis changed across runs is the
part worth keeping — so this table has no unique constraint, and
`DeepResearchReportRepository` has no update or delete path. Caching reads the
newest row matching `(company_id, deterministic_fingerprint,
evidence_fingerprint, prompt_version)`.

No draft is ever stored. `save` accepts a `DeepResearchReport`, the type only
deep validation constructs.

## Phase 6B — single-stock preparation

One command refreshes everything the system knows about one company and hands
back a fresh brief:

```
stock-screener deep-research prepare MU
```

### The order, and why it is that order

| # | Stage | Does |
| --- | --- | --- |
| 1 | `profile` | Resolve the ticker to a company row |
| 2 | `market_data` | `update_market_data(tickers=[T])` |
| 3 | `fundamentals` | `update_fundamentals(tickers=[T])` |
| 4 | `benchmark` | `update_benchmark()`, only if it has fallen behind |
| 5 | `score` | `score_market(tickers=[T], persist=True)` |
| 6 | `filings` | `update_filings(tickers=[T])` — `D.` evidence |
| 7 | `filing_text` | `update_filing_text(tickers=[T])` — `X.` evidence |
| — | assembly | `assemble_deep_brief(...)`, **zero network** |

The order is forced by dependencies. Metrics are computed from stored prices and
stored statements, so 2 and 3 precede 5. Relative strength needs the benchmark,
so 4 precedes 5. The brief is bounded by the score's own date, so 5 precedes
assembly.

**Every stage is an existing pass, narrowed by the `tickers` argument those
passes have always taken.** There is no second fundamentals engine, no
deep-research metric, and no second score: the snapshot written is an ordinary
`COMPOUNDER_V1_1` row, indistinguishable from one a nightly run would write.

### Network first, assembly second

`preparation` performs every network call. `brief` performs none — it reads the
database and nothing else. That is the same split Phase 3 draws between
`prepare_filing_evidence` and `assemble_brief`, and it is what makes a brief
reproducible: assemble twice over unchanged data and the fingerprints match, so
the cache can work.

The deterministic half of the brief is not rebuilt here. It comes from
`research.assemble_deterministic_evidence` — the same function the Phase 3
assembler uses — so a deep brief and a Phase 3 brief for the same company on the
same score date carry byte-identical score evidence, metrics, periods, filings
and excerpts.

### Freshness

`DataFreshness` answers three questions from the brief itself: what was
refreshed on this run, what was already current and reused, and what could not be
brought up to date. The dates are read from the database rather than from the
run, so a stage that degraded leaves the previous date standing and the brief
reports what is actually stored.

### Fatal versus degraded

| Fatal — preparation raises | Degraded — recorded, run continues |
| --- | --- |
| The ticker resolves to no company | An optional provider is unavailable or out of quota |
| No score snapshot exists after scoring | A pass fails for this one company |
| A database error | No readable filing text exists |

A company that comes back `INSUFFICIENT_DATA`, `UNSUPPORTED_SECTOR` or
ineligible is **not** a failure. It gets a brief carrying the status that says
why, because fabricating a score to make deep research work would be the one
outcome worse than no answer.

### Resolving a ticker that is not in the universe

`update_fundamentals` and every other pass iterate *stored* companies, so an
unknown symbol would silently do nothing at every stage. Rather than inventing
symbol discovery, preparation asks the fundamentals provider for the one profile
— the same `get_company_profile` call `update_fundamentals` already makes per
company — and stores it through the same repository. A market-wide
`update-universe` is never triggered. A symbol the provider does not recognise is
fatal.

### Ranking

`MarketRanking` ranks against **every company's most recent scored snapshot**,
not against one `score_date`. Scoring a single ticker writes a snapshot on
today's date, and that date then holds exactly one company — ranking within it
reported "1 of 1" for every prepared company, which is true and useless.

### Not built in Phase 6B

External collection and web search, the deep prompt, any model call, the deep
validator, and any UI or job kind. A brief assembled by this path always has
`external=()`.

## Phase 6C — current external evidence

```
stock-screener deep-research prepare MU --external
```

Without the flag the brief carries `external=()`, which is a complete brief.
Deterministic preparation never depends on a search vendor being configured,
reachable or paid for.

### The stages

```
prepare (network)  →  collect (network)  →  assemble (zero network)
```

`collect_external_evidence` is the only new network stage. `assemble_deep_brief`
takes already-collected evidence as an argument and still fetches nothing, which
is what keeps fingerprints reproducible.

### The provider boundary

`ExternalResearchProvider` is one method — `search(query, since, limit)` returning
`ExternalSearchResult`. Adapters translate a vendor payload and stop: they assign
no tier and reject nothing on quality grounds, because who is trustworthy is
collection policy and an adapter deciding it would make the policy untestable
without a transport. `TavilySearch` is the live implementation;
`MockExternalResearch` serves fixtures and is what every test uses.

There is no crawler, queue, browser, scraper, vector store or embedding
anywhere in it.

### Source policy

| Tier | Who | Examples |
| --- | --- | --- |
| `TIER_1_PRIMARY` | the filer or the company itself | SEC, the company's own IR host, Business Wire, PR Newswire, GlobeNewswire |
| `TIER_2_REPUTABLE` | news organisations with an editorial process | Reuters, AP, Bloomberg, WSJ, FT, CNBC, Barron's |
| `TIER_3_SUPPORTING` | trade and industry press | Tom's Hardware, EE Times, DigiTimes, TrendForce, sector Dives |

**An unrecognised domain is rejected, not demoted.** An allowlist that admits the
unknown is not an allowlist, and the failure mode is an SEO content farm cited in
a research report.

Social platforms, forums and aggregator finance sites are named in a denylist as
well — redundant against that default, and kept so a rejection reads as a
decision. Reddit, X, StockTwits, Substack, Medium, Seeking Alpha, Motley Fool,
Benzinga, Zacks, Yahoo Finance, Nasdaq.com and similar are all refused. The last
two matter because they mostly re-host wire copy, so admitting them means the
same release arriving twice under two publishers.

### What is rejected, and why

| Reason | Means |
| --- | --- |
| `DENIED_SOURCE` | named in the denylist |
| `UNTRUSTED_SOURCE` | in no tier — the default for anything unrecognised |
| `OUT_OF_WINDOW` | published before the window (90 days by default) |
| `SEC_DUPLICATE` | the same filing is already carried as `X.` text |
| `DUPLICATE_URL` | the same document under a different link |
| `DUPLICATE_EVENT` | another outlet's version of a story already accepted |
| `OFF_TOPIC` | the company is not named in the headline |
| `MARKET_ROUNDUP` | a daily market column listing the company among tickers |
| `REACTION_CAP` | enough market-reaction pieces were already accepted |
| `MISSING_DATE` | SEC material whose date could not be established |
| `LANDING_PAGE` | a section index or home page, not an account of an event |
| `THIN_EXCERPT` | too little quotable text to support a claim |
| `MALFORMED` | failed contract validation |
| `DOMAIN_CAP` / `ITEM_CAP` | the publisher or the set was already full |

### Event classes and the reaction cap

Every accepted item is classified from its headline into `PRIMARY_DEVELOPMENT`,
`EARNINGS`, `COMPANY_ACTION`, `INDUSTRY` or `MARKET_REACTION` — keyword-driven,
no embeddings and no model. Only one thing depends on it: **at most two
`MARKET_REACTION` items per set.**

Live MU collection produced seven before the cap, all about one quarter — a
premarket move, an intraday move, two "how the stock recovers" pieces, a
price-target reaction. Seven citations pointing at one fact, crowding out a
$250bn investment commitment and a strategic supply agreement that appeared once
each. Candidates are sorted best-tier-then-newest before the cap applies, so the
reaction pieces that survive are the best-sourced and most recent.

`PRIMARY_DEVELOPMENT` is the default, so an unrecognised headline is never
silently capped.

### Query construction

Anchored on the quoted company name with the ticker as parenthesised context:
`"Micron Technology" (MU)`. Three shapes were measured against live results:

| Shape | Outcome |
| --- | --- |
| `"Micron Technology"` alone | 18 raw across four queries — too narrow |
| `"Micron"` alone | recall restored, but pulled in general tech news |
| `"Acacia Research" ACTG` (bare ticker) | 20 results, none about Acacia |
| `"Micron Technology" (MU)` | 80 raw, best recall, noise caught downstream |

Leading with the name is what fixes an ambiguous symbol — `ACTG` is also a DNA
base sequence, and a ticker-led search returned whey-protein papers. Recall is
the scarce resource and precision is cheap: `OFF_TOPIC` rejects anything whose
headline does not name the company, so a wide query costs a few rejections while
a narrow one costs evidence that does not exist.

### Deduplication

Three passes, all deterministic and none of them semantic. Canonical URL first —
scheme, `www.`, tracking parameters, fragments, AMP suffixes and trailing slashes
all removed. Then same-event detection: headlines reduced to meaningful words
and compared by overlap ratio, so six outlets carrying one earnings story keep
one. Then caps: at most three items per publisher and fifteen overall, filled
best-tier-first so a full set is the best available rather than the first
returned.

### SEC evidence is not collected twice

`X.` is authoritative for filings. A search result whose URL or title carries an
accession number the brief already holds is rejected as `SEC_DUPLICATE`. The rule
is narrow on purpose: it catches the identical document, and lets through
*commentary about* a filing, which is a different thing and often worth having.

### Excerpts

Bounded by `MAX_EXCERPT_CHARS`, taken as the source's own words, never rewritten.
The collector stores evidence; interpretation is the synthesis layer's job, and
paraphrasing here would launder the collector's opinion into the record.

### Fingerprints

Unchanged from 6A, and now exercised. The external fingerprint moves when a
source is added or removed, or when an excerpt materially changes. It does not
move when only `retrieved_at` changes, or when the vendor reshuffles its ranking —
ids are derived from the canonical URL, and items are sorted by id before hashing.

### Not built in Phase 6C

Synthesis, drafts, reports, any model call, UI, alerts, scheduled monitoring,
sentiment scoring, social ingestion, peer comparison, vector stores, embeddings.

## Not built in Phase 6

Deliberately absent, and not to be started without being asked:

- external collection and web search
- the deep prompt and any model call
- the deep validator — `provenance` states the rules; nothing applies them yet
- the numeric whitelist for deep claims
- data-refresh orchestration
- any UI, endpoint or job kind
- a staleness policy beyond `ExternalEvidence.age_in_days`
