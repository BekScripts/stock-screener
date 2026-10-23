---
type: reference
title: R1.10 V2 window certification
---

# R1.10 V2 window certification

`stock_screener.prospective_source.window` certifies a prospective window and
arms it under `H2_PROSPECTIVE_SHADOW_PROTOCOL_V2`. Run by
`stock-screener prospective certify-window [--arm]`.

## The hole this closes

R1.9 built the sources and left a timing gap wide enough for the September
failure to come through again. Its admissibility check asked only whether a
snapshot's facts predated the instant — and a current-only snapshot satisfies
that trivially, because its semantic time *is* its retrieval time. A universe
fetched at 23:00 would have passed for a 16:00 decision.

So sources are now classified by what they can say about a moment other than now:

| Class | Example | Latest admissible retrieval |
| --- | --- | --- |
| `SOURCE_HAS_EVENT_TIME` | SEC acceptance | any time; each fact carries its own availability |
| `SESSION_BOUNDED` | a completed daily bar | any time; the session is the fact |
| `CURRENT_AT_RETRIEVAL_ONLY` | broker asset list, tradability | **at or before the decision instant** |

**Retrieval time is load-bearing for the third class and only the third class.**
A late current-only snapshot is refused as `CURRENT_ONLY_SOURCE_TOO_LATE`. The
one escape is a provider-supplied `effective_at` at or before the instant — it
must come from the provider, because an effective time we assign ourselves is
the retrojection the rule forbids wearing a field name.

An envelope that declares no class defaults to `CURRENT_AT_RETRIEVAL_ONLY`, so
forgetting fails closed.

## The capture sequence

`PROSPECTIVE_CAPTURE_SEQUENCE_V2`, frozen before the arm so the decision day has
nothing left to decide:

| Stage | When | Sources |
| --- | --- | --- |
| A | **before** the instant | universe, tradability — sealed, `REQUIRED_PREDECISION_SOURCE_UNAVAILABLE` on failure |
| B | the instant | nothing ranked yet |
| C | after the close | market (session-bounded), SEC (acceptance-bounded), then freeze six fingerprints |
| D | after the freeze | score, rank, apply the frozen rule, one decision packet |
| E | next session's open | a separate linked execution packet |

The score may be computed after the instant: what matters is that every input was
admissible at it, and the arithmetic adds no information. **Stage A is the one
stage that cannot be recovered** — prices and filings can be fetched late, the
universe cannot.

## V1 → V2

V1 stays byte-identical. `v1_v2_diff` compares the twelve fields H2's science
rests on and refuses to certify if any moved; `certify_window` raises before
arming rather than arming on a protocol that drifted.

V2 adds a named disposition for a capture that fails closed on an unavailable
source — distinguishing a pre-sequence miss (no transition lost, the window
starts later) from a mid-sequence one (the chain the criteria measure is broken)
— and makes six-of-six readiness a **precondition of arming**. It changes
nothing else: not the rule, the band, the cadence, P1–P4, the aggregate rule, or
the window length.

### A defect R1.9 shipped and this phase caught

R1.9's generator imported H2's *historical* aggregate rule
(`C1 AND C2 AND C3 AND C4`) in place of the *prospective* one
(`P1 AND P2 AND P3 AND P4`). Nothing was armed under it, so it was a draft that
failed verification rather than an authority being rewritten. The corrected V2 is
emitted here, and `r1_10_v1_v2_diff.json` records both digests and the reason.
A regression test now asserts the criteria and the aggregate expression name the
same things.

## September 30 stays missed

`MISSED_SESSIONS` holds `2026-09-30` and `refuse_missed_session` raises
`MISSED_DECISION_NOT_BACKFILLABLE` for it. The missed opportunity is counted
separately from the observation count, so neither can absorb the other: at arm,
**valid observations 0, missed eligible opportunities 1**, and the next
successful capture is `VALID_OBSERVATION_1` rather than a renumbering of history.

## Arming

`activate_v2` binds to a full commit, refuses a worktree, and explains why V1's
activation will not do — it is bound to a commit from before the source plane
existed and to a fingerprint that has since moved, so it cannot authorise
semantics it never saw. `arm_v2` requires that activation to exist on disk and
refuses a window whose cutoff has passed.

Both records are append-only, and idempotency keys on the **binding** rather than
the body: these records carry a clock timestamp, so comparing whole bodies would
make every re-run look like a conflicting rewrite.

## Standing checks

| Checkpoint | When | Command |
| --- | --- | --- |
| early readiness | a week or more out | `prospective readiness` |
| final pre-arm | before the cutoff | `prospective certify-window` |
| pre-decision day | the preceding session, before its close | `prospective preflight` |
| decision day | before the instant, while Stage A is still possible | `prospective preflight` |

None consumes an observation, and a canary that passed in October proves nothing
about December — the runner requires a fresh pre-event result.
