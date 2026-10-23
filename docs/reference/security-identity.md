---
type: reference
title: Security identity
---

# Security identity

What a security *is*, kept separate from what it is currently called. This page
is a **state record**, checkable against the code and the tests named in it. It
defines no strategy, calibrates no threshold, and changes no score.

Everything in the point-in-time layer bounds what a past decision could *know*.
This bounds what a past decision could be *about*. A study that joins on a ticker
is studying a lease rather than a company, and no amount of point-in-time care
downstream repairs a security that is silently two securities.

## The problem, stated precisely

Three handles are available, and all three are labels rather than identities.

| Handle | Why it is not an identity |
| --- | --- |
| **Ticker** | A lease. `FB` became `META` with no change of company; `BBBY` passed from Bed Bath & Beyond to Beyond, Inc. on 2025-09-02, twenty-eight months after the first one's equity was extinguished. Joining on it **splices two companies into one history**. |
| **CUSIP** | A dated identifier of an *issue*. A reverse split retires one CUSIP and mints another while the ticker, the issuer and the economics carry straight through. Keying on it **splits one company's history in two**. |
| **CIK** | Stable, and the right handle where it exists — but the free record establishes one for about an eighth of delisted symbols. A schema that requires it **cannot hold the names it exists to hold**. |

Migration `0026` chose the third and hit exactly that wall: `historical_securities.cik`
was `NOT NULL`, so the 87.5% of delisted symbols with no recoverable CIK had
nowhere to be stored at all.

## The shape

The identity is `historical_securities.id` — an opaque internal number that names
the security and nothing else. Every external handle hangs off it as a **dated
interval**.

```
historical_securities.id          ← the identity. Never a CUSIP, never a ticker.
    ├── security_identifiers      ← cusip, ticker, venue, valid_from, valid_to,
    │                                source, confidence, lineage
    ├── cik                       ← still unique where known, now nullable
    ├── historical_observations   ← what it filed, selected on filed_date
    ├── historical_prices         ← what it traded at
    └── historical_corporate_actions
```

Three rules follow, and they are what the tests hold:

1. **A ticker change preserves `security_id`.** The old interval closes, a new
   one opens against the same security.
2. **A ticker reused by another issuer takes a different `security_id`.** Two
   securities, two interval sets, and no join between them exists to be made.
3. **A reverse split preserves `security_id`.** A new CUSIP is a new *interval*,
   never a new security.

Ranges are inclusive at both ends and must not overlap for one identifier: an
identifier names at most one security on any date. `succeed_interval` is the
single operation all three rules reduce to — it closes an interval the day before
its successor opens, and it cannot be given a different `security_id`, so it can
neither split a security nor splice two.

### Why the CUSIP issuer base is a rule and not a heuristic

The CUSIP standard reserves characters 1–6 for the **issuer** and 7–8 for the
*issue*. So two CUSIPs sharing those six characters are two issues of one
company, and two that differ in them are two companies — a fact about the
identifier scheme, not an inference from the data. `classify_symbol` groups a
symbol's evidence on it, and no lineage is ever drawn across that boundary. That
is what stops `BBBY`'s two occupants — Bed Bath & Beyond at `075896` and
Beyond, Inc. at `690370` — from being spliced.

Within one issuer, a second CUSIP opens a `CUSIP_CHANGE` interval at `INFERRED`
confidence. It is deliberately **not** called a reverse split: the evidence
records no corporate action, and naming one would be a guess. A caller holding
split evidence records `REVERSE_SPLIT` itself. Either way `security_id` is the
same on both sides, which is the property that matters.

### Confidence is stored, because the three kinds are not interchangeable

| Confidence | Means |
| --- | --- |
| `OBSERVED` | A source recorded this pairing on these dates. The interval's endpoints are settlement dates that appear in a published file. |
| `INFERRED` | Derived by a rule stated in `domain.identity` — the issuer base, an interval closed where the next opens. Reproducible, not observed. |
| `ASSERTED` | Established from evidence outside the feeds and supplied by a caller. The only route by which `REORGANISATION` or a corroborated `REVERSE_SPLIT` can be recorded at all. |

A study wanting only intervals a file actually printed can ask for them.

## The evidence: SEC fails-to-deliver

The SEC publishes, twice a month since 2004, every security in which a delivery
failed to settle — settlement date, CUSIP, symbol, issuer description. It exists
to make short-selling failures visible and is read here for something incidental
to that purpose: **it is the only free record that pairs a CUSIP with a trading
symbol *on a date*, back to 2004, for names that have long since left the
market.**

Two archive families cover the span, and the file list is read from the SEC's own
index page rather than generated from a date pattern — the same series is served
from four different directories, and two months of 2025 sit under a path no other
month uses. A guessed URL is a silently skipped file, and a skipped file is a
security with no evidence.

### The asymmetry that governs every use of it

**A positive match is evidence. An absence is not.**

A security appears in the file only when a delivery in it failed, and before
**2008-09-16** only when the failing balance also cleared a reporting threshold.
A name absent from the entire twenty-two-year record may have traded for two
decades without one failed delivery.

This is enforced structurally, not by convention. `domain.EvidenceVerdict` has
exactly two members — `PRESENT` and `UNPROVEN` — and **there is no `ABSENT`**, so
no code path in this system can record the opposite conclusion even by accident.
`existence_evidence` additionally flags a window beginning before the threshold
was lifted as `threshold_censored`, because such an absence is uninformative for
a second, independent reason.

### Evidence is stored apart from conclusions

`ftd_identity_observations` holds one row per distinct `(cusip, symbol,
issuer_name)` triple, with the settlement window it was seen over and the count
behind it. It is a separate table from `security_identifiers` for a reason that
matters: **evidence exists for symbols that resolved to nothing**, and those are
exactly the rows an audit of what is missing is made of. An interval presupposes
an identity; evidence presupposes nothing.

The aggregate rather than every published line is a deliberate trade. Storing
every line would add tens of millions of rows carrying a failure quantity this
project has no use for, to answer a question the aggregate answers exactly as
well. What is preserved is everything needed to go back: each archive's name, URL
and **SHA-256** in `ftd_source_files`, and a digest of each triple beside the
aggregate. The lines are reproducible from the files; the files are pinned by
their digests.

## Resolving a delisted symbol

Every function takes a delisting — a symbol **with** its date — and never a bare
symbol, because a bare symbol is unanswerable. `BBBY` on 2023-05-02 and `BBBY` on
2025-09-30 name different companies.

The issuer holding a symbol on date `D` is **the latest one to have taken it up
by then** — the same rule `universe_members_as_of` already uses for membership:
*the latest record dated on or before the date decides*.

| Spans beginning by `D` | Outcome |
| --- | --- |
| One begins latest | **RESOLVED** — that span's evidence becomes the security's intervals. |
| None began by `D` | **UNRESOLVED** — the record cannot place the name, and says nothing about whether it existed. |
| Two begin on the same day | **AMBIGUOUS** — nothing is created. Choosing would be a guess, and a guessed identity is worse than an absent one because a study cannot see it. |

### The rule this replaced, and why it was wrong

Phase 9A asked instead whether a span had begun by `D` *and had stopped being
seen shortly after it*. That bounds a span's end from above and never from below,
so **any long-dead earlier occupant also qualified**. `ATVI` is the worked case:
Activision Inc held the symbol from 2004 to 2008-07-31 and Activision Blizzard
from 2008-08-08 to its 2023 merger, and both satisfied the test — so the merger
every source agrees about came back `AMBIGUOUS`. `TWTR` failed identically
against Tweeter Home Entertainment, which last failed a delivery in 2007.

Correcting it was a Phase 9B change, and it moved the numbers materially:

| | Old rule | Corrected |
| --- | --- | --- |
| Resolved | 2,410 | **3,144** |
| Ambiguous | 680 | **14** |
| Unresolved | 2,358 | **2,290** |

The correction cannot splice: a span is eligible only if it had begun by the
date, so a later occupant can never answer for an earlier one's exit, and
genuinely overlapping windows are still `AMBIGUOUS` from `classify_symbol` before
span selection is reached. The remaining 2,290 unresolved are almost entirely
symbols the fails record does not carry at all.

A symbol that resolves attaches to an existing `historical_securities` row when
one matches on **both** ticker and a last trading date inside the span — the
Phase 8I cohort carries real CIKs and is better evidence than anything derivable
here. The date half is what makes the match safe; matching on ticker alone is
precisely how two issuers become one row.

## Contamination in the live table

`companies` holds what is investable now, keyed by ticker. When a symbol is
reissued, the row that held it keeps accumulating, and every query reads two
companies as one. `companies` row 2147 is the proven case: ticker `BBBY`, the
name of an unrelated 2025 issuer, Bed Bath's collapse to $0.0743 on 2023-05-02, a
twenty-six-month hole, then a fresh price series.

### Two issuers under one symbol is not yet a finding

Three filters stand between "the CUSIP issuer changed" and "this row holds two
companies", and each removes a class of false positive that the first draft of
this scan produced:

| Filter | Ruled out | Why |
| --- | --- | --- |
| Handover gap > `HANDOVER_GAP_DAYS` (180) | 596 of 1,840 handovers | Accenture's symbol passes from issuer `G1150G` to `G1151C` across a **two-day** gap: that is Accenture Ltd re-incorporating as Accenture plc, one company throughout. Barrick's `ABX` takes a new CUSIP on renaming. Measured across the roster the shapes separate cleanly — 432 handovers complete inside a week, 1,182 take over a year — and the threshold sits in the thin trough between them, at about the 120 trading sessions the Phase 8J audit used. |
| The row holds bars on both sides | 1,137 of the remaining 1,244 | A symbol reused in 2008 says nothing about a row whose price history begins in 2019. The earlier occupant never entered that row. |
| The row's own price series breaks | 37 of the remaining 107 | **A gap in the fails record is a gap in *failures*, not in trading.** A company whose deliveries stop failing vanishes from the file while trading normally. Amalgamated's holding-company reorganisation is the shape: 868 quiet days in the file, continuous bars throughout. |

What survives is **69 of 5,766 live rows** the record actually shows stopping and
restarting under two issuers — `companies` row 2147 among them, found
independently, with an 804-day break between Bed Bath & Beyond (`075896`) and
Beyond, Inc. (`690370`).

Every filter makes the scan more conservative on purpose. A row wrongly called
clean costs a study one name it should have excluded; a row wrongly called
contaminated invites someone to take a scalpel to a company that was never two.

`detect_contaminated_companies` finds these and **writes nothing**. Not to
`companies`, not to `historical_securities`, not to `security_identifiers`.
Splitting such a row means deciding which prices, filings, snapshots and scores
belong to which issuer across every child table, and re-deriving every score built
on the mixture — a data operation with real consequences that must be separately
authorised. Merging would be worse, since it is the error itself.

What a report earns is the ability to **exclude**. A study that knows row 2147 is
contaminated can drop it; one that does not reads a fabricated twenty-six-month
drawdown as a fact about a company.

## Forward returns learn a second identity

`forward_returns` could only ever be measured against a live company, which is
survivorship bias in the *outcome* half of the record: the securities whose
outcomes matter most are the ones that left.

```sql
company_id              INTEGER NULL  REFERENCES companies(id)
historical_security_id  INTEGER NULL  REFERENCES historical_securities(id)
CHECK ((company_id IS NULL) <> (historical_security_id IS NULL))
```

Exactly one, enforced by the database. Never both — two claims about one
measurement — and never neither, which would be an orphan.

The uniqueness rule is **two partial unique indexes**, one per identity kind, and
not one constraint spanning both columns. A single constraint over a nullable
pair enforces nothing: SQL holds every NULL distinct from every other, so
`(NULL, 5, 2024-06-28, 20)` would sit beside itself indefinitely, and the
duplicate measurements this table refuses would arrive through exactly that gap.

**No row was migrated.** Every existing measurement keeps its `company_id` and
its values; `historical_security_id` is NULL on all of them. Attaching a delisted
name's outcomes needs its identity proven first, and that is a separate,
deliberate operation.

## Phase 9B: the outcome chain

Phase 9A gave a delisted security an identity. Phase 9B proves it can carry an
outcome — that a name absent from `companies` travels the whole way from
identity evidence to a measured forward return, and lands in the **same query**
as a survivor.

```
FTD evidence → historical_security_id → dated identifier → historical_prices
    → historical_corporate_actions → delisting / terminal event
    → forward_returns → terminal outcome
```

### The architecture decision: no new tables

The obvious move was a historical price store, and it turned out to be already
built. `historical_prices` and `historical_corporate_actions` have existed since
migration `0026`, keyed by `historical_security_id`; `forward_returns` learned its
second identity in `0027`; `historical_securities.terminal_value_per_share` was
already there. So:

**Phase 9B adds no migration, no table, no column, no index and no constraint.**

What it adds is the code that walks the chain: a `HistoricalPriceSource`, a
`measure_historical_forward_returns` pass, a price backfill that needs no CIK, and
a read model that resolves labels and terminal outcomes by identity. The terminal
outcome is **derived on read** rather than stored, for the same reason
`classify_delisting` is: it is a pure function of columns the tables already hold,
and a second stored copy could only disagree with them.

### The measurement is the company path with one substitution

`measure_historical_forward_returns` calls the same `domain.forward_returns`, over
the same benchmark calendar, producing the same four statuses, writing to the same
table. Only the bars come from somewhere else. That sameness is the point: a
delisted security's outcome is not a parallel dataset, it is a row.

The calendar still comes from the benchmark, and for a delisted name that matters
more than for a survivor — deriving sessions from its own bars would let a
security that stopped trading in March appear to reach its 60-session horizon on
time, when what actually happened is that the market went on without it. Reading
the benchmark is what turns that into `INTERRUPTED`.

### Fetching prices for a symbol that was later reissued

Prices come from a **ticker-keyed** feed, which is the one place the identity
model could be undone by its own plumbing: a request for `BBBY` running past Bed
Bath's era returns Beyond, Inc.'s bars and splices two companies into one series.

The protection is the window. Every fetch is bounded to `[earliest identifier
interval, last trading date + 5 days]`, so a request can only reach the era in
which *this* security held the symbol. Verified on the cohort — TWTR's series ends
2022-10-27, PXD's 2024-05-02, VMW's 2023-11-21, each exactly at its own exit.

### Terminal outcomes, matched by identity and never by price

`delistings` is keyed by ticker, so a security's exit is the delisting whose
effective date falls inside one of **its** identifier intervals. VMware is the
worked case: three rows under `VMW` — two stock-merger legs and the cash election
stating $142.50 — and only the intervals say which belong to this security.
Where several match, one stating a consideration is preferred over one that does
not.

`domain.terminal_outcome` is handed reported considerations and never a price
series, so there is no path by which a collapsing tape becomes a settlement:

| Status | Means |
| --- | --- |
| `SETTLED` | A source states the consideration. ATVI at $95.00, VMW at $142.50. |
| `EXTINGUISHED` | A source *reported* a worthless removal. Zero is the filed fact, labelled with the mechanism that filed it. |
| `UNKNOWN` | The security left and nothing establishes what a holder received. Every receivership; PXD's all-stock merger; Bed Bath, which traded to $0.0743 and is still `UNKNOWN`. |
| `ONGOING` | It had not left. |

### The cohort, end to end

Measured from a 2023-01-31 decision date on `stage9b.db`:

| Security | 20 sessions | 120 sessions | Last seen | Terminal |
| --- | --- | --- | --- | --- |
| **ATVI** | REALISED +0.3% | REALISED +21.9% | — | `SETTLED` **$95.00** ACQUIRED |
| **VMW** | REALISED −10.4% | REALISED +33.2% | — | `SETTLED` **$142.50** ACQUIRED |
| **PXD** | REALISED −10.2% | REALISED −0.5% | — | `UNKNOWN` ACQUIRED (all-stock; no consideration stated) |
| **SIVB** | REALISED −8.9% | **INTERRUPTED** | 2023-03-09 | `UNKNOWN` RECEIVERSHIP |
| **FRC** | REALISED −14.2% | **INTERRUPTED** | 2023-04-28 | `UNKNOWN` RECEIVERSHIP |
| **BBBY** | REALISED −44.2% | **INTERRUPTED** | 2023-05-02 | `UNKNOWN` |
| **TWTR** | NO_ENTRY | NO_ENTRY | — | `SETTLED` $54.20 ACQUIRED |

Four of these are the tests that matter. SIVB and FRC are `INTERRUPTED` with no
number and `UNKNOWN` terminal values, because no feed files a receivership and
inventing −100% would libel two banks whose equity value nobody published. BBBY
traded to $0.0743 and is still `UNKNOWN`. And TWTR is `NO_ENTRY` at a decision
date after it left — it has a terminal outcome and no entry, which is the correct
description of a security you could not have bought that day.

### Where a delisted security is still not a survivor

The score. `reconstruct_historical_score` needs EDGAR fundamentals, which need a
CIK, and the free record establishes one for a minority of these names. A
CIK-unresolved security is `INSUFFICIENT_DATA` and is **not** discarded:
price availability and score availability are separate concepts, and collapsing
them would drop exactly the names a survivorship-aware study needs most.
`CompounderScore V1.2` is unchanged and was not widened to reach them.

### The measured state of the outcome chain

Produced on `stage9b.db`, a throwaway copy of `stage9.db`. The primary database
was not opened; its SHA-256 is unchanged.

| | |
| --- | --- |
| **Identity** | |
| Historical securities | **3,150** |
| With a dated identity | **3,147** (99.9%) |
| CIK-resolved / unresolved | **402** / **2,748** |
| With prices | **2,569** (81.6%) |
| With corporate actions | **189** |
| **Outcomes** | |
| Historical-security measurements | **655,095** over 51 month-end decision dates, 2022-01-31 → 2026-03-31 |
| Company-backed measurements | **30,669,800**, unchanged |
| REALISED / INTERRUPTED / NO_ENTRY / UNPRICED / TRUNCATED | 235,681 / 18,937 / 397,235 / 3,235 / 7 |
| Horizons | 131,019 rows at each of 1, 5, 20, 60, 120 sessions |
| **Terminal outcomes** (of the 2,569 with outcomes) | |
| `SETTLED` — a consideration is stated | **1,267** |
| `EXTINGUISHED` — a reported worthless removal | **147** |
| `UNKNOWN` — nothing establishes a value | **1,155** |

### What changed for survivorship

Phase 8J measured the damage: **3,801 securities left the market** over
2022-01..2026-08 and the record held **22** of those exits as observable events —
0.6%, because `companies` contains only what is investable today.

Against the 3,806 exits the registry now records in that window, **2,207 have
measurable outcomes**: 58.0%, up from 0.6%. The mixed query on 2023-01-31 returns
5,765 companies and 2,569 delisted securities from a single
`WHERE observation_date = ?`.

### Invariants, checked rather than assumed

Across all **31,324,895** rows: **0** with both identities, **0** with neither,
**0** carrying a number without `REALISED`, **0** marked `REALISED` without both
prices, **0** with an entry on or before its decision date. The 30,669,800
company-backed rows hash to `ccbf236d…b94dd0` before and after every Phase 9B
operation — byte-identical, and none migrated.

### What is still missing, and it is not the architecture

**Historical universe membership is structurally zero.** `universe_events` is
keyed by `company_id`, so no universe pass has ever witnessed a delisted
security and none can until membership is recorded against an identity rather
than against `companies`. A study can now measure what a delisted name *did*; it
still cannot reconstruct *which securities were investable* on a past date. The
audit reports the two separately, and price coverage is never allowed to stand in
for membership — 2,230 securities were trading on 2023-01-31, and that is
presence, not eligibility.

Three further gaps are data rather than design: 581 of 2,745 backfilled securities
returned no bars at all (the free feed begins ~2020-07); 2,290 delisted symbols
carry no fails-to-deliver evidence and so resolve to no identity; and 45% of
covered securities have no establishable terminal value, receiverships and
all-stock mergers among them.

## Phase 9C: historical universe membership

Phase 9B proved a delisted security can carry an outcome. This answers the
question a backtest asks *before* an outcome: **which securities were there to be
chosen from on that date?**

### The finding that shapes the whole design

The universe is defined by `evaluate_eligibility`, and its inputs are not
uniformly dated:

| Input | Dated in this system? |
| --- | --- |
| price, market cap, liquidity, staleness | **yes** — `pit_feature_snapshots` (1,168 dates × 5,765 companies), the observation record (5.78M rows, 2009→2026) |
| exchange | **no** — every stored value carries 2026-08-26, the day capture first ran; `security_identifiers.venue` is NULL on all 3,147 rows; the fails file carries no venue |
| security type (name) | **no** |
| sector | **no**, and absent for 5,146 of 5,769 companies |
| active flag | **no** |

So a *complete* membership reconstruction is impossible from the free record. No
code changes that. What the design does instead is say so, per row.

### Three concepts, kept apart

| | Question | Where it lives |
| --- | --- | --- |
| **Identity** | which security is this? | `security_identifiers` (9A) |
| **Membership** | was it in the universe over this span? | `universe_membership` (9C) — a slow-moving listing fact, stored as dated intervals |
| **Eligibility** | did it clear the thresholds that day? | `evaluate_eligibility` against PIT metrics — unchanged, not re-implemented |

### The state and confidence model

`universe_membership` carries dual identity — `company_id` **or**
`historical_security_id`, exactly one — so `members_as_of(D)` returns delisted
names without a flag, the same shape `forward_returns` has had since 0027.

| State | Confidence | Means |
| --- | --- | --- |
| `NON_MEMBER` | `OBSERVED` | A dated exit removed it, or every dated trace of it begins after the date and begins late enough that the record's own reach cannot explain the silence. |
| `MEMBER` | `INFERRED` | Dated evidence places it in the market and its listing attributes qualify it — **but those attributes are undated**, so the verdict names what it carried in `inferred_inputs`. |
| `UNKNOWN` | — | The record cannot place it. Reported, never dropped. |

**No `MEMBER` verdict this system can currently produce is `OBSERVED`**, and the
model is built so that becoming observed requires a dated venue source rather
than a change of opinion. `ListingAttributes.fully_dated` is the single gate, and
it is False for every security today.

### Price presence is never membership

Rule B is enforced structurally. Price and settlement evidence *bound* an
interval; the membership claim always rests on the listing attributes. A security
with a full price series and no recorded venue is `UNKNOWN` — proven directly by
`TestPricePresenceIsNotMembership`.

### Measured on `stage9b.db`

20,412 intervals over 5,769 companies and 3,150 historical securities: 6,049
`MEMBER/INFERRED`, 11,496 `NON_MEMBER/OBSERVED`, 111 `NON_MEMBER/INFERRED`,
2,756 `UNKNOWN`. Zero securities have no dated evidence at all.

Across 51 month-end decision dates, 2022-01-31 → 2026-03-31:

| | min | median | max |
| --- | --- | --- | --- |
| Members | 4,776 | 4,953 | 5,482 |
| **of which delisted** | **0** | **199** | **368** |
| Unknown | 361 | 1,093 | 1,547 |

The delisted-member count falls monotonically to zero at the present date, which
is the shape a correct reconstruction must have: the further back a study looks,
the more of its universe has since died. On 2022-01-31 the universe holds **368
securities that no longer exist in `companies`** — securities a roster-based
backtest would silently omit.

### A defect this audit caught

The first reconstruction reported zero unknown securities per date, and
4,776 + 2,613 did not equal 8,919. A security whose covering interval was
`UNKNOWN` matched neither the member nor the non-member branch and was not
counted as unknown either, so **1,530 of 8,919 securities vanished from the
audit** — a universe hiding its own uncertainty, which is precisely the failure
this phase exists to prevent. Fixed, and held by
`test_every_security_lands_in_exactly_one_group`.

### Known limitations

| Limitation | Quantified |
| --- | --- |
| **No `OBSERVED` membership is achievable.** Exchange, security type and sector are undated everywhere. | 0 of 6,049 member intervals are observed |
| **A contaminated live row carries one continuous membership.** Where a `companies` row holds two issuers' history, its membership interval spans both — the layer separates the two *identities* correctly but cannot un-mix a row that is itself two companies. Repairing it means splitting prices, filings and scores, which is a separately-authorised data operation. | 1,486 live rows carry more than one CUSIP issuer under their ticker; 1,458 hold a single open `MEMBER` interval |
| **Historical securities with no recorded venue are `UNKNOWN`.** | 2,759 of 3,150 carry no exchange |
| **Ticker changes are not represented in the FTD-derived identity record.** | 0 securities carry more than one ticker; 268 carry more than one CUSIP |

## The measured state of the record

Produced on `stage9.db`, a throwaway copy. The primary database was not opened;
its SHA-256 is unchanged. Every figure below is a query against the tables, which
is why `identity audit` re-derives the resolution verdicts read-only rather than
reporting what some run once printed.

| | |
| --- | --- |
| **Fails-to-deliver evidence** | |
| Archives processed | **433**, zero failed |
| Settlement range | **2004-03-22 → 2026-08-14** |
| Raw published rows | **27,977,878** |
| Distinct identity triples | **95,773** |
| Unique CUSIPs / tickers / issuer names | **69,724** / **71,692** / **69,944** |
| CUSIPs failing their own check digit | **0** |
| **Delisted-symbol resolution** | |
| Symbols considered | **5,448** |
| Resolved to a `security_id` | **2,410** |
| Unresolved | **2,358** |
| Ambiguous | **680** |
| Ticker-reuse cases | **836** |
| CUSIP-change lineage cases | **183** |
| Reverse-split lineage cases | **0** — only ever `ASSERTED`, and nothing has asserted one |
| **Security identity** | |
| Historical securities | **2,524** (402 from Phase 8I + 2,122 minted here) |
| With an FTD-derived identity | **2,410** — 2,122 new rows, 288 attached to Phase 8I rows |
| CIK-resolved / CIK-unresolved | **402** / **2,122** |
| Identifier intervals | **2,633** |
| **Live company contamination** | |
| Active company rows | **5,769** |
| Contaminated | **69** |
| **Forward returns** | |
| Total measurements | **30,669,800** |
| Company-backed | **30,669,800** |
| Historical-security-backed | **0** |

The last row is the one to check. Phase 9A gives the schema the shape to hold a
delisted security's outcomes and migrates **none** into it.

The 2,358 unresolved symbols are the honest cost of the source: a security whose
deliveries never failed is not in the file, and `UNRESOLVED` is what the record
supports. It is not, and cannot be made into, a claim that those securities did
not exist.

## What this phase deliberately did not do

- **No `cusip` column on `companies`.** The task permitted one "only if genuinely
  useful for current-state retrieval". It is not: there is no authoritative free
  source for a live company's CUSIP, and deriving one from the fails record means
  joining on the ticker — the exact contamination this layer exists to detect.
  Current-company identity and historical-security identity stay separate spaces.
- **No contaminated row was merged or split.** Detected and reported only.
- **No forward return was migrated** onto a historical security.
- **No score, signal, threshold or ranking rule moved.** `COMPOUNDER_V1_2` and
  Market Signals V1 are byte-unchanged, and the parity tests that prove it are
  untouched and green.

## Known limitations

| Limitation | Why it is not fixed here |
| --- | --- |
| A security whose deliveries never failed has no evidence at all, so its identity is unrecoverable from this source. | A property of the source, not of the code. It produces `UNRESOLVED`, never a claim of non-existence. |
| Pre-2008-09-16 evidence is threshold-censored — only failures above the reporting minimum were published. | Recorded as `threshold_censored` on the verdict rather than corrected. |
| The fails file records no corporate action, so a CUSIP change within one issuer cannot be attributed to a reverse split, a re-incorporation or a class re-designation. | Recorded as `CUSIP_CHANGE` at `INFERRED` confidence. `REVERSE_SPLIT` is only ever `ASSERTED` from evidence outside the feed. |
| `venue` is NULL on every FTD-derived interval. | The file carries no exchange. NULL is "not established", never a default. |
| A symbol whose two occupants' evidence windows overlap resolves to neither. | The record genuinely does not separate them. Reported as `AMBIGUOUS`. |

## Regression coverage

| Test | Protects |
| --- | --- |
| `packages/domain/tests/test_identity.py::TestATickerChangePreservesTheSecurity` | requirement 1 — `FB`→`META` is one security |
| `packages/domain/tests/test_identity.py::TestATickerReuseIsADifferentSecurity` | requirements 2 and 5 — `BBBY`'s two occupants cannot be spliced |
| `packages/domain/tests/test_identity.py::TestAReverseSplitPreservesTheSecurity` | requirements 3 and 4 — new CUSIP, same id; the retired CUSIP stops answering |
| `packages/domain/tests/test_identity.py::TestEvidenceAndItsAbsence` | requirements 6, 7 and 8 — no CIK needed, absence proves nothing, disagreement is a status |
| `packages/domain/tests/test_identity.py::TestIntervalsAreNeverInterpolated` | every interval endpoint is a date the record carries |
| `tests/integration/test_security_identity.py::TestHistoricalSecuritiesStayOutOfTheLiveUniverse` | requirement 9 — resolution writes no `companies` row |
| `tests/integration/test_security_identity.py::TestForwardReturnsCarryExactlyOneIdentity` | requirement 11 — the check constraint, both ways |
| `tests/integration/test_security_identity.py::TestDuplicateMeasurementsAreRefusedPerIdentityKind` | requirement 12 — refused independently per identity kind |
| `tests/integration/test_security_identity.py::TestExistingMeasurementsAreUntouched` | requirement 10 — values unchanged, historical count zero |
| `tests/integration/test_security_identity.py::TestContaminationIsReportedNotRepaired` | the scan writes nothing |
| `tests/integration/test_migrations.py::test_the_identity_migration_preserves_a_populated_historical_security` | the `historical_securities` rebuild does not cascade-delete its three children |
| `packages/domain/tests/test_scoring.py`, `tests/unit/test_ranking_state_parity.py` | requirement 13 — V1.2 parity, untouched |

## Commands

```
identity ingest-ftd      # read the published fails-to-deliver history
identity resolve         # resolve delisted symbols to stable security ids
identity contamination   # report live rows two issuers have held
identity audit           # the state of the layer, every figure a query
```

`ingest-ftd` caches archives under `FTD_CACHE_DIR` (about 600 MB across 433
files) and is resumable and idempotent: an archive whose SHA-256 is already
recorded is skipped, so re-running widens no window and inflates no count.
