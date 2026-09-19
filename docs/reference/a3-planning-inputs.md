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

## The authoritative plan (B2.14R2)

`evidence regenerate-a3` over the corrected package, with three additions to
the command and none to the planner:

- **A forensic expectation is a gate, not a target.** `--expect-forensic-rows`
  and `--expect-forensic-sha` name what an earlier forensic run over the same
  inputs produced; the result is classified `EXACT_FORENSIC_REPRODUCTION`
  when rows and ordered digest both match and `FORENSIC_PRODUCTION_MISMATCH`
  otherwise, recorded in the comparison. A mismatch stops the command before
  anything is authorized or mirrored. Nothing about the run reads the
  expectation.
- **Authorization is a record, written last.** `--authorize` writes
  `authoritative_a3_plan.json` — status `AUTHORITATIVE_V2_A3_PLAN`,
  `AUTHORIZED_FOR_EXPANSION`; the plan digest, row count and fingerprints;
  the package digest and its window definition; the historical comparison
  (count-only); and the chain from `--supersedes-package-sha`,
  `--supersedes-plan-sha`, `--forensic-commit`, `--correction-commit` to the
  corrected package and this plan. It has no timestamp, so a rerun mirrors as
  a hit. `write_authorization` refuses a plan that did not validate, did not
  reproduce on its second run, or missed a forensic expectation it was given.
- **Superseded artifacts are refused, not avoided.** `regenerate_a3` refuses
  a package directory carrying `SUPERSEDED.json`; `write_a3` refuses a plan
  directory carrying one. The mirror prefix follows the plan directory
  (`a3_r1` → `control/<generation>/a3-r1/`), so the superseded `a3/` objects
  are never written to.

`plan_a3_v2.meta.json` now also carries `plan_sha256` and the explicit
`candidate_window` (definition, bounds, date field, semantics) from the
package's bindings.

### The live run — 2026-09-18

Package `79f644f1…` re-hashed component by component and bound to the live
manifest `13722e02…`, index `3deb46b7…` and its fingerprints, resolution
`161429f5…` and `stage9k.db` (universe `b8eaed3e…`, 8,919, re-proven
read-only); both database digests unchanged before and after; 0 SEC
requests, 0 evidence reads or writes.

| | authoritative `a3_r1` | forensic run 1 (B2.14R) | superseded `a3` | historical |
| --- | --- | --- | --- | --- |
| targets | **61,628** | 61,628 | 62,924 | 61,941 (−313, −0.5%) |
| ordered digest | `0efa7d038e063099dff86ff6e74ea9a2aab61feed62c45f29829511eba9b9507` | same | `a3e7df19…` | `53e3f0ca…` — count-only comparator |
| unordered target set | `42d92d38…` | — | — | — |
| estimated download | 6,599,925,841 bytes, 6.147 GiB | same | 6,807,725,520 | ~6.6 GiB |
| solvable gaps reached | 164,572 of 164,572 | same | 166,075 of 166,075 | 165,553 |
| expected months | 155,114.5 | same | 156,531.6 | 152,381 |
| raw `UNKNOWN` floor | 77,697 | same | 76,194 | 76,029 |
| months per target | 2.670 | 2.670 | 2.639 | 2.673 |
| forms | 8-K 59,937 · 10-Q 1,176 · 10-K 380 · 20-F 96 · 20-F/A 19 · 40-F 18 · 40-F/A 2 | — | 8-K 61,160 · 10-Q 1,249 · 10-K 378 · … | — |
| unique CIKs / accessions | 4,167 / 61,583 | — | 4,174 / 62,875 | — |
| filing dates | 2022-01-03 … 2026-07-21 | — | includes 1,347 filed 2021 | — |
| validation | all seven zero | — | all zero | — |
| determinism | run 2 identical: rows, bytes, digest | — | identical | — |
| classification | `EXACT_FORENSIC_REPRODUCTION` | — | `SUPERSEDED` | `BYTE_DIGEST_COMPARISON_INCONCLUSIVE` |

Against the superseded plan: 61,577 targets in both; the 1,347 only in the
old plan are exactly its 2021-filed targets; 51 targets are new, the
cascade the forensic review predicted where a 2021 filing had taken a
month. No target in the authoritative plan is dated before 2022-01-01.

Independent audit over every row (not the command's own validation): each
target is a frozen candidate, not held, of a qualifying form, dated inside
the window, carrying its candidate's estimate; every cover key is a
`G1_SOLVABLE` gap whose month-end is on or after the filing date and within
90 days of it; the 61,628 targets together claim all 164,572 solvable gaps;
ranks are contiguous; 0 duplicates; 182,727 selected-plan edges, none
future-invalid; every covered gap belongs to the target's CIK (45
co-registrant copies, none collapsed). A spot audit seeded from the plan
digest — 320 samples over top, middle, bottom, each form, co-registrant,
one-month and high-marginal strata — found 0 failures, and a 200-row sample
(seed 20260918) is recorded with each target's covered months.

Mirror: 10 files as 20 objects under `control/…/a3-r1/`, every one
`MIRRORED` and read back equal; 1,302 control objects; the superseded
`a3/` and `a3-inputs/` objects untouched. **Recovery from B2 alone:**
with `sqlite3.connect`, the engine factory and the assembly refused, the
package's 27 files were recovered from the newest `a3-inputs-r1/`
snapshots into a temp directory (package `79f644f1…` re-hashed), the
unchanged planner run over them twice, and the result — 61,628 rows,
`0efa7d03…` — equals the mirrored plan and the mirrored authorization
record: `A3_PLAN_RECOVERY_SUFFICIENT = YES`. 74 control GETs, 76 lists, 0
puts, 0 evidence-prefix keys.

The plan is `AUTHORITATIVE_V2_A3_PLAN` and `AUTHORIZED_FOR_EXPANSION`.
Its execution is B2.15.

## Executing the plan (B2.15)

`evidence expand-a3` consumes the authorized plan. It does not plan: the
module never calls the assembly or the planner, and the only decision it
makes about what to fetch is to refuse anything that is not the authorized
plan.

### Authority

`load_authorized_plan` refuses a directory carrying `SUPERSEDED.json`, one
without `authoritative_a3_plan.json`, a record whose status is not both
`AUTHORITATIVE_V2_A3_PLAN` and `AUTHORIZED_FOR_EXPANSION`, a record naming
another plan digest, a plan that does not hash to the digest the caller
demanded, and a record or package digest that differs from the ones given.
The newest file, the largest plan and the most recent directory are not
authority; the record plus exact digests are.

### Frozen batches

The plan is cut into consecutive batches in plan order (1,000 targets;
61,628 → 62 batches, the last of 628). Each batch is a JSONL file of its
targets — rank, `(cik, accession)`, form, date, document, URL — with a
record binding it to the plan, the package and the authorization record;
`batch_set.json` lists all of them under one digest. Files that exist must
be byte-identical on a rerun: membership is never recomputed. Every batch
file is mirrored under `control/<generation>/a3-expansion-r1/batches/` and
read back before the first request.

### The chain, per target

```text
authorized target → SEC GET → bytes, SHA-256 → B2 PUT → HEAD
    → body GET → remote SHA == fetched SHA → manifest append → checkpoint
```

The SEC side is sequential through the existing rate-limited, retrying
client; the bucket side of up to `--workers` targets is in flight at once,
because a PUT and its read-back are independent per object. The manifest
is appended by one thread, in plan order, only after that target's remote
body has been read back and hashed — neither the PUT response, the HEAD,
the ETag nor the metadata digest is trusted for that. A batch is
`BATCH_COMPLETE` only when every target is trusted; a request that fails
is retried in a second pass over the same targets, never replaced, and
what still fails stops the run: an `HTTP 404`/`410` as verdict C naming
the targets, anything else as verdict B, resumable. A 401/403/429 on the
final attempt stops before the next request.

### Resume, and objects nobody recorded

The checkpoint (`execution/expansion_checkpoint.json`, snapshotted to the
mirror after every batch) binds to the generation, the baseline manifest
and marker digests, the package, plan, record and batch-set digests, and
lists the completed batches; a checkpoint bound to anything else is
refused. A completed batch is skipped without a storage call. Inside a
batch, a target the manifest already trusts costs one HEAD (the object is
there at the recorded size) and no SEC request. An object at a target's
key with no manifest row — an interrupted PUT — is fetched again from the
SEC and the remote body read back; it is adopted, without a PUT, only when
both hash the same and are the same length, and the manifest row records
the fetched provenance and says so. Other bytes at that key stop the run
as verdict D; nothing is overwritten.

### Milestones and the terminal gate

Every tenth batch the production prefixes are listed and compared with
the trusted set (no bodies), the caller's database digests re-verified,
and the manifest snapshotted to the mirror. After the last batch every A3
artifact is downloaded and hashed against its record — the expansion
only; the baseline's 37.1 GB terminal verification is inherited from
B2.11 through its marker — then the whole namespace is listed and
reconciled, the final manifest snapshotted with stage
`A3_EXPANSION_COMPLETE` and its parent baseline digest, the deterministic
`a3_expansion_complete.json` written and mirrored, and recovery proven
from the bucket alone: baseline marker, corrected package, authorization
record, the superseded plan's sidecar (so recovery can tell `a3e7df19…`
from `0efa7d03…`), batch set and files, checkpoint, final snapshot,
marker and the evidence inventory. Only then is the checkpoint `COMPLETE`.

### The live run — 2026-09-19

`evidence expand-a3 --live` with every digest bound: plan `0efa7d03…`
(61,628 rows), record `2efb042f…`, package `79f644f1…`, baseline marker
`6c25b3c3…` naming manifest `13722e02…` and 38,146 trusted, both database
digests verified before, at every milestone and after. 62 batches frozen
(set `2590ff77…`) and mirrored before the first request; 61,628 targets
resolved from the index, none unresolved.

Two invocations. The first ran 28 batches (07:59–09:57 UTC) and stopped
as verdict B when the HEAD verifying one PUT timed out — the backend has
no retry on the write path, so `_store_and_verify` gained a bounded one
(the next attempt asks the bucket whether the object is there and is the
SEC body) with two regression tests, `make check` green, plan and
database digests re-proven. The second invocation (10:04–13:32 UTC)
resumed batch 028 with 365 cache hits (one HEAD each, no SEC request),
adopted the one object the interrupted PUT had left — the SEC body was
fetched again and matched it byte for byte, no re-PUT — and ran the
remaining 33 batches to `EXPANSION_HARVEST_COMPLETE`.

| | |
| --- | --- |
| targets / trusted / failed | 61,628 / 61,628 / 0 (0 `FAILED` rows; 0 rows for anything outside the plan) |
| by form, planned = trusted | 8-K 59,937 · 10-Q 1,176 · 10-K 380 · 20-F 96 · 20-F/A 19 · 40-F 18 · 40-F/A 2 |
| new downloads / cache hits / adopted | 61,262 + 365 (run 1) / 365 / 1 |
| SEC attempts / retries / refusals | 61,264 / 1 / 0 (401/403/429: 0) |
| bytes: planned estimate / actual | 6,599,925,841 / **5,528,340,425** (the estimate is a per-form catalogue mean) |
| B2 evidence, both runs | PUT 61,628 · HEAD 123,622 · GET 123,256 (61,628 read-backs + 61,628 terminal) · LIST 24 · DELETE 0 |
| batches | 62; 180–344 s, median 234 s; 4 h 09 m of batch time; 5 h 27 m wall including the stop and the terminal pass |
| milestones 009–059 | objects = trusted at 48,146 · 58,146 · 68,146 · 78,146 · 88,146 · 98,146; 0 orphans, 0 missing |
| terminal A3 verification | 61,628 downloaded and hashed: 0 digest mismatches, 0 size mismatches, 0 read failures |
| final namespace | 99,774 objects = 99,774 trusted records; 0 orphan objects, 0 orphan records, 0 missing, 0 path violations, 0 zero-byte, 0 duplicate URLs; submissions 5,733 · filings 94,041 · fulltext 0; 42,669,775,288 logical bytes |
| final manifest | `a5653f1ca065bddf50a8354faedde56df044f738efc6c0e92c17dd2cfc92c029`, 99,774 records, snapshotted with stage `A3_EXPANSION_COMPLETE` and parent `13722e02…`; the `BASELINE_COMPLETE` snapshot preserved |
| marker | `a3_expansion_complete.json` `af88cc247002cddf752a28967211ab82b953b3992814f769b71f301ad4ce0813`, mirrored and read back |
| control mirror | 322 objects under `a3-expansion-r1/` (124 batch files, the set, 62 summaries, 67 checkpoint snapshots, the marker); 0 conflicts |
| recovery from the bucket alone | baseline, corrected inputs, authoritative plan, superseded plan distinguishable, batch plans, completed state, final manifest, marker, evidence inventory — all YES: `A3_EXPANSION_RECOVERY_SUFFICIENT = YES` |

The baseline's 37.1 GB was not downloaded again; its verification is
inherited from B2.11 through the marker the expansion is bound to.

## The post-A3 index (B2.16)

`evidence build-post-a3-index` builds the final derived index without
reading the baseline corpus again. The B2.12 index is re-proven first —
file digest, metadata, completion marker, SQLite integrity and every
semantic fingerprint recomputed and compared — and refused as a conflict
if any of it disagrees; it is then copied, the copy re-proven the same
way, and only the delta is parsed into the copy. The B2.12 index file is
never opened for writing.

The delta is the final manifest's trusted filings whose keys the baseline
build did not ledger, and it must equal the authorized plan's
`(cik, accession)` identities exactly — missing, extra and duplicate
identities are counted and any of them refuses the build. Dates, listing
order and modification times play no part. Each delta artifact is one
GET; its length and digest are checked against the manifest before the
parser sees a byte; the parse is the B2.12 extraction unchanged; the
build ledger gains a row per artifact inside bounded transactions, so a
resume skips what it committed and refuses a build bound to any other
input (the state table holds every digest and the delta itself).

Finalization runs the B2.12 structural checks over the whole index, the
semantic fingerprints over the whole index, and the same fingerprint
queries restricted to the rows that are not the delta's — `filings`,
`securities` and `cover` by URL, the catalogue tables whole — which must
reproduce the B2.12 fingerprints digit for digit before the index is
promoted to `sec_evidence_index_v2_post_a3.db`. The small metadata,
fingerprints and `post_a3_index_complete.json` are mirrored under
`control/<generation>/post-a3-derived/`; the index itself is rebuildable
from the manifest, the bucket and that metadata, and is not mirrored.

## What A3 resolved (B2.16)

`evidence materialize-pit` runs the unchanged B2.13 assembly — the same
identity join, reachability, held set and 90-day rule — over the post-A3
index and compares every universe month with the standing the corrected
package froze before acquisition. A month open before and covered after
is `RESOLVED`; open both times is `STILL_OPEN` with its gap class
re-measured (an A3 filing that yielded nothing turns the candidate it was
into no candidate); covered before and open after would be `REGRESSED`
and is counted, never assumed away. `DECIDED` months are frozen verdicts
and are not in the comparison.

Attribution is the model's own: the assembly names, for a covered month,
the newest common-stock observation on or before the month-end that
joined to the identity — the *source* — and that is what the outcome row
records, with whether it is an A3 filing. Separately, *participation*
counts the A3 observations for the identity inside the month's window; it
is a set, not a cause. Per target the package records what the filing
emitted (cover rows, rows with a symbol, common-stock rows), how many
months it is the source of, and how many it participates in, beside the
planner's predicted potential and yield-adjusted months. Every resolved
month's realized edge is rechecked: the source date is on or before the
month-end and within 90 days, and the source is an observation that
joined `EXACT` to that identity; a violation stops the materialization as
a defect. The package is written twice from the frozen index and must
fingerprint identically; the expected 155,114.479 is printed beside the
observed count and gates nothing.

### The live run — 2026-09-19

**Index.** `evidence build-post-a3-index` bound to the final manifest
`a5653f1c…` (99,774 trusted; its newest mirrored snapshot equal), the A3
marker `af88cc24…` (local and mirrored), the B2.12 index `3deb46b7…`
(metadata, marker, integrity `ok`, all five fingerprints recomputed and
equal) and the plan `0efa7d03…`; both database digests unchanged before
and after. Delta: 61,628 filings beyond the baseline ledger (94,041 −
32,413), 0 missing, 0 extra, 0 duplicate, 45 co-registrant rows. One
invocation, 58.6 minutes, 0 resumes: 61,628 GETs, 5,528,340,425 bytes, 0
byte or digest mismatches, 0 missing, 0 storage failures, 0 read retries;
61,628 parsed, 0 failed; forms 8-K 59,937 · 10-Q 1,176 · 10-K 380 · 20-F
96 · 20-F/A 19 · 40-F 18 · 40-F/A 2 (= the plan). Rows: filings 32,413 →
94,041 (+61,628); securities 43,001 → 124,479 (+81,478); catalogue
716,290 and catalogue_stats 5,733 unchanged (+0); build ledger 99,774.
Structured fields: `Security12bTitle` 42,849 → 124,050, `TradingSymbol`
42,207 → 122,824, `SecurityExchangeName` 42,691 → 123,699; co-registrant
accessions 17 → 39 (surplus rows 29 → 75: the plan's 45 plus one A3 copy of
an accession the baseline held under another CIK); 21 acceptance-stamp
conflicts between co-registrant catalogues, the later stamp kept as
before. Baseline invariance: catalogue `98f66a22…`, catalogue_stats
`a809c94c…`, filings (32,413 rows) `8fb487b3…`, securities (43,001)
`98083e2c…`, cover `a0c00e69…` — all unchanged. Fingerprints of the whole
index: filings 94,041 `0aebbf30…`, securities 124,479 `ee19167a…`, cover
124,479 `b84800d4…`. `integrity_check` ok; 0 duplicate filing identities.
Index `sec_evidence_index_v2_post_a3.db`, 344,674,304 bytes,
`e6ce97d6bf8ecb1f3586fd3e7afa71a687a39bc7648fa3e45b877d181447abef`;
marker `post_a3_index_complete.json` `d91ff18d…`; metadata, fingerprints
and marker mirrored under `post-a3-derived/` (6 objects), 0 conflicts. The
B2.12 index is byte-unchanged.

**PIT.** `evidence materialize-pit` over that index, bound to the corrected
package `79f644f1…` (pre-A3 coverage `1fc94fc1…`, 268,780 months), the
plan, the universe `b8eaed3e…` and the resolution; two runs, every
fingerprint identical; ~12 s per assembly.

| | pre-A3 (frozen) | post-A3 (observed) |
| --- | --- | --- |
| universe months | 268,780 | 268,780 |
| `DECIDED` / `COVERED` / `OPEN` | 10,317 / 16,194 / **242,269** | 10,317 / 171,814 / **86,649** |
| newly resolved | — | **155,620** (64.23% of the open months) |
| regressed (covered → open) | — | 0 |
| predicted solvable 164,572 | | 155,040 resolved (94.21%), 9,532 still open |
| predicted unsolvable 77,697 | | 580 resolved anyway, 77,117 still open |
| expected solved | 155,114.479 | actual 155,620: +505.5, ×1.0033; actual / predicted-solvable 0.9456 |

Transitions: G1 → RESOLVED 155,040 · G1 → G1 8,439 · G1 → G4 1,093 (the
only candidates were A3 filings that yielded no common-stock cover) ·
G2 → RESOLVED 535 · G2 → G2 27,668 · G3 → RESOLVED 33 · G3 → G1 48 ·
G3 → G4 13 · G3 → G3 11,991 · G4 → RESOLVED 12 · G4 → G4 34,395 ·
G5 → G5 3,002. The 580 predicted-unsolvable months that resolved are
model-consistent, not a defect: coverage joins a cover row's *symbol* to
an identity at the row's date, while a gap's class was derived from the
identity's *resolved issuer* — an A3 filing under some CIK naming the
symbol of an unresolved identity (535, `G2`), of an identity whose issuer
reach was ambiguous (33, `G3`), or a month no filing of the identity's own
issuer reached (12, `G4`) covers it under the unchanged rule. Still-open
months after A3 by class: G1 8,487 · G2 27,668 · G3 11,991 · G4 35,501 ·
G5 3,002. No month-end join state changed (0 identity-state transitions).

By year (open before → resolved / still open): 2022 54,901 → 32,275 /
22,626 · 2023 54,351 → 34,974 / 19,377 · 2024 54,827 → 36,949 / 17,878 ·
2025 57,010 → 38,936 / 18,074 · 2026 (frozen horizon) 21,180 → 12,486 /
8,694. Identities with an open month 6,524: improved 4,028 (fully resolved
1,255, partially 2,773), no improvement 2,496; months resolved per
identity 0: 2,496 · 1–3: 166 · 4–6: 146 · 7–12: 215 · 13+: 3,501; the
1,048 `NO_CIK` identities keep 27,668 open months and gained 535.

Every one of the 155,620 resolved months has an A3 filing as its source
(0 baseline sources); realized edges checked 155,620, future-invalid 0,
source-identity violations 0. Participation: 138,692 resolved months have
exactly one A3 observation in their window, 16,879 two, 49 three or more.
Of the 61,628 targets, 59,813 emitted cover rows, 58,540 a common-stock
row, 58,050 participate in at least one newly resolved month and 58,030
are the source of at least one; 1,815 emitted no cover rows; 490 emitted
common-stock cover that benefited no month. Months-as-source per target:
0: 3,598 · 1: 5,728 · 2: 9,162 · 3: 41,859 · 4: 902 · ≥5: 379 — sum
155,620 against 164,572 predicted potential months. By form (parsed /
common-stock cover / participating): 8-K 59,937 / 57,086 / 56,611 · 10-Q
1,176 / 1,037 / 1,033 · 10-K 380 / 312 / 308 · 20-F 96 / 76 / 71 · 20-F/A
19 / 15 / 13 · 40-F 18 / 13 / 13 · 40-F/A 2 / 1 / 1.

Package `plans/generation_v2/post_a3_pit/`: month outcomes 242,269 rows
`a955a2b7…`, target realization 61,628 rows `eda53706…`, gap transitions
`7b073478…` (12 rows), identity summary 6,524 rows `c8b79f26…`, summary
`0a0c55cd…`; `pit_final_complete.json` `e5f3bbbd…`; the six small files
mirrored under `post-a3-derived/pit/`, 0 conflicts. Existing consumers
(`coverage`, `join`, `sample`, `reachability`, `expand`) run against the
new index unchanged. No A4 was planned.

## The residual audit (B2.17)

`evidence audit-pit` asks what the 86,649 months still open after A3 *are*,
without touching the model. It re-proves the B2.16 package from its own
fingerprints (242,269 rows, no duplicates, 155,620 + 86,649, 0 regressed,
0 future-invalid), runs the unchanged assembly over the post-A3 index once
more for the reasons, the reach and the post-A3 candidates, and gives each
residual month one primary audit class from the reason the assembly
itself states, one sufficiency category that follows from the class, and
diagnostic flags:

| assembly reason | audit class | sufficiency |
| --- | --- | --- |
| `G1` — an unheld qualifying filing reaches the month | `R9_POTENTIALLY_RESOLVABLE_WITH_EXISTING_UNHELD_SEC_FILINGS` | `SEC_MORE_MAY_HELP` — the only class counted as such |
| `G2` — no resolved CIK | `R2_NO_HISTORICAL_CIK` (flag `DECLINED_CURRENT_MAP_MATCH` for the 135) | `IDENTITY_DATA_REQUIRED` |
| `G3` — symbol does not attach at the month-end, or the issuer's reach is ambiguous | `R7_TICKER_OR_IDENTITY_AMBIGUITY` | `IDENTITY_DATA_REQUIRED` |
| `G3` — identity not credited by the issuer's filings; `G5` — issuer reach `MULTI_CLASS_UNASSIGNED` | `R5_SECURITY_CLASS_AMBIGUITY` | `IDENTITY_DATA_REQUIRED` |
| `G5` — no listing observation this month | `R6_EXCHANGE_EVIDENCE_MISSING` | `STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL` |
| `G4` — held qualifying filings within 90 days carry no common-stock cover | `R4_ELIGIBLE_FILINGS_NO_USEFUL_COVER_EVIDENCE` | `SEC_MORE_UNLIKELY_TO_HELP` |
| `G4` — held filings in reach name other symbols / this symbol without joining | `R5` / `R7` | `IDENTITY_DATA_REQUIRED` |
| `G4` — no qualifying filing of the issuer within 90 days | `R3_NO_ELIGIBLE_SEC_FILINGS` | `NON_SEC_EVIDENCE_REQUIRED` |

The A3 marginal curve is a prefix *union* in plan order of the resolved
months each target's evidence supports (its `EXACT` common-stock
observations inside a month's 90-day window) — no month is credited to one
document. The residual opportunity is the post-A3 candidate set (unheld,
in-window, reaching a post-A3 `G1` month) and the unchanged planner run
over it; the result is written as `residual_sec_forensic_plan.jsonl` with
`FORENSIC_ONLY` / `NOT_AUTHORIZED_FOR_ACQUISITION` in every row and a
status sidecar, and is not mirrored. Two audits must fingerprint
identically.

### The live audit — 2026-09-19

Bound to the post-A3 index `e6ce97d6…`, manifest `a5653f1c…`, PIT marker
`e5f3bbbd…`, package `79f644f1…`, plan `0efa7d03…`, universe `b8eaed3e…`;
two runs, all fingerprints identical; both databases unchanged.

**Planning realization.** Predicted solvable 164,572 → 155,040 resolved /
9,532 not (94.21%); predicted unsolvable 77,697 → 580 resolved / 77,117
not (0.75%); the four cells sum to 242,269. Expected 155,114.479, actual
155,620, +505.5 (+0.33%). Transitions: G1 → RESOLVED 155,040 · G1 8,439 ·
G4 1,093; G2 → RESOLVED 535 · G2 27,668; G3 → RESOLVED 33 · G1 48 · G4 13
· G3 11,991; G4 → RESOLVED 12 · G4 34,395; G5 → G5 3,002.

**The residual, 86,649.** Final classes: G1 8,487 · G2 27,668 · G3 11,991 ·
G4 35,501 · G5 3,002. Audit classes: R9 8,487 · R2 27,668 · R3 30,860 · R4
4,505 · R5 6,998 · R7 6,338 · R6 1,793 (R1, R8, R10, R11: 0). Sufficiency:
`SEC_MORE_MAY_HELP` **8,487 (9.8%)** · `SEC_MORE_UNLIKELY_TO_HELP` 4,505
(5.2%) · `IDENTITY_DATA_REQUIRED` **41,004 (47.3%)** ·
`NON_SEC_EVIDENCE_REQUIRED` **30,860 (35.6%)** ·
`STRUCTURALLY_UNRESOLVABLE_UNDER_CURRENT_MODEL` 1,793 (2.1%) · `UNKNOWN` 0.
Flags: `FOREIGN_ISSUER` 25,689 · `IDENTITY_NOT_CREDITED_BY_ISSUER_FILINGS`
5,713 · `MONTH_JOIN_AMBIGUOUS` 4,284 · `HELD_FILINGS_WITHOUT_COVER_ROWS`
3,251 · `LISTING_NOT_OBSERVED_THIS_MONTH` 1,793 · `MONTH_JOIN_OTHER_OCCUPANT`
1,556 · `HELD_COVER_ROWS_WITHOUT_COMMON_SYMBOL` 1,254 ·
`ISSUER_REACH_MULTI_CLASS_UNASSIGNED` 1,209 · `ISSUER_REACH_IDENTITY_AMBIGUOUS`
400 · `DECLINED_CURRENT_MAP_MATCH` 252 · `HELD_COVER_NAMES_OTHER_SYMBOLS` 76
· `HELD_COVER_NAMES_SYMBOL_BUT_DID_NOT_JOIN` 60 (the cover row joined
`AMBIGUOUS` at its own date — newly listed 2025–26 issuers whose symbol the
record cannot yet attribute then — while the month-end join is `EXACT`: the
occupancy-aware join being conservative, not a defect) · `MONTH_JOIN_UNRESOLVED`
38.

**NO_CIK.** 2,871 unresolved identities in the resolution; 1,046 of them
have residual months — 27,668, all `R2`; A3 resolved 535 of their months
through other issuers' symbol-joined filings. The 135 declined current-map
matches: 7 identities carry 252 open months, none resolved by A3, 252
residual — an upper bound (`UPPER_BOUND_ONLY`) on what independently
proven identity could recover.

**Filings exhausted.** 18,953 residual months have at least one held
qualifying filing within 90 days (1,842 identities; forms 8-K 14,144 ·
20-F 2,913 · 10-Q 1,596 · 10-K 729 · 40-F 388 · 20-F/A 247 · 40-F/A 33);
4,505 of them have held filings carrying no common-stock cover at all.

**A3 utility.** 61,628 documents, 5,528,340,425 bytes; 59,813 with cover
rows, 58,540 with common-stock cover, 58,050 participating in ≥1 resolved
month, 1,815 with no cover rows, 490 with cover but no benefit, 3,578 with
zero utility. 2,525 resolved months per 1,000 documents; 30,225 per GB.
By form (downloaded / common cover / participating / months supported per
document / per GB): 8-K 59,937 / 57,086 / 56,611 / 2.81 / 78,851 · 10-Q
1,176 / 1,037 / 1,033 / 2.65 / 1,709 · 10-K 380 / 312 / 308 / 2.43 / 1,139
· 20-F 96 / 76 / 71 / 2.01 / 579 · 20-F/A 19 / 15 / 13 / 1.79 / 897 · 40-F
18 / 13 / 13 / 2.00 / 7,063 · 40-F/A 2 / 1 / 1 / 1.50 / 43,755.

**Marginal curve (prefix union).** 10%: 6,163 docs, 0.39 GB, 19,236 months
(12.4%), 3,121 per 1,000 docs, 52,708 per GB · 25%: 45,233 (29.1%), 2,936 ·
50%: 89,136 (57.3%), 2,893 · 75%: 132,949 (85.4%), 2,876 · 90%: 149,533
(96.1%), 2,696 · 100%: 155,620, 2,525 per 1,000, 30,225 per GB. Deciles 1–7
add 17,200–19,200 months each (2,800–3,100 per 1,000 docs, 53,000–86,000
per GB); decile 8 adds 15,711 (1.59 GB, 10,598 per GB); decile 9 adds
9,605 (1,558 per 1,000, 11,136 per GB); **decile 10 adds 6,087 (3.9% of
the total) for 1.21 GB (21.9% of the bytes): 988 per 1,000 documents,
5,406 per GB** — a third of the average per document, a tenth of the
average per GB.

**Residual SEC opportunity (`RESIDUAL_OPPORTUNITY_AUDIT`, not A4).** Of
207,938 unheld in-window candidates, 12,320 (8-K 8,958 · 10-Q 2,425 · 10-K
934 · 20-F 3; 1,095 issuers) reach 8,487 residual months — 9.8% of the
residual, 3.5% of the original 242,269 — with marginal-month density 1:
3,033 · 2–3: 9,241 · 4–6: 46; 28,256 candidate edges, 0 future-invalid.
The unchanged planner over them: **3,640 targets** (8-K 2,772 · 10-Q 609 ·
10-K 256 · 20-F 3), 2,046,364,597 estimated bytes, 2.33 months per target,
7,978 yield-adjusted; 2,332 months per 1,000 documents (below A3's 2,525
average, above its final decile's 988) and 4,453 months per estimated GB
(A3 actual 30,225; A3 final decile 5,406). Availability
`UNKNOWN_UNTIL_EXECUTION`. After that theoretical acquisition 78,162 months
(90.2% of the residual) would remain.

**Where the residual is.** By year (open → resolved / residual): 2022
54,901 → 32,275 / 22,626 (58.8%) · 2023 54,351 → 34,974 / 19,377 (64.3%) ·
2024 54,827 → 36,949 / 17,878 (67.4%) · 2025 57,010 → 38,936 / 18,074
(68.3%) · 2026 21,180 → 12,486 / 8,694 (59.0%). LIVE identities 5,287:
211,558 → 151,119 / 60,439 (71.4%); DELISTED 1,237: 30,711 → 4,501 /
26,210 (14.7%). Foreign issuers (catalogue entirely 20-F/40-F) 813:
25,778 → 89 / 25,689 (0.35%) — 29.6% of the residual, almost all
`NON_SEC_EVIDENCE_REQUIRED` or `SEC_MORE_UNLIKELY_TO_HELP`: an annual 20-F
reaches at most three month-ends a year under the 90-day rule and its 6-Ks
are not qualifying filings. Identities: 6,524 with open months — 1,255
fully resolved, 2,773 partially, 2,496 with no improvement (all months
still open); among the 5,269 with residual months: 1 open month 1,056 ·
2–3 1,134 · 4–6 467 · 7–12 471 · 13+ 2,141. Concentration is low: top 10
identities 0.63% of the residual, top 50 3.2%, top 100 6.3%, top 500
31.0%; the top ten are all 55-month identities — six foreign ADRs (LYG,
MUFG, NGG, NOK, NTB, NVO), two closed-end funds that file no qualifying
form (MCI, MPV), two identity-blocked (MGRB, MOG.A).

**Decision.** Remaining 86,649; SEC-reachable 8,487 (9.8%) at 3,640
documents and ~2.0 GB, projected 2,332 per 1,000 documents and 4,453 per
GB — roughly the efficiency of A3's ninth decile per document and a
seventh of A3's average per GB; identity work would address 41,004 (47.3%);
another evidence source 30,860 (35.6%); 1,793 (2.1%) cannot be admitted
by the model; 4,505 (5.2%) have their filings already held and empty.
Verdict **A — freeze V2 PIT evidence; no further SEC acquisition
justified**: the SEC-reachable share is small and would be acquired on the
declining part of the curve, and 90% of what remains is not a filings
problem. The forensic plan is not authorized; the next phase is B2.18.
