---
type: reference
title: Paper shadow soak protocol
---

# Paper shadow soak protocol

`PAPER_SHADOW_SOAK_PROTOCOL_V1`. The rules under which the paper operations
control plane earns the right to be trusted unattended, and the recorder that
holds the evidence. Implemented in `src/stock_screener/paper_operations/soak.py`,
`soak_report.py` and `drills.py`.

P2.3 built a control plane and proved it refuses correctly under test. This
protocol is what distinguishes that from *having operated*. It adds no trading
capability and no authority: it is a preregistration, a recorder and a scorer.

## It is prospective, and that is the whole design

The protocol is frozen before the first observation and scored only against
sessions that had not yet happened when it was frozen. `SoakStore.qualifies`
is the boundary, and it refuses a record for a session that closed before
activation with `SOAK_RETROSPECTIVE_OBSERVATION`.

This is not pedantry about dates. A soak scored over sessions already in the
past is a search for a window that looks clean, and the system under test is one
whose failure modes are rare. Twenty retrospective sessions chosen after the fact
say nothing about the twenty-first; twenty preregistered ones do.

## The goalposts cannot move

`PaperShadowSoakProtocol` is frozen once. `freeze_protocol` refuses a second,
different protocol with `SOAK_PROTOCOL_DRIFT`, and `activate` refuses a second
activation with `SOAK_ALREADY_ACTIVATED`.

Every threshold lives inside the protocol's own body, so the protocol
fingerprint changes if any of them does. `evaluate_soak` takes **no** threshold
arguments — a caller cannot pass a kinder limit, because there is nowhere to pass
one. The scorer reads the frozen protocol or it has nothing to score against.

The failure this prevents is the ordinary one: nineteen clean sessions, one
duplicate order, and a quiet redefinition of success as a rate. There is no rate
field anywhere in the report.

## Evidence appends; it is never edited

`record_observation` never overwrites. An observation id is content-addressed
over the protocol, the session, the run id and the observation's own
fingerprint, which gives two properties at once:

- recording the identical observation twice is idempotent, so a retry is safe;
- a *corrected* observation has different content, so it gets its own file and
  cannot silently replace the original.

A correction is recorded with `supersede`, which keeps both and states the
reason. The history of what was observed is part of the evidence, not noise to
be tidied away.

## Sessions are counted from the calendar

`eligible_sessions` derives the expected sessions from the schedule and the
execution calendar between activation and now. Nothing is counted by hand, and
`missing_sessions` names the eligible sessions with no record rather than
quietly scoring over a shorter denominator.

A session that was eligible and has no record is a gap in the evidence. It
blocks promotion. It is not a safety violation — see below.

The calendar itself is declared data in `soak_calendar.py`, not a computation: a
forward calendar cannot be read from stored bars, so the 76 sessions are written
down before the first of them, with four full closures and two early closes
named explicitly. The early closes are excluded rather than merely noted,
because the soak's declared run time is 15:45 ET and those days shut at 13:00 —
keeping them would schedule a run into a closed market on two of the sessions
the frozen protocol is scored on, with no way to correct it afterwards.

If the exchange closes unexpectedly, a listed session will not happen and the
soak shows it as an eligible session with no record. That blocks promotion until
a person says why, which is the right outcome.

## FAIL and not-A are different verdicts

`SoakVerdict` has four values, and the distinction between two of them is the
one that matters:

| Verdict | Means |
| --- | --- |
| `A_PAPER_SHADOW_SOAK_PASSED` | every gate met, prospectively |
| `B_SOAK_IN_PROGRESS` | nothing is wrong; there is not yet enough evidence |
| `C_SOAK_HALTED_DEFECT_REQUIRES_RESTART` | a defect needs a fix and a new soak |
| `FAIL_SAFETY_VIOLATION` | something happened that must never happen |

Ten gates are zero-tolerance and produce `FAIL`: a duplicate remote order, a
duplicate accounting event, a duplicate admitted fill, a duplicate scheduled run
that caused execution, a trade on stale data, a backfilled missed run, an
ignored reconciliation break, a completed session that does not reconcile, an
unexpected order on an open day, and an unexplained remote position.

Five gates block an `A` without being breaches: an eligible session with no
record, an unresolved critical or blocking incident, a required drill not
passed, and too few qualifying observations. Missing evidence is missing
evidence, and a control plane that correctly refused to trade is the system
working. Reporting either as a safety violation would be wrong on the facts and
would also be the kind of noise that gets real violations ignored.

## Performance is not a gate

No gate reads a return, a fill price, a slippage figure or a profit. The report
has no field for any of them. The soak is about whether the machinery is safe to
run unattended; twenty sessions is far too small a sample to say anything about
whether the strategy makes money, and scoring it on returns would invite exactly
the tuning the preregistration exists to prevent.

## The six drills

A quiet soak proves the wrong thing. Twenty sessions in which nothing went wrong
establish that the system works when nothing goes wrong, which was never in
doubt. `DrillType` names the six paths a normal session never touches:

| Drill | Asserts |
| --- | --- |
| `KILL_RESTART` | a kill mid-run stops the submission, and survives a restart |
| `DUPLICATE_SCHEDULER` | two invocations of one slot are one run and one order |
| `CRASH_RECOVERY` | a run left mid-flight resumes rather than being re-run |
| `PROVIDER_UNAVAILABLE` | an unreachable venue blocks; the simulator is not substituted |
| `REMOTE_STATE_UNKNOWN` | an unresolved submission blocks the whole account |
| `STALE_INPUT` | an input outside its freshness policy blocks |

Every drill is driven by an **injected** fault against a stub environment and a
fake clock, over a throwaway operational root. That is deliberate rather than a
shortcut. The two most important drills are "the provider became unreachable"
and "a submission's fate is unknown", and arranging either against a real venue
means interrupting a real order mid-flight — an experiment that can itself cause
the duplicate it is testing for. An injected fault reproduces the decision the
system has to make with none of the exposure, and the decision is the thing
under test. Each record carries `injected_fault: true`, because an injected
outage and a real one are different evidence and only the first is safe to
arrange on purpose.

`run_required_drills` raises `SOAK_PROMOTION_GATE_FAILED` if any drill's
observed behaviour differs from what was expected. A failing drill is recorded
and then refused, not recorded and shrugged at: it means the control plane does
not do what the soak is about to assume it does.

## The operational store

Under the operations root, in its own namespace:

```
.operations/soak/protocol.json          the frozen rules
.operations/soak/activation.json        when observation began
.operations/soak/observations/*.json    one file per observation
.operations/soak/drills/*.json          one file per drill
```

Every write goes through `atomic_write_verified` and every path through
`guard_operations_root`, so the soak cannot be written into a research
directory or a database path.

## Known limitations

- **A coordinated local rollback is undetectable.** The observation files, like
  the execution, accounting and operations journals before them, are
  hash-chained with a head anchor. That detects tampering, deletion, reordering
  and insertion, and truncation via the anchor. Someone who rolls back the
  chain *and* the anchor together, locally, leaves a consistent record. Nothing
  on one filesystem can establish otherwise; it would take an external witness.
- **The drills are injected, not real.** They prove the decision, not the
  provider's behaviour under a genuine outage. The P2.2E canary is the only
  evidence in this programme that came from a real venue, and it found two
  defects that 211 fake-transport tests had passed.
- **Twenty sessions is a floor chosen before the data, not a sufficiency
  proof.** It is enough to exercise every drill and to see a month of calendar
  variation. It is not enough to bound the rate of a failure mode that occurs
  once a quarter.
- **A clean soak authorises nothing by itself.** `A` means the control plane
  operated as specified over the preregistered window. It is not a live trading
  authorisation, and this package has no path to one.
