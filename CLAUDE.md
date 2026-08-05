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
