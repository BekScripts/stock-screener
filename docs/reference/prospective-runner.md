---
type: reference
title: R1.7 prospective shadow runner
---

# R1.7 prospective shadow runner

`stock_screener.prospective_runner` activates the frozen H2 protocol, arms
itself before the first qualifying decision, and waits. Ending armed with zero
observations and a sealed verdict is the successful outcome.

Run by `stock-screener prospective activate|arm|status`. Reads only.

## Why it cannot trade

`order_capability_reachable` parses each module and inspects imported names,
attribute accesses and call targets — capability is a property of code, not of
text, so a string mentioning a broker is data while an import is an ability.
`bind_runner` calls it before changing any state. There is no `force` parameter
anywhere in the package, and no broker credential is read or required.

## The calendar

The frozen dataset's `benchmark_prices` table stops where the dataset stops, so
it cannot say when the next quarter ends. `NYSE_SESSION_CALENDAR_V1` writes the
rules out instead: weekday, minus rule-derived holidays (including Good Friday
from the Easter computation and Juneteenth from 2022), minus known irregular
closures, with the observance rule that a Saturday New Year's Day is not
observed.

It is checkable rather than asserted. `verify_against_history` replays the
calendar over the frozen benchmark sessions, and `bind_runner` refuses if it
disagrees — it currently reproduces all **1,531** sessions from 2020-07-27 to
2026-08-28 with zero additions or drops. A future unscheduled closure cannot be
derived from any rule; it would appear as an absent session in the price source,
and the runner must fail closed on a decision it cannot price.

## Activation

One record, written once, at the runtime clock's instant.

| Refused | Why |
| --- | --- |
| a supplied timestamp | it could backdate the window, so it is an explicit error rather than a validated convenience |
| a non-commit code reference | prospective packets bind to code, and uncommitted code cannot be bound to |
| a dirty working tree (CLI) | the same reason, checked before anything is written |
| a second activation under a different binding | it would move the window's start |

An identical restart re-reads the stored record. Idempotency keys on what is
stored, not on a freshly stamped one — the clock moves between calls, and a
restart must not look like a second activation. What must match is the binding:
the commit, the candidate, the protocol, the contract and the semantic
fingerprint.

**Activation is not an observation.** It starts the clock that the first
decision is derived from.

## Arming, and the derived first decision

The first eligible decision is derived here and nowhere else:

> the first quarter-end session strictly after activation for which the runner
> was armed no later than the close of the immediately preceding session.

Both conditions are required. A quarter failing the second is recorded in
`skipped_quarters` and never enters the sequence — so there is nothing to
reconstruct later, which is the point. Arming exactly at the preceding close
satisfies the cutoff; one minute after does not.

The arm record exists because a later decision claiming the runner was armed is
worth nothing unless a durable record predates it. It is mirrored immediately
for that reason.

## The state machine

`DEFINED → ACTIVATED → ARMED → WAITING_FOR_DECISION → DECISION_CAPTURED →
WAITING_FOR_EXECUTION_REFERENCE → EXECUTION_CAPTURED → … → WINDOW_COMPLETE →
EVALUATED`, with `INVALIDATED_BY_DEFECT` reachable from any capture state.

No state may be skipped, and `ARMED` leads only to `WAITING_FOR_DECISION` —
there is no path from armed to a captured decision that bypasses waiting for the
decision instant. `EVALUATED` and `INVALIDATED_BY_DEFECT` are terminal.

The observation count is **derived from the packets that exist**, never
incremented, so a duplicated call cannot advance it.

## Semantic code fingerprint

A window runs for a year and code changes during it. Remembering which changes
matter is not a control, so it is computed: thirteen modules that can affect the
universe, features, score inputs, ranking, selection, target count, identity,
execution reference or accounting contribute to one digest. Logging and
presentation do not.

A moved fingerprint classifies as `SEMANTIC_RESEARCH_CHANGE` and halts
confirmatory capture until reviewed. A missing module is an error, not a skip.

## Decision and execution packets

They are separate records, and that separation is load-bearing: merging them
would put a price the decision could not have seen inside the record of that
decision.

- a **decision packet** carries no outcome field — `forward_return`,
  `execution_price`, `reference_open` and their kin are rejected at
  construction;
- an **execution packet** links to its decision by digest and sets
  `modifies_decision_packet: false`;
- a **correction** links to both and changes neither. A material one — meaning
  the decision used information that was not actually valid at the decision
  instant — yields `PROSPECTIVE_TEST_INVALIDATED_BY_DEFECT` rather than a fix.

`finalise` writes once. An identical retry is accepted; different bytes under
the same key raise `PacketConflictError`.

## Paired construction

`paired_targets` takes **one** ranked cross-section and returns both target
sets, so fetching them separately is impossible rather than discouraged. It also
returns the shared target count from the frozen top-decile rule —
`ordinary_target_count(746) == 74` and `(1010) == 101`, matching R1.2 exactly.

With no incumbents the two sets are equal, which is the initial-formation
invariant the protocol requires.

## Status

Read-only, fetches nothing. The criteria report as
`SEALED_UNTIL_WINDOW_COMPLETE` with each of P1–P4 `NOT_YET_EVALUATED`, and
performance as `NOT_EVALUATED_FOR_RESEARCH_DECISION`. No turnover reading is
exposed: reading nothing is easier than reading something and disregarding it.

## Runtime state

Under `.evidence/prospective/h2_v1/`, mirrored to
`control/PROSPECTIVE_SHADOW_STREAM_V1/prospective/h2-v1/`. Deliberately not
under `research/r1_*`, so historical and prospective provenance cannot blur.

| File | Holds |
| --- | --- |
| `prospective_activation.json` | the one-time activation, bound to a commit |
| `prospective_arm_record.json` | arming and the derived first decision |
| `prospective_runner_state.json` | the durable state machine |
| `prospective_runner_binding.json` | what was verified before any state changed |
| `prospective_research_ledger.jsonl` | append-only provenance |
