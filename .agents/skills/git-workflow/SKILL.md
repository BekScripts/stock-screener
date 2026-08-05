---
name: git-workflow
description: Branch, commit, or open a pull request. Covers branch naming, Conventional Commits, commit scope and granularity, PR descriptions, and the operations that always require confirmation first. Use when committing, branching, opening or updating a PR, writing a commit message, or asked about git history.
---

# Git workflow

## Always confirm first

Never do any of these without explicit approval, even if it seems obviously
right:

- committing or pushing when the user did not ask
- `push --force` (use `--force-with-lease` when approved)
- `rebase`, `reset --hard`, `commit --amend` on pushed commits
- deleting a branch, or anything that rewrites shared history
- committing directly to `main`

If you are on `main` and need to commit, create a branch first.

## Branches

```
<type>/<short-description>       feat/invoice-retry
<type>/<ticket>-<description>    feat/PROJ-412-invoice-retry
```

Types match commit types below. Lowercase, hyphenated, no personal names.
Include the Jira key when there is one — the Atlassian MCP server can then link
the branch to the issue.

## Commits — Conventional Commits

```
<type>(<scope>): <subject>

<body>

<footer>
```

| Type | For |
| --- | --- |
| `feat` | new capability |
| `fix` | bug fix |
| `refactor` | behaviour unchanged |
| `test` | tests only |
| `docs` | documentation only |
| `chore` | tooling, dependencies, config |
| `ci` | workflow changes |
| `perf` | performance |

Scope is the member or area: `feat(billing):`, `fix(app):`, `chore(deps):`.

```
feat(billing): add cursor pagination to invoice listing

Offset pagination degraded past ~50k rows because the database still
scanned every skipped row. Cursor pagination keeps it constant-time.

Refs: PROJ-412
```

Rules:

- Subject in the imperative, under 72 characters, no trailing period. "add
  pagination", not "added pagination" or "adds pagination".
- **The body explains why, not what.** The diff already shows what changed. If
  the change is self-evident, omit the body.
- Breaking change: `feat(api)!:` and a `BREAKING CHANGE:` footer explaining the
  migration.

## Granularity

One logical change per commit. A commit that fixes a bug, renames a variable,
and bumps a dependency cannot be reviewed, reverted, or bisected.

Keep separate: dependency upgrades, formatting-only changes, and behaviour
changes. Formatting mixed into a logic change hides the logic change.

## Before committing

```bash
make check          # must pass
git diff --staged   # read it — no debug prints, no secrets, no stray files
```

Never commit: `.env`, credentials, `.venv/`, editor config, commented-out code,
or a file you did not intend to touch.

## Pull requests

The description explains the change to someone with no context:

```markdown
## What
One paragraph on what changed.

## Why
The problem this solves. Link the issue or Jira ticket.

## How
Notable implementation decisions, and anything rejected along the way.

## Testing
What you ran, and what a reviewer should check by hand.
```

- Small PRs get real reviews; 2,000-line PRs get rubber-stamped.
- Self-review the diff before requesting review.
- CI green before asking anyone to look.
- Use the GitHub MCP server to open the PR and read review comments.

## Checklist

- [ ] Not on `main`; branch named by convention
- [ ] `make check` passes
- [ ] Staged diff read; no secrets, debug output, or stray files
- [ ] Conventional Commit with correct type and scope
- [ ] Subject imperative and under 72 chars; body explains why
- [ ] One logical change per commit
- [ ] User approved the commit/push
