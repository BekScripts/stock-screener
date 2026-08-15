# 0006. Score snapshots are versioned, and history is never re-scored

- Status: Accepted
- Date: 2026-08-14

## Context

Phase 2 stores one CompounderScore per company per day. Two of the product's
most useful views — Improving Fast, and the 7- and 30-day changes shown beside
every ranking row — are differences between two of those snapshots.

The formula will change. It is a first attempt at a scoring system that the
specification explicitly expects to be calibrated by hand after the first real
ranking is reviewed. A curve will move, a weight will be re-cut, a sub-score will
be added.

That creates a trap. If today's score is computed under a revised formula and
yesterday's under the original, subtracting them reports the formula change as a
change in the business. A company whose revenue, margins and price were all
unchanged would appear at the top of Improving Fast because a valuation curve was
made more generous. The view most likely to be trusted is the one most easily
corrupted this way, because a large positive delta is exactly what it selects
for.

Recomputing history under the new formula would avoid mixed comparisons, but it
destroys the record of what the tool actually said on the day — which is the only
evidence available for whether the scoring works.

## Decision

Every score carries a `score_version`, hard-coded in `domain.scoring` and stored
on every row. It is `COMPOUNDER_V1` today.

Any change to a curve, a weight, a coverage policy or the set of sub-scores means
a new version identifier. The uniqueness constraint on `score_snapshots` spans
`(company_id, score_date, score_version)`, so a v2 score never overwrites the v1
history beside it.

Every read takes a version, and every comparison — the prior-snapshot lookup, the
7-day change, the 30-day change — is filtered to a single version. There is no
code path that subtracts one version's score from another's.

Historical snapshots are never recomputed. A day's row is replaced only by
re-running the same version on the same day, which is what makes the nightly job
safe to run twice.

## Alternatives considered

**One score per company per day, overwritten when the formula changes.** The
simplest schema, and it makes every comparison self-consistent. Rejected because
it silently rewrites what the tool said yesterday: the first question after a
formula change is "did this actually improve the ranking?", and that question
cannot be answered against a history that has been retconned.

**Recompute all history under the newest formula, keeping one version.** Gives
comparable series and a clean table. Rejected on cost and honesty: it needs
point-in-time metrics for every past day — the fundamentals and prices as they
were then, not as they are now — which is the "complex point-in-time filing
warehouse" the MVP specification puts explicitly out of scope. Without that, the
recomputation would silently use today's restated financials for a score dated
three months ago.

**Version the formula but compare across versions anyway, flagging the mixed
comparison in the UI.** Rejected because a flag on a number does not stop the
number being sorted on. Improving Fast ranks by the delta; a mixed delta at the
top of the list has already done its damage by the time anyone reads the caveat.

## Consequences

A formula change resets the score-change views. The day v2 ships, no company has
a v2 snapshot from thirty days ago, so Improving Fast is empty and fills in over
the following month. That is a real and visible cost, paid every time the formula
changes — and it is the intended pressure not to change it casually.

The table grows by one row per company per day per version. For a few thousand
companies on a personal nightly job this is immaterial, and it buys the ability
to run two formulas side by side during a calibration.

Comparisons can be made against a snapshot older than the window they claim: with
a young history, the 7-day change may be measured against a snapshot 31 days old.
The alternative — reporting `None` unless a snapshot falls inside the window —
would leave both change columns empty for the first month of use. The rule is
documented in [CompounderScore v1](../reference/compounder-score.md).

Deleting a formula version's history is a deliberate act, not a side effect of
shipping a change.
