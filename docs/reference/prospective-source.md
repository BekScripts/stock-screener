---
type: reference
title: R1.9 prospective source plane
---

# R1.9 prospective source plane

`stock_screener.prospective_source` supplies the six as-of-bounded inputs a
prospective decision packet needs, stores the snapshots behind them immutably,
gates readiness conjunctively, and records what the frozen protocol says about
the quarter R1.8 could not capture.

Run by `stock-screener prospective build-source-plane`, with standing checks
`prospective readiness` and `prospective preflight`.

## The invariant it exists for

R1.8 was lost because the newest obtainable data was five weeks older than the
decision instant and there was no gate that said so beforehand. The envelope
carries two timestamps that must never be conflated:

| | |
| --- | --- |
| `retrieved_at` | when the bytes arrived |
| `semantic_as_of` | the latest moment any fact inside was knowable |

`admissible_for` applies one rule in one direction: **late retrieval is
permitted, late information is not.** A payload whose facts postdate the instant
is never admissible however it was obtained; a payload fetched hours after the
close is fine if everything in it was knowable by the close.

A naive timestamp is refused outright — it cannot be compared to an instant.

## The four adapters

Each proves its own answer was knowable, and the proof differs by source.

| Source | Semantics | Bound |
| --- | --- | --- |
| universe / tradability | `CURRENT_AT_RETRIEVAL` — the broker asset list has no history | freshness bound; as-of is the retrieval moment, never backdated |
| SEC availability | genuinely point-in-time via `acceptanceDateTime` per accession | `accepted_at <= T`; no freshness bound, because an old filing is not a stale one |
| market | daily bars | `bar.session <= decision session`; later bars dropped and counted |

`companyfacts`, `get_financial_statements` and `get_statement_profile_by_cik` are
refused by name: a current aggregate cannot attribute a value to an accession, so
it cannot prove the value was public at the instant.

### The capture-timing constraint

Discovered while proving the plane, and worth stating plainly: **a current-state
source must be snapshotted at or before the decision instant.** The universe
carries no history, so its semantic time is its retrieval moment, and a universe
taken at 23:00 cannot serve a 16:00 decision. Prices and SEC acceptance are
bounded by session and accession and may be fetched afterwards. This is why the
capture job must run *at* the instant rather than later that evening.

## Readiness is conjunctive

Six inputs: `universe`, `sec_availability`, `market`, `score_inputs`,
`tradability`, `ranked_cross_section`. A decision packet needs all six
fingerprints, so **five of six is BLOCKED, not nearly ready**. A missing source
blocks rather than being skipped, because a silently absent input is precisely
the R1.8 failure.

The freshness policy is fail-closed, prospective only, and never applied
retroactively — §34's requirement, and the records say so.

## The store

Content-addressed and immutable, under `prospective-source/`, separate from the
historical evidence namespace. `refuses_historical_namespace` rejects a write
landing in `filings`, `submissions`, `fulltext`, `evidence`, `manifests`,
`research` or `control`, and a read verifies the payload still digests as its
envelope claims. Raw payloads are preserved alongside normalised records, so a
decision stays reconstructable from the bytes it used rather than from its
conclusion.

## The governance audit

`audit_v1` reads the frozen V1 protocol and reports which clauses bear on the
missed quarter.

**V1 settles** that September 30 can never count: a quarter the runner did not
capture live is reconstructable only as a labelled diagnostic, and an outage
transition is never replaced after the fact.

**V1 does not settle** whether the window continues. Its arming clause — "skip
that quarter … the next eligible quarter becomes the first observation" — is
triggered by a missed *arm cutoff*, and September 30's cutoff was satisfied five
days early. Borrowing that disposition would be an interpretation rather than a
reading, and it would be the convenient one. `early_termination` does not list
source unavailability either.

So the disposition is **`P3_PROTOCOL_SILENT_AMENDMENT_REQUIRED`**, and
`H2_PROSPECTIVE_SHADOW_PROTOCOL_V2` states it explicitly. V1 is never edited.

## What V2 changes, and what it must not

V2 adds exactly two things: a named disposition for a capture that fails closed
on an unavailable source, distinguishing a pre-sequence miss (no transition is
lost; the window starts later) from a mid-sequence one (the chain the criteria
measure is broken, so the window is invalidated); and readiness as a
**precondition of arming** rather than a hope at capture.

It changes nothing about H2 — not the rule, not the band, not the cadence, not
P1 through P4, not the aggregate rule, and **not the window length**. A
source-plane failure is not a reason to ask less of a hypothesis.

## Standing checks

- **`prospective readiness`** — the pre-event gate. Exercises the live adapters
  and reports each input READY or BLOCKED. Run days before a decision.
- **`prospective preflight`** — the decision-day check, meant to run *before*
  the instant. Reports the armed, bindings, calendar, store and adapter gates
  plus the time gate, and creates nothing.

## The canary

Classified `PROSPECTIVE_SOURCE_READINESS_CANARY`, `NOT_H2_DECISION`,
`NOT_PROSPECTIVE_OBSERVATION`. It exercises the real adapters at its **own**
as-of — never the September 30 instant — and increments no count. What it proves
is adapter liveness; whether a particular future instant is servable is a
different question the readiness gate answers near that decision.
