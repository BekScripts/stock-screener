---
name: supervise
description: Run the AI project supervisor in read-only shadow mode and propose at most one next safe engineering task.
disable-model-invocation: true
---

# Shadow project supervision

This skill is advisory only. It must not mutate the repository, GitHub, runtime,
broker, deployment, credentials, or trading authority.

Read:

- `.ai/SUPERVISOR_CONTRACT.md`
- `.ai/supervisor-state.json`
- `.ai/CODEX_SUPERVISOR_PROMPT.md`

Then run:

```bash
uv run scripts/ai_supervisor.py validate
```

If the authoritative project-state file is missing, stop with
`NO_SAFE_TASK`. Do not recover by reading an unmerged feature branch as though
it were authoritative.

Otherwise render the supervisor prompt:

```bash
mkdir -p .claude/tmp
uv run scripts/ai_supervisor.py render \
  --output .claude/tmp/ai-supervisor-prompt.md
```

Follow that prompt and return exactly one supervisor verdict.

## Forbidden actions

Do not run or recommend executing any command that creates/edits an issue,
creates/switches a branch, commits, pushes, opens/edits/merges a PR, edits a
workflow, deploys, mutates runtime or broker state, uses credentials, or enables
LIVE trading.

In particular, this skill must never invoke `/task` or `/ship` automatically.

A `READY_TASK` result is a **proposal for comparison with the owner-approved
next task**, not permission to implement it.
