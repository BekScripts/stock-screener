---
type: reference
title: Project state
---

# Project state

The single checkpoint of what Compounder Radar *is* right now: which phase is
current, which decisions are frozen, what the schema and score definitions are,
and what must not be changed without being asked.

This page is a **state record, not a plan**. It describes what exists. Intent
lives in the phase briefs; rationale lives in the ADRs; procedure lives in the
skills.

## How this page is maintained

Update it **in the same commit** as any of the following. A change to one of
these that leaves this page stale is an incomplete change.

| Trigger | What to update |
| --- | --- |
| A phase completes or is frozen | Current phase, Frozen decisions, Next phase |
| A new `score_version` | Score definitions, Regression requirements |
| A new migration | Database migrations, Data contracts |
| A new frozen rule or ADR | Frozen decisions, Things explicitly not to change |
| A limitation found and accepted | Known limitations |
| A validated rollout or measurement | Completed experiments |
| Data promoted to the primary database | Completed experiments, Latest checkpoint |
| Any merge to `main` | Latest checkpoint |

Everything asserted here must be checkable against the repository. No number
appears without the run or the test that produced it.

---

## Current phase

**B2.5 — relocate and harden the local control plane** is the current
checkpoint on `feat/phase-b2-storage-abstraction`, on top of B2.2–B2.4's storage
abstraction. The artifact lifecycle runs through `EvidenceStorage`; the control
plane — manifest, checkpoints, plans, derived index — has a root of its own,
`SEC_CONTROL_DIR`, so losing the artifact volume no longer loses the record of
what it held. See [Storage layout](storage-layout.md#b25-control-plane) for the
two planes, the resolution rules and the failure-domain tests, and
[the B2.2–B2.4 section](storage-layout.md#b22b24-storage-contract-checkpoint)
for exception, parser, counter and boundary policies. The pre-refactor rollback
point is `pre-b2-ssd-state` (`13d586b10dfc077325b2729602a30b74d01a8af0`).

**Phases 1 through 7 are complete. All of them are frozen.** The MVP finished at
Phase 5; Phases 6 and 7 are layers over it, not revisions of it.

| Phase | Scope | State |
| --- | --- | --- |
| 1 | Ingestion, eligibility screen, universe | complete, frozen |
| 2 | Metric engine, CompounderScore, rankings, CLI, read-only API | complete, frozen |
| 3 | AI research — contract, provider, brief, pipeline | complete, frozen |
| 4 | Dashboard and watchlist | complete, frozen |
| 5 | Run control — jobs, ticker search, CSV export | complete, frozen |
| 6 | On-demand deep research (6A–6E: contract, prepare, collect, generate, dashboard) | complete, frozen |
| 7 | International coverage (7B–7F: currency, IFRS, cadence, FX, rollout) | complete, frozen |
| 8A | Market opportunity audit | complete, frozen |
| 8B | Point-in-time capture | complete, under review |
| 8C | Market signals | complete, under review |
| 8D | Point-in-time metric engine | complete, accepted |
| 8E | Data readiness — observation population and blocker audit | complete |
| 8F | Availability semantics and price depth | complete, under review |
| 8G | Point-in-time research outcomes | **complete, under review** |
| 8H | Backtest-readiness harness — survivorship, universe-as-of, score reconstruction | **complete, under review** |
| 8I | Historical securities — delisted names reconstructable from free EDGAR + Alpaca | **complete, under review** |
| 8J | Research dataset — backfilled signals and outcomes, coverage audit, identity guard | **complete, under review** |
| 9A | Identity foundation — stable `security_id`, dated identifiers, SEC fails-to-deliver evidence | **complete, under review** |
| 9B | Historical-security outcome integration — delisted names in the same outcome query | **complete, under review** |
| 9C | Historical point-in-time universe membership — dated intervals, three states | **complete, under review** |

**Phases 8B, 8C, 8F, 8G, 8H, 8I and 8J are implemented and awaiting review.**

Phase 8A was an audit, not an implementation: it established that the ranking is
not "dead" (Top 100 median ATR 4.01% against 3.36% for the universe) and that the
real blockers are measurement, not scoring. Its findings are in the Phase 8A
report; the ones that became requirements are recorded below.

Phase 8C adds the descriptive market-state layer over 8B's feature record:
peer percentiles, a multi-label setup, and a tri-state tradability verdict. It
produces no score, no ranking and no blend — Phase 8A falsified all three.

Phase 8D closes the last structural point-in-time gap: fundamental metrics can
now be built from what had actually been filed by a given date, reusing the
frozen normalisation and the frozen arithmetic unchanged. It adds no table and no
migration.

Phase 8G adds the outcome half: a market capitalisation correct on a past date,
forward returns at five horizons, and a delisting registry. It also produced the
measurement that now governs the phase — **across 4,599 companies and the 120
sessions after 2024-01-02, not one security stopped trading.** A universe that
size loses roughly a hundred names to acquisitions and failures over six months.
The zero is a property of `companies`, which holds what is investable today and
therefore cannot contain anything that stopped being so.

Phase 8H is the readiness harness that folds the point-in-time layers into one
audit. `stock_screener.backtest` answers, for a security on a date, the seven
questions a survivorship-aware study needs settled — universe membership,
filed fundamentals, point-in-time market cap, CompounderScore, signals, forward
outcomes and delisting fate — and names every gap it cannot fill. Its findings:
the CompounderScore is fully reconstructable from point-in-time inputs because
`score_company` has no cross-sectional term; delisting fate is classified as
`ACQUIRED`, `BANKRUPT`, `DELISTED`, `RECEIVERSHIP` or `UNKNOWN`
(`domain.DelistingReason`); and market-signal history is backfillable from stored
prices with no provider and no look-ahead. It adds no table, no migration and no
score version. See [Backtest readiness](backtest-readiness.md).
Phase 8I closes the last of those gaps: a delisted security is now reconstructable
from the free record that outlives its listing. `historical_securities` (migration
`0026`) is keyed by **CIK** — stable across a ticker change, distinct under a
reused ticker, separate for an acquiree and its acquirer — and is deliberately not
a `companies` row, so a delisted name is addressable without entering the
investable set. Its three child tables mirror the point-in-time company record
(observations selected on `filed_date`, daily prices, corporate actions), and
`stock_screener.backtest.reconstruct_historical_score` reads them through the same
frozen `build_company_metrics` and `score_company` the live path uses. A
representative cohort validates it end to end: ATVI reconstructs to a $65.5bn
market cap, a 53.9 score and a $95.00 terminal value; a delisted bank is declined
as `UNSUPPORTED_SECTOR`; and every gap the free data cannot fill — an FDIC-only
bank's fundamentals, a pre-2009 filer, pre-2021 prices, a receivership's terminal
value — is kept `UNKNOWN`, never guessed.
Phase 8J turns the mechanisms into a dataset. `stock_screener.backtest.backfill`
runs the *existing* daily passes over sessions that have already happened —
`capture_day`, `derive_market_signals` and `measure_forward_returns`, unchanged —
idempotently, resumably and one transaction per date, with a price cache that is
proven equivalent to the repository read field by field. `backtest.coverage`
audits the result as a funnel from "in the record" to "studiable without
inventing anything", and reports universe membership as `reconstruct_universe`
returns it rather than substituting a proxy for it.

It also fixed an identity defect the audit exposed. Delistings join to companies
by ticker, and a ticker is not an identity: `CZR`'s 2020 exit belongs to the
Caesars that merged into Eldorado, not to the `CZR` printing bars today.
`reconstruct_universe` was removing 127 live companies from every universe on
that basis, and `security_readiness` was reporting them as acquired. The price
record now adjudicates — a company still trading after an exit did not take it.
No score, signal or version moved; this is a data-quality fix in the audit layer.

**Measured on `stage9.db`, a throwaway copy of `stage8e.db`.** Forward returns:
1,064 daily decision dates over 2022-01-03..2026-03-31, **30,669,800 measurements**
across 5,765 securities, zero failed and zero missing. Market signals: 56
month-end sessions over 2022-01-31..2026-08-28, **282,998 signal rows**, zero
failed and zero missing; the full daily pass over 1,168 sessions runs at ~10s a
session. Historical securities: **402 ingested**, 400 with fundamentals, 402 with
prices, 291 with a stated terminal value, zero failures. The monthly coverage
audit reports **51 consecutive month-ends with a median of 1,698 fully usable
securities** (minimum 1,533, maximum 1,793), 2022-01-31 through 2026-03-31.

Look-ahead was checked rather than assumed: across 30.67M measurements, zero rows
have an entry on or before the decision date, zero have an exit on or before the
entry, zero carry a number without `REALISED`, and a 200,000-row sample confirms
every horizon spans exactly N benchmark sessions.

The phase's own finding is the one that governs it: **the historical universe
cannot be reconstructed at all.** Every company's membership record begins on
2026-08-26, the day capture first ran, so `universe_members_as_of` returns
*undetermined* — not baseline — for every earlier date, and the historical
universe is empty on every date in the study window. That is not a backfillable
gap. `companies` holds today's roster, so writing membership events for past
dates would assert that today's survivors were present then, which is the exact
survivorship bias the point-in-time record exists to remove.

Two further limitations were measured and are accepted rather than fixed. **The
dataset is censored, not survivorship-aware.** 3,801 securities left the market
over 2022-01..2026-08; the record holds **22** of those exits as observable
events (0.6%), because `companies` contains only what is investable today. The
delisted names that would close the gap have recoverable prices — the free
provider still serves them by ticker, verified on ATRS, SWIR, SIVB and BBBY — but
nowhere to be stored: `price_history` requires a `companies` row and
`historical_securities.cik` is `NOT NULL`, while the free record establishes a
CIK for only **12.5%** of delisted tickers (Form 25 full-text search, which is
additionally skewed to Nasdaq because only its exhibits embed the symbol).

**A third identity problem is present and not fixable in this schema.** 638 of
5,765 companies (11.1%) have a price series with a discontinuity longer than 120
sessions, and only 34 of those have a delisting recorded inside the span. The
proven case is `companies` row 2147: ticker `BBBY`, named *Neighborhood
Intelligence, Inc.*, carrying Bed Bath & Beyond's collapse to $0.0743 on
2023-05-02, a 26-month hole, then an unrelated company from 2025-07-14. One row,
two issuers, and no CIK on `companies` to separate them.

Strategy research is **not** started and is not to be started without being
asked. The binding constraint has moved but not lifted: the mechanisms are now
built and tested, but two of their inputs only accumulate with time — universe
membership is baseline-only until witnessed transitions land, and the *recorded*
score and signal series begin the day capture first ran. Reconstruction reaches
back over the observation record; the witnessed history does not yet exist.

### Data state

**The observation record is populated on staging.** A full-universe run on
2026-08-30 ingested **5,711,643 observations across 5,497 of 5,769 companies
(95.3%)**, with filing dates spanning 2009-04-15 to 2026-08-28 and zero undated.
2,632 companies carry ten years or more of point-in-time filing history. The
primary database did not receive it — as with Phase 7F, code and data promotion
are separate operations.

**Correction, found during Phase 8A:** the primary database *does* contain the
foreign cohort. GFI, ERO, DLO and ARIS all rank inside the current Top 20, and a
full `COMPOUNDER_V1_2` run is stored for 2026-08-26. The paragraph below was
written from the Phase 7 documents and was already stale; the database is the
authority.

~~**The primary database has not received the Phase 7F foreign cohort.**~~
`compounder_radar.db` is byte-identical to its pre-rollout state — the entire
Phase 7 validation ran against copies, verified by row count and checksum before
and after each stage.

Code completion and data promotion are separate operations. Promoting the cohort
is a deliberate, separate step: apply the migrations, then run the ordinary
passes — `update-eligibility-volume`, `enrich`, `update-fundamentals`, `score` —
against the real database.

**The outcome layer is populated on staging only.** `stage8e.db` carries 82,321
corporate actions across 5,769 companies, 5,796 delistings over 2020–2026, and
28,825 forward-return measurements from 2024-01-02. Market features and signals
were backfilled for that date to prove they can be: 4,651 of each, from price
data alone. `compounder_radar.db` has received none of it.

---

## Frozen decisions

### B2 evidence storage boundary

- Artifacts under `filings/`, `submissions/` and `fulltext/` use `EvidenceStorage`.
  Manifest JSONL and all other control-plane files remain local in B2.4.
- Definite absence is distinct from unavailable storage. Parser I/O failures
  now propagate intentionally, protecting PIT evidence from silent omission.
- Unsafe manifest keys are path violations only, never normal missing artifacts.
  Subset audits list touched artifact prefixes against the whole trusted set.
- **ETag is never evidence SHA-256**, including for any future backend.
- No object-store backend or evidence migration is part of this checkpoint.

### B2.5 control plane

- **The control plane has its own root.** `SEC_CONTROL_DIR` names where the
  manifest, checkpoints, plans, the reserved migration ledger and the derived
  index live; `EvidenceControlRoot` is the only way to reach them and it holds
  paths, never artifacts. Unset means the legacy colocated layout beneath
  `SEC_EVIDENCE_DIR`, which every command reports as a shared failure domain.
  The fallback is transitional.
- **Neither root is ever created.** An unreachable control root raises
  `ControlPlaneUnavailableError`; an unreachable artifact root raises
  `StorageUnavailableError`. Nothing conjures a directory on the boot disk to
  stand in for a volume that is not mounted.
- **Unwritten is not gone.** A manifest or checkpoint whose directory is present
  but which has not been written reads as empty or unattempted; one whose
  directory is missing raises. A vanished control plane never presents as a
  project that has done nothing.
- **The manifest records which artifact, never where.** `Entry.path` is an
  artifact-plane key; the same line is written whichever backend stored the
  bytes, and no control-plane operation lists or reads the artifact namespace.
- **Capacity is a local-backend question.** `EvidenceStorage` carries no
  free-space operation; `LocalEvidenceStorage.free_bytes()` and the
  `LocalCapacity` protocol do, and `preflight_corpus` asks only a backend that
  can answer. The corpus reconciliation gate runs against every backend.
- **`sec_evidence_index.db` is rebuildable, not evidence.** The manifest plus
  the artifact corpus is the source of truth; the index and its fingerprint are
  a local cache under `derived/` and are never mirrored.

These are load-bearing. Each is enforced by structure or by a test, not by
convention, and none may be relaxed without an explicit instruction.

### Cross-cutting

- **Missing is not zero.** A metric the data cannot support is `None` all the way
  through — model, column, CSV cell, JSON. `0.0` means the company reported zero.
  In scoring, a missing metric earns neither zero points nor full marks; its
  weight is carried by the other metrics in its own component, never by another
  component. (ADR-0007)
- **The score records its own rules.** Every snapshot carries a `score_version`
  and no comparison crosses versions. A curve, weight, cap, coverage minimum or
  ranking-admission rule means a *new* version — never an edit to an existing
  one — and `docs/reference/compounder-score.md` changes in the same commit.
  (ADR-0006)
- **A data-quality fix is not a policy change.** The test is one question: *does
  any company's number change?* Recognising a company the existing policy already
  excluded, or correcting a misread input, is not a version bump. Prove it with a
  test rather than asserting it.
- **Historical snapshots are never rewritten.** A company excluded today keeps
  the rows it earned under the rules of the day it was scored.
- **Not every company gets a number.** A bank, an ineligible security and a
  company with two quarters of history each get a stored row carrying the status
  that says why.
- **No metered provider gates the market.** The broad scan runs on Alpaca and
  EDGAR alone. FMP is optional enrichment. A `PRELIMINARY` ranking is a valid
  ranking. A provider failure or `429` must never prevent scanning, scoring or
  ranking. (ADR-0008)
- **The frontend calculates nothing.** Every score, subscore, metric and claim on
  a screen came out of the database through `stock_screener.api`.

### Phase 3 — research

- A brief is bounded by the `score_date` of the snapshot it explains and prefers
  the figures the score itself recorded.
- Nothing reaches the database without passing validation. The order is
  brief → draft → `validate_report` → persist; `save_report` takes a
  `ResearchReport`, which only validation builds.
- A provider failure is a `FAILED` report, never an exception that ends a run.
- **`D.` proves a filing exists; `X.` quotes what it says.** An `EXTRACTED` claim
  must cite an `X.` excerpt, and a number found in filing text supports only a
  claim citing the excerpt it appears in — never pooled across a brief.

### Phase 5 — jobs

- **A job is an existing command, never new pipeline code.** `JOB_KINDS` maps a
  key to a CLI argument list and is the entire allowlist. A caller picks a key,
  never an argument.
- **A job carries no result.** It answers "is it running, did it work" and
  nothing else.
- **One pipeline run at a time, and research beside it.** Pipeline kinds are
  mutually exclusive; `research` is guarded per ticker.
- **The job endpoints spend money and have no authentication.** They are only
  reasonable because the API binds localhost. `JOBS_ENABLED=false` is the off
  switch, and binding the API elsewhere without one is a mistake.

### Phase 6 — deep research

- **Web evidence never changes the CompounderScore.** `research.EvidenceKind` has
  no `W.` member; score-explaining sections accept `DETERMINISTIC` claims only;
  `DeepResearchReport` has no score field. All three are load-bearing.
- **Phase 3 is frozen and reused, never edited.** `deep-research` imports Phase 3
  types outright. Never add a member to a Phase 3 enum, never subclass a Phase 3
  class. `ResearchReport` and `DeepResearchReport` are unrelated types in
  different tables.
- **Deep reports append; they never overwrite.** `deep_research_reports` has no
  unique constraint, because a deep report is a dated investigation.
- **A deep report issues no instructions.** No BUY/SELL/HOLD, no price target, no
  position sizing — absent from the contract structurally.
- **A stage is an existing pass, never new pipeline code.** No second
  fundamentals engine, no deep-research metric, no second score.
- **Network first, assembly second.** Everything that fetches lives in
  `preparation`; `brief` only reads.
- **An unrecognised domain is rejected, not demoted.** The allowlist in
  `sources.py` is the whole policy and its default is refusal.
- **`X.` stays authoritative for filings.** A web copy of a filing the brief
  already quotes is rejected; commentary about one is not.
- **Collection is optional and never fatal.** `external=()` is a complete brief.
- **Nothing unvalidated is stored or served.**
- **An empty section says which kind of empty it is.** `NO_EVIDENCE` means the
  brief had nothing; `NO_VALID_CLAIMS` means nothing survived validation.

### Phase 8B — point-in-time capture

- **The operational tables stay mutable; the record does not.** `score_snapshots`
  upserts and `financial_snapshots` overwrites, because a screener must be able to
  re-score and to accept a restatement. The `pit_*` tables and
  `fundamental_observations` sit beside them and are never updated. Both layers
  are correct; conflating them is what makes a backtest dishonest.
- **A day is wholly captured or wholly absent.** The run row reaches `COMPLETE`
  in the same transaction as the snapshots. A failure rolls everything back and
  records a `FAILED` run carrying no rows.
- **`filed_date` is the availability date; `period_end` never is.** Point-in-time
  selection compares `filed_date <= as_of`. An observation with no filing date is
  excluded from selection rather than falling back to its period end.
- **A restatement appends.** Two filings of one period are two rows. Idempotency
  comes from a content fingerprint, not from a unique constraint over nullable
  columns, which SQL would let duplicate.
- **`BASELINE` is not an event.** A state seen for the first time is dated the day
  it was seen and asserts nothing about when it began. Only `OBSERVED` rows are
  transitions the system watched. No historical membership is ever invented.
- **A corporate action is never inferred from a price movement.** An unadjusted
  split and a genuine halving are the same bar. The table is created empty and
  stays empty until a source is wired to it.
- **Windows are strict.** A feature named `_60` used exactly 60 sessions or is
  `None`. A window that quietly shortens itself makes two rows of a time series
  incomparable while looking identical.
- **Features are structured values, never a composite.** No OpportunityScore, no
  ranking, no signal — Phase 8A showed the dimensions are anti-correlated and a
  scalar over them means nothing.

### Phase 8C — market signals

- **No composite, ever.** The dimensions are anti-correlated (volatility against
  distance-from-high, Spearman -0.58), so an averaged 0-100 score ranks highest
  whatever is mediocre at everything. There is no score column in
  `pit_signal_snapshots` and there must never be one.
- **No blend with CompounderScore.** A 50/50 percentile blend retained one name
  of the fundamental top twenty. The layers stay separate and a future strategy
  decides how to weigh them.
- **Tradability is three-valued.** `UNVERIFIED` is never collapsed into either
  neighbour. Missing data is its own answer.
- **Liquidity reads in one direction only.** Stored volume is a single venue and
  a strict subset of the tape, so clearing a floor proves a minimum and failing
  it proves nothing. No IEX-to-consolidated ratio is ever assumed.
- **Structural activity outranks volatility.** Flat-bar share, zero-move share
  and closing-band width are checked before anything else, because HBB carried a
  3.91% average true range while printing an unchanged bar on 59.9% of sessions.
- **Every threshold is provisional.** None is fitted to returns, because one day
  of history cannot support calibration. They live in a parameter object so a
  future version calibrates without touching the rules.
- **Signals read the feature record and nothing else.** Not `score_snapshots`,
  not `financial_snapshots`, not `companies` — that is what makes a past
  signal unable to drift.

### Phase 8D — point-in-time metrics

- **The temporal bound has exactly one home.** `available_as_of` selects
  observations by `filed_date`; everything downstream is indifferent to when it
  is called. `build_company_metrics` was already a pure function over injected
  periods, so no arithmetic changed.
- **The provider's normalisation is reused, never reimplemented.**
  `periods_from_facts_document` is shared byte for byte by the live path and the
  point-in-time path. A second normaliser would drift, and drift here would look
  like history rather than like a bug.
- **No new metric version.** The definitions are unchanged; only the source and
  its bound differ. Minting a version to record that nothing moved would devalue
  the mechanism `score_version` depends on. See ADR-0012.
- **Nothing is persisted.** A metric is a function of one company's own facts and
  can be rebuilt exactly from the immutable observation record, so a second copy
  would earn nothing.
- **`period_end` is never a proxy for availability**, at any level, with no
  fallback. An observation without a filing date is stored, counted, and never
  selected.

### Phase 8F — availability semantics

- **`as_of` means the close of that trading day**, 16:00 America/New_York, and
  the close follows daylight saving. Stated once in
  `stock_screener.pit.decision_time` rather than implied by a comparison
  operator.
- **Availability is decided by an instant, never by a date.** Measured on the
  full 5.78M-observation record: 61.0% of observations come from filings
  accepted after the close, so `filed_date <= as_of` reveals 3.5M of them early;
  39.0% were public before it, so `filed_date < as_of` would withhold 2.3M
  unnecessarily. Neither operator is a correct test — which is why the fix was
  a new table and not a changed character.
- **Where the instant is unknown, the filing waits.** The fallback requires
  `filed_date < as_of`. It can delay a filing by a day and can never reveal one
  early.
- **Raising the price window does not backfill.** The market pass resumes from
  each company's last stored session, so `update-market --since` is the only way
  to deepen existing history.
- **Feeds are never mixed in one series.** IEX and SIP disagree by 36x on volume
  and differ on the close, and `price_history` records no provenance, so a
  partial SIP backfill would create an invisible discontinuity.

### Phase 8G — research outcomes

**Migrations `0024` (`forward_returns`) and `0025` (`delistings`).** Both
additive, neither uses `batch_alter_table`.

- **A market capitalisation is anchored on the share count's date, not the
  valuation's.** AAPL split four-for-one on 2020-08-31 and did not file again
  until 2020-10-30; for those two months the newest available count is pre-split
  and the price is not. Anchoring on the valuation date applies no factor and
  values Apple at $557bn. `domain.pricing.market_cap_as_of` takes
  `share_count_date` for this reason, and two regression tests name the wrong
  anchor so reintroducing it fails.
- **A recovered price is a lower bound, never an estimate.** Splits are undone
  exactly; distributions are not, because `adjustment=all` removes them too.
  The residual is one-directional and roughly the cumulative distribution yield
  since the date — about 3% for AAPL over six years. Removing it needs a raw
  price series, which the provider serves and this project does not store.
- **The entry is the next session's open.** A snapshot computable at a close
  cannot be filled at that close. Close-to-close would pay the entry at a price
  the decision depended on.
- **The horizon is counted on the market's calendar**, taken from the benchmark.
  A security's own bars would let one that stopped trading appear to reach its
  horizon on schedule.
- **A security that stops trading has no return, and saying so is the point.**
  `INTERRUPTED` carries no number: -100% would libel every company acquired for
  cash, 0% would erase every failure, and omitting the row would rebuild the
  survivorship bias. `UNPRICED` is kept separate for a gap in our own record, so
  a count of delistings never counts our missing bars.
- **`delistings` is keyed by ticker, not `company_id`.** Giving each delisted
  security a company row would fix the join by putting thousands of untradeable
  tickers into the table the scanner, the rankings and the dashboard all read as
  the investable set.
- **Delisting discovery is by date range, not by symbol.** Asking about a symbol
  can only confirm the fate of one already known. The provider's inactive-asset
  list is not a registry: 18 of 20 well-known large-cap delistings appear in
  neither the active nor the inactive list, while the corporate-action record
  has all of them with terminal values.
- **Receivership is not covered.** SVB and First Republic appear under no type.
  Documented, not guessed at.
- **`build_company_metrics` gained `share_basis_close`, defaulting to None.**
  The default path is byte-identical to before, which is what allows a
  point-in-time caller to correct the market capitalisation without touching a
  live score. No score version changed; no company's number moved.

### Phase 8I — historical securities

- **Identity is the CIK, never the ticker.** *Superseded by Phase 9A*, which
  found the availability wall: the free record establishes a CIK for about an
  eighth of delisted symbols, so `cik NOT NULL` made the table unable to hold the
  names it exists to hold. The identity is now `historical_securities.id`; the CIK
  remains stored and unique where it exists. The reasoning that a ticker is not an
  identity is unchanged and is what Phase 9A generalises.
- **A historical security is never a `companies` row.** `companies` is the
  current investable universe; the historical layer is separate tables with no
  foreign key to it, so the two sets cannot contaminate one another and a
  reconstruction cannot read today's company state — a delisted name has none.
- **Activity is decided by the date, not by today.** A historical security is
  eligible only on a date at or before its last trading session; the live path's
  current `is_active` flag is a look-ahead for a delisted name and is closed here.
- **The frozen machinery is reused, never reimplemented.** Fundamentals go through
  `periods_from_observations`, the market cap through `as_traded_price`,
  eligibility through `evaluate_eligibility`, the score through `score_company` —
  the same functions the live path calls. A test asserts the two paths produce an
  identical score from identical data.
- **UNKNOWN is preserved wherever free data cannot establish the truth.** An
  FDIC-only bank's fundamentals (absent from EDGAR), a pre-2009 filer (no XBRL),
  pre-2021 prices (beyond the free feed's reach) and a receivership's terminal
  equity value (in no feed) are recorded as gaps and left NULL — never a guessed
  zero. A receivership takes its status only from asserted evidence, never the
  action feed.

### Phase 9A — security identity

- **The identity is an opaque internal id, and every external handle is a dated
  interval against it.** A ticker is a lease, a CUSIP is a dated identifier of an
  *issue* remade by a reverse split, and a CIK exists for a minority of delisted
  names. None of the three may be the key: keying on a ticker splices two
  companies into one history, keying on a CUSIP splits one company's history in
  two, and requiring a CIK stores neither.
- **CUSIP is never promoted to the primary key.** It is stored as a dated
  identifier — `(security_id, cusip, ticker, venue, valid_from, valid_to, source,
  confidence)` — and a reverse split closes one interval and opens another
  against the *same* `security_id`.
- **The CUSIP issuer base is a rule, not a heuristic.** The standard reserves
  characters 1–6 for the issuer, so two issues sharing them are one company's and
  two that differ are two companies'. No lineage is ever drawn across that
  boundary, which is what keeps `BBBY`'s two occupants apart.
- **The issuer holding a symbol on a date is the latest to have taken it up by
  then** — the same rule `universe_members_as_of` uses for membership. *Corrected
  in Phase 9B.* The original test asked whether a span had begun by the date and
  stopped being seen shortly after it, which bounds a span's end from above and
  never from below, so any long-dead earlier occupant also qualified: `ATVI` came
  back ambiguous between Activision Inc (last seen 2008) and Activision Blizzard,
  and `TWTR` between Tweeter Home Entertainment (2007) and Twitter. Resolution
  went from 2,410 to **3,144** symbols and ambiguity from 680 to **14**.
- **A positive fails-to-deliver match is evidence; an absence is not.** A
  security appears in the SEC's file only when a delivery failed, and before
  2008-09-16 only above a reporting threshold. `domain.EvidenceVerdict` has
  exactly two members, `PRESENT` and `UNPROVEN`, and **no `ABSENT`** — so no code
  path can record the opposite conclusion even by accident.
- **Nothing is fabricated.** No CUSIP, CIK, ticker or issuer mapping is invented,
  and no interval endpoint is interpolated: every `valid_from` and `valid_to` is a
  settlement date the record actually carries. Evidence that fits two issuers
  produces `AMBIGUOUS` and creates nothing.
- **Evidence is stored apart from conclusions.** `ftd_identity_observations`
  holds evidence for symbols that resolved to nothing, which is exactly what an
  audit of what is missing is made of. An interval presupposes an identity;
  evidence presupposes none.
- **The two identity spaces stay separate.** Nothing in the identity layer writes
  to `companies`. Contaminated live rows are **detected and reported, never merged
  or split** — re-attributing a row's prices, filings and scores between two
  issuers is a data operation requiring its own authorisation. No `cusip` column
  was added to `companies`: there is no authoritative free source for a live
  company's CUSIP, and deriving one from the fails record means joining on the
  ticker, which is the contamination being detected.
- **A forward return names exactly one identity.** `company_id` or
  `historical_security_id`, enforced by a check constraint, with uniqueness held
  by two partial unique indexes — one per identity kind. A single constraint over
  a nullable pair would enforce nothing, SQL holding every NULL distinct from
  every other. **No existing measurement was migrated**; attaching a delisted
  name's outcomes needs its identity proven first.
- **No score, signal, threshold or ranking rule moved.** `COMPOUNDER_V1_2` and
  Market Signals V1 are byte-unchanged and their parity tests are untouched.

### Phase 9B — historical-security outcomes

- **A delisted security's outcome is a row, not a dataset.** `forward_returns`
  carries `company_id` **or** `historical_security_id`, so an ordinary query over
  a decision date returns both kinds. There is no `include_delisted` flag and no
  `UNION` for a caller to remember — which matters because the failure mode of
  every alternative is silent: a forgotten union does not raise, it returns a
  tidy survivor-only answer.
- **The measurement is the company path with one substitution.** The same
  `domain.forward_returns`, the same benchmark calendar, the same four statuses,
  the same table; only the bars come from `historical_prices`. The calendar stays
  the benchmark's, so a security that stopped trading is `INTERRUPTED` rather
  than appearing to reach its horizon on time.
- **Phase 9B adds no schema.** No migration, table, column, index or constraint —
  `0026` and `0027` were already sufficient. Terminal outcomes are **derived on
  read** for the same reason `classify_delisting` is: a stored second copy could
  only disagree with the columns it was derived from.
- **A ticker-keyed price fetch is bounded to the security's own era.** Prices come
  from a symbol-keyed feed, which is where the identity model could be undone by
  its own plumbing. Every request spans `[earliest identifier interval, last
  trading date + 5 days]`, so a reissued symbol cannot pull a later occupant's
  bars into this security's series.
- **A terminal event is matched by identity, never by symbol.** A security's exit
  is the delisting falling inside one of *its* identifier intervals. VMware has
  three rows under `VMW`; only the intervals say which is this security's, and the
  one stating a consideration is preferred over the ones that do not.
- **A price is never evidence of a terminal value.** `domain.terminal_outcome` is
  handed reported considerations and no price series, so a collapsing tape cannot
  become a settlement. Bed Bath traded to $0.0743 and its terminal value is
  `UNKNOWN`; every receivership is `UNKNOWN`, because no feed files one.
- **Price availability and score availability are separate.** A CIK-unresolved
  security has no EDGAR fundamentals and is `INSUFFICIENT_DATA`, and keeps its
  prices, its identity and its outcomes regardless. `CompounderScore V1.2` was
  not widened to reach it, and is byte-unchanged.

### Phase 9C — historical universe membership

- **Identity, membership and eligibility are three concepts, not one boolean.**
  Identity is `security_identifiers`; membership is a slow-moving listing fact
  stored as dated intervals in `universe_membership`; eligibility is the frozen
  threshold screen applied per date. Collapsing any two is how a universe becomes
  today's roster wearing a date.
- **No `MEMBER` verdict can currently be `OBSERVED`, and the schema says so per
  row.** Exchange, security type and sector are undated everywhere in this
  system — every stored exchange carries the day capture first ran. A member
  verdict therefore carries an attribute backwards, `confidence` records that it
  did, and `inferred_inputs` names which. `ListingAttributes.fully_dated` is the
  single gate on promotion, and becoming observed requires a dated venue source
  rather than a change of opinion.
- **Price presence is never membership.** Price and settlement evidence bound an
  interval; the membership claim rests on the listing attributes. A security with
  a full price series and no recorded venue is `UNKNOWN`.
- **`UNKNOWN` is a first-class state and is always reported.** Every security
  lands in exactly one of members, non-members or unknown — a universe that loses
  securities between its groups hides its own size, which the first
  reconstruction did for 1,530 of 8,919 securities before it was caught.
- **The record's own reach is not evidence about the market.** `NOT_YET_IN_RECORD`
  is only asserted when a security's first dated trace is strictly after the
  observation record's floor; at the floor the period stays `UNKNOWN`.
- **Membership is dual-identity, like outcomes.** `company_id` or
  `historical_security_id`, exactly one, so `members_as_of(D)` returns delisted
  securities without an `include_delisted` flag.

### Phase 7 — international

- **The filing's unit is authoritative, never the vendor's.** `reporting_currency`
  comes from the XBRL unit key; `quote_currency` is what the listed share trades
  in. One field held both and whichever provider wrote last won — mixing TSM's
  TWD statements with its USD market cap yields an EV/Revenue of 0.38x against a
  true 24.07x.
- **Convert the market side, never the statements.** Only the market
  capitalisation crosses currencies. Every ratio reads
  `CompanyMetrics.market_cap_for_ratios` — converted where a rate exists, `None`
  where one does not, never the unconverted figure.
- **A rate belongs to a date and a source.** `fx_rates` stores one row per
  `(base, quote, rate_date, provider)`. Rates are bounded to five days before the
  score date, never after it. `FX_UNAVAILABLE` is a warning, not an exclusion.
- **Coverage is never bought with a fabricated period.** Four annual periods are
  not a trailing year; six-month figures are never halved into quarters.
- **A share count that cannot be multiplied by the price is not read.**
  `MarketCapSource.CALCULATED` is never produced for 20-F/40-F filers.
- **A score built on `STALE` fundamentals keeps its number everywhere the number
  describes the company, and is excluded from current rankings.**

---

### Phase 9K.1 — free listing evidence

`listing_observations` records where a security traded, month by month, from
Nasdaq's public *Market Share by Symbol* files. Four rules govern it.

- **The `Listed*` family changed meaning and the code must know it.** Tape B
  (Arca, NYSE American, regional) through 2021-12; Tape A (NYSE) from 2022-01,
  with 2021-07 an exception inside the earlier era. `domain.tape_for` predicts a
  tape from the month, `domain.classify_tape` reads one from the file's own
  contents, and only a file where the two agree is imported. The rest are
  `QUARANTINED` rows carrying both tapes and the reason.
- **Absence is never non-membership.** `ListingEvidenceState` has no
  `NOT_LISTED` member, so no caller can write one. A security that did not trade
  in a month is simply missing, and 34.6% of observed symbols have a hole inside
  their own run.
- **Monthly is never daily.** `month_state` may return `MARKET_SHARE_OBSERVED`;
  `daily_state` never does, because the file attests to a month and a security
  that left on the 5th appears in it exactly as one that traded all month.
- **NYSE American is censored, and counted.** It appears in no importable file.
  247 live companies are stored on it; the audit reports them rather than
  treating them as absent.

Scope is **2022-01 onward** — the first month Nasdaq and NYSE are both covered.
Nothing before it is imported.

### Phase 9L.5 — the repaired planner, validated on fresh data

A representative 1,008-filing batch was harvested and measured against a
prediction frozen before any of it was downloaded. **actual / yield-adjusted
expected = 0.979** — inside the 0.90–1.10 gate, against v1's 0.412 on the same
kind of test.

Acquisition was clean in a single pass: 1,008/1,008 downloaded, 1,010 requests
(two 503s retried), no recovery pass needed, and a replay of the same manifest
issued **0 HTTP requests**. The corpus grew by exactly +1,008 trusted filings to
40,163 / 34,392, every integrity category zero, 1,008 new artifacts rehashed and
1,957 prior artifacts sampled, all clean.

What the batch was built to test, it tested. The dominant single-identity
stratum — 95% of Plan A3 — landed at **1.004**. The rank deciles are flat
(D1–D3 0.950, D4–D7 1.002, **D8–D10 0.985**), so calibration does not decay in
the low-ranked targets that make up the tail of the plan; years run 0.950–1.000
with no temporal bias; live 0.982 against delisted 0.903. **Zero future leakage,
zero incorrect cross-class joins, zero CIK-sharing over-credit.**

Two things needed care rather than acceptance.

- **The multi-CIK stratum reads 0.640 as frozen and 0.876 once corrected.** The
  frozen gap set predates batch 1's parse, and batch 1 over-represented
  multi-class filers 19x — so it had already closed 55 of the 203 months this
  batch was credited with. No download supplies a month twice. Both figures are
  reported; the corrected one meets the ≥0.80 guide, and the residual decomposes
  to already-covered 51, no-cover-row 24, subset-naming 9, with nothing
  unexplained. The traces confirm the repair holds where it matters:
  `HEI`/`HEI.A`, `GTN`/`GTN.A`, `KELYA`/`KELYB`, `SENEA`/`SENEB`, `MKC`/`MKC.V`
  and `UHAL`/`UHAL.B` are all genuine dual common classes, each credited *and*
  delivered.
- **The held-set source was wrong and is now the manifest.** `held_keys` read the
  derived index, a snapshot that lags the corpus; that is what let 9L.4 plan 800
  filings already on disk. `held_from_manifest` replaces it in the CLI, and a
  `--replay` flag makes re-running a completed manifest a deliberate act rather
  than something the overlap guard has to permit by accident.

Plan A3 is unchanged and now 1,008 targets closer: **60,933 remaining**, verified
by `(CIK, accession)` set arithmetic rather than subtraction, 6.53 GiB,
calibrated expectation 152,076 months. The final 1,941 targets beyond the 60k
knee buy 1,941 safe months for 1.28 GiB — 0.15% of free space — so they are
kept.

### Phase 9L.4 — separating issuer reach from security coverage

The planner is repaired structurally rather than statistically, and revalidated
against two independent samples with no network access.

The confusion was between two questions that share a filing but not an answer:
*can we obtain this?* (`(CIK, accession)`, issuer level) and *what may it
support?* (`(identity, month-end)`, security level). `sec_evidence/reachability.py`
answers only the second, from evidence that already exists.

The rule is not "distrust multi-identity issuers" — that would be a second wrong
answer of the same shape, since `LSXMA`/`LSXMB`/`LSXMK` are three genuine Liberty
tracking classes. A filing is credited with exactly the identities the record
already shows carrying common stock; where several identities share an issuer and
none is yet known to be the common one, the credit is **zero**, not a share. Of
264 multi-security CIKs: 191 resolve to exactly one common identity, 58 carry
more than one real common class, 15 remain unassigned. 312 identities the old CIK
rule would have credited are now excluded, and `AIZ`/`AIZN` credits one and
excludes one.

Both revalidations pass on their large samples. Against the 800-document batch:
raw ceiling 2,891, expectation 2,679, actual 2,544 — **0.950**, with the
multi-class stratum at **0.947** where v1 scored 0.397. Against an independent
3,000-document holdout drawn from the pre-existing corpus (SHA `91b9ef4b…`),
simulated by removing those observations from planner-visible evidence:
**0.940** overall and **0.951** single-identity.

Two things the measurement forced.

- **The raw prediction is a ceiling, and the expectation needs a measured rate.**
  The residual decomposes with nothing unexplained: 81.7% is filings carrying no
  usable common-stock cover row, 12.4% joins that are not `EXACT`, and only
  **5.8%** the remaining subset-naming effect. So the expectation multiplies the
  ceiling by a measured usable-common-row rate per form (8-K 92.2%, 10-Q 90.8%,
  10-K 85.8%, 20-F 57.9%) — computed with the holdout excluded, and materially
  lower than the 9L.1 any-symbol yields it replaces.
- **The candidate pool must exclude what is already held, from the live
  manifest.** The derived index predates the 9L.3 harvest, which makes it the
  right snapshot for *evidence* (no leakage into its own prediction) and the
  wrong one for *acquisition*. A batch-2 overlap assertion caught v3 planning 800
  filings already on disk.

`expansion_plan_v3` — 61,941 targets, 6.6 GiB, 165,553 safe months, 152,381
expected, 70.6% `UNKNOWN` reduction. The repaired gap model moves 6,717 months
out of solvable into `G3_IDENTITY_AMBIGUOUS` (8,016) and
`G5_MULTI_CLASS_UNASSIGNED` (702); the floor rises to 76,029 (29.4%). A knee now
exists: 60,000 targets reach 98.8% of the gap for 5.4 GiB. 8-K remains
overwhelming at 68,897 months/GiB against 10-Q's 1,429.

Plan status: **v1 superseded** (CIK-sharing over-credit), **v2 provisional** — a
structural recomputation from observed reach, not a haircut, but not the
class-aware model either, and its rows carry a `plan_version` of `v1` from a
stamping defect — **v3 the structurally repaired candidate**, not authoritative
until a representative network sample validates it.

### Phase 9L.3 — the planner was wrong, and the validation caught it

The 800-target validation batch was acquired, verified and measured. Acquisition
is sound; **the planner's coverage arithmetic was not**, and the batch existed
precisely to find that out before 61,984 filings were fetched on a false premise.

Acquisition passed cleanly. All 800 targets reached a terminal state, the corpus
grew by exactly +800 trusted artifacts and +63,954,126 bytes, every error
category reconciled to zero, and a third replay of the same manifest issued
**0 HTTP requests** — exact-manifest convergence. Storage came in at 0.0596 GiB
against 0.08 estimated, with the 8-K mean within 3% of the 9L.1 prior.

The defect: the planner credited a filing with covering **every identity sharing
its CIK**. Those secondary identities are baby bonds, preferred stock and
depositary shares. CIK 1042046 (`AFG`, `AFGB`, `AFGC`, `AFGD`, `AFGE`) yields 16
common-stock rows and 64 `NOTE_OR_DEBT`; CIK 7323 yields no common-stock row at
all. Measured: 2,238 identities assumed reachable, **784 actually reached**.

The split is decisive. Targets on single-identity CIKs realised **98.3%** of
prediction; targets on multi-identity CIKs realised **37.8%**. The identity layer
was never at fault — it correctly refused 1,821 non-common rows and made 842 safe
common-stock joins, with **zero future leakage** and correct dated-occupant
attribution for `LSXMA`/`LSXMB`/`LSXMK` and `PARA`/`PARAA`.

Two rules follow.

- **A filing covers an identity only where that CIK's cover pages actually yield
  a common-stock row that joins to it.** Never inferred from CIK sharing. The
  corrected model predicts 2,891 for the batch against 2,544 observed — 1.14x,
  where the original predicted 6,402 — 2.52x.
- **A stratified validation sample is not a representative one.** The batch was
  built to over-weight multi-class filers and does so by **19.3x** (92.6% of its
  targets versus 4.8% of the candidate pool). Its 39.7% realisation is a
  worst-case stress result, and reading it as the plan-wide expectation would be
  as wrong as the defect it exposed. Across the whole pool the correction costs
  5.1%, not 60%.

`expansion_plan_v2` (61,007 targets, 6.2 GiB, 163,413 calibrated months, 70.5%
`UNKNOWN` reduction) is proposed alongside v1, which is left untouched.

### Phase 9L.2 — the selective expansion plan

"Expand selectively" now has a number. **61,984 filings and 6.4 GiB close 100% of
the solvable gap**, against 269,538 filings and ~150 GiB for the naive
fetch-the-whole-window approach — 77% fewer documents and 96% less storage for
identical coverage. Nothing was downloaded.

The gap was classified before anything was planned, because the distinction
between gaps filings can close and gaps they cannot is the whole decision. Of
258,463 `UNKNOWN` month-verdicts: **172,270 (66.7%) are solvable**, 36,661
(14.2%) have no resolvable CIK, 30,070 (11.6%) have a CIK but no catalogue filing
that could cover them, 606 (0.2%) are identity-ambiguous, and 18,856 (7.3%) are
already covered. The floor is therefore **67,337 `UNKNOWN` (26.1%)** — a 73.9%
reduction is the ceiling, and no budget moves it.

Three findings shaped the plan.

- **8-K carries the phase.** Alone it reaches 98.0% of the solvable gap for 2.4
  GiB across 60,701 filings. 10-Q reaches 70% for 81.3 GiB; 10-K 27% for 40.2
  GiB. The 8-K population is genuinely useful rather than merely numerous: 42 KB
  mean, 96.4% observed cover yield, and event-driven timing that spreads through
  the year where periodic reports cluster.
- **Coverage-first greedy run to exhaustion *is* the interval cover.** Plan A and
  the minimum-temporal-cover plan return the same 61,984 filings, because each
  selection re-scores against what is already covered and a filing whose months
  are wholly taken contributes zero. They are reported as one result, not two.
- **The co-registrant duplicate is real and now explained.** 9L.1's "32,601 vs
  32,584" was an artifact of matching on accession alone: 17 accessions are held
  under more than one CIK — Entergy and Southern Company utility families filing
  one document for several registrants — giving 29 surplus artifacts, all with a
  single distinct SHA-256. Matching on `(cik, accession)` reconciles exactly, and
  that pair is the planner's filing identity.

The shared-CIK limitation was quantified rather than fixed: 264 CIKs are claimed
by more than one tracked identity, covering 620 identities, 69 of which keep
`cik=NULL`, across 20,428 month rows and 19,201 `UNKNOWN`. Almost all are
preferred and baby-bond tickers (`RILYG`, `AFGB`, `SOJC`, `MGRB`) — non-common
securities a common-stock universe excludes anyway — with `AIZ`/`AIZN` at CIK
1267238 the worked case. **Recommendation for a later phase: model CIK as
issuer-level evidence in its own relation rather than as a unique column on
`historical_securities`.** No migration was run.

Catalogue completeness was checked offline: of 2,604 referenced history files,
2,415 end before 2022 and only **189 could overlap the window**, so the 269,538
candidate set is essentially complete for the period being reconstructed.

Plan artifacts live under `derived/` — `expansion_plan_v1.{csv,jsonl,sha256}` and
`validation_batch_1.jsonl` — and are excluded from reconciliation: trusted ==
files_on_disk == 38,355, 0 orphans, and the corpus fingerprint is unchanged.

### Phase 9L.1 — what the corpus can actually support

The corpus was read offline and measured against the monthly universe. **Six
filings per security is not enough, and the reason is time, not volume.**

Extraction succeeded completely: 32,584 of 32,584 filings parsed, no failures and
no missing files, yielding 43,163 registered-class rows in 1,086 s. 30,093 of
those rows are common stock, 12,274 something else, and 796 the evidence cannot
place. Classification is overwhelmingly title-led (39,760 `TITLE_ONLY`, 2,532
`TITLE_AND_MEMBER`, 147 `CONFLICTING`), exactly as the 9K.2 policy intends.

The binding constraint is *when* the filings are from. A harvest capped at the
six most recent filings per security produced a corpus that is 98.2% dated 2022
or later and 81.9% dated 2026, spanning a median of **100 days** per filer. The
research universe spans 55 months. Six recent filings cannot describe it, and the
measured result is that this corpus closes only **8.0%** of the universe's
258,463 `UNKNOWN` month-verdicts at the current 90-day carry-forward.

Three rules were established here.

- **A source database is named, never inherited.** `bind_source` takes an
  explicit path, opens it `mode=ro`, and refuses unless the universe it holds
  matches the expected count and digest. This is the 9K.25 near-miss made
  impossible: the wrong database produced a fully green run over a population
  3,150 identities smaller, and nothing in the output said so.
- **A filing is a point, not an interval.** Everything that turns dated cover
  facts into monthly coverage is inference, so direct and carried figures are
  never pooled. Carry-forward is forward-only; measured across 0/30/60/90/120/180
  days it buys 0 / 61.5 / 107.1 / 147.2 / 186.0 / 263.3 percent more
  security-months over the exact-date floor. **90 days is retained** — it is the
  point where the curve is still steep without a filing speaking for two quarters
  it cannot see.
- **Derived output is not evidence.** The 229 MB index lives under `derived/`,
  excluded from reconciliation exactly as `checkpoints/` is, and is rebuildable
  from the manifest and the stored bytes. The corpus fingerprint was identical
  before and after the whole analysis.

What expansion would buy is now measured rather than guessed. Every qualifying
filing inside the universe's own window is 292,866 documents, of which 269,521
are not held; at measured per-form sizes that is **≈150 GiB** (8-Ks dominate the
count at 42 KB mean, while 10-Qs and 10-Ks carry the bytes). Fetching them would
raise the reachable `UNKNOWN` share from 8.0% to **76.7%**.

It cannot reach 100%, and the residue is a different problem: **37,949 monthly
rows (14.1%) belong to identities with no resolvable CIK**, and 978 of the 1,252
historical securities in the universe have no CIK at all. No quantity of SEC
filings answers those — that gap is a historical ticker→CIK limitation of the
free record, not a harvesting shortfall.

Two structural limits were also recorded. `historical_securities` carries
`UniqueConstraint("cik")`, so a multi-class filer cannot store both classes under
one CIK; the second keeps `cik=None` and loses its filer handle. And 2,053 of
5,771 submissions catalogues reference 2,604 further history files that were
never fetched, so even the *metadata* of the deep past is incomplete.

Retaining whole documents is justified rather than assumed: on a deterministic
400-document sample, 98.5% carry at least one material-event signal — 56.2%
earnings, 53.8% an explicit 8-K item, 49.5% acquisition language, 12.0% listing
or delisting events.

### Phase 9K.25 — the production SEC evidence corpus

The corpus is complete. All 8,919 production identities have been harvested
across 36 chunks of 250 (the last holds 169), and the terminal state is **38,355
trusted artifacts** — 5,771 submissions catalogues and 32,584 filing documents —
over 38,540 manifest records and 37,695,067,178 logical bytes. Every artifact's
SHA-256 was recomputed from disk at the end and matched what the manifest
recorded.

29,479 HTTP requests were issued in this phase for 3 non-200 responses, all
`503 Service Unavailable` and all recovered by the retry policy: each of the
three URLs ends `DOWNLOADED status=200` in the manifest with real bytes and a
digest. No target finished `FAILED` or `SKIPPED`. Every one of the 28 chunks
converged on its third pass with zero new bytes, so two fetching passes were
always sufficient.

Two rules were added here.

- **Verification is tiered, and the tiers are reproducible.** Rehashing the whole
  corpus after every chunk reached 1,523 s at 8,963 artifacts and grows with the
  corpus, so 28 more of those would have cost more hours than the fetching did.
  Instead every chunk gets a full structural reconciliation and a full SHA of
  everything it wrote, plus a sample of the older corpus taken by a fixed rule —
  **sorted by `(sha256, url)`, even stride, `max(500, 5%)`**. The ordering comes
  from the artifacts' own digests, never from process state or the clock, so
  anyone can reproduce exactly which artifacts were checked. Five full-corpus
  rehashes at offsets 3000, 4250, 5500, 6750 and 8000 and a sixth at the end
  confirmed the tiers: no artifact the sampling passed was later found to have
  drifted.
- **The sweep's database is named explicitly, and read-only.** `.env` points
  `DATABASE_URL` at `compounder_radar.db`, but the production universe lives in
  `stage9k.db`; a sweep run without the override selects a *different* 5,769
  identities and every chunk still passes its gates. The completed checkpoints
  are what settle it — each stores the SHA-256 of its frozen identity list, and
  all six from 9K.23–9K.24 reproduce from `stage9k.db` and from nothing else.
  The sweep now opens it `mode=ro&uri=true` so it cannot be written at all.

Of the 6,919 identities in this phase, 4,763 (68.8%) resolved to a CIK and were
requestable — 4,475 through the EDGAR ticker map and 288 already stored — and
2,156 did not. An unresolved identity is recorded as such and never guessed at.

The primary database is untouched: `compounder_radar.db` is byte-identical
(`02a6660b…`, 249,155,584 bytes) with an unchanged mtime and no WAL or journal
sidecar, as is `stage9k.db` (`20a77d24…`), whose WAL is empty and predates the
sweep.

### Phase 9K.13 — the SEC evidence writer

Phase 9K.12 found that the SEC harvest had no code: `SEC_EVIDENCE_DIR` was
referenced only by the setting that declares it and the resolver that verifies
it. `src/stock_screener/sec_evidence/` is that subsystem — storage, manifest,
harvest and sample — plus `api_clients.SecDocuments`, which fetches one document
and returns the response facts a stored artifact needs to be auditable.

Four rules govern it.

- **The root is verified, never created.** `open_evidence_root` requires the
  configured directory to already exist and only then creates the four known
  subdirectories beneath it. An unmounted drive raises.
- **Writes are anchored to that root.** An earlier draft created missing parents
  by walking up to the nearest directory that exists, which protects nothing:
  `/Volumes` always exists, so an unmounted `BEK` would have had the whole
  evidence tree built underneath it on the boot disk. Directories are now only
  created *below* a root just confirmed present, and a destination outside it is
  refused.
- **An artifact is whole or absent.** Temporary file in the destination
  directory, flush, `fsync`, atomic rename. A partial download never becomes a
  file the next run trusts.
- **Cache reuse is proved, not assumed.** An artifact is reused only when the
  manifest records it as stored *and* the bytes still hash to the recorded
  digest. Anything else is `INVALID_CACHE` and re-fetched, and a file with no
  manifest entry is re-fetched too — adopting it would invent provenance.

Outcomes are `DOWNLOADED`, `CACHE_HIT`, `INVALID_CACHE`, `FAILED`, `SKIPPED`.
Budgets (`max_requests`, `max_bytes`) are checked before each request and end a
run with `SKIPPED` entries, which is what makes the next run a continuation. The
CLI is `stock-screener evidence status | sample | harvest`, and a dry run issues
no request and writes nothing, the manifest included.

**No harvest was run.** No network request was made in this phase.

### Phase 9K.11 — point-in-time occupant attribution

Attributing a monthly observation asked "is this symbol in `companies`?" first and
stopped there if it was, which reads today's roster as a description of every
month the symbol ever had. `BBBY` was the measured case: all 29 observed months —
Bed Bath & Beyond's 17 on Nasdaq through 2023-05 and Beyond, Inc.'s 12 on NYSE
from 2025-08 — were credited to the live company. One occupant where the record
plainly holds two, and survivorship bias written directly into the universe.

Three rules now govern it.

- **The first question is whether the symbol changed hands, not who holds it.**
  `classify_symbol` answers it from the published fails-to-deliver record. Where
  one issuer ever held a symbol, the live row and any stored delisted row are the
  same business and the live one answers, exactly as before.
- **`last_trading_date` is not the boundary, though it reads like one.** Of the
  138 symbols carrying both a live row and a delisted row, **127** have a last
  trading date within days of the present, because the free record's last trading
  date is the last settlement its files happened to show. Leggett & Platt is not
  its own dead predecessor. Trusting that field would have moved 127 symbols off
  the live roster wholesale.
- **The live roster is reachable only from the most recent span.** Inside a
  sequence, `holder_spans` names the month's occupant and `security_identifiers`
  says which stored security carried that issuer's CUSIP. A month between two
  occupants' evidence is `REUSED` — recorded as a change of hands whose holder
  cannot be named — never handed to whoever holds the letters now.

The same defect had a mirror image in the class join: `build_universe` passed one
occupancy span, and `occupancy_of` places a date before the first run with the
first occupant, so a predecessor's cover page could still classify a successor's
months. A live company that inherited its symbol now carries its predecessor's
tenure as a run of its own, and the occupancy test refuses the old filing.

Measured over the staging record (458,708 observations, 268,780 monthly verdicts):

| | before | after |
| --- | --- | --- |
| `RESOLVED_COMPANY` | 233,480 | 226,469 |
| `RESOLVED_HISTORICAL` | 33,458 | 45,492 |
| `REUSED` | 11,629 | 35,102 |
| `UNRESOLVED` | 180,136 | 151,530 |
| `ELIGIBLE` monthly verdicts | 9,256 | 9,066 |
| live securities ever `ELIGIBLE` | 1,714 | 1,659 |
| delisted securities ever `ELIGIBLE` | 486 | 522 |

301 `ELIGIBLE` rows moved off a live company. 55 live companies lost months they
never traded in and 36 delisted securities gained months they did.

## Architecture

### Two deployables, one repository

A deliberate exception to the one-deployable rule. They live together because the
dashboard's types mirror these endpoints and a response-shape change must land on
both sides in the same commit. **Do not move the frontend to another repository.**

| Deployable | Is | Owns |
| --- | --- | --- |
| `src/stock_screener/` | Python backend, CLI, read-only API | all data, every calculation, every score |
| `frontend/` | the dashboard, Next.js | rendering what the API returns |

A third deployable needs an ADR first and probably its own repository.

### Packages

| Package | Import | Holds | I/O |
| --- | --- | --- | --- |
| `packages/domain` | `domain` | models, metric engine, eligibility, CompounderScore, curves, sectors, statements, cadence, survivorship | none |
| `packages/data-access` | `data_access` | SQLAlchemy models, repositories, session | database |
| `packages/api-clients` | `api_clients` | Alpaca, EDGAR, FMP, research providers, external search, FX | HTTP |
| `packages/research` | `research` | Phase 3 contract, prompt, validation | none |
| `packages/deep-research` | `deep_research` | Phase 6 contract, brief, evidence, provenance, prompt, validation | none |

Placement rule: one caller → a module in `src/stock_screener/`; two or more
callers → a package. Never create one speculatively.

### Application modules

```
src/stock_screener/
  cli.py            every command; the only entry point a job may spawn
  api.py            the read-only API plus the job endpoints
  config.py         the typed settings — the only reader of the environment
  dashboard.py      read models for the dashboard endpoints
  providers.py      provider wiring and composition
  fx.py             rate resolution and storage
  jobs.py           JOB_KINDS, spawn, track
  logging.py        structlog setup
  scanning/         ingestion, scanner, report
  scoring/          engine, enrichment, rankings, report
  research/         candidates, brief, runner, report, store
  deep_research/    preparation, collection, sources, brief, runner, report
  pit/              point-in-time capture, metrics, actions, returns, delistings
  signals/          market-signal derivation from the feature record
  backtest/         readiness harness, score reconstruction, historical securities
```

### Frontend routes

`/` · `/stocks/[ticker]` · `/watchlist` · `/jobs` · `/research` ·
`/research/[ticker]`

---

## Score definitions

### Versions

| Version | State | What it is |
| --- | --- | --- |
| `COMPOUNDER_V1` | history only | the original rules |
| `COMPOUNDER_V1_1` | history only | V1 plus three guards; no weight or curve changed |
| `COMPOUNDER_V1_2` | **current** | V1.1's formulas exactly, plus one ranking-admission policy |

`CURRENT_SCORE_VERSION = COMPOUNDER_V1_2`, in
`packages/domain/src/domain/scores.py`.

**V1.1's guards:** redistribution capped at a 1.15 uplift; acceleration credit
withheld from a rebound against a flat three-year trend; revenue growth plus
acceleration capped where growth does not persist.

**V1.2's policy:** a score built on `STALE` fundamentals is excluded from current
rankings. Every formula, curve, weight, threshold and penalty is V1.1's. It
needed a version because it changes what a ranking *is*, not because a number
moved.

### Components

| Component | Points |
| --- | --- |
| Growth | 35 |
| Financial Quality | 25 |
| Valuation | 25 |
| Market Confirmation | 15 |

Risk penalties are applied after; full rules in
[CompounderScore](compounder-score.md).

### Scoring status

`SCORED` · `INSUFFICIENT_DATA` · `UNSUPPORTED_SECTOR` · `NOT_ELIGIBLE` · `ERROR`.
`final_score` is a number only for `SCORED`.

### Freshness

`CURRENT` · `STALE`. Recorded on the snapshot, never derived by the reader,
because the bound scales with reporting cadence. `domain.is_rank_eligible` is the
single place the question is answered. Rows written before migration `0020` read
as `CURRENT`.

| | Stale score |
| --- | --- |
| Numerical score | kept |
| Stock detail page | shown, with a notice |
| Research and Deep Research | available |
| Historical snapshots | untouched |
| Current rankings — all four views | excluded |

### Ranking views

`rankings` (top) · `hidden-gems` · `wrong-price` · `improving`. All four are
current views and all four exclude stale scores.

---

## Data contracts

### Domain enums

| Enum | Members |
| --- | --- |
| `ScoringStatus` | `SCORED`, `INSUFFICIENT_DATA`, `UNSUPPORTED_SECTOR`, `NOT_ELIGIBLE`, `ERROR` |
| `Freshness` | `CURRENT`, `STALE` |
| `ExclusionReason` | `PRICE_BELOW_MINIMUM`, `MARKET_CAP_BELOW_MINIMUM`, `LOW_LIQUIDITY`, `INACTIVE`, `UNSUPPORTED_SECURITY_TYPE`, `UNSUPPORTED_CURRENCY`, `MISSING_REQUIRED_DATA` |
| `PeriodCadence` | `QUARTERLY`, `SEMIANNUAL`, `ANNUAL`, `UNKNOWN` |
| `StatementProfile` | `GENERAL`, `FINANCIAL_INSTITUTION` |
| `MarketCapSource` | provenance of a market cap; `CALCULATED` is withheld from 20-F/40-F filers |
| `VolumeBasis` | which tape a liquidity figure came from |

Anything a package exports lives in its `__all__`. Reaching into a submodule from
outside is a bug.

### Eligibility thresholds

`min_price` 2.0 · `min_market_cap` 100,000,000 · `min_avg_dollar_volume`
1,000,000 · `min_trading_days` 20. Defaults mirror `.env.example`; the
application builds `EligibilityThresholds` from `Settings`.

### Prompt versions

`research.CURRENT_PROMPT_VERSION = RESEARCH_PROMPT_V2` ·
`deep_research.CURRENT_DEEP_PROMPT_VERSION = DEEP_RESEARCH_PROMPT_V2`. A prompt
version change invalidates the deep-research cache.

### API surface

Read-only unless marked.

```
GET    /health
GET    /api/companies
GET    /api/companies/search
GET    /api/companies/{ticker}/score
GET    /api/scan
GET    /api/rankings
GET    /api/rankings/hidden-gems
GET    /api/rankings/wrong-price
GET    /api/rankings/improving
GET    /api/rankings/{view}/export
GET    /api/stocks/{ticker}
GET    /api/stocks/{ticker}/research
GET    /api/deep-research/{ticker}
GET    /api/deep-research/{ticker}/history
GET    /api/deep-research/reports/{report_id}
GET    /api/watchlist
POST   /api/watchlist/{ticker}          write
DELETE /api/watchlist/{ticker}          write
GET    /api/jobs/kinds
POST   /api/jobs                        write, spawns a process
GET    /api/jobs
GET    /api/jobs/{job_id}
```

### Job kinds

`daily` · `scan` · `score` · `enrich` (spends money) · plus the research kinds.
The map in `src/stock_screener/jobs.py` is the entire allowlist.

### CLI

`update-universe` · `update-market` · `update-eligibility-volume` ·
`update-benchmark` · `update-fundamentals` · `scan` · `run-scan` · `score` ·
`enrich` · `rankings` · `hidden-gems` · `wrong-price` · `improving` · `explain` ·
`run-daily`, plus the `research` group (`candidates`, `brief`, `update-filings`,
`update-filing-text`, `run`) and the `deep-research` group (`prepare`, `run`).

---

## Database migrations

Head is **`0030`**. One revision per schema change; the chain is linear.

| Rev | Adds |
| --- | --- |
| `0001` | initial schema — `companies`, `financial_snapshots`, `price_history` |
| `0002` | `reporting_currency` |
| `0003` | consolidated average daily share volume |
| `0004` | `score_snapshots` — one score per company per day per version |
| `0005` | `benchmark_prices` |
| `0006` | point-in-time share counts and market-cap provenance |
| `0007` | gross-profit basis — which concept produced the figure |
| `0008` | `research_reports` |
| `0009` | `filings` |
| `0010` | `filing_excerpts` |
| `0011` | `watchlist` |
| `0012` | `jobs` |
| `0013` | `deep_research_reports` |
| `0014` | external collection reuse |
| `0015` | score coverage — market run or single company |
| `0016` | `score_snapshots.exclusion_reasons` |
| `0017` | `fx_rates`; splits reporting currency from quote currency |
| `0018` | `period_start` and `cadence`; existing rows backfilled `QUARTERLY` |
| `0019` | `statement_profile`, `consolidated_avg_volume`, `volume_source` |
| `0020` | score freshness; NULL reads as `CURRENT` |
| `0028` | `universe_membership` — effective-dated membership verdicts against both identity kinds, with the confidence behind each. Additive; no existing table touched |
| `0029` | `pit_feature_snapshots` and `pit_signal_snapshots` gain dual identity |
| `0030` | `listing_source_files`, `listing_observations` — free monthly venue evidence |
| `0027` | `security_identifiers`, `ftd_source_files`, `ftd_identity_observations` — the identity layer. Relaxes `historical_securities.cik` to nullable and opens `forward_returns` to a second identity with a check constraint and two partial unique indexes. Rebuilds two tables; see the safety note below |
| `0026` | `historical_securities` and its `historical_observations`, `historical_prices`, `historical_corporate_actions` children — delisted names keyed by CIK, separate from `companies`. Additive, no `batch_alter_table` |
| `0025` | `delistings` — the securities that left the market, keyed by ticker because they have no company row |
| `0024` | `forward_returns` — what a security did at 1/5/20/60/120 sessions from an observation date |
| `0023` | `filing_acceptance` — when the SEC accepted each filing, so availability is judged by instant rather than by date |
| `0022` | `pit_signal_snapshots` — the derived market-state layer, append-only and versioned |
| `0021` | the point-in-time layer — `pit_capture_runs`, `pit_score_snapshots`, `pit_feature_snapshots`, `fundamental_observations`, `universe_events`, `corporate_actions` |

### Tables

`companies` · `financial_snapshots` · `price_history` · `benchmark_prices` ·
`fx_rates` · `score_snapshots` · `filings` · `filing_excerpts` ·
`research_reports` · `deep_research_reports` · `watchlist` · `jobs`

Phase 8B's append-only layer, beside them: `pit_capture_runs` ·
`pit_score_snapshots` · `pit_feature_snapshots` · `fundamental_observations` ·
`universe_events` · `corporate_actions`

Phase 8C adds one more: `pit_signal_snapshots`.

Phase 8I adds the historical-security layer, separate from every table above:
`historical_securities` · `historical_observations` · `historical_prices` ·
`historical_corporate_actions`.

Phase 9A adds the identity layer over it: `security_identifiers` (dated CUSIP,
ticker and venue intervals against a stable `security_id`) and the evidence they
are built from — `ftd_identity_observations` and `ftd_source_files`.

### The migration safety rule

**Never verify a migration against `compounder_radar.db`.** Use a temporary
database or a throwaway copy.

A migration is not only DDL: `batch_alter_table` rebuilds a table on SQLite, and
dropping `companies` under enforced foreign keys cascades through every child
declaring `ON DELETE CASCADE` — price history, fundamentals, scores, filings,
excerpts, both research tables and the watchlist. Migration `0017` did exactly
that and deleted about 1.6 million rows from the primary database. That is why
`tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows`
exists.

**`0027` rebuilds two tables, and one of them is a parent.** Making
`historical_securities.cik` nullable needs a SQLite table rebuild, and that table
has three children declaring `ON DELETE CASCADE`. The revision disables foreign-key
enforcement for the rebuild — which SQLite's own documentation prescribes — and the
subtlety that makes it dangerous is that `PRAGMA foreign_keys` is **silently
ignored inside a transaction**, which Alembic normally has open. The naive version
appears to work, does nothing, and cascades. `_disable_foreign_keys` commits at the
driver level first and then *verifies* the pragma took effect, raising rather than
proceeding if it did not: a migration that cannot make itself safe must fail loudly.
`tests/integration/test_migrations.py::test_the_identity_migration_preserves_a_populated_historical_security`
is the proof on the real upgrade path. The `forward_returns` rebuild needs no such
care — nothing has a foreign key *to* it, so dropping it cascades to nothing.

---

## Known limitations

**The legacy colocated layout is still supported, and is one failure domain.**
Every corpus harvested before B2.5 — the BEK corpus included — keeps its
manifest, checkpoints and derived index beneath `SEC_EVIDENCE_DIR`, and with
`SEC_CONTROL_DIR` unset the code reads that layout unchanged. No control file
was moved; relocating an existing corpus's control files is a deliberate
operator step for a later phase, as is any remote mirror of them. `EvidenceRoot`
and the `harvest()`/`preflight()` compatibility forms remain for that layout.
Backend temporaries are hidden from artifact listing (`temporary_files` remains
zero). The local backend does not pin mount identity against concurrent
replacement. See [`EvidenceRoot` after B2.5](storage-layout.md#evidenceroot-after-b25).

**Historical listing evidence is monthly and partly censored.** Free sources give
Nasdaq and NYSE from 2022-01 at month resolution only; NYSE American is not
covered at any date, and no free daily listing source exists. A 2022+ universe
built on it is survivorship-aware at monthly resolution and censored below that.
See `docs/reference/listing-evidence.md`.


Documented rather than fixed. Each is a deliberate stop, not a backlog item.

| Limitation | Consequence |
| --- | --- |
| **6-K interim statements** — an untyped envelope; most carry no financial statements and the ones that do are indistinguishable without opening them (hundreds of MB per filer) | Foreign quarterly reporters that publish interims only via 6-K become `STALE` and leave current rankings. All 191 current-rank-eligible foreign companies are annual filers. 6-K exhibit discovery is not to be implemented without being asked. |
| **us-gaap capital expenditure basis** (`KNOWN_CAPEX_BASIS_LIMITATION`) | 7 affected names, none in the Top 100, maximum score sensitivity 0.04 points. Normalisation unchanged. |
| **Filing-instance bandwidth** | 458.7 MB of 613.2 MB total traffic; median instance 5.45 MB, maximum 49.6 MB. Where it fails it fails safely — CGAU kept its 2023 history. |
| **Annual-only period semantics** | TSM, ASML, SAP and NVO remain unrankable. Currency is no longer their blocker; period semantics is. They get a correct currency, a stated exclusion reason and an indexed 20-F — not a score. |
| **No ADR ratio lookup** | Cover-page share counts on 20-F/40-F are not read; `market_cap_source` falls to `PROVIDER` or `UNKNOWN`. |
| **Single-exchange price bars** | The IEX share of consolidated volume ranged from 0.006% to 12% across 259 companies, so the $1M gate cannot be applied to stored bars. `update-eligibility-volume` reads the consolidated tape instead. |
| **No authentication on the job endpoints** | They spend money. Only reasonable because the API binds localhost. |
| **FDIC-only bank fundamentals** (historical layer) | First Republic and Signature file under §12(i) with the FDIC, so EDGAR `companyfacts` returns 404. Their prices are free via Alpaca; their fundamentals are recorded as `NO_EDGAR_FUNDAMENTALS` and left UNKNOWN, never guessed. |
| **Pre-2009 XBRL** (historical layer) | XBRL was not mandatory before 2009, so a filer that delisted earlier (Lehman) has no machine-readable fundamentals. Recorded, not reconstructed. |
| **Pre-~2021 free price history** (historical layer) | Alpaca's free IEX feed does not reach back past roughly 2021 (AAPL 2016, Sprint 2020, Sears 2018 all return no bars), so an older delisting is `NO_FREE_PRICE_HISTORY` and cannot be given a point-in-time market cap. |
| **Receivership terminal value** | No corporate-action feed files a receivership, so a failed bank's terminal equity value is UNKNOWN unless asserted from evidence outside the feed — never a guessed 0 or -100%. |
| **Fails-to-deliver silence** (identity layer) | A security whose deliveries never failed is absent from the SEC's record entirely, so its identity is unrecoverable from that source. Produces `UNRESOLVED`, and `EvidenceVerdict` has no `ABSENT` member, so no code path can turn it into a claim of non-existence. |
| **Pre-2008-09-16 threshold censoring** (identity layer) | Before that date the fails file carried only failures above a reporting minimum, so an absence over an earlier window is uninformative for a second, independent reason. Flagged as `threshold_censored`, not corrected. |
| **No corporate action in the fails record** (identity layer) | A CUSIP change within one issuer cannot be attributed to a reverse split, a re-incorporation or a class re-designation. Recorded as `CUSIP_CHANGE` at `INFERRED` confidence; `REVERSE_SPLIT` is only ever `ASSERTED` from evidence outside the feed. |
| **A re-incorporation is indistinguishable from a change of hands** (listing layer) | Because of the row above, PIT attribution reads KKR & Co. L.P. becoming KKR & Co. Inc. as two occupants, exactly as it reads Randgold becoming Barrick. Of the 75 symbols whose `ELIGIBLE` months moved in Phase 9K.11, 32 carry plainly different issuer names (`GOLD`: Randgold → Barrick → GOLD.com; `PARA`: Premara → Paramount → Banzai; `WELL`: Capsalus → Welltower) and 43 share a name stem and are likely re-incorporations or class changes (`KKR`, `CCL`). The layer takes the conservative side and reports `REUSED` for both, costing 301 `ELIGIBLE` rows — 3.3% of 9,256. Separating them needs a corporate-action source the free record does not carry. |
| **No venue on an FTD-derived interval** (identity layer) | The file carries no exchange, so `security_identifiers.venue` is NULL. "Not established", never a default. |
| ~~**No historical universe membership**~~ | **Addressed by Phase 9C**, which added a dual-identity `universe_membership` layer. 51 month-ends now reconstruct with a median of 4,953 members, 199 of them delisted. What remains is that no member verdict can be `OBSERVED` — see below. |
| **No `OBSERVED` membership is achievable** (membership layer) | Exchange, security type and sector are undated everywhere in this system, so every one of the 6,049 member intervals is `INFERRED` and names the attributes it carried back. Closing this needs a dated listing source — SEC Form 8-A/25 filings carry one — which is a separate ingestion, not a code change. |
| **A contaminated live row carries one continuous membership** (membership layer) | Where a `companies` row holds two issuers' history, its membership interval spans both: the layer separates the two *identities* correctly but cannot un-mix a row that is itself two companies. 1,486 live rows carry more than one CUSIP issuer under their ticker; 1,458 hold a single open `MEMBER` interval. Repairing this means splitting prices, filings and scores across two issuers, which is a separately-authorised data operation. |
| **Free price history begins ~2020-07** (outcome layer) | Alpaca's free IEX feed serves nothing earlier, so a security that delisted before then has an identity and a terminal event but no measurable outcome. 581 of 2,745 backfilled securities returned no bars at all. |
| **All-stock mergers state no consideration** (outcome layer) | PXD's exit into XOM records an exchange ratio and no cash, so its terminal value is `UNKNOWN`. Computing one would need the acquirer's price on the effective date, which is a second security's history and a deliberate later step. |

---

## Completed experiments

Measured runs, kept because they are the evidence behind decisions above.

### B2.5 control-plane separation — 2026-09-15

- `tests/integration/test_control_plane.py`: **35 passed**. Artifact root
  removed with the control root intact — manifest entry, checkpoint (stage
  `COMPLETE`), target manifest and fingerprint all read back; every artifact
  operation raises `StorageUnavailableError` and the fake SEC client records no
  further request. Control root removed with the artifacts intact —
  `Manifest.entries`, `load_checkpoint`, `harvest_artifacts` and
  `open_control_root` all raise `ControlPlaneUnavailableError`, no directory is
  recreated, no request is spent.
- The manifest line written under the separated layout equals the line the
  colocated layout writes, `recorded_at` aside; neither root's path appears in
  it.
- A memory backend passes `preflight_corpus` with a one-petabyte reserve: no
  universal disk-capacity requirement remains.
- Every pre-B2.5 evidence suite passed unmodified: 613 tests across the storage,
  harvest, offline, expand, sweep, CLI, targets, plan, dedup, reachability,
  budget, manifest-scale and pagination files.

### B2.4 refactor verification — 2026-09-14

- Final focused storage/evidence pass: **417 passed**, exit 0. Suites:
  `test_storage_contract`, `test_storage_local`, `test_storage_neutral`,
  `test_storage_equivalence`, and `test_sec_evidence_{storage,harvest,offline,expand,sweep}`.
- Pre-commit `make check`: **3,234 passed, 0 failures, 88.22% coverage**, exit 0;
  lint, formatting and strict mypy also passed. `make docs-build` passed.
- Runtime filesystem audit: **0 unjustified artifact leaks**, classified in
  [Storage layout](storage-layout.md#filesystem-search-audit).
- Read-only database SHA-256 checks matched the supplied baseline:
  - `compounder_radar.db`: `02a6660b81d96a948a1a5b8729290e301cbb9ab26155e5f07c3cf0b6beef7540`
  - `stage9k.db`: `20a77d24a4084caa49af723d488a1c9319562b32a34969b28780adfe898486e4`

### Phase 7F rollout — validated Stage C, against a copy

| | |
| --- | --- |
| Foreign cohort | 762 |
| Fundamentals recovered | 289 |
| Numerical scores | 206 |
| **Current-rank eligible** | **191** |
| Stale scored but excluded | 15 |
| Insufficient | 82 |
| Not eligible | 461 |
| Unsupported sector | 13 |
| Provider unavailable | 0 |

Cadence of the current-rank eligible: 191 `ANNUAL`, 0 `SEMIANNUAL`, 0
`QUARTERLY`. Taxonomy: 147 IFRS, 44 us-gaap.

Foreign entrants: **GFI (#11)** and **ERO (#19)** in the Top 20; DLO, ARIS, CYD,
KGC, IAG in the Top 50; TBBB, B, AU, MTA in the Top 100. Every Top-20 and Top-50
entrant was reconciled against its filed statements.

**CGAU keeps its 73.48 and leaves the ranking** — its newest statement is from
2023, against a 2026 market capitalisation.

### V1.1 → V1.2 parity

1,929 companies scored under both versions with **zero** numeric differences.
Every scoring file is byte-unchanged since the last V1.1 commit.

### Three defects the rollout exposed

Each was caught by running real companies, not by reasoning:

- The composite provider replaced the EDGAR profile wholesale, erasing the
  reporting currency only EDGAR knew. The profile is now merged field by field.
- The enrichment pass re-scored without an FX resolver, turning a complete
  `PRELIMINARY` score into a `FINAL` one missing every currency-sensitive metric
  — verification made the score *worse* for exactly the companies it verified.
- A deposit-funded bank (Kaspi.kz) scored 74.78 and ranked twenty-first because
  both available classifications called it a technology company.
  `domain.statements` now reads the statements instead.

### Liquidity pass economics

Running `update-eligibility-volume` before the metered pass took provider
requests for the 762-company cohort from **762 to 353**.

---

## Regression requirements

The B2 checkpoint adds `test_storage_contract.py`, `test_storage_local.py`,
`test_storage_equivalence.py` and `test_storage_neutral.py`: local/memory backend
equivalence, strict absence-versus-unavailability, unsafe-key refusal, parser
failure propagation, opaque ETags, reconciliation counters, and batch-local
sibling orphan detection. B2.5 adds `test_control_plane.py`: control-root
resolution without creation, unwritten-versus-gone for manifests and
checkpoints, the separated-root lifecycle, both failure-domain halves, the
legacy colocated layout, and backend-neutral preflight. Existing SEC evidence
regression suites remain gates.

These must keep passing. Each one encodes a decision that cost something to
learn.

| Test | Protects |
| --- | --- |
| `tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows` | the 1.6-million-row deletion never recurs |
| `tests/integration/test_migrations.py::test_the_migrated_schema_matches_the_models` | migrations and models cannot drift |
| `tests/integration/test_scoring_pipeline.py::test_a_current_company_scores_exactly_what_v1_1_scored` | V1.2 moved no number — the golden value was measured against the last V1.1 commit |
| `tests/integration/test_scoring_pipeline.py::test_every_current_ranking_view_excludes_a_stale_score` | the V1.2 policy applies to all four views |
| `tests/integration/test_scoring_pipeline.py::test_a_stale_company_keeps_its_score_and_leaves_the_ranking` | a stale score is withheld from rankings, not deleted |
| `tests/integration/test_scoring_pipeline.py::test_a_snapshot_predating_freshness_still_ranks` | pre-`0020` rows are not retroactively stale |
| `tests/integration/test_scoring_pipeline.py::test_a_bank_stays_in_the_database_and_out_of_the_ranking` | unsupported sectors are recorded, not dropped |
| `packages/domain/tests/test_audit_regressions.py` | ticker normalisation, bar validation, boundary inclusivity, consolidated-volume preference |
| `packages/domain/tests/test_scoring_v11_guards.py` | the three V1.1 guards, their stacking, and component maximums |
| `packages/domain/tests/test_period_cadence.py` | staleness scales with cadence; `UNKNOWN` is never reported stale |
| `packages/domain/tests/test_scoring_currency_safety.py` | an enterprise value without a conversion is rejected outright |
| `packages/domain/tests/test_statements.py` | a bank is recognised from its statements; a manufacturer with a finance arm is not |
| `packages/deep-research/tests/test_brief.py` | fingerprints are reproducible across reassembly and refetch |
| `tests/integration/test_pit_capture.py::test_a_completed_day_cannot_be_changed_by_a_later_score` | hindsight cannot rewrite the record — the property the whole phase rests on |
| `tests/integration/test_pit_capture.py::test_a_partial_run_leaves_no_day_behind` | a crashed capture leaves no half-day marked good |
| `tests/integration/test_migrations.py::test_the_identity_migration_preserves_a_populated_historical_security` | `0027` rebuilds a parent with three cascading children and deletes none of them |
| `packages/domain/tests/test_identity.py::TestATickerReuseIsADifferentSecurity` | two issuers under one symbol are never spliced into one history |
| `packages/domain/tests/test_identity.py::TestAReverseSplitPreservesTheSecurity` | a new CUSIP is a new interval, never a new security — and the retired one stops answering |
| `packages/domain/tests/test_identity.py::TestEvidenceAndItsAbsence` | `EvidenceVerdict` has no `ABSENT`: silence in the fails record can never become a claim |
| `tests/integration/test_security_identity.py::TestHistoricalSecuritiesStayOutOfTheLiveUniverse` | resolving a delisted symbol writes no `companies` row |
| `tests/integration/test_security_identity.py::TestForwardReturnsCarryExactlyOneIdentity` | a measurement names one identity, never both and never neither |
| `tests/integration/test_security_identity.py::TestDuplicateMeasurementsAreRefusedPerIdentityKind` | the partial unique indexes bite for each identity kind independently |
| `tests/integration/test_historical_outcomes.py::TestASecurityWithNoCompanyRow::test_a_historical_security_outlives_the_deletion_of_its_company_row` | the survivorship test — the live row is deleted and the outcome is unchanged |
| `tests/integration/test_historical_outcomes.py::TestTheMixedUniverseQuery` | survivors and delisted names come back from one query, no union and no flag |
| `tests/integration/test_historical_outcomes.py::TestIdentityIsNeverTheTicker` | two occupants of one symbol keep separate prices, outcomes and terminal events |
| `tests/integration/test_historical_outcomes.py::TestTerminalOutcomes::test_a_collapsing_price_never_becomes_a_terminal_value` | a falling tape is never read as a settlement |
| `tests/integration/test_historical_outcomes.py::TestTheScoreBoundary` | outcome availability and score availability stay separate concepts |
| `tests/integration/test_historical_outcomes.py::TestNoLookAhead` | a later interval, action or delisting cannot reach the decision-date state |
| `tests/integration/test_universe_membership.py::TestSurvivorsAndExits::test_membership_survives_the_deletion_of_every_company_row` | historical membership outlives the emptying of `companies` |
| `tests/integration/test_universe_membership.py::TestPricePresenceIsNotMembership` | Rule B — a priced security with no venue is `UNKNOWN`, never a member |
| `tests/integration/test_universe_membership.py::TestInferenceIsNeverDressedAsObservation` | no member verdict is ever written as observed, and each names what it carried |
| `tests/integration/test_universe_membership.py::TestFutureInformationCannotLeakBackward` | a 2024 listing is not a 2022 member; a future exit cannot rewrite the past |
| `tests/integration/test_pit_attribution.py::TestBedBathAndBeyondAreTwoOccupants` | the live holder of a reused symbol never owns its predecessor's months, and the predecessor never owns the successor's |
| `tests/integration/test_pit_attribution.py::TestALiveCompanyIsAClaimantNotADefault` | a stored exit date alone never moves a symbol off the live roster — the 127 same-business rows stay put |
| `tests/integration/test_pit_attribution.py::TestAPredecessorsFilingNeverClassifiesItsSuccessor` | the mirror defect: an old cover page cannot classify a later occupant's months |
| `tests/unit/test_sec_evidence_storage.py::TestTheRootIsNeverCreated` | an unmounted evidence root raises and leaves no directory on the boot disk |
| `tests/unit/test_sec_evidence_storage.py::TestAnArtifactIsWholeOrAbsent` | an interrupted write leaves nothing, and a destination outside the root is refused |
| `tests/integration/test_sec_evidence_harvest.py::TestAnArtifactIsNeverFetchedTwice` | a verified artifact is reused; a corrupt or unprovenanced one is re-fetched |
| `tests/integration/test_sec_evidence_harvest.py::TestBudgetsStopTheRunCleanly` | request and byte limits stop the run and the next run resumes |
| `tests/integration/test_universe_membership.py::TestIdentityIsNeverTheTicker` | ticker reuse gives two independent membership histories |
| `tests/integration/test_pit_capture.py::test_the_first_capture_records_a_baseline_not_a_dated_event` | no membership date is ever fabricated |
| `tests/integration/test_filing_observations.py::test_a_restatement_appends_and_the_original_survives` | a revision never destroys the figure it replaced |
| `tests/integration/test_filing_observations.py::test_period_end_is_never_used_as_availability` | the classic look-ahead trap stays shut |
| `packages/data-access/tests/test_pit_repositories.py` | immutability, append-only writes, and `filed_date` selection |
| `packages/domain/tests/test_features.py` | a named window used exactly its named length |
| `tests/integration/test_market_signals.py::test_a_stored_signal_cannot_change_when_operational_tables_move` | a derived signal cannot drift when the operational tables are rewritten |
| `tests/integration/test_market_signals.py::test_deriving_signals_cannot_alter_any_score` | the market layer is inert with respect to CompounderScore |
| `tests/integration/test_market_signals.py::test_a_new_version_appends_beside_the_old_one` | changing the rules never restates what was said at the time |
| `packages/domain/tests/test_signals.py` | percentile determinism, multi-label setups, and the tri-state verdict |
| `tests/integration/test_pit_metrics.py::test_a_restatement_is_invisible_until_it_is_filed` | the worked restatement: January sees nothing, March the original, May the revision |
| `packages/domain/tests/test_pricing.py::test_anchoring_on_the_valuation_date_would_undervalue_by_the_factor` | the wrong anchor is named, so reintroducing it fails rather than passes quietly |
| `packages/domain/tests/test_pricing.py::test_the_window_between_a_split_and_the_next_filing` | the two months where the newest filed count is on the old basis |
| `packages/domain/tests/test_pricing.py::test_the_valuation_is_continuous_across_the_split` | a company's size does not change because its shares were divided |
| `packages/domain/tests/test_outcomes.py::test_the_observation_session_is_never_the_entry` | a snapshot computable at a close is never filled at that close |
| `packages/domain/tests/test_outcomes.py::test_an_interrupted_outcome_has_no_return` | neither -100% nor 0% is assumed for a security that stopped trading |
| `packages/domain/tests/test_outcomes.py::test_a_one_day_hole_is_unpriced_not_interrupted` | our missing bars are never counted as delistings |
| `tests/integration/test_pit_outcomes.py::test_a_delisting_is_stored_without_a_company_row` | the delisting registry never pollutes the investable set |
| `tests/integration/test_pit_outcomes.py::test_without_a_benchmark_there_is_no_calendar_and_nothing_is_measured` | a security's own bars never stand in for the market's sessions |
| `tests/integration/test_pit_metrics.py::test_the_operational_table_cannot_influence_a_historical_metric` | a historical metric cannot drift when `financial_snapshots` is overwritten |
| `tests/integration/test_pit_metrics.py::test_a_metric_waits_for_its_slowest_input` | a multi-concept metric uses only facts available at the date |
| `tests/integration/test_pit_metrics.py::test_two_decision_dates_see_two_different_companies` | the end-to-end temporal scenario |
| `packages/api-clients/tests/test_edgar_observations.py::test_rebuilding_a_document_reproduces_the_same_periods` | the point-in-time path normalises identically to the live one |
| `packages/domain/tests/test_survivorship.py` | the delisting-reason taxonomy, the as-of membership rule, and the readiness gaps |
| `tests/integration/test_backtest_readiness.py::test_a_security_delisted_before_the_date_is_not_a_member` | a delisted name leaves the past universe — survivorship in its pure form |
| `tests/integration/test_backtest_readiness.py::test_a_filing_becomes_visible_only_after_it_was_filed` | the reconstructed score reads no fundamental before its filing date |
| `tests/integration/test_backtest_readiness.py::test_a_delisted_only_security_is_answerable_through_its_fate` | a security with no company row is answered through the registry, never dropped |
| `tests/integration/test_backtest_readiness.py::test_signals_backfill_for_a_past_date_from_stored_prices` | signal history is backfillable and point-in-time |
| `tests/integration/test_historical_securities.py::test_a_delisted_security_reconstructs_with_no_companies_row` | a delisted name is scoreable with no `companies` row |
| `tests/integration/test_historical_securities.py::test_a_namesake_company_cannot_change_the_historical_market_cap` | a reused ticker's live company cannot leak into a historical score |
| `tests/integration/test_historical_securities.py::test_a_future_split_does_not_leak_a_share_count_backward` | a split after the date corrects the price, never the count |
| `tests/integration/test_historical_securities.py::test_the_historical_path_scores_identically_to_the_company_path` | the historical path reuses the frozen scorer, not a second one |
| `tests/integration/test_historical_securities.py::test_a_missing_terminal_value_stays_none_never_zero` | an unpriced receivership stays UNKNOWN |

### The gate

`make check` — lint, format check, mypy strict, pytest with coverage failing
under **80%**. It is what CI runs; run it rather than the individual tools.
`make check-web` is separate and needs Node. Touching `frontend/` means both.

---

## Next phase

**B2.6** follows this checkpoint — either the Backblaze B2 configuration and
credentials boundary, or the `B2EvidenceStorage` implementation, decided
separately. Neither has been started. Relocating the BEK corpus's control files
into a `SEC_CONTROL_DIR`, mirroring control metadata remotely, evidence
migration and Plan A3 remain outside this checkpoint's authorization.

Two pieces of work are *identified* but not authorised, and neither is to be
started without being asked:

1. **Promoting the Phase 7F cohort to the primary database.** The code is done;
   the data operation is not. It means applying the migrations and running the
   ordinary passes against `compounder_radar.db`.
2. **Phase 7D — period semantics.** Annual and semiannual reporting periods as
   first-class citizens. This is what still keeps TSM, ASML, SAP and NVO
   unrankable.
3. **Point-in-time market capitalisation.** Nothing records what it was on a past
   date, so any ratio against it is point-in-time on its fundamental side only.
   Share counts are now in the observation record, so it is derivable — but only
   once corporate actions exist, because stored closes are split-adjusted.
4. **Filing acceptance time.** `filed_date` is a date; 60.3% of same-day filings
   are accepted after the close. See the Phase 8E audit.

A longer-term direction document exists at `docs/reference/roadmap.md`. It is
aspiration, not commitment: nothing in it is authorised, scoped or scheduled, and
nothing in it overrides *Things explicitly not to change*.

---

## Things explicitly not to change

Not a backlog. These exclusions are deliberate.

**Do not build without being asked:**

- a second LLM provider, or a second execution path for a job
- scheduled or automatic deep research, report diffing, alerts, notifications
- charts, portfolios, positions, price targets
- authentication or deployment config
- filing exhibits, 6-K exhibit discovery
- converting *statements* into another currency
- annual or semiannual period semantics, ADR ratio lookup
- reading filing documents to recover figures the companyfacts API omits

**Do not touch:**

- B2.4 and B2.5 leave `COMPOUNDER_V1_2`, `MARKET_SIGNALS_V1`,
  `ResearchUniverseMonth`, PIT identity semantics, Plan A3, forward returns,
  price history and database schema frozen. No BEK access, credentials, corpus
  migration or SDK additions.
- any scoring file, without a new `score_version` and a doc change in the same
  commit
- a Phase 3 enum or class — `deep-research` imports them, never extends them
- `uv.lock` by hand — use `uv add` / `uv remove`
- `.github/workflows/` — ask first
- the frontend's location — it stays in this repository
- `compounder_radar.db`, for any migration verification

**Never:**

- read `os.environ` directly — add a typed field to `config.py` and a line in
  `.env.example`
- commit a secret, in code, tests, fixtures or docs
- leave a failing or skipped test without saying so explicitly
- let a calculated figure become indistinguishable from a supplied one

---

## Latest checkpoint

| | |
| --- | --- |
| Commit | The B2.5 commit containing this checkpoint, `refactor: separate SEC evidence control plane`; parent `1b56b86` (B2.2–B2.4, `refactor: decouple SEC evidence artifacts from local storage`) |
| Branch | `feat/phase-b2-storage-abstraction` |
| Migration head | `0031` — the research-universe layer (9K.7). Nothing since 9K.11 added one. |
| Score version | `COMPOUNDER_V1_2` (unchanged by anything in Phase 8, 9A, 9B or 9C) |
| Feature version | `MARKET_FEATURES_V1` |
| Signal version | `MARKET_SIGNALS_V1` |
| Primary database | untouched by 8A–8J and 9A–9L.5; all work ran against staging copies |
| Checkpoint date | 2026-09-15 |
