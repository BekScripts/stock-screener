---
type: explanation
title: Overview
---

# app

> TODO: one-line description of this project.

## Getting started

```bash
make setup     # install dependencies, link agent skills, create .env
make check     # lint, types, tests
```

## How these docs are organised

Documentation follows [Diátaxis](https://diataxis.fr): four types, split by what
the reader is trying to do. Every page declares its `type` in frontmatter, and a
page belongs to exactly one.

| Section | The reader is… | Written as |
| --- | --- | --- |
| [Tutorials](tutorials/index.md) | learning by doing | a lesson that always works |
| [How-to](how-to/index.md) | achieving a specific goal | a recipe for someone who knows the basics |
| [Reference](reference/index.md) | looking something up | dry, complete, no opinions |
| [Explanation](explanation/index.md) | trying to understand | discussion of the why |
| [Decisions](adr/index.md) | asking why it's built this way | numbered, immutable ADRs |

The split exists because these modes conflict. A tutorial that stops to explain
trade-offs loses the beginner; a reference page with a worked example goes stale.
See the `documentation` skill before adding a page.

## Where the code lives

| Path | Contains |
| --- | --- |
| `src/app/` | the application |
| `packages/*/` | shared libraries |
| `tests/` | tests for `src/` |
| `.agents/skills/` | the procedures AI agents follow in this repo |
