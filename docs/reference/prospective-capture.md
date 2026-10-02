---
type: reference
title: R1.12 prospective capture pipeline
---

# R1.12 prospective capture pipeline

`stock_screener.prospective_capture` turns the sealed prospective source
snapshots into a decision packet and its execution reference. It is the executor
R1.11 found missing.

## The hole this closes

R1.11 certified the armed December window and reported every gate green:
bindings matched, the calendar replayed 1,531 frozen sessions, the live source
canary returned 6/6 READY, timing semantics behaved as frozen, preflight said
`GO`. It then found that no decision could be produced at all.

Two things were wrong, one inside the other.

`decision_packet()`, `execution_packet()` and `paired_targets()` had **zero
production callers**. Stages `D_DECISION` and `E_EXECUTION_REFERENCE` were
specified in the frozen capture sequence and implemented nowhere.

Underneath that, the declared derivation was impossible. `readiness.assess`
marked `score_inputs`, `tradability` and `ranked_cross_section` READY whenever
the three fetched sources were admissible, recording `fingerprint: null` and the
note *"derivable once its sources are admissible"*. But no fetched source carried
a reported fundamental value: the universe is a broker asset list, the market
snapshot is daily bars, and `sec_availability` holds only acceptance metadata —
accession, form, `accepted_at`, `filed_date`. A CompounderScore cannot be built
from any of it. "6/6 READY" was a claim about derivability that nothing could
have satisfied.

## What readiness means now

Two definitions, named so they cannot be confused:

| Kind | Asks | Module |
| --- | --- | --- |
| `SOURCE_AND_DERIVABILITY` | are the fetched sources admissible? | `prospective_source.readiness` |
| `EXECUTABLE_CAPTURE_READINESS` | has a producer emitted a validated, fingerprinted artifact? | `prospective_capture.readiness` |

A component is `SOURCE_READY` when it is fetched and temporally admissible, and
`DERIVED_READY` only once a producer has actually run. Everything else is
`BLOCKED`, including a component that is merely derivable. No component is ever
ready with a null fingerprint, and `CAPTURE_READY` is conjunctive over every
component, both executors, the store, the calendar binding, the activation and
the arm.

R1.9's and R1.10's reports are not rewritten. They were accurate under the
definition they were made with; the definition was the problem.

## The six inputs

Four are fetched and two are produced. `tradability` belongs with the fetched
ones — the frozen capture sequence lists it as a stage-A current-only source,
supplied by the same broker asset list as the universe.

| Input | Kind | Temporal rule |
| --- | --- | --- |
| `universe` | fetched | current-only: retrieved at or before the instant |
| `tradability` | fetched | current-only, same retrieval |
| `market` | fetched | `bar.session <= decision session` |
| `sec_availability` | fetched | `accepted_at <= decision instant` |
| `score_inputs` | produced | bounded by the above |
| `ranked_cross_section` | produced | bounded by the above |

Two further sources feed `score_inputs` and are fingerprinted inside its
provenance rather than counted among the six: **filing observations**, which
carry the reported values, and **corporate actions**, which recover the price
basis a share count was stated against.

Filing observations are read through `get_filing_observations`, not
`get_financial_statements`. The frozen `NON_PIT_SAFE_OPERATIONS` refuses the
second and not the first, and the distinction is the whole point: the merged view
resolves every period to its most recently filed value and so destroys the record
of when each became knowable, while the unresolved view keeps one observation per
concept, per period, per filing. A restatement appears twice, and only the copy
already public at the instant is admitted — joined to the acceptance snapshot by
accession.

## The pipeline

```text
stage A   universe + tradability, retrieved before the instant, sealed
stage B   the time gate reaches T; nothing is ranked yet
stage C   market + SEC acceptance + filing observations + corporate actions
stage C2  all six fingerprints frozen
stage D   score inputs -> COMPOUNDER_V1_2 -> ranked cross-section ->
          ONE_DECILE_HYSTERESIS_V1 -> decision packet
stage E   the execution session's open -> execution packet
```

Every arrow is executable code. The package is append-only across the instant:
`seal_predecision` writes the first half and `complete_post_close` refuses to
replace it, because refreshing the universe after the close would substitute a
world the decision could not have seen.

## One strategy implementation

Selection goes through `membership_hypothesis.select` and ranking through
`membership_hypothesis.rank`. Scoring goes through `domain.score_company`, on
metrics built by `domain.build_company_metrics` from periods built by
`api_clients.periods_from_observations` — the same functions the historical
reconstruction calls, on inputs from prospective snapshots instead of stored
rows. Nothing here reimplements a weight, a curve or a tie-break: a prospective
copy could drift from the frozen original without a single test failing.

Ranking is by descending score with ties broken on `(kind, identity_id)`. A
ticker is display metadata and never a ranking key, so prospective listings are
resolved to research identities first; a listing with no research identity is
excluded and counted, never given a new one. `HISTORICAL` identities are delisted
and cannot appear in a current broker universe, so a prospective cross-section is
`COMPANY`-only.

## Freeze before score

The six inputs are sealed before the first rank is computed, and no network call
happens afterwards. That is what makes a decision reproducible from the artifacts
alone, and what lets a crash between the halves be survivable: a fresh process
loads the sealed package, verifies every payload still digests as sealed, and
continues.

## The gates

| Gate | Failure |
| --- | --- |
| the session was already missed | `MISSED_DECISION_NOT_BACKFILLABLE` |
| the fingerprint moved since the arm | `SEMANTIC_RESEARCH_CHANGE` |
| HEAD moved since the arm | `COMMIT_DRIFT` |
| the instant has not been reached | `DECISION_INSTANT_NOT_REACHED` |
| the input package is not complete | `SOURCE_INCOMPLETE` |
| fewer than six non-null fingerprints | `DECISION_INPUT_PACKAGE_INCOMPLETE` |
| the session source is not the authoritative one | `CALENDAR_BINDING_MISMATCH` |
| no decision to price | `NO_PRIOR_DECISION` |
| the execution session has not opened | `EXECUTION_SESSION_NOT_OPEN` |

The missed-session check runs first, before any source is read or value
computed. The temptation to reconstruct September 30 is strongest now that the
code to do it exists.

## Calendar authority

R1.11 found that the runner validates its calendar against whatever session
source it is handed, and that two databases here answer very differently: the
research dataset holds 1,531 sessions from 2020, the operational database 282
from 2025. Both replays return `agrees: true`, because the calendar reproduces
whatever subset it is shown. The existing digest gate proves only *which file*
was passed, so the thinner database with its own correct digest cleared every
check and printed a reassuring number.

`bind_calendar` therefore binds the calendar to the session source itself — its
identity, count, span and a digest over the sessions — and a materially weaker
source raises `CALENDAR_BINDING_MISMATCH`.

## What cannot be reached

`audit_package` walks the package's syntax trees and refuses three classes of
reachable name: forward returns and any other outcome, sector caps and rankings,
and broker order capability. It is AST-based rather than textual because a grep
flags the declaration that forbids an order as if it were one — which is how
R1.7's first guard failed.

An execution *reference* is the price a decision would have executed at,
recorded after the fact. The stream has no broker authorisation, and broker
requests remain zero.

## Known degradations

Declared rather than absorbed, because the prospective plane has no source for
either and improvising one would be worse:

- `FX_UNAVAILABLE` — no FX source is designated, so a foreign filer's
  currency-sensitive ratios are unavailable, exactly as the live path degrades
  when no rate resolves. Missing stays missing.
- `SHARE_BASIS_CORRECTED_FROM_OBSERVED_ACTIONS_ONLY` — only the corporate
  actions the window retrieved are applied to the share-count basis.

## Commands

| Command | Stage |
| --- | --- |
| `prospective pipeline-canary` | the whole chain, live, without a decision |
| `prospective capture-readiness` | `EXECUTABLE_CAPTURE_READINESS` |
| `prospective seal-predecision` | stage A, before the instant |
| `prospective capture-decision` | stages C through D |
| `prospective capture-execution-reference` | stage E |

The canary runs the production fetchers, score builder, scorer and ranker and
reports six real fingerprints at its own as-of. It stops before H2 selection, so
it can never consume a prospective observation.
