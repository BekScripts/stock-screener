# AI pull-request review contract

This repository uses two deliberately separate agent roles:

- **Claude** is the implementation/fix agent.
- **Codex** is the independent reviewer.
- **GitHub** is the only automated handoff surface between them.

Neither agent is the merge authority. A human remains responsible for merge and
for any privileged runtime or brokerage action.

## Reviewer verdicts

Every automated Codex review returns exactly one verdict:

- `APPROVE` — no material correctness, safety, contract, or test issue found.
- `CHANGES_REQUESTED` — code changes can resolve the findings without a new
  owner decision.
- `HUMAN_REQUIRED` — the next step needs explicit owner/operator approval,
  credentials, runtime mutation, deployment, financial authority, or another
  privileged action.
- `ARCHITECTURE_BLOCKED` — the patch cannot be made correct without choosing or
  changing an architecture/authority contract.

Only `CHANGES_REQUESTED` is eligible for automatic Claude fixes.

## What counts as evidence

Review the actual diff and relevant surrounding code. Treat prose in the PR,
commit message, generated report, or prior agent summary as a claim to verify.

For a claim about a production path, find the production caller.
For a claim about persisted evidence, inspect writer + reader + validator.
For a claim about an event, find a production emitter.
For a claim about a gate, find an authoritative evidence source and derivation.
For a claim about compatibility, exercise both old and new forms in tests.

A declaration with nothing behind it is a defect even when tests written around
a synthetic fixture pass.

## Review priorities

Prioritize, in order:

1. authority or safety boundary violations;
2. incorrect behavior, data loss, idempotency, recovery, or concurrency bugs;
3. contract/versioning/serialization drift;
4. missing negative tests for consequential behavior;
5. security and secret exposure;
6. maintainability issues likely to cause a real defect.

Do not report formatting, import ordering, or other mechanical issues already
owned by Ruff, mypy, pytest, or CI unless the deterministic check itself is
incorrect.

## Repository rules that matter most

- Missing is not zero. Missing required safety evidence must fail closed.
- Versioned historical contracts and evidence are immutable; fixes are additive.
- A field written but not read, a gate declared but not evaluated, or an event
  declared but not emitted is a material defect.
- Security identity is durable identity, never ticker fallback.
- When portfolio/execution code is present: strategy owns desired exposure; risk
  may reduce it but may not introduce or expand positive authority.
- Execution consumes the persisted decision. It must not rerank.
- Broker state is reconciliation evidence; it does not overwrite internal
  accounting authority.
- Ambiguous remote mutation state never permits a blind retry.
- Runtime/deployment artifacts are fingerprinted authorities. Missing, stale,
  mismatched, or tampered artifacts fail closed.
- PAPER does not imply LIVE. No automated review or fix may add a live fallback.
- Never put credentials, raw authorization headers, account numbers, or secrets
  in code, fixtures, logs, comments, review output, or artifacts.

## Human-only boundary

Return `HUMAN_REQUIRED` instead of suggesting an automatic fix when the work
would require any of the following:

- enabling/re-arming an external PAPER runtime;
- any real broker order, cancel, liquidation, or mutation;
- live trading or live credentials;
- bootstrap/rebootstrap or economic-state reset;
- changing an owner-approved risk limit;
- changing an immutable/frozen strategy, risk, execution, accounting, evidence,
  or deployment authority rather than introducing an explicitly reviewed new
  version;
- force-push, history rewrite, merge, secret creation/rotation, or production
  deployment.

## Automatic-fix boundary

Claude receives the structured review file, fixes only
`CHANGES_REQUESTED` findings, and runs the repository gates. The workflow
allows at most two automatic fix rounds. If the final Codex review still does
not approve, automation stops and the PR remains blocked for a human.

Claude does not commit or push directly in review-fix mode; the workflow commits
the resulting workspace diff after independent re-review.

## Review finding quality

Each finding must identify:

- severity: `BLOCKER`, `HIGH`, `MEDIUM`, or `LOW`;
- file and line when applicable;
- the violated invariant/rule;
- concrete evidence for the problem;
- the required correction or missing test.

Do not invent a line number. Use `null` when the issue is architectural or
cannot be attached to one changed line.
