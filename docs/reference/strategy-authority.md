---
type: reference
title: AlphaTrader strategy authority
---

# AlphaTrader strategy authority

`ALPHATRADER_COMPOUNDER_MONTHLY_V1`. The layer that turns the frozen research
record into desired exposure. Implemented in `src/stock_screener/strategies/`.

This is the link the operational chain was missing. P2.3.1 set out to wire a
concrete paper-operations environment and found that seven of the eight
authorities an operational run needs already existed — portfolio construction,
`PORTFOLIO_RISK_ENGINE_V1_2`, execution planning, the certified Alpaca paper
adapter, `PORTFOLIO_ACCOUNTING_V2`, reconciliation, input freshness — and the
first did not. `StrategyIntent` was constructed in exactly one production
location, a JSON deserialiser, so the system could authorise, size, route, fill
and book a decision it had no way to make.

## The authority boundary

```
research evidence
    -> STRATEGY           what exposure is wanted
    -> portfolio construction
    -> PORTFOLIO_RISK_ENGINE_V1_2    what exposure is permitted
    -> EXECUTION_ENGINE_V1_2         what orders achieve it
    -> EXTERNAL_PAPER
    -> PORTFOLIO_ACCOUNTING_V2
```

The strategy expresses a *preference*. It may not size a final position, read
portfolio cash or buying power, construct an order, write an accounting event or
repair a reconciliation — and not because a rule forbids it, but because the
package contains no code and no import for any of it. A static test asserts the
absence of each one, which is what keeps the boundary true after the next edit.

The risk engine may cut any exposure here to zero, and nothing the strategy
emits can restore the difference.

## What it decides

| | |
| --- | --- |
| cadence | monthly, on the last market session of the month |
| decision instant | that session's close, 16:00 America/New_York |
| execution eligibility | the first calendar session **strictly after** the decision |
| selection | top decile of the eligible universe by `COMPOUNDER_V1_2` total score |
| target count | `ceil(eligible / 10)`, at least one, never more than `eligible` |
| tie-break | durable identity `(kind, identity_id)` ascending |
| weighting | equal relative exposure, `1 / K` |
| direction | long only |
| hysteresis | none |
| market-signal overlay | none |
| missing score | not selectable — never zero, median, or carried forward |

`show-policy` prints that policy and its fingerprint, which is what a deployment
grant binds to.

### Why monthly, and not daily

Operations runs daily. That is not a reason to re-rank daily. A daily top-decile
rank churns the portfolio on noise in the ranking's tail and makes turnover an
artefact of the operational schedule rather than of the strategy. A non-decision
session returns `NO_STRATEGY_DECISION_DUE` and nothing else — in particular it
does not imply liquidation, which `StrategyRunReport.implies_liquidation`
asserts rather than leaves to a caller's judgement.

### Why `ceil`, with a floor of one

The decile rounding rule is named `CEIL_ELIGIBLE_OVER_TEN_FLOOR_ONE_V1` so it
cannot drift into an implementation detail:

```
N=0  -> 0     N=1  -> 1     N=9  -> 1     N=10 -> 1
N=11 -> 2     N=19 -> 2     N=20 -> 2     N=21 -> 3
```

`floor` would select nothing from any universe below ten, and a strategy that
silently holds nothing whenever its universe is small is a strategy whose
behaviour nobody tested. `ceil` is monotone in N with no plateau at the boundary
and can never exceed N.

### Why no hysteresis, and why H2 is not borrowed

A name that falls just outside the new decile leaves the target. The retention
rule that would keep it is `ONE_DECILE_HYSTERESIS_V1`, which is H2 research
property — and H2's own `PROMOTION_LADDER` records `BROKER_PAPER` as
`NOT AUTHORISED — requires its own approved phase`, with `paper_orders: NOT
AUTHORISED` in the protocol beside it. Using it here would contradict the record
that makes it evidence. A static test asserts no H2 module is imported.

### Why no market-signal overlay

`MARKET_SIGNALS_V1` has a declared role in the research *data* class — peer
percentiles over feature snapshots — but no frozen contract gives it a selection
or market-timing role for a strategy. Introducing a regime overlay here would be
a new, untested rule wearing an existing version's authority, so the first
strategy ranks on the Compounder score alone.

## Point in time

The frozen admissibility rule is the one `pit/decision_time.py` already states:
a filing is available iff the SEC accepted it at or before the close of its
`as_of`, and an observation with no stored acceptance time falls back to
`filed_date < as_of`.

A `ScoreSnapshot` does not record which filings it read. What it records is
`calculated_at` — when the scoring run computed it. A run that computed at
instant `T` read the database as it stood at `T`, so it cannot have used
anything published after `T`, whatever the individual acceptance times were. If
every record satisfies `calculated_at <= decision_at`, no information from after
the decision reached the decision. That is the property established, from a
timestamp the record actually carries rather than from one it does not.

Three consequences are deliberate:

- **A record computed after the decision is refused, and no older record is
  substituted.** A fallback to the last admissible snapshot would be a different
  decision wearing this one's fingerprint.
- **A record with no `calculated_at`, or a naive one, is refused rather than
  dropped.** Dropping it would shrink the eligible count, move the decile
  boundary and change the selection — a silent alteration rather than a refusal.
- **Instants are compared as instants, never as strings.** Lexicographic
  comparison of ISO-8601 is wrong whenever two offsets differ, and wrong in both
  directions: `2026-10-30T18:00:00+00:00` is 14:00 New York and sorts *after* a
  `16:00:00-04:00` close it actually precedes, while
  `2026-10-30T13:00:00-08:00` is 17:00 New York and sorts *before* the same
  close it actually follows. The second mistake admits evidence from after the
  decision, which is the one thing the check exists to prevent. It was present
  in the first draft of this layer and is now asserted in both directions.

The limit of the argument, stated rather than buried: `calculated_at` bounds
availability relative to the **decision**, not relative to `score_date`. A
backfilled snapshot contains no information from after its `calculated_at`, so
it is admissible for a later decision, but it is not a reconstruction of what
was knowable on its `score_date`. For prospective operation — the score computed
forward, the decision following it — the two coincide.

## Eligibility and missingness

Eligibility is `domain.scores.is_rank_eligible(status, freshness)`, consumed
rather than re-derived: that function is the whole of the V1.2 policy, and a
second copy here would let the strategy and the ranking views disagree about the
same company on the same day.

A security with no admissible score is not eligible for selection. It is never
assigned zero, a median or a previous score. A record whose status says `SCORED`
while carrying no number is counted separately as
`SCORED_WITHOUT_A_NUMBER`, because folding it into `INSUFFICIENT_DATA` would
make a data fault look like a company's circumstances.

## State

Only what distinguishes one decision from the next: the last decision, and the
exposures it still wants. Exits are not carried — a name at desired zero is one
the strategy no longer wants, and keeping it would make it exit again next
month.

Broker positions are deliberately **not** strategy state. A strategy that read
its holdings from the venue would conflate what it wants with what it has, and
those differ for reasons that are none of its business: a risk cut, a partial
fill, a pending order. It would also let a reconciliation break silently change
the next decision's exits.

Corrupt or wrong-version state is refused, never defaulted. A defaulted previous
selection would exit every holding.

## Deployment authorization

`STRATEGY_DEPLOYMENT_AUTHORIZATION_V1` binds a grant to the strategy's exact
**fingerprint**, not only its name and version. A name is too coarse — a changed
decile rule under the same name would inherit the old approval — and a version
is only as honest as whoever remembered to bump it. The fingerprint changes
whichever way the policy changes, so a modified strategy stops being authorised
without anyone having to notice.

`EXTERNAL_PAPER` is the only authorisable environment. Live environments are
named in `FORBIDDEN_ENVIRONMENTS` rather than merely absent, so a reader
checking whether live is possible finds an answer instead of inferring one from
a missing branch, and a future edit that tried to add one has to delete a
constant that says no. An empty fingerprint is refused: it would authorise every
later edit.

## No AI authority

No model determines official holdings for this strategy version. The package
imports no provider and the manifest records `ai_decision_authority: NONE`. AI
may later interpret a decision; it may not make one.

## What this is not

`ALPHATRADER_COMPOUNDER_MONTHLY_V1` defines a strategy **authority**, not a
validated edge. No backtest result justified any rule in it and nothing was
tuned — the decile size, the cadence and the equal weighting are the existing
frozen research convention, chosen because they already existed. The policy
carries `efficacy_claim: NONE` and a test asserts the artefact makes no claim to
be profitable, superior, validated or production-ready. Measuring whether it
earns anything is a separate research phase with its own preregistration.

## Known limitations

- **Admissibility rests on one timestamp.** `calculated_at` is a sound bound and
  a coarse one: it establishes that nothing postdates the decision, not that
  every input was the best available at the decision. Per-filing admissibility
  would need the score to record which filings it read, which `COMPOUNDER_V1_2`
  does not and which is frozen.
- **The strategy cannot tell a backfilled snapshot from a prospective one.** See
  the limit above. For operational use the caller supplies the current
  cross-section, which is computed forward.
- **A forward calendar must be supplied.** `market_sessions` reads stored
  benchmark bars, so it can only describe sessions that already happened. Month
  ends come from whatever calendar the caller provides, and a wrong calendar
  produces a wrong month end silently.
- **Twenty-one eligible names select three.** The decile rule is a fraction of
  whatever is eligible, so a narrow universe produces a concentrated target. The
  risk engine's position and count limits are what bound that, not the strategy.
