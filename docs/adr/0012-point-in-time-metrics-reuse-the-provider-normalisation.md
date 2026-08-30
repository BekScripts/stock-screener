# 0012. Point-in-time metrics reuse the provider's normalisation, and need no new metric version

- Status: Accepted
- Date: 2026-08-29

## Context

Phase 8B recorded every reported figure as an append-only observation carrying
the date it was filed, and built `FundamentalObservationRepository.as_of` to
select what had been filed by a date. Phase 8C consumed the immutable feature
record for market signals. Both reports named the same remaining gap: the
fundamental metric path still reads `financial_snapshots`, which is overwritten
when a filing is restated, so any historical study using fundamentals inherits
look-ahead.

Auditing that gap turned up something better than expected.
`build_company_metrics` is already a pure function over injected periods — it
opens no session and reads no table. The arithmetic layer was never the problem.
The look-ahead lives entirely in its three callers, each of which loads periods
from the mutable table.

That left one real question. Observations are stored at **fact level**: one row
per concept, per period, per filing, as the SEC published it. `FinancialPeriod`
is **normalised**: revenue, gross profit, capital expenditure, total debt.
Between the two sits a substantial body of logic in `api_clients.edgar` —
taxonomy detection, concept chains merged least-specific-first, capital
expenditure assembled from a stated total or its components, borrowings summed
with double-counting guards, cadence classification, and the money-unit rule
that Phase 7 exists to enforce.

Something has to perform that normalisation for a point-in-time read.

## Decision

**We will rebuild a company-facts document from the stored observations and run
the existing normalisation over it, unchanged.**

An observation carries every field the SEC publishes on a fact, so the document
is exactly reassemblable. The normalisation was extracted into
`periods_from_facts_document`, which both callers now share: the live provider
path, which fetches the document, and `periods_from_observations`, which rebuilds
one. The extraction changed no logic, and the 335 existing api-clients tests
pass unaltered.

The alternative was a second normaliser reading observations directly. We
rejected it because the two would drift, and drift in this layer would not look
like a bug — it would look like history. A historical margin computed by a
subtly different capital-expenditure rule is indistinguishable, after the fact,
from a business that behaved differently.

**We will not introduce a new metric version.**

Every metric definition is unchanged, and demonstrably so: the same
`build_company_metrics` performs the arithmetic and the same
`periods_from_facts_document` performs the normalisation. Only the *source* and
its temporal bound differ. A version identifier exists to record that a
calculation changed; nothing here changed one, and minting a version to record
that nothing moved would devalue the mechanism that `score_version` depends on.

A point-in-time metric for a past date will often differ from what the
operational path reports for that date today. That is the feature, not a version
change: one is built from what was known then, the other from what is known now.

**Nothing is persisted.** No table, no migration. Point-in-time metrics are
derived on demand from the observation record, which is itself immutable and
already stored. Persisting them would add a second copy of a value that can be
rebuilt exactly, and Phase 8C's argument for storing signals — that percentiles
are cross-sectional and that thresholds live in code that changes — applies to
neither: a metric is a function of one company's own facts, and the definitions
are frozen.

## Consequences

**The temporal bound has one home.** `available_as_of` selects observations by
`filed_date`, and everything downstream is indifferent to when it is called. The
arithmetic layer cannot tell whether its periods came from today's table or from
a rebuilt past, which is the property that makes the split safe.

**An observation without a filing date is unusable point-in-time.** It is stored,
counted and reported, but never selected. `period_end` is not a proxy for
availability and there is no fallback to it.

**The operational path is untouched.** Scanning, scoring and the dashboard still
read `financial_snapshots` and behave exactly as before. This phase adds a
capability beside them; it replaces nothing.

**The market side is not yet point-in-time.** Price bars are bounded by the same
date, but market capitalisation is read from the mutable company profile,
because nothing in this repository records what it was on a past date. Any ratio
against market capitalisation is therefore point-in-time on its fundamental side
only, and that limitation is documented rather than hidden.

**A gap in Phase 8B's extraction was found and closed.** The cover-page share
count is read by the normalisation from two places under different tag names;
the extraction captured neither correctly, so a rebuilt document lost the figure
market capitalisation is calculated from. Fixed, with a test asserting the
round-trip preserves it.
