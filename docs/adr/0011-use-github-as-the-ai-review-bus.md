# 0011. Use GitHub pull requests as the AI review bus

- Status: Accepted
- Date: 2026-10-22

## Context

Implementation has been performed with Claude while independent architecture and
safety review happened separately. The manual handoff copied a completion report
out of Claude, pasted it into another reviewer, copied a corrective prompt back,
and repeated. That preserved independence but made the human a transport layer,
lost line-level review context, and made it too easy for a prose report to stand
in for inspection of the actual code.

The repository already has CI, shared agent instructions, Claude configuration,
and Codex configuration. Pull requests already provide an immutable-ish commit
boundary, a diff, CI results, review threads, and an audit trail.

The trading-related parts of the project also require a hard distinction between
ordinary code correction and privileged operator decisions. An automated fixer
must not turn a review finding into permission to re-arm a runtime, mutate a
broker account, change owner-approved limits, or enable LIVE behavior.

## Decision

GitHub pull requests will be the automated handoff surface.

Claude is the implementation/fix agent. Codex is the independent reviewer.
Codex runs read-only and returns a structured verdict. Only
`CHANGES_REQUESTED` may invoke Claude automatically.

The loop permits at most two automatic fix attempts per PR. Each Claude fix is
pushed by the Claude GitHub App to the current PR branch, producing a new PR
head that receives fresh CI and Codex review. `HUMAN_REQUIRED` and
`ARCHITECTURE_BLOCKED` stop automation. A final non-approval fails the
`AI Review Gate` check.

The workflow never merges. Main remains human-controlled, and privileged
runtime/broker/deployment actions remain outside GitHub Actions.

Repository review rules live in `AGENTS.md` and
`.ai/REVIEW_CONTRACT.md`. The machine-readable review schema lives beside
them.

## Alternatives considered

### Keep the manual copy/paste loop

This preserves independence but makes the owner a message bus, loses direct
access to line-level context, and scales poorly as review cycles become more
frequent.

### Let one model implement and self-review

This is simpler but removes the independent-review property that has repeatedly
found integration defects hidden by plausible implementation reports.

### Have the models call each other directly

A private model-to-model channel is harder to audit and can blur authority. A
GitHub PR gives both agents the same durable evidence and leaves the full
conversation visible to the human.

### Automatically merge after approval

Rejected. Automated review is an additional reviewer, not merge authority, and
this repository contains runtime and financial boundaries where explicit human
approval remains intentional.

## Consequences

Review/fix cycles no longer require copying reports and prompts between tools.
Findings stay attached to the exact PR and code revision.

The repository now depends on OpenAI API access, Anthropic API access, and the
Claude GitHub App for the automated loop, and incurs API usage costs. If either
service is unavailable, the review gate fails closed rather than merging
without review.

Agent prompts and review contracts become production-like workflow
configuration and need the same care as other CI changes.

The active AlphaTrader feature branch may be ahead of GitHub main; this
automation change must be incorporated into that branch before its first
automated PR.
