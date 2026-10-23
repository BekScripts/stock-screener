---
name: ship
description: Validate and open one pull request for the explicit /task attached to the current branch.
disable-model-invocation: true
---

# Ship the active task

This skill turns one explicit `/task` into one pull request. It never creates a
task, never merges a PR, and never treats ordinary conversation as requirements.

Read `.agents/skills/git-workflow/SKILL.md` before doing anything.

## Preconditions

1. Run:
   ```bash
   uv run scripts/ai_task.py status --json
   git branch --show-current
   git status --short
   ```
2. Refuse to continue if there is no active task, the current branch differs
   from the task branch, the branch is `main`/`master`, or HEAD is detached.
3. Read the linked issue with:
   ```bash
   gh issue view <issue-number> --json number,title,state,url,body
   ```
   The issue must still be open.
4. Refresh the local cached task body from the issue body before rendering the
   PR. Write the exact remote body to `.claude/tmp/ai-task-body.md`, then run:
   ```bash
   uv run scripts/ai_task.py sync-body \
     --body-file .claude/tmp/ai-task-body.md
   ```
5. Fetch `origin/main` and verify the current branch contains the latest
   `origin/main`. Do **not** rebase, merge, reset, or rewrite history on the
   user's behalf merely to satisfy this check. If it is behind, stop and tell
   the user to sync the branch first.

## Verify and commit

1. Inspect the complete task diff and relevant surrounding code.
2. Run `make check`.
3. If any file under `frontend/` changed relative to `origin/main`, also run
   `make check-web`.
4. If a check fails, stop and fix/report the failure; do not open the PR as if
   it passed.
5. Self-review for secrets, stray files, debug output, generated artifacts, and
   scope drift.
6. If there are uncommitted intended changes, follow the git-workflow skill for
   staging and conventional commits. Invoking `/ship` expresses intent to ship,
   but do not bypass Claude Code permission prompts for `git commit` or other
   protected operations.
7. Push the current branch with ordinary `git push`. Never force-push. Do not
   hide the push inside a helper script; the direct command must remain visible
   to the permission system.

## Build the PR description

Create `.claude/tmp/ai-pr-title.txt` containing a concise PR title that
follows the repository's naming conventions.

Create `.claude/tmp/ai-pr-implementation.md` containing a concise summary of
what the branch actually implements. Claims must be supported by the diff.

Create `.claude/tmp/ai-pr-testing.md` containing the exact verification
commands and outcomes. Do not claim a check passed if it did not run.

Render the PR body:

```bash
uv run scripts/ai_task.py render-pr \
  --implementation-file .claude/tmp/ai-pr-implementation.md \
  --testing-file .claude/tmp/ai-pr-testing.md \
  --output .claude/tmp/ai-pr-body.md
```

The rendered body deliberately contains the durable task contract first, then
implementation/testing evidence, and `Closes #<task>`. Do not replace the task
contract with a summary of the conversation.

## Open exactly one PR

Before creating anything, check whether this branch already has an open PR:

```bash
gh pr view "<current-branch>" --json number,url,title
```

If one exists, report it instead of creating a duplicate.

Otherwise create one directly so Claude Code's permission rule remains visible:

```bash
gh pr create \
  --base main \
  --head "<current-branch>" \
  --title "$(cat .claude/tmp/ai-pr-title.txt)" \
  --body-file .claude/tmp/ai-pr-body.md
```

Do not hide PR creation inside another script. Never merge the PR.

After creation, report the PR URL and remind the user that the repository's
Codex → Claude fix → Codex review loop now owns automated review/fix rounds.
