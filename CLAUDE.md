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
