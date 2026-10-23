@AGENTS.md

## Claude Code specifics

The instructions above are shared with every agent. These apply to Claude Code
only.

### Skills

Skills are symlinked from `.agents/skills/` into `.claude/skills/`, so `/<name>`
works for every skill in the AGENTS.md table. After adding a skill to
`.agents/skills/`, run `make sync-skills` to create the symlink.

### Plan mode

Use plan mode before: any change spanning more than three files, adding or
removing a workspace package, and any change under `.github/workflows/`.

### Subagents

Delegate broad codebase searches to the `Explore` agent rather than reading
files one by one. Keep the main context for the change itself.

### Verifying

`make check` is the gate. Run it before reporting a task complete — not the
individual tools, since `check` is what CI runs.

**Never verify a migration against `compounder_radar.db`.** Use a temporary
database or a throwaway copy. A migration is not only DDL: `batch_alter_table`
rebuilds a table on SQLite, and dropping `companies` under enforced foreign keys
cascades through every child declaring `ON DELETE CASCADE` — price history,
fundamentals, scores, filings, excerpts, both research tables and the watchlist.
Migration `0017` did exactly that and deleted about 1.6 million rows from the
primary database, which is why
`tests/integration/test_migrations.py::test_migrating_a_populated_database_preserves_its_rows`
exists: it populates every one of those tables against a temp database and
counts the rows on the other side. Run that rather than the real thing.


### Automated review-fix mode

When `.ai/current-review.json` exists, Claude is running as the implementation
agent in the GitHub AI review loop.

- Read `.ai/REVIEW_CONTRACT.md` and the review JSON before editing.
- Address only findings whose verdict is `CHANGES_REQUESTED`; do not broaden
  scope while fixing review comments.
- A `HUMAN_REQUIRED` or `ARCHITECTURE_BLOCKED` verdict is a stop condition,
  not permission to invent a policy or workaround.
- Do not modify `.github/workflows/**` automatically in response to an ordinary
  product-code review finding.
- Run `make check` after fixes. If frontend code changed, run
  `make check-web` when the runner has the required Node dependencies.
- The repository owner pre-authorizes the Claude GitHub Action, when invoked by
  `AI PR review loop`, to create and push ordinary fix commits to the current
  PR branch only. That is the explicit commit/push approval required by the
  git-workflow skill for this mode.
- After the requested fix is verified, stage the current fix, commit it, and
  publish only to the current PR branch. The workflow grants only the narrow
  publication commands needed for that same-branch update.
- That authorization does not permit merge, rebase, force-push, writes to
  `main`, runtime re-arm, broker mutation, deployment/state transition, secret
  changes, or LIVE trading.
