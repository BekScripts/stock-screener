---
type: reference
title: Backtest data contract
---

# Backtest data contract

`BACKTEST_DATA_CONTRACT_V1` is the machine-readable statement of what a
historical strategy may see, when it became knowable, which identity it
belongs to, what the labels are, and which digests define the dataset it
reads. It binds `PIT_RESEARCH_DATASET_V2` — the frozen research dataset
built over `SEC_EVIDENCE_GENERATION_V2` — and it is fail-closed: research
code that does not present the contract's bindings is refused.

Three names, three concepts:

| Name | Is |
| --- | --- |
| `SEC_EVIDENCE_GENERATION_V2` | the evidence generation: 99,774 trusted artifacts, manifest `a5653f1c…`, **acquisition frozen** |
| `PIT_RESEARCH_DATASET_V2` | the frozen research dataset: the post-A3 index `e6ce97d6…`, the PIT materialisation (marker `e5f3bbbd…`), the residual audit (`ae49ae16…`), and the research database's canonical projections |
| `BACKTEST_DATA_CONTRACT_V1` | the rules a backtest must honour to read it, and the digests it must bind to |

The contract lives under `<SEC_CONTROL_DIR>/contracts/backtest_v1/`, is
written by `evidence freeze-backtest-contract`, and is mirrored under
`control/SEC_EVIDENCE_GENERATION_V2/backtest-contract/`. The module is
`stock_screener.backtest_contract`.

## Files

| File | Holds |
| --- | --- |
| `backtest_data_contract_v1.json` | the whole contract: bindings, lineage, horizon, identity, time and availability semantics, missingness, current-metadata audit, price and corporate-action semantics, universe filters, `COMPOUNDER_V1_2` and `MARKET_SIGNALS_V1` input classifications, allowed feature sets, forbidden label sets, research policy, the first safe research class |
| `backtest_dataset_manifest.json` | the dataset identity and every semantic fingerprint, by layer |
| `backtest_readiness_matrix.json` | one row per subsystem: `READY`, `READY_WITH_DOCUMENTED_LIMITATION`, `BLOCKED`, `PARTIAL` or `NOT_REQUIRED_FOR_FIRST_BASELINE`, each with why, impact and minimum fix |
| `historical_time_semantics.json` | the timestamps, the decision instant, SEC availability, same-day filings, execution timing, forward returns, the rebalance primitive |
| `feature_label_separation.json` | the layers, the label fields, the reviewed lookalikes, the modules that may not import labels |
| `backtest_run_manifest_schema.json` | the JSON schema every future backtest run must emit |
| `backtest_readiness_summary.json` | the verdict and the subsystem lists |
| `backtest_contract_complete.json` | the deterministic completion marker: bindings, core fingerprints, verdict, blockers, the first safe research class, and the digest of every other file |

Every file is written with sorted keys and no timestamp, so a second freeze
over the same inputs reproduces it byte for byte.

## Bindings

`ContractBindings` names what a run must present: `dataset_id`,
`contract_version`, `universe_sha256`, `evidence_manifest_sha256`,
`post_a3_index_sha256`, `pit_marker_sha256`, `planning_inputs_sha256`,
`a3_plan_sha256`, `pit_audit_marker_sha256`, `database_sha256`, and the
dataset fingerprints. `load_contract(directory, expect=)` refuses the
contract unless every one matches and every file hashes as the completion
marker recorded.

## Fingerprints

`fingerprint_dataset` digests a canonical projection of each table a
historical observation is built from — a total order, no autoincrement ids,
no observation timestamps — over a read-only connection:

| Layer | Projections |
| --- | --- |
| UNIVERSE | `historical_universe` (research_universe_months), `universe_membership`, `security_identifiers` |
| FEATURES | `fundamental_observations`, `filing_acceptance`, `historical_observations`, `price_history`, `historical_prices`, `benchmark_prices`, `corporate_actions`, `feature_observations` (pit_feature_snapshots) |
| SIGNALS | `signal_observations` (pit_signal_snapshots) |
| LABELS / EVALUATION ONLY | `forward_return_labels` (forward_returns), `delistings` |

## The rules the contract states

Each is a rule the code already enforces, with its owner named in the
contract; the contract adds none.

- **Observation identity** is `(kind, identity_id)` — `COMPANY → companies.id`,
  `HISTORICAL → historical_securities.id` — exactly one set on every
  dual-identity row. A ticker is an attribute of a (security, date), a CIK is
  the issuer, a CUSIP is a dated identifier of an issue; none is a key.
- **`as_of` is a decision at the close** (16:00 America/New_York). A company
  filing is available iff `accepted_at <= decision_instant(as_of)`; where the
  instant is unknown, `filed_date < as_of`. **Historical securities** select
  by `filed_date <= as_of` today, so research must apply `filed_date < as_of`
  to them (one-session lag, never early) — `PIT_SAFE_WITH_LAG`.
- **Execution** is the open of the first session strictly after `as_of`;
  the only decision points are CLOSE and OPEN; no intraday precision exists.
- **Forward returns** are labels: 1/5/20/60/120 sessions on the SPY calendar,
  entry at the next session's open, adjusted basis, five statuses;
  `INTERRUPTED` carries no number and a terminal leg must be a stated policy,
  never 0 or −100 % by default.
- **Missing is not zero.** `UNKNOWN`, `UNVERIFIED`, `OPEN` and `None` are
  states; no generic `fillna(0)`.
- **Current metadata** — `companies.exchange`, `.sector`, `.statement_profile`
  — is undated and reaches the eligibility verdict; it is recorded as
  `CURRENT_APPLIED_BACKWARD`, and `MEMBER` verdicts are `INFERRED` for that
  reason. Sector is `BLOCKED` for research.
- **Prices** are daily, single-venue (IEX), `adjustment=all`; as-traded
  prices are recovered exactly through splits and are lower bounds because
  distributions are not undone; liquidity floors read in one direction.
- **The label layer is not imported** by `domain.scoring`, `domain.signals`,
  `domain.metrics`, `domain.eligibility`, `stock_screener.scoring`,
  `stock_screener.signals`, `stock_screener.pit.metrics`,
  `stock_screener.pit.capture`, `stock_screener.backtest.scores` or
  `stock_screener.backtest.historical` — a static test holds it.

## First safe research class

Monthly cross-sectional research on the frozen identity-month universe:
signal at the last session's close of each month from PIT fundamentals, the
frozen `COMPOUNDER_V1_2` reconstruction and `MARKET_SIGNALS_V1`; long-only;
execution at the next session's open; horizons 20 or 60 sessions; delisted
securities included with `INTERRUPTED` outcomes handled by a stated rule.
Not supported yet: intraday strategies, sector-neutral construction,
consolidated-liquidity filters, regime overlays.

## Research policy

Every parameterised or learned strategy declares development, validation
and final-holdout ranges before its first run; `variants_tried`,
`parameter_sweeps` and `selection_criterion` are required run-manifest
fields; every run names a passive benchmark and reports the required metrics
(CAGR, volatility, Sharpe, Sortino, max drawdown, Calmar, turnover, hit
rate, exposure, cash utilisation, costs, slippage, benchmark-relative return,
trades, holdings, concentration). B2.18 produced no strategy result.

## Canaries

`tests/integration/test_backtest_contract.py` holds the leakage canaries:
a filing accepted after the decision instant cannot change what a decision
saw (same-day 15:55 admitted at the close, 17:30 not, OPEN admits neither);
a later restatement is invisible earlier; a bar after the decision date
changes the label and not the feature; no feature or scoring module imports
the label layer; two occupants of one symbol never join to each other; two
classes under one CIK are not both credited by a filing naming one; a
delisted security keeps its historical months without a `companies` row;
`UNKNOWN` stays `UNKNOWN`; the contract loads under its own bindings and
refuses altered ones or a tampered file.
