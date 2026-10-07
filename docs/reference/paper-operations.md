---
type: reference
title: Paper operations control plane
---

# Paper operations control plane

`PAPER_OPERATIONS_ENGINE_V1`. The layer that decides whether the system may act,
and records what happened. Implemented in `src/stock_screener/paper_operations/`.

P2.0–P2.2 established that *one* authorised external paper execution can be made
safe. This package is what makes running it again — unattended, on a schedule —
also safe, and most of what it contains is refusal.

It is versioned apart from `EXECUTION_ENGINE_V1_2` and
`PORTFOLIO_ACCOUNTING_V2`, and neither moves because an operational coordinator
exists. A scheduling change must not invalidate a reproducible execution
artefact.

## It gains no authority

The authority chain is unchanged:

```
strategy / research   finds opportunities
portfolio / risk      decides what may be held
execution             decides how an authorised delta becomes an order
broker                decides what fills
accounting            decides holdings, cash and P&L
operations            decides whether the system may act, and records it
```

Operations may schedule, gate, pause, kill, recover, require reconciliation and
record incidents. It may never invent a target, an order, a fill, a position, a
cash balance or a risk approval. That is structural rather than aspirational:
`PaperOperationsCoordinator` takes planning and submission as an injected
callable, so the module has no code path that could build either.

## The default is no

| Condition | Result |
| --- | --- |
| fresh install | `NOT_ARMED` |
| no kill-switch file | `KILLED` |
| unparseable kill-switch file | `KILLED` |
| kill state this build does not recognise | `KILLED` |
| kill state whose fingerprint does not match | `KILLED` |
| expired authorization | authorises nothing |

Absent control state is never permission. Any other default means a disk error
grants the right to trade.

Every control-state write is flushed, `fsync`ed, atomically renamed **and read
back and compared** before it is treated as committed. For a kill switch, "I
wrote it" and "it is there" are different claims and only the second is worth
anything after a power loss.

## Arming is not order authorization

`PaperOperationsAuthorization` permits the *coordinator* to operate. It
authorises no security, quantity or side. Every order still needs the full
portfolio → risk → `ExecutionPlan` → `Order` chain, and arming grants none of
it.

The distinction is load-bearing: if arming could authorise an order, one
operator action would stand between a scheduled process and an arbitrary trade.

A grant names one account binding fingerprint — never a wildcard, which would
keep authorising after a credential was repointed, exactly when it must stop —
and the commit the operator approved. `expires_at` is optional and no default is
invented: a grant that silently expired would strand the scheduler, one that
never expires outlives its reason. The operator picks.

## Kill and pause

| | Blocks new submissions | Cancels owned open orders | Liquidates positions | Touches unknown orders |
| --- | --- | --- | --- | --- |
| `PAUSED` | yes | no | **no** | **no** |
| `KILLED` | yes | yes | **no** | **no** |

**A kill does not liquidate.** A filled position is an accounting fact an
authorised decision created, and only another authorised decision may unwind it.
A kill switch that flattened the book would be the largest unreviewed trade the
system ever made, at exactly the moment something is already known to be wrong.

**A kill does not touch what it cannot prove it owns.** An open order the
execution journal does not account for belongs to somebody else; it is reported
as `UNEXPECTED_REMOTE_ORDER` and left alone.

**A kill is recorded before the venue is contacted.** Cancelling needs the
provider and the provider may be down, so writing the state first means a
provider outage cannot prevent the system from becoming killed.

If an owned order's fate cannot be established, the state becomes
`KILLED_WITH_UNRESOLVED_REMOTE_ORDER` — strictly worse than `KILLED` and never
collapsed into it, because there may be a working order nobody is tracking.

**Resuming is a recovery, not a restart.** `require_resume_authorized` demands a
clean three-way reconciliation, no open blocking incident and no unresolved
owned order. `RunState.KILLED` has no outgoing edge in the transition table.

`disarm` is the narrower action again: it blocks future scheduled runs, cancels
nothing and touches no position.

## Permission is not cached

The kill switch is re-read before **every single order**, not once per run.
`SubmissionGate.require` runs per submission and counts its checks so a test can
assert it. A process armed at startup has not been armed for the rest of time,
and an operator who kills mid-run must not have to wait for the run to finish.

## Scheduling

`PAPER_OPERATIONS_SCHEDULE_V1` holds a timezone, a local time of day, an
explicit execution window and an explicit session calendar. It holds no
strategy — a schedule says *when* to look, not what to do.

Sessions come from `ExecutionCalendar`: never `date + 1`, never a weekday guess,
never a hard-coded holiday. A Friday slot's successor is Monday, and arithmetic
would schedule a run against a closed market.

### Run identity

`DERIVED_OPERATIONS_RUN_ID_SHA256_V1`. The run id is a digest of the slot's
semantic inputs — schedule fingerprint, session, account binding, mode — and
carries no clock. Invoking the scheduler twice for one slot yields the same id,
so the second invocation recovers the first run instead of starting a
competitor. A UUID would make every invocation a new run, which is the easiest
way to submit one decision as two orders.

### A missed run is never backfilled

If the process starts after the window closes, the slot is `MISSED` and nothing
is submitted. An order planned against yesterday's prices, sizing and risk state
is not a late version of yesterday's decision — it is a new, unreviewed one
wearing an old authorisation.

### Overlap

A durable `RunLease` admits one mutation-capable run per account. A stale lease
is **never** taken on age: a stale lease and a crashed run are the same
observation from outside, and the crashed run may already have submitted. The
recorded run must be recovered first.

Durable rather than an in-process mutex, because an in-process lock protects
against two threads and nothing against two processes — which is the case that
actually happens when a cron job overlaps a manual invocation.

## Health gates mutation

Eighteen required checks, declared as data in `REQUIRED_HEALTH_CHECKS` so a new
requirement cannot be added without appearing there, and evaluated as a list so
one failure does not hide the rest.

| State | Means | May mutate |
| --- | --- | --- |
| `HEALTHY` | every required check passed | **yes** |
| `DEGRADED` | something is wrong that does not stop a run being understood | no |
| `BLOCKED` | an external condition stops us | no |
| `UNHEALTHY` | our own records do not verify | no |

There is deliberately **no `DEGRADED` path to a mutation**. Every
"degraded but probably fine" exception anyone has written is the one that later
turns out to have been the problem.

A required check that did not *run* is reported in `not_reached` and fails the
report. A check that never ran has established nothing, and treating silence as
a pass is how a gate permits what it was built to stop.

## Nothing is substituted for missing truth

| Condition | Behaviour |
| --- | --- |
| stale required input | `STALE_OPERATIONAL_INPUT`, no submission |
| missing required input | `REQUIRED_OPERATIONAL_INPUT_MISSING`, never guessed or reused |
| provider unreachable | `BLOCKED_PROVIDER_UNAVAILABLE`; **no fallback to the local simulator** |
| reconciliation break | `BLOCKED_RECONCILIATION_BREAK`, never repaired |
| `REMOTE_STATE_UNKNOWN` | blocks the **whole account**, not just the affected name |
| missed slot | `MISSED`, never run late |

Freshness is inclusive at the limit — an input exactly at its `max_age_seconds`
is fresh — stated explicitly because `<` and `<=` differing between two sources
produces a run that is fresh by one check and stale by another. A future-dated
input is not fresh either: it cannot be trusted to describe the present.

## Incidents

`APPEND_ONLY_INCIDENTS_NO_AUTO_REPAIR_V1`. An incident id is *derived* from the
condition it describes, so re-detecting the same problem appends to one incident
rather than opening a hundred copies that read as a hundred problems.

Status is derived from the history rather than stored, so there is no mutable
field that could disagree with the record of how it got there. An automatic
actor may detect and escalate; it may not resolve a blocking incident — a system
that clears its own alarms has no alarms.

| Severity | Blocks submission | Kills |
| --- | --- | --- |
| `INFO` | no | no |
| `WARNING` | no | no |
| `BLOCKING` | yes | no |
| `CRITICAL` | yes | yes |

`CRITICAL_AUTO_KILL_REASONS` is frozen and short: account mismatch, paper
environment not proven, live trading not authorised, ledger integrity failure,
execution journal integrity failure, duplicate accounting fill, control state
unverifiable. Each means the system has lost track of what is true.

Ordinary trouble — a stale input, an unreachable provider, a reconciliation
break — *blocks* instead. Escalating routine problems to a state needing
operator recovery would train an operator to clear kills without reading them,
which is worse than not having one. An unclassified reason code defaults to
`BLOCKING`, because the safe reading of "I do not know what this is" is "do not
trade".

## The run journal

`PAPER_OPERATIONS_JOURNAL_HASH_CHAIN_V1` plus a mandatory
`PAPER_OPERATIONS_JOURNAL_HEAD_ANCHOR_V1`. Same pattern as the execution journal
and the accounting ledger, for the same reason: a chain detects tampering,
deletion, reordering and insertion, and cannot detect its own truncation. Only
the anchor catches that, and it may never move backward.

An illegal run transition is refused **before** the record reaches disk — the
P1.4 lesson. A journal containing a transition the state machine rejects can
never be replayed.

A run's current state is derived from the journal rather than cached, so there is
no second authority that could disagree with the history.

Stated plainly: **a coordinated local rollback of the journal and its anchor
together, to a mutually consistent earlier state, is undetectable from local
files alone.** Closing that needs an anchor held where this process cannot reach.

## Modes

`OBSERVE_ONLY` and `EXTERNAL_PAPER`. There is no live member, and adding one
would not be sufficient to enable live trading — the transport is host-locked
and the paper proof binds the origin — but it would be the first step, and the
enum is where that step is refused.

Observe-only runs recovery, health, reconciliation, input capture and planning,
reports what it *would* have submitted marked `NOT_SUBMITTED`, and makes
**zero** external mutations — asserted by counting, not by prose.

## The operational store

Control state lives under `.operations/` and never shares a root with SEC
evidence, the point-in-time research corpus, the accounting ledger or the
execution journal. Those are different trust domains with different retention
rules, and a control file among them would eventually be swept up by a cleanup
written for the other kind. `guard_operations_root` refuses such a path.

## Commands

```bash
stock-screener paper-ops status           # local only; no network, no credential
stock-screener paper-ops arm --confirm --mode EXTERNAL_PAPER
stock-screener paper-ops disarm --reason "end of day"
stock-screener paper-ops pause
stock-screener paper-ops kill --confirm
stock-screener paper-ops show-incidents
stock-screener paper-ops verify-journal
```

`arm` and `kill` both require `--confirm`; there is no default-yes and no live
mode. `kill` does not need the provider to be reachable.

## Known limitations

- **Coordinated local rollback is undetectable.** As above, and the same limit
  applies to the execution journal and the accounting ledger.
- **Operational control is not execution quality.** This plane can establish
  that a run was permitted, reconciled and recorded. It says nothing about
  whether the orders it permitted were good ones.
- **A soak has not been run.** The control plane is complete and tested offline;
  no multi-day unattended paper operation has happened yet. That is P2.4.
