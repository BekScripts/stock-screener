# 0003. Normalise provider data into domain models at the boundary

- Status: Accepted
- Date: 2026-08-12

## Context

Compounder Radar needs market data and fundamentals from commercial vendors.
Phase 1 has Alpaca credentials but no fundamentals key, and the Phase 1
specification is explicit that the eventual fundamentals vendor "can be selected
based on available subscription/API access" — that is, it is not yet known.

Vendors disagree about almost everything that matters here. Alpaca compresses
bar fields to single letters (`c` for close) and has no fundamentals at all. FMP
reports capital expenditure as a negative number, splits one quarter across three
endpoints, and sometimes omits `totalDebt` in favour of two maturity buckets.
Both paginate differently, and both meter requests.

Meanwhile the part of the system that actually matters — the metric engine — is
pure arithmetic over revenue, margins and share counts. Its correctness is
checkable against hand-worked examples, and Phase 2's entire scoring model will
be built on top of it.

The question was where vendor-shaped data stops being vendor-shaped.

## Decision

We will normalise provider responses into `domain` models inside the adapter, at
the boundary. `packages/api-clients` accepts vendor payloads and returns
`CompanyProfile`, `PriceBar` and `FinancialPeriod`; nothing else in the system
ever sees a vendor field name, status code or pagination token.

Three consequences of that rule are decided here too:

- **Sign conventions are normalised at the boundary.** Capital expenditure is
  stored as a positive outflow whatever the vendor sent.
- **Missing stays missing.** A field a vendor omitted becomes `None`, never
  `0.0`, in the model and in the database column.
- **Persistence stores the normalised shape.** `financial_snapshots` mirrors
  `FinancialPeriod`, and raw vendor payloads are not the primary schema.

## Alternatives considered

**Store raw vendor JSON and normalise at read time.** Rejected. It preserves
everything, which is genuinely useful for a later reconciliation, but it puts
vendor-shaped data in the database, which means every consumer — the scanner,
the API, Phase 2's scorer — needs its own knowledge of each vendor's quirks. It
also makes a schema change at the vendor a silent behaviour change rather than a
loud adapter failure. Phase 1's own instruction is "do not store vendor-specific
response objects as the primary schema".

**Pass dictionaries between layers and normalise in the ingestion service.**
Rejected. It leaves the adapter trivial but pushes vendor knowledge one layer up,
where it mixes with database access, so neither can be tested without the other.
The metric engine's freedom from I/O is the property that makes its arithmetic
testable, and this erodes it by degrees.

**Normalise to one vendor's shape and adapt others to it.** Rejected. It sounds
cheaper than defining our own models until the "canonical" vendor is replaced,
at which point the vocabulary of a vendor no longer in use is embedded
throughout the system.

**Depend on vendor-computed metrics rather than deriving our own.** Rejected on
the specification's instruction to calculate metrics rather than trusting
precomputed vendor scores, and because vendors disagree on TTM windows and
dilution in ways that would make rankings unstable across a vendor change.

## Consequences

Swapping a data vendor is a change to one adapter and one setting. The metric
engine and the eligibility screen have no dependency on who supplied the data,
which is what makes them testable against hand-worked numbers rather than
against recorded API responses.

The mock providers become genuinely useful rather than a testing crutch: because
they return the same domain models as the real ones, a clone with no credentials
runs the entire pipeline, and the integration tests exercise real ingestion code.

What becomes harder: every new vendor field needs a domain field before it can be
used, so adding one touches the model, the adapter, the table and a migration. A
vendor's extra data is discarded rather than kept "just in case" — if a
reconciliation later needs the raw payloads, they will have to be captured
deliberately, and history before that point will not exist.

We are also committed to defending the boundary in review. Nothing in Python
prevents an adapter returning a dict, and the cost of one leak is not a broken
build but a slow drift back to vendor-shaped data everywhere.

A smaller commitment made alongside this: money is stored as `Float`, not
`Numeric`. Everything downstream is a ratio or a ranking, where double precision
is ample, and it avoids `Decimal` conversion on every read. This would be the
wrong call for a ledger; if Compounder Radar ever needs exact currency
arithmetic, this decision should be superseded rather than patched.
