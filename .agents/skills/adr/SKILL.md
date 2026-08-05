---
name: adr
description: Record an architecture decision as an ADR in docs/adr/. Covers when a decision warrants one, the record format, and how to supersede an earlier decision. Use when choosing between technical approaches, picking a library or datastore, changing a structural convention, or when asked why something was built a certain way.
---

# Architecture decision records

An ADR captures a decision that is **expensive to reverse**, and — the part
people skip — the alternatives that were rejected and why. Six months later the
question is never "what did we choose", it is "did we already consider X?".

## When to write one

Write an ADR for: choosing a datastore, framework, or protocol; introducing a
significant dependency; changing a repo-wide convention; a deliberate trade-off
someone will later think is a mistake.

Don't write one for: naming, formatting, anything a lint rule enforces, or
decisions that are cheap to undo. Those belong in a skill or in code.

If you're unsure, ask: **would a competent newcomer be tempted to change this
back?** If yes, write the ADR.

## Format

Sequentially numbered, never renumbered, never deleted:

```
docs/adr/0001-record-architecture-decisions.md
docs/adr/0002-use-uv-workspaces.md
```

```markdown
# 0002. Use uv workspaces for shared packages

- Status: Accepted
- Date: 2026-08-01

## Context

What forced a decision. The constraints, the pressures, what was true at the
time. Written so someone with no memory of the discussion understands the
problem before seeing the answer.

## Decision

What was chosen, in the active voice: "We will use uv workspaces…"

## Alternatives considered

The options rejected, and the specific reason each lost. This is the section
that earns the document its keep — without it you will re-litigate the same
choice annually.

## Consequences

What becomes easier, what becomes harder, and what you are now committed to.
Include the bad parts honestly; an ADR listing only benefits is marketing.
```

## Status

`Proposed` → `Accepted` → `Superseded by ADR-NNNN`.

**Never edit a decision after it is accepted, and never delete one.** If it
turns out wrong, write a new ADR that supersedes it and link both ways. The
record of having been wrong is the most useful part of the archive.

## Checklist

- [ ] Decision is genuinely expensive to reverse
- [ ] Next sequential number; existing records untouched
- [ ] Context explains the problem before the answer
- [ ] Alternatives listed with the specific reason each was rejected
- [ ] Consequences include the downsides
- [ ] Superseded records updated with a link, not deleted
