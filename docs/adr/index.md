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
