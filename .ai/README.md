# AI PR loop

GitHub is the handoff bus between Claude (implementation) and Codex
(independent review). The automation is intentionally limited to repository
code. It has no AlphaTrader runtime, brokerage, deployment, or LIVE-trading
authority.

## One-time repository setup

Add repository Actions secrets:

- `OPENAI_API_KEY` — used by the read-only Codex reviewer.
- `CLAUDE_CODE_OAUTH_TOKEN` — used by Claude Code when a review is fixable. Generate it with `claude setup-token` while signed into the intended Claude subscription.

Add the repository Actions variable:

- `AI_PR_LOOP_ENABLED=true`

Install/authorize the official Claude GitHub App for this repository. The App
matters because Claude's fix commit must trigger a fresh
`pull_request/synchronize` workflow on the new PR head. GitHub deliberately
prevents the default `GITHUB_TOKEN` from recursively triggering workflows.

Do not put Alpaca credentials, deployment credentials, or runtime secrets in
GitHub Actions.

After one successful canary PR, protect `main` and require:

- `CI`
- `AI Review Gate`
- at least one human approval

The repository currently has no branch protection, so this is an important
final setup step.

## Flow

1. Claude or a human opens/updates a same-repository, non-draft PR.
2. Codex reviews the exact PR head read-only under
   `.ai/REVIEW_CONTRACT.md`.
3. `APPROVE` passes `AI Review Gate`.
4. `CHANGES_REQUESTED` is handed to Claude as structured JSON.
5. Claude fixes only those findings, runs the repository gate, and the Claude
   GitHub App pushes a fix commit to the same PR branch.
6. That push creates a new PR head and triggers fresh CI + Codex review.
7. At most two Claude fix attempts are allowed per PR.
8. `HUMAN_REQUIRED` and `ARCHITECTURE_BLOCKED` stop immediately.
9. The workflow never merges.

The sticky PR comment stores the automatic-fix count and the latest review, so
the state survives across workflow runs without adding state files to the
repository.

## Why the fixer pushes itself

The workflow intentionally does not commit Claude's edits with the default
GitHub Actions token. A commit made with that token does not reliably trigger
the next workflow run. Using Claude's GitHub App makes the new head observable
to normal PR automation, so branch protection can require checks on the exact
commit being merged.

## Cost and credentials

The Codex reviewer uses the OpenAI API and is billed through the OpenAI API account. Claude fixes use `CLAUDE_CODE_OAUTH_TOKEN`, so Claude Code runs against the limits of the connected Claude subscription instead of the separate Anthropic API credit balance.

## Active local AlphaTrader branch

The long-running AlphaTrader execution branch may be ahead of GitHub `main`.
Merge this automation PR first, then bring the automation commit into that local
feature branch before opening its first automated PR.
