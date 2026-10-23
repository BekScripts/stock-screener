# AI PR loop

GitHub is the handoff bus between Claude (implementation) and Codex
(independent review). The automation is intentionally limited to repository
code. It has no AlphaTrader runtime, brokerage, deployment, or live-trading
authority.

## One-time repository setup

Add repository Actions secrets:

- `OPENAI_API_KEY` — used only by the read-only Codex review action.
- `ANTHROPIC_API_KEY` — used only by Claude Code when a review is fixable.

Then add the repository Actions variable:

- `AI_PR_LOOP_ENABLED=true`

In **Settings → Actions → General → Workflow permissions**, allow GitHub Actions
to use a read/write repository token; the workflow needs `contents: write` only
to commit an independently approved Claude fix back to the PR branch.

Do not put Alpaca credentials or runtime secrets in GitHub Actions.

After the first successful run, protect `main` and require:

- `CI`
- `AI Review Gate`
- at least one human approval

The workflow ignores draft PRs and PRs from forks. It allows at most two Claude
fix rounds. `HUMAN_REQUIRED` and `ARCHITECTURE_BLOCKED` never invoke Claude.

## Flow

1. Claude or a human opens a non-draft PR.
2. Codex reviews the current PR workspace read-only using the structured review
   contract.
3. `APPROVE` passes the gate.
4. `CHANGES_REQUESTED` gives the exact structured findings to Claude.
5. Claude edits the checkout and runs `make check` (plus `make check-web`
   when frontend files changed).
6. Codex reviews the modified workspace again.
7. At most two fix rounds are attempted.
8. The workflow commits any accepted/partial Claude fixes to the PR branch.
9. Any final non-approval fails `AI Review Gate` and requires a human.

## Cost and credentials

This workflow uses the OpenAI and Anthropic APIs, so it is billed through those
API accounts rather than ChatGPT/Claude subscription message allowances. Keep
the keys repository-scoped and rotate them if exposed.

## Local phase branch

The long-running AlphaTrader execution work may be ahead of GitHub `main`.
Merge this automation PR first, then merge/rebase that automation commit into
the active local feature branch before opening its first automated PR.
