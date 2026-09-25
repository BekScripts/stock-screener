---
type: reference
title: R1.6 H2 candidate and shadow protocol
---

# R1.6 H2 candidate and shadow protocol

`stock_screener.prospective_protocol` freezes the H2 candidate on its
preregistered mechanism result, closes everything the frozen history can still
offer, and defines the prospective experiment that could supply independent
mechanism evidence. It observes nothing and starts nothing.

Run by `stock-screener backtest freeze-h2-candidate`. Reads only.

## Why it cannot act

`binding.capability_reachable` reads this package's own source and reports any
import of a name that could simulate, fetch or reach a broker — `simulate`,
`PriceBook`, `performance`, `httpx`, `requests`, `alpaca`, `broker`,
`create_engine` and the rest. `bind_r1_5_record` calls it before reading a
value. A phase that defines a future experiment must not be able to start it.

## The candidate

`H2_MEMBERSHIP_STABILITY_CANDIDATE_V1`, carrying four state words at once so
none can be read alone: `FROZEN_CANDIDATE`,
`PROSPECTIVE_CONFIRMATION_PENDING`, `NOT_PRODUCTION`, `NO_LIVE_CAPITAL`.

**It is frozen because the preregistered mechanism passed** — C1 through C4 all
met under a rule fixed before execution. It is **not** frozen because it
returned more: the record states in the same breath that H2's 1.35 percentage
point edge over seventeen rebalances on one realised path is noise, took no
part in the verdict, and is not why anything is frozen.

Historical development is closed: the parameter search, additional retention
widths, frequencies, weightings and holding-period rules are all `PROHIBITED`,
and `historical_clean_holdout` is `NONE`. A new variant would need a new
hypothesis id and would still carry `EXPLORATORY_SUPPORT_ONLY` on this dataset.

### The promotion ladder

| Stage | Status |
| --- | --- |
| `FROZEN_H2_CANDIDATE` | CURRENT |
| `PROSPECTIVE_SHADOW` | NEXT — preparation authorised by R1.6, activation by R1.7 |
| `BROKER_PAPER` | NOT AUTHORISED |
| `SUPERVISED_SMALL_LIVE` | NOT AUTHORISED |
| `CONSTRAINED_AUTONOMOUS` | NOT AUTHORISED |

Shadow means internally simulated reference execution. Paper means orders into a
broker paper account. Live means real capital. R1.6 authorises preparation for
the first transition and nothing beyond it.

## The prospective contract

`PROSPECTIVE_SHADOW_CONTRACT_V1` is the explicit bridge from a frozen dataset to
a live stream, written out rather than assumed. The asymmetry it exists for is
the direction of error: historically a late filing was simply absent, while
prospectively it may arrive minutes after the decision instant and be sitting
there when anyone looks. So the obligation becomes active — capture what was
knowable at 16:00 America/New_York and freeze it.

It fixes the identity key, feature timestamps, the designated price source, the
missingness rule (`UNPRICED`, never a substituted price), corporate actions,
terminal treatment, and the storage separation: the stream is
`PROSPECTIVE_SHADOW_STREAM_V1` and must never write into
`PIT_RESEARCH_DATASET_V2`.

## The protocol

`H2_PROSPECTIVE_SHADOW_PROTOCOL_V1`. A **mechanism** confirmation study, not an
alpha one: four transitions cannot establish a return effect.

| | |
| --- | --- |
| Window | 5 decisions — one formation plus **4 transitions** |
| First eligible decision | a **rule**, not a date; `NOT_YET_ASSIGNED` |
| Comparator | `COMPOUNDER_QUARTERLY_TOP_DECILE_V1`, paired on identical inputs |
| Execution | `CLOSE_SIGNAL_NEXT_SESSION_OPEN`, shadow only |

The window cannot move in either direction: no stopping early on success, no
extending on a poor result, no choosing the best four afterwards, no dropping or
retrospectively replacing a transition.

**The first eligible decision is derived, never written down now.** It is the
first quarter-end whose decision instant falls strictly after activation and for
which the runner was armed by the close of the preceding session. A quarter that
misses that is skipped — reconstructing it would be a backtest wearing a
prospective label, and any reconstruction carries `RETROSPECTIVE` and
`NOT_PROSPECTIVE_CONFIRMATION` and never enters the criteria.

### The four criteria

| | Requirement |
| --- | --- |
| P1 | H2's cumulative annualised turnover strictly below the **paired comparator's** |
| P2 | H2's cumulative annualised entry/exit churn turnover strictly below the comparator's, as an absolute quantity |
| P3 | same target-count rule, 0 hysteresis-caused cash, no hysteresis-caused exposure loss |
| P4 | 0 accounting, identity, leakage, same-close and **retrospective-replacement** findings |

`PROSPECTIVE_MECHANISM_CONFIRMED` requires **P1 AND P2 AND P3 AND P4**. No
weighting, no majority, no qualitative override, no partial label.

P1 and P2 are **paired and directional**. They compare against the comparator
run on the same prospective cross-sections, never against the historical
1.6177×. Requiring the historical magnitude would fit the prospective test to an
effect size that came out of the data which generated the hypothesis.
`historical_values_are_references_only` carries those numbers with
`any_of_these_is_a_prospective_threshold: false`.

P4's last clause makes protocol integrity a pass condition: a rewritten decision
packet fails the test rather than being quietly absorbed by it.

### No early verdict

`interpret_prospective` returns `PENDING` until every transition is in, whatever
the interim readings show, and the candidate stays
`PROSPECTIVE_CONFIRMATION_PENDING`. `evaluate_prospective` takes no performance
argument, so the mechanism verdict cannot see one.

## The schemas

Written before any decision exists, so the first real quarter cannot shape them.

- **`prospective_decision_packet_schema.json`** — the packet frozen at each
  decision instant. `additionalProperties: false`, and no outcome field exists:
  a packet carrying a forward return fails its own schema. Immutability is by
  key and digest — same key, same digest is a repeat; same key, different digest
  is a protocol conflict.
- **`prospective_research_ledger_schema.json`** — append-only provenance,
  classifying each decision's code change as `NONE`,
  `NON_SEMANTIC_INFRASTRUCTURE` or `SEMANTIC_RESEARCH_CHANGE`, and recording
  `post_decision_corrections` with `rewrote_a_frozen_packet: false`.
- **`prospective_evaluation_schema.json`** — exactly four criteria and
  `performance_entered_verdict: false` as a constant.

## Status at freeze

| | |
| --- | --- |
| protocol definition | FROZEN |
| prospective runner | NOT_YET_ACTIVE |
| first eligible decision | NOT_YET_ASSIGNED |
| observations | 0 |
| completed transitions | 0 |
| prospective verdict | PENDING |

R1.7 builds and activates the runner.
