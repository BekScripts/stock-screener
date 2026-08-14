# 0004. Apply the liquidity threshold only to consolidated volume

- Status: Accepted
- Date: 2026-08-14

## Context

The eligibility screen requires an average daily dollar volume of at least $1m.
That number is calibrated against the consolidated tape — every U.S. venue.

A free Alpaca data plan cannot read the consolidated tape. Requesting it returns
`403 subscription does not permit querying recent SIP data`; the only feed
available is IEX, a single exchange. Measured across four symbols, IEX carries
**1.7% to 3.7%** of consolidated volume, and the share is not constant between
securities or over time.

Applying the $1m threshold to IEX volume therefore behaves like a threshold of
roughly $25m–$50m on the real market. Compounder Radar exists to find smaller,
under-recognised companies — its Hidden Gems view is defined as market cap under
$5bn — so that distortion would remove much of the intended universe, silently,
with every excluded company labelled `LOW_LIQUIDITY` as though the figure were
whole-market.

## Decision

Volume carries its basis. `VolumeBasis` is one of `CONSOLIDATED`, `PARTIAL` or
`UNKNOWN`, and it travels with the figure from the adapter through to the CSV.

The threshold is applied **only** to a `CONSOLIDATED` figure. Where the only
volume available is partial, the security is not excluded; the result carries an
`EligibilityWarning.LIQUIDITY_UNVERIFIED`, the console marks the number with an
asterisk and prints a footnote, and the CSV exports both the basis and the
warning as columns.

Where a fundamentals provider supplies a consolidated average share volume — FMP
does, on the same profile request already made, for every symbol tested — that
is preferred over anything derived from price bars.

The insufficient-history check is unchanged and still excludes: four sessions is
not a twenty-day average regardless of which feed produced them.

## Alternatives considered

**Scale IEX volume by a constant.** Rejected, and explicitly ruled out in the
Phase 1 review. The measured share ranged from 1.7% to 3.7% across four symbols;
a single multiplier would be wrong by a factor of two either way, and would
produce a confident number with no basis.

**Lower the threshold when the feed is IEX.** Same objection with extra steps. It
also bakes a market-structure assumption into configuration, where it would rot
silently as IEX's share drifts.

**Keep excluding on partial volume.** Rejected. It is the status quo and it is
wrong in the most damaging direction for this project: it discards small
companies while reporting a specific, credible-looking dollar figure as the
reason.

**Require a paid data plan.** Rejected as a Phase 1 gate. It is the right answer
eventually, and `ALPACA_FEED=sip` is a one-line change once the plan exists, but
the tool must be usable before then.

## Consequences

With `edgar+fmp` configured the threshold works properly and on better data than
before: FMP's consolidated average gives Apple an ADV of $16.3bn, against the
$618m the IEX bars implied.

Without a consolidated source the liquidity filter stops filtering. A scan run
that way returns more companies than it should, and the operator must read the
asterisk to know it. That is a deliberate trade — an over-inclusive list that
says so beats an under-inclusive one that does not.

The basis is a property of the configured feed at scan time, not of each stored
bar. Re-ingesting after changing `ALPACA_FEED` mixes two bases in one table, so
the feed and the price history should be changed together.

Phase 2 must not treat `LIQUIDITY_UNVERIFIED` as a scoring input. It is a
statement about the data, not about the company.
