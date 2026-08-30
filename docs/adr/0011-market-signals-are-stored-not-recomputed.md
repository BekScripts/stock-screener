# 0011. Market signals are stored, not recomputed on read

- Status: Accepted
- Date: 2026-08-29

## Context

Phase 8B captures market features into `pit_feature_snapshots`: one immutable
row per company per day, versioned, never updated. Phase 8C derives market
signals from those features — peer percentiles, a multi-label setup, and a
tri-state tradability verdict.

Derivation is a pure function. The same features and the same thresholds always
produce the same signals, and the features are already stored immutably. That
makes a second table look redundant: the signals could be computed whenever
somebody asks for them, from rows that cannot change.

The alternative is to store the derived result beside the features, as
`score_snapshots` is stored beside `financial_snapshots`.

Two things make the choice load-bearing rather than a matter of taste.

**Percentiles are cross-sectional.** A signal is not a function of one
company's features. It is a function of that company's features *and the whole
market's on the same day*. Deriving on read therefore means rebuilding the
cross-section for every query, and — worse — means the answer depends on which
companies happen to be selected at read time. Two callers asking about one
security with different filters would get different percentiles for it.

**Code changes; stored rows do not.** Derivation-on-read reproduces history only
while the code that produced it still exists and still behaves identically.
Thresholds live in code. A future phase with real history will recalibrate them.
At that moment every "historical" signal recomputed on read silently becomes a
statement about the new rules applied to old data, which is precisely the
substitution of hindsight for record that Phase 8B was built to prevent.
Versioning the rules helps only if a reader can still run every past version's
code, which means never deleting any of it.

## Decision

**We will store derived market signals in `pit_signal_snapshots`,** append-only,
unique on `(company_id, snapshot_date, signal_version)`, with no update or
delete path on the repository — the same contract as the other point-in-time
tables.

Each row carries **both** `signal_version` and `feature_version`, because a
signal is only meaningful against the features it read, and **`peer_population`**,
because a percentile without its population is not a percentile. It carries
`run_id`, identifying the capture run whose feature rows were consumed, so the
provenance is the exact rows and not merely the date.

Re-deriving a day under new rules **appends a second version beside the first**.
Neither restates the other, and a reader can compare what two sets of rules said
about the same day — which is the only honest way to evaluate a rule change.

Nothing in the table ranks. There is no composite column, no score, and no
blend with CompounderScore; Phase 8A falsified both, and the schema is where
that is easiest to enforce and hardest to erode.

## Consequences

**Storage cost is real and accepted.** One row per company per day per signal
version, roughly 5,800 rows a day at current universe size. Trivial next to
`price_history`, which holds 1.5 million rows for thirteen months.

**A signal can disagree with its own features.** If a future version changes a
threshold, the stored V1 row and a fresh V2 row will differ for the same day.
That is the intended behaviour and the reason both versions are stored.

**Derivation is a separate command from capture.** `capture` writes features;
`derive-signals` reads them and writes signals. Keeping them apart is what makes
a new signal version derivable over the whole existing history without
re-capturing anything, and it means a bug in the signal rules can never damage
the feature record.

**The features remain the source of truth.** Anything in `pit_signal_snapshots`
can be rebuilt from `pit_feature_snapshots` by running the corresponding
version's code. The stored signals are a cache of a judgement, not a new fact —
and if the two ever disagree, the features win.

**A reader must filter on version.** Selecting from this table without a
`signal_version` predicate will mix rules once a second version exists, exactly
as it would on `score_snapshots`. The repository methods all require one.
