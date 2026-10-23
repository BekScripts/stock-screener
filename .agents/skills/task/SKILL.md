---
name: task
description: Start, update, inspect, or clear the explicit development task attached to the current branch.
argument-hint: "<task prompt> | update <requirement change> | status | clear"
disable-model-invocation: true
---

# Explicit development task

This skill defines the task boundary. Never infer a new task from ordinary chat.
Only the user invoking `/task` may create or intentionally update the task contract.

The exact arguments typed by the user are:

<task-arguments>
$ARGUMENTS
</task-arguments>

Read `.agents/skills/git-workflow/SKILL.md` before changing branches or using
GitHub.

## Modes

Interpret the arguments in this order:

- no arguments or `status`: show the active task.
- `update <text>`: append an explicit requirement update to the active task.
- `clear`: clear local state only after confirming the linked GitHub issue is
  already closed.
- anything else: start a new task using the entire argument string as the
  original prompt.

Do not treat words like "update", "status", or "clear" inside a longer normal
task prompt as a mode unless they are the first token.

## Status

Run:

```bash
uv run scripts/ai_task.py status
```

If an active task exists, also read the remote issue with `gh issue view` and
report if its state differs from the local expectation. Do not modify anything.

## Start a task

1. Run `uv run scripts/ai_task.py status --json`, `git status --short`, and
   `git branch --show-current`.
2. If local task state exists:
   - read the linked issue with `gh issue view <number> --json state,url,title`;
   - if it is open, stop: one branch has one active task;
   - if it is closed, run `uv run scripts/ai_task.py clear` before continuing.
3. Never start a task on a detached HEAD.
4. If currently on `main` or `master`, derive a short conventional branch
   name from the task (`feat/`, `fix/`, `docs/`, `chore/`, etc.) and create
   it with `git switch -c <branch>`. Do not commit or push merely because the
   task was created.
5. If already on a non-default branch, bind the task to that branch. If the
   working tree obviously contains unrelated work, stop and explain rather than
   mixing tasks.
6. Create `.claude/tmp/` if needed. Write these files:
   - `.claude/tmp/ai-task-title.txt`: a concise task title.
   - `.claude/tmp/ai-task-body.md`: the issue body below.

The issue body must use exactly this structure:

```markdown
## Original task / prompt

<copy the complete text inside <task-arguments> verbatim; do not summarize,
correct grammar, or silently omit anything>

## Acceptance criteria

<derive concrete, testable criteria from the prompt; do not invent requirements>

## Constraints

<derive explicit constraints from the prompt plus applicable repository safety
boundaries; distinguish repository rules from user-requested constraints>
```

The **Original task / prompt** section is immutable. Acceptance criteria may
clarify the prompt but may not replace or narrow it.

7. Create the GitHub issue directly so Claude Code's permission policy can
   surface the side effect:

```bash
gh issue create --title "<title>" --body-file .claude/tmp/ai-task-body.md
```

Do not hide GitHub mutation inside another script.

8. From the URL returned by `gh issue create`, bind the durable issue to the
   current local branch:

```bash
uv run scripts/ai_task.py bind \
  --issue-url "<issue-url>" \
  --branch "<current-branch>" \
  --title-file .claude/tmp/ai-task-title.txt \
  --body-file .claude/tmp/ai-task-body.md
```

9. Report the issue URL, branch, and a short acceptance-criteria summary. Then
   continue normal work only when the user asks; creating the task is not
   permission to implement unrelated changes.

## Update requirements

For `/task update <text>`, the text after the first `update ` is an explicit
requirement change. Preserve that update verbatim.

1. Require active task state and verify the current branch matches it.
2. Write the exact update text to `.claude/tmp/ai-task-update.md`.
3. Run:

```bash
uv run scripts/ai_task.py prepare-update \
  --text-file .claude/tmp/ai-task-update.md \
  --output .claude/tmp/ai-task-body.md
```

4. Edit the linked issue directly:

```bash
gh issue edit <issue-number> --body-file .claude/tmp/ai-task-body.md
```

5. Only after the GitHub edit succeeds, refresh local context:

```bash
uv run scripts/ai_task.py sync-body \
  --body-file .claude/tmp/ai-task-body.md
```

Later entries under **Requirement updates** override earlier conflicting
requirements. Never rewrite the original prompt.

## Clear

For `/task clear`, first read the linked GitHub issue state. If it is still
open, do not clear it; explain that the task is still active. If it is closed,
run:

```bash
uv run scripts/ai_task.py clear
```

Clearing local state does not delete an issue, branch, commit, or PR.
