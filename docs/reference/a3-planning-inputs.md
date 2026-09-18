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
| `candidates` | `tuple[Candidate, ...]` | Every catalogue filing that could support a window month-end and is not held | V2 index `catalogue` minus the held set | planning rule on `filing_date` | ordered `(cik, accession)` | yes |
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
| Candidate eligibility | `supported_months(filing_date)` intersects the window's month-ends; `(cik, accession)` not held | `plan` |
| Acceptance fallback | none is needed: every V2 filing carries its own catalogue stamp; `stage9k.filing_acceptance` is untouched and unread | — |

The classification rule and the planning rule differ by up to one month at a
boundary: a filing accepted on the 15th supports that month-end under the
planning rule and classifies the following month under the classification
rule. The assembly reports the difference (`covered_under_classification_rule`)
and does not reconcile it; reconciling it would change the planner.

## The package

`<SEC_CONTROL_DIR>/plans/generation_v2/a3_inputs/`, mirrored file for file as
digest-addressed snapshots under `control/<generation>/a3-inputs/`.

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
