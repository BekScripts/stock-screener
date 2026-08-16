---
type: explanation
title: Decisions
---

# Architecture decisions

Decisions that are expensive to reverse, with the alternatives that were
rejected and why. Records are numbered sequentially, never renumbered, and never
deleted — a decision that turned out wrong is superseded by a new record, not
edited away.

See the `adr` skill for when to write one and the format to use.

| # | Decision | Status |
| --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-use-uv-workspaces.md) | Use uv workspaces for shared packages | Accepted |
| [0003](0003-normalise-provider-data-at-the-boundary.md) | Normalise provider data into domain models at the boundary | Accepted |
| [0004](0004-apply-the-liquidity-threshold-only-to-consolidated-volume.md) | Apply the liquidity threshold only to consolidated volume | Accepted |
| [0005](0005-absent-debt-is-unknown-not-zero.md) | Absent debt is unknown, never zero | Accepted |
| [0006](0006-score-snapshots-are-versioned-and-immutable.md) | Score snapshots are versioned, and history is never re-scored | Accepted |
| [0007](0007-missing-metrics-are-carried-by-their-own-component.md) | A missing metric is carried by its own component | Accepted |
| [0008](0008-broad-scan-on-free-data-metered-enrichment-last.md) | Scan on free data; spend metered requests on candidates only | Accepted |
| [0009](0009-the-research-contract-is-its-own-package.md) | The AI research contract is its own package, and holds no model client | Accepted |
