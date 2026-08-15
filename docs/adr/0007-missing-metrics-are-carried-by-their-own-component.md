# 0007. A missing metric is carried by its own component, or the company is not scored

- Status: Accepted
- Date: 2026-08-14

## Context

The screener's data is uneven by construction. EDGAR yields no fiscal-Q4 share
count, so dilution is often unknown. Absent debt is `None` rather than zero
([ADR-0005](0005-absent-debt-is-unknown-not-zero.md)), so enterprise value and
net cash are frequently unavailable. A company public for two years has no
three-year CAGR. A free FMP plan reaches fewer quarters than a paid one.

CompounderScore is built from twelve sub-scores worth 100 points between them. If
each unavailable metric contributed zero, the score would rank companies by the
completeness of their filings rather than by their quality — and it would punish
exactly the young, fast-growing companies the tool exists to surface, because
they are the ones with the shortest history. If each unavailable metric
contributed full marks, the same mechanism would run in reverse and reward
missing data.

Something has to decide what an unknown is worth, and the decision is
load-bearing: it applies to a large fraction of the real universe on any given
night.

## Decision

Three rules, applied in order.

**A missing metric produces no points and no penalty.** Its sub-score is `None`,
it is named in `missing_metrics`, and it appears in the breakdown as unavailable.

**Its weight is carried by the other metrics in the same component,
proportionally:**

```text
component score = points earned × (component maximum / available weight)
```

A growth component missing its 6-point CAGR scores what it earned out of the
remaining 29 points and scales the result by 35/29. The company keeps the share
of the component it earned on what is known. Each component's maximum is fixed;
weight never moves between components, so a missing valuation metric cannot
become growth points.

**A component below half its weight, or missing a metric declared required, is
`INSUFFICIENT_DATA` — and so is the company,** which then does not appear in any
ranking. Only current revenue growth and the primary valuation multiple are
required.

Completeness is reported separately as `data_coverage`, beside the score rather
than inside it.

## Alternatives considered

**Score missing metrics as zero.** Simple, and defensible for a metric like FCF
yield where a negative value is also zero. Rejected as a general rule because it
conflates "the company did not generate cash" with "we could not calculate
whether it did", and the two lead to opposite research decisions. It would also
make the score a proxy for filing completeness.

**Score the company on the metrics available and leave the maximum at 100.** No
rescaling: a company with 91 points' worth of available metrics is scored out of
100. Rejected because it is the zero rule wearing a different hat — the missing 9
points are simply unearnable, so incomplete data lowers the score by exactly the
amount of the gap.

**Impute a missing metric from a peer or sector median.** Standard practice in
quantitative screening, and it keeps every company scoreable. Rejected for v1: an
imputed value is indistinguishable from a reported one downstream, which is the
failure this codebase's central rule exists to prevent, and the point of the
project is that every ranking can be explained from facts about the company.

**Redistribute weight across components as well as within them.** Would let a
company with no valuation data still be scored on growth, quality and momentum,
rescaled to 100. Rejected because the components are not substitutes: a score
built without any valuation input is a different measurement wearing the same
number, and it would rank against companies whose valuation was assessed.

**Let momentum be optional, since it is only 15 points.** Tempting, and it would
have made scoring work before the benchmark series existed. Rejected for the same
reason: it would silently produce two populations of scores that are not
comparable. The visible cost is that a missing benchmark makes the whole market
`INSUFFICIENT_DATA`, which is loud, obvious, and fixed by one command.

## Consequences

Redistribution can flatter a company scored on a narrow set of metrics: a growth
component with only revenue growth and acceleration available extrapolates from
two metrics to 35 points. The 50% coverage floor bounds how far that can go, and
`data_coverage` makes it visible, but it does not eliminate it.

The floor also excludes real companies. A recently listed company with six
quarters of history is `INSUFFICIENT_DATA` for growth until it has eight, and
does not appear in the ranking at all — even though it may be the most
interesting company in the universe. It keeps a stored row with its status, so
the exclusion is visible rather than silent.

A component's sub-score weights must always sum to its maximum, or every company
is scored against the wrong denominator. `_build_component` raises `ScoringError`
when they do not, and the run stops rather than producing a plausible-looking
market-wide ranking.
