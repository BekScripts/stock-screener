---
type: reference
title: A3 planning inputs
---

# A3 planning inputs

What Plan A3 is computed from, where each input comes from, which dated rule
governs it, and how the assembled inputs are frozen so the planner can be run
from the frozen package alone. Built by `stock-screener evidence
build-a3-inputs` (Phase B2.13); consumed by the planner in `sec_evidence/plan.py`,
which this phase does not run and does not change.

## The contract

`plan.PlanningInputs` has five fields. The planner's selection functions read
`candidates` only; `gaps`, `form_yield`, `form_bytes` and `held` are what the
candidates were computed from, frozen beside them so the computation is
auditable.

| Field | Type | Meaning | Source | Dated rule | Determinism | Survives |
| --- | --- | --- | --- | --- | --- | --- |
| `gaps` | `tuple[Gap, ...]` | Every `UNKNOWN` universe month that held evidence does not support, classed by what could close it | `research_universe_months` (frozen) × V2 index cover rows joined to identities × the frozen resolution × the V2 catalogue | planning rule (below) | ordered `(kind, id, month_end)` | yes |
| `Gap.identity` | `(str, int)` | `("COMPANY", company_id)` or `("HISTORICAL", historical_security_id)` | the universe row | — | — | yes |
| `Gap.cik` | `int \| None` | The issuer the frozen resolution assigns | `filer_resolution_v1.jsonl` | none: resolution is per generation, never per date | frozen file, digest-checked | yes |
| `Gap.symbol` | `str` | The symbol the universe row recorded for that month | the universe row | — | — | yes |
| `Gap.month_end` | `date` | The planner's key date: `month_end(observation_month)` | derived | — | — | yes |
| `Gap.previous_observation`, `days_since`, `next_observation` | `date \| None`, `int \| None` | Nearest common-stock cover rows joined to the identity, before and after the month-end | V2 index `securities` (COMMON_STOCK, symbol present) through `join_symbol` at the row's `evidence_date` | `evidence_date` is the SEC acceptance date | join is a pure function of the identity tables | yes |
| `Gap.identity_state` | `str` | How the symbol attaches to the identity at the month-end: `EXACT`, `AMBIGUOUS`, `UNRESOLVED`, or `OTHER_OCCUPANT` when the join names another security | `join_symbol(symbol, month_end)` | dated join, occupancy-aware | — | yes |
| `Gap.universe_state` | `str` | What the frozen row recorded (`UNKNOWN` for every gap) | the universe row | — | — | yes |
| `Gap.gap_class` | `GapClass` | `G1_SOLVABLE`, `G2_NO_CIK`, `G3_AMBIGUOUS`, `G4_NO_CANDIDATE`, `G5_OTHER` | derived (table below) | — | — | yes |
| `candidates` | `tuple[Candidate, ...]` | Every catalogue filing dated inside the research window, `WINDOW_START <= filing_date <= WINDOW_END` (both inclusive), and not held — `CANDIDATE_WINDOW_DEFINITION` `HISTORICAL_9L_V1` | V2 index `catalogue` minus the held set | acquisition scope: the window; then the planning rule on `filing_date` for what it may cover | ordered `(cik, accession)` | yes |
| `Candidate.cik`, `accession` | `int`, `str` | The filing identity, `(cik, accession)` | the catalogue row | — | — | yes |
| `Candidate.form` | `str` | One of `FILING_FORMS`; the catalogue holds nothing else | the catalogue row | — | — | yes |
| `Candidate.filing_date` | `date` | The catalogue filing date. The acceptance stamp is planning metadata the planner does not rely on for an undownloaded document | the catalogue row | planning rule | — | yes |
| `Candidate.estimated_bytes` | `int` | The measured mean size of held documents of that form, rounded | V2 index `filings.bytes` | — | integers in, one rounding | yes |
| `Candidate.yield_rate` | `float` | Share of held documents of that form with at least one COMMON_STOCK cover row carrying a symbol | V2 index | — | integer ratio; JSON `repr` round-trips exactly | yes |
| `Candidate.covers` | `frozenset[(identity, month_end)]` | The solvable gaps the filing could support: for every identity the issuer's reach credits, every `supported_months(filing_date)` that is an open `G1_SOLVABLE` gap | derived | planning rule | serialised sorted | yes |
| `form_yield` | `Mapping[str, float]` | `yield_rate` per form | V2 index | — | — | yes |
| `form_bytes` | `Mapping[str, Mapping[str, float \| int]]` | `count`, `usable_common`, `total`, `mean`, `median`, `min`, `max` per form | V2 index | — | — | yes |
| `held` | `frozenset[(int, str)]` | `(cik, accession)` of every trusted V2 filing | the V2 manifest through `held_from_manifest` | — | ordered on write | yes |

No field is populated from anything the sources do not hold. The one historical
input that does not survive — the V1 held state, including the 800- and
1,008-filing validation batches — is deliberately not reconstructed: the V2
held set is the V2 baseline and nothing else.

### Gap classes

| Class | Assigned when | Actionable |
| --- | --- | --- |
| `G2_NO_CIK` | the frozen resolution gives the identity no CIK | no |
| `G5_OTHER` | the month has no listing observation (no filing can admit it), or the issuer's reach is `MULTI_CLASS_UNASSIGNED` | no |
| `G3_AMBIGUOUS` | the symbol does not attach `EXACT` to the identity at the month-end; or the issuer's reach is `IDENTITY_AMBIGUOUS`; or the issuer is reached but this identity is not credited (known non-common, or unclassed under `SAFE_COMMON_CLASS`) | no |
| `G1_SOLVABLE` | the identity is credited and an unheld catalogue filing's `supported_months` reach the month-end | yes |
| `G4_NO_CANDIDATE` | the identity is credited and no unheld catalogue filing reaches the month-end | no |

Checked in that order. Only `G1_SOLVABLE` keys enter any candidate's `covers`.

## Which timestamp controls what

| Question | Rule | Where it lives |
| --- | --- | --- |
| Classifying a universe month (`ResearchUniverseMonth.state`) | a filing may classify month `M` only if accepted on or before the first day of `M` and within `CLASS_CARRY_FORWARD_DAYS` (90) of it, inside the same occupancy run | `listing.class_for_observation`, `domain.classify_month` (Phase 9K.7). **Read as frozen input; never recomputed here.** |
| Held evidence supporting a month (`COVERED`, `previous_observation`) | a cover row dated `evidence_date` supports month-end `M` when `evidence_date <= M` and `M - evidence_date <= CARRY_FORWARD_DAYS` (90); `evidence_date` is the SEC acceptance date (`ACCEPTED` for all 32,413 V2 filings) | `plan.supported_months` semantics, `extract.evidence_date_for` (Phase 9L) |
| Security-class evidence | the cover row's `security_class` as parsed (`domain.classify_security`), attached to an identity by `join_symbol(symbol, evidence_date)`; only `COMMON_STOCK` rows with a symbol count as observations | `extract`, `join` |
| Reachability (which identities a filing may be credited with) | `build_reachability` over the snapshot of joined observations: not date-aware by design — a planning heuristic that predicts what an unread filing may support and never produces a stored PIT verdict | `reachability` (Phase 9L.4) |
| Candidate eligibility | `filing_date` inside `[WINDOW_START, WINDOW_END]`, both ends inclusive (`inputs.in_candidate_window`); `(cik, accession)` not held. Acquisition scope, not point-in-time: a December 2021 filing may support January 2022 under the planning rule and is still not a candidate | `plan` constants; `inputs` |
| Acceptance fallback | none is needed: every V2 filing carries its own catalogue stamp; `stage9k.filing_acceptance` is untouched and unread | — |

The classification rule and the planning rule differ by up to one month at a
boundary: a filing accepted on the 15th supports that month-end under the
planning rule and classifies the following month under the classification
rule. The assembly reports the difference (`covered_under_classification_rule`)
and does not reconcile it; reconciling it would change the planner.

## The package

`<SEC_CONTROL_DIR>/plans/generation_v2/a3_inputs_r1/` (`A3_INPUTS_DIRECTORY`),
mirrored file for file as digest-addressed snapshots under
`control/<generation>/a3-inputs-r1/`. The first package, `a3_inputs/`
(`B2.13/1`, mirrored under `a3-inputs/`), is superseded and kept: it carries an
additive `SUPERSEDED.json`, and the new package's `supersession.json` names it.
The manifest and marker record `inputs_version`, `candidate_window_definition`,
the window's bounds and semantics, `assembly_sha256` (the assembly module's
digest) and `supersedes`.

| File | Rows / content | Order |
| --- | --- | --- |
| `pit_coverage_v2.jsonl` | one row per frozen universe month: identity, CIK, symbol, month, frozen state and class, frozen `class_source_accession` / `class_evidence_as_of`, listing observed, `identity_state`, issuer reach, `status` (`DECIDED` / `COVERED` / `OPEN`), gap class, nearest observations, source CIK and accession, reason | `(kind, identity_id, observation_month)` |
| `reachability_v2.jsonl` | one row per tracked identity under a resolved issuer: reach kind, identity class, credited, tracked count, joined observation counts | `(cik, kind, identity_id)` |
| `coverage_gaps_v2.jsonl` | the `Gap` rows | `(kind, identity_id, month_end)` |
| `candidates_v2.jsonl` | the `Candidate` rows, `covers` as sorted `[kind, id, month_end]` triples | `(cik, accession)` |
| `held_v2.jsonl` | `(cik, accession)` | `(cik, accession)` |
| `form_yield_v2.json`, `form_bytes_v2.json` | per form | by form |
| `conflict_report_v2.json` | the frozen-verdict versus V2-evidence diagnostic | — |
| `*.sha256`, `*.summary.json` | one digest per component; one summary per JSONL | — |
| `planning_inputs_manifest.json` | the bindings, every component digest, the package digest, counts, the assembly summary and timings | — |
| `a3_inputs_complete.json` | the deterministic marker: bindings, component digests, package digest, counts — no timestamp | — |

Every JSONL row is compact JSON with sorted keys; the package digest is SHA-256
over `<sha256>  <name>` lines in `COMPONENTS` order. `load_planning_inputs`
recomputes every component digest, checks the package digest, and — given the
bindings the caller has proven against the live sources — refuses a package
frozen against any other `stage9k` digest, universe digest, resolution digest,
manifest digest, index digest or semantic fingerprint.

## The live run — 2026-09-18

Bound to `stage9k.db` `20a77d24…` (universe `b8eaed3e…`, 8,919 identities),
resolution `161429f5…`, manifest `13722e02…`, index `3deb46b7…` and its five
fingerprints, all verified before the assembly and the derived-index marker
verified against its mirrored copy; both database digests unchanged after.
Two assemblies in one process and a second invocation into another directory
produced identical components and the same package digest
`83680eadaa06317c10a0a26e68f5547a022207dc1bc1e5c1283a8427b5db3ce5`; only the
manifest's `timings` differ between invocations. 12.8 s per assembly.

| | |
| --- | --- |
| universe months | 268,780 over 6,652 identities and 55 months |
| status | `DECIDED` 10,317 · `COVERED` 16,194 · `OPEN` 242,269 |
| gaps | `G1_SOLVABLE` 166,075 · `G2_NO_CIK` 28,203 · `G3_AMBIGUOUS` 12,085 · `G4_NO_CANDIDATE` 32,904 · `G5_OTHER` 3,002 |
| raw `UNKNOWN` floor | 76,194 (historical 76,029 — diagnostic only) |
| observations | 42,204 symbol-bearing cover rows: 31,464 `EXACT`, 819 `AMBIGUOUS`, 9,921 `UNRESOLVED`; 28,849 common-stock rows joined |
| issuer reach | `SAFE_SINGLE` 5,471 · `SAFE_COMMON_CLASS` 201 · `MULTI_CLASS_UNASSIGNED` 21 · `IDENTITY_AMBIGUOUS` 40 over 6,048 tracked identities, 5,720 credited |
| candidates | 279,772 of 301,880 window filings (22,108 held); 235,630 carry a cover key; 657,826 cover keys over exactly the 166,075 solvable gaps; 324 co-registrant copies kept distinct |
| form yield | 8-K 0.944 · 10-Q 0.931 · 10-K 0.883 · 40-F 0.718 · 20-F/A 0.653 · 40-F/A 0.619 · 20-F 0.588 |
| conflicts | of 54,998 frozen `class_source_accession` rows (compared in the dashed form): 5,333 held in V2 and joining to the same identity with the same acceptance date, 785 held but not joining, 44,625 catalogued but not held by the six-per-security baseline, 4,255 absent from the V2 catalogue; 0 timestamp disagreements |
| the 135 declined map matches | 264 universe rows, 252 open, all `G2_NO_CIK` |
| mirror | 25 files as 50 objects, all `MIRRORED`, every one read back equal; no database or index mirrored |

## Regenerating Plan A3 (B2.14)

`stock-screener evidence regenerate-a3` loads the frozen package through
`load_planning_inputs` — bound to the manifest, index, fingerprints and
resolution on disk, and to the database when one is named — and runs the
planner over `inputs.candidates`. Nothing is reassembled; the command's tests
run it with the database, the network and the assembly disabled.

### The historical configuration

| | |
| --- | --- |
| entrypoint | `plan.plan_coverage_first(candidates)` — Plan A |
| score | coverage: marginal month-ends, then form yield, then size, then filing date, then `(cik, accession)` |
| limit | none — run to exhaustion, which 9L.2 showed is the minimum temporal cover |
| minimum marginal | 1 |
| writer | `plan.write_plan` — `expansion_plan_v1.{csv,jsonl,sha256}`, JSONL rows as `json.dumps(asdict(target), sort_keys=True)` + newline, digest SHA-256 over the JSONL bytes |
| row stamp | `plan_version` `v1` (the writer's constant, unchanged since 9L.4's "stamping defect" note) |

### What the historical digest can and cannot say

`53e3f0ca…` (61,941 rows) is SHA-256 over the historical `expansion_plan_v3`
JSONL bytes, produced by the writer above; that much the 9L.2 and 9L.4 records
establish, and the writer survives byte-identically from the pre-B2 checkpoint
`13d586b`. What cannot be established is that the writer and the stamped fields
(`plan_version`, `selection_strategy`, the `reason` text) were identical at the
moment A3 was written, because `plan.py` first entered git at that checkpoint,
after 9L.5. The historical rows themselves are lost. So a digest that differs
is `BYTE_DIGEST_COMPARISON_INCONCLUSIVE` on its own; only a row-count or
identity-set difference is evidence about the selection.

### Validation

Every target must be in the frozen candidate pool, outside the frozen held set,
unique on `(cik, accession)`, of a qualifying form, carry the candidate's own
byte estimate, and cover at least one `G1_SOLVABLE` gap. The planner has no
byte budget, so none is checked. Two runs must agree on targets, bytes, metrics
and digest.

### The plan package

`<SEC_CONTROL_DIR>/plans/generation_v2/a3/`, mirrored file for file under
`control/<generation>/a3/`: `plan_a3_v2.jsonl` (the writer's bytes),
`plan_a3_v2.sha256`, the writer's own `expansion_plan_v1.{csv,jsonl,sha256}`,
`plan_a3_v2.summary.json`, `plan_a3_v2.fingerprints.json` (ordered plan,
unordered target set, ordered identity sequence, per-form, per-CIK),
`plan_a3_v2.meta.json` (package digest and bindings, algorithm file digests,
configuration, metrics, validation, determinism, comparison) and, last,
`plan_a3_v2_complete.json`.

### The live run — 2026-09-18

Package `83680ead…` re-hashed component by component and bound to the live
manifest, index, fingerprints, resolution and `stage9k.db` (universe `b8eaed3e…`
re-proven read-only; both database digests unchanged before and after).

| | V2 | historical |
| --- | --- | --- |
| targets | **62,924** | 61,941 (+983, +1.6%) |
| digest | `a3e7df1929d3160158334de7c163b09bf5e644f6a7c353a6e377b532871dcf7b` | `53e3f0ca…` — inconclusive |
| estimated download | 6,807,725,520 bytes, 6.34 GiB | ~6.6 GiB |
| safe potential months | 166,075 (every solvable gap) | 165,553 |
| expected months | 156,531.6 (0.9425 of potential) | 152,381 (0.9204) |
| raw `UNKNOWN` floor | 76,194 | 76,029 |
| expected remaining | 85,737.4 of 242,269 open | — |
| months per target | 2.639 | 2.673 |
| forms | 8-K 61,160 · 10-Q 1,249 · 10-K 378 · 20-F 97 · 20-F/A 20 · 40-F 18 · 40-F/A 2 | — |
| identities reached | 4,213 of 4,213 with a solvable gap | — |
| unique CIKs / accessions | 4,174 / 62,875 (49 co-registrant copies) | — |
| validation | all zero | — |
| determinism | run 2 identical: rows, bytes, digest, metrics | — |
| verdict | `BYTE_DIGEST_COMPARISON_INCONCLUSIVE`; the count differs, so the selection differs | — |

Where the +983 sits, from the frozen package alone: at the historical packing
of 2.673 months per target the V2 potential of 166,075 would take 62,131
targets (+190 from the 522 extra solvable months); the remaining +793 follows
from V2's lower packing, 2.639 months per target, which is a property of which
candidates exist and when they were filed — 279,772 candidates against the
historical ~269,5xx, from catalogues harvested two weeks later, with the 800
and 1,008 V1 validation filings unheld and so eligible again, and 22,108 of
the window's filings excluded as held rather than V1's 33,384. The 135
declined delisted mappings remove demand rather than add it: their 252 open
months are `G2_NO_CIK` and have no candidate in V2, where V1 resolved them.
The one-month classification/planning boundary is shared with V1's code and
contributes no delta. Yield rates are the V2 corpus's (8-K 0.944 against
0.922), which raises the expectation, not the count. None of these is a
proven cause of a specific row; the historical rows are lost.

## The forensic review (B2.14R)

Read-only, over the frozen package, the frozen plan and the V2 index; the
plan, the package and both databases were byte-identical before and after.
Outputs under `<SEC_CONTROL_DIR>/plans/generation_v2/a3/forensics/`, mirrored
file for file under `control/<generation>/a3/forensics/`.

### What survived, and when things happened

No V1 plan row or batch list survives anywhere in git, the docs, the fixtures
or the control root; the stash holds B2.9R rescue tooling only. The
comparators are the 9L.1–9L.5 aggregates recorded at checkpoint `13d586b`.
The frozen universe was built 2026-09-04 15:56 UTC and `stage9k.db` has not
been written since (proven), so V1 and V2 planned over identical gap demand.
V2 catalogues were fetched 2026-09-16/17 (proven from the manifest). V1's
baseline, batches and plan fall between 2026-08-30 and the 2026-09-07
checkpoint date (inferred; the V1 manifest is lost).

### Algorithm and configuration identity

`plan.py`, `reachability.py`, `join.py`, `coverage.py` and `sample.py` are
byte-identical to `13d586b`; `expand.py` differs only by the B2.3 storage
refactor; `inputs.py` is the B2.13 reconstruction and not historical code.
Strategy, score, limit, minimum marginal, plan stamp, form eligibility,
reachability and the order key are proven the same. **The candidate window is
proven different**: 9L.1 counted 292,866 qualifying filings "inside the
universe's own window" and 269,521 not held, and V2's catalogues give
291,623 and 269,566 under `filing_date` within `[2022-01-01, 2026-07-31]` —
within 45 of the historical pool — whereas the B2.13 rule ("any filing whose
90-day reach touches a window month-end") admits 10,257 filings from
November–December 2021 and gives 279,772.

### The ablation

| Run | Change | Candidates | Targets | vs historical | Packing | Potential | Ordered digest |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | none (frozen) | 279,772 | 62,924 | +983 | 2.639 | 166,075 | `a3e7df19…` |
| 1 | V1 window: `filing_date >= 2022-01-01` | 269,566 | 61,628 | −313 | 2.670 | 164,572 | `0efa7d03…` |
| 2, 3 | + V1 validation held set | — | not runnable: the batch lists are lost; bound ≤ 800 targets | | | | |

Run 0 minus run 1 is exactly the frozen plan's 1,347 targets filed in 2021,
less 51 replacements; every changed target belongs to one of the 3,503
issuers with a 2021 candidate, and 1,503 January–February 2022 solvable
months are unreachable under the V1 window. No combination was searched.

### Why the digest cannot be a gate

A faithful replay of the greedy selection reproduces the frozen plan and
finds that 58,707 of 62,924 selections (93%) had another live candidate tied
on all four scored components — same-day 8-Ks of different issuers, up to
323 wide — decided by `(cik, accession)`. Because candidates of different
issuers cover disjoint gaps, those ties change rank, not membership; but the
rank is in every row, so any local change to the candidate set moves the
ordered digest of every later row. With the V1 candidate set bounded but not
listed and the writer's state at A3 time unprovable, the historical digest is
a **count-only comparator**.

### Safety, independently of V1

657,826 candidate-to-gap edges, 0 future-invalid (every edge has `filing_date
<= month_end <= filing_date + 90 days`); 3 edges where the SEC index's
acceptance stamp postdates the credited month-end — index anomalies where the
filing date precedes acceptance — none selected. 24 shared accessions as 49
co-registrant rows, all with disjoint identity sets. 0 duplicates, 0 outside
the pool, 0 held, 0 invalid forms, 0 without a solvable cover, 0 wrong
estimates; every one of the 166,075 solvable gaps reached; 28 deterministic
spot samples, 0 failures.

### Verdict and the decision it leaves open

**C — an input-assembly discrepancy, not source-state drift.** The frozen V2
package admits candidates outside the historical candidate window. The
admitted filings are PIT-valid under the planner's own rule and they reach
1,503 months the historical window cannot; but B2.13 was a reconstruction of
the historical harness, and on this one definition it did not reconstruct it.
The fix is not applied here: either restrict candidates to the 9L window in
`inputs.py`, or keep the extended window as a recorded policy change with its
own inputs version — then rebuild the package and regenerate. Under the strict
window the unchanged planner produces 61,628 targets, digest `0efa7d03…`.

## The corrected package (B2.13R)

Verdict C in B2.14R became one change: `inputs.in_candidate_window` restricts
candidates to `WINDOW_START <= filing_date <= WINDOW_END`. The evidence for
the definition: `plan.py` holds the constants (`2022-01-01`, `2026-07-31`,
byte-identical since `13d586b`) and says "every qualifying filing inside that
window"; 9L.1 counted 292,866 such filings and 269,521 not held; the V2
catalogues give 291,623 and 269,566 under this rule. At the boundaries no
filing is dated 2022-01-01 or 2021-12-31, so the start is inclusive by
definition; 348 filings are dated 2026-07-31 and `supported_months` credits a
filing with its own day's month-end, so the end is inclusive by the planner's
own rule. `A3_INPUTS_VERSION` is `B2.13R/1`; a package built under any other
`candidate_window_definition` is refused on load.

### The live rebuild — 2026-09-18

Assembled fresh through the repaired code (not by filtering the old package),
bound to the same sources as B2.13, both database digests unchanged before
and after, 0 SEC requests, 0 evidence reads or writes; two assemblies in one
process and a second invocation into another directory: identical components,
package `79f644f1f8af85422b68911ef5427e6150886294a8ae8b4da57a34e4c9a7622e`.

| | superseded `83680ead…` | corrected `79f644f1…` | delta |
| --- | --- | --- | --- |
| catalogue rows in window | 301,880 (extended) | 291,623 | −10,257 |
| held in window | 22,108 | 22,057 | −51 |
| candidates | 279,772 | 269,566 | −10,206: 7,133 Nov 2021, 3,073 Dec 2021 (8-K 7,174, 10-Q 2,844, 10-K 181, 20-F 3, 20-F/A 1, 40-F 1, 40-F/A 2) over 3,503 issuers; 0 added; every common row identical |
| candidates with a cover key | 235,630 | 227,642 | −7,988 |
| cover keys | 657,826 | 647,373 | −10,453 |
| `G1_SOLVABLE` | 166,075 | 164,572 | −1,503 (1,319 Jan 2022 + 184 Feb 2022 month-ends, 1,319 identities → `G4_NO_CANDIDATE`) |
| `G4_NO_CANDIDATE` | 32,904 | 34,407 | +1,503 |
| `G2` / `G3` / `G5` | 28,203 / 12,085 / 3,002 | same | 0 |
| raw `UNKNOWN` floor | 76,194 | 77,697 | +1,503 |
| held | 32,413 | 32,413 | 0 (reachability, held, yield, bytes and conflicts keep their digests) |
| future-invalid edges | 0 | 0 | the same 3 SEC-index acceptance anomalies remain candidates |

Component digests: coverage `1fc94fc1…`, reachability `0ff895b3…`, gaps
`2037534d…`, candidates `56719b7d…`, held `4ac8220f…`, yield `4bc5064c…`,
bytes `84c270fa…`, conflicts `49350662…`. Mirror: 27 files as 54 objects under
`a3-inputs-r1/`, every one read back equal; the superseded package's 50
objects untouched, plus a `SUPERSEDED.json` snapshot beside each of the old
package and the old plan. Plan regeneration from this package is B2.14R2.
