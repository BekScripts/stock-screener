---
name: ci-cd
description: Change GitHub Actions workflows, debug a failing CI run, or set up release automation. Covers the workflow layout, action pinning, permissions, caching with uv, path-filtered matrix builds for the monorepo, and publishing. Use when the task mentions CI, GitHub Actions, a failing build, a workflow file, deployment, or releasing.
---

# CI/CD

**Ask before changing anything in `.github/workflows/`.** A broken workflow
blocks every branch in the repo, and the failure often shows up on someone
else's PR rather than yours.

## Workflows

| File | Trigger | Does |
| --- | --- | --- |
| `ci.yml` | push, PR | lint, types, tests across the changed packages |
| `release.yml` | tag `v*` | build and publish |
| `docs.yml` | push to main | build and deploy the docs site |

## The local/CI contract

**Every CI check must be runnable locally through `make`.** If CI runs something
you cannot reproduce with one command, you have built a debugging trap — someone
will spend an hour pushing commits to find out what the runner does differently.

`make check` is the contract. CI calls it; you call it before pushing. Adding a
check to CI means adding it to the Makefile in the same change.

## Security rules

**Pin third-party actions to a full commit SHA**, with the version in a comment.
A tag is mutable — whoever controls the repo can repoint `v4` at any code, and
it runs with your secrets:

```yaml
- uses: astral-sh/setup-uv@v7          # mutable, avoid
- uses: astral-sh/setup-uv@e92bbb8...  # pinned  (v7.1.2)
```

`actions/*` from GitHub itself may use a major tag.

**Set the narrowest `permissions:` block that works.** Default to
`contents: read` at the workflow level and widen per-job only where needed.

**Never put a secret in a workflow file.** Use `secrets.*`, and prefer OIDC over
long-lived tokens — `id-token: write` plus PyPI trusted publishing means no
token exists to leak.

**Never run untrusted code with secrets.** `pull_request_target` and
`workflow_run` run with write permissions against a fork's code. Avoid both
unless you know exactly why you need them.

## Monorepo builds

`ci.yml` currently uses one coarse `dorny/paths-filter` check: docs-only changes
skip the expensive jobs, everything else runs the full suite. That is the right
default for a workspace with few packages — a per-package matrix costs more in
maintenance than it saves in minutes until the suite is genuinely slow.

When you do split it per package, know the trap: **path filters miss transitive
dependencies.** If `app` imports `billing`, a change to `billing` must also
retest `app`. The filter has to encode that, and it has to be updated in the
same commit that adds the dependency to `pyproject.toml`. Forget, and CI reports
green on a break.

This is the main reason not to split early. A coarse filter that always runs
everything is never wrong; a stale fine-grained one is silently wrong.

## Caching

`setup-uv` with `enable-cache: true` handles the dependency cache, keyed on
`uv.lock`. Don't hand-roll `actions/cache` for the venv. Never cache anything
keyed on something mutable, and never cache test results.

## Debugging a failure

1. Read the actual error, not the summary — expand the failing step.
2. Reproduce locally: `make check`. Most failures reproduce immediately.
3. If it only fails in CI, the difference is environment: Python version, a
   missing service, an env var, or filesystem case sensitivity (macOS is
   case-insensitive, the runner is not).
4. Fix the cause. **Never** disable a check, add `continue-on-error`, or mark a
   test skipped to get a green tick. If a check must be temporarily disabled,
   say so explicitly and open an issue.

## Checklist

- [ ] Every CI check runs locally via `make`
- [ ] Third-party actions pinned to a full SHA with a version comment
- [ ] `permissions:` is minimal
- [ ] No secret literal; OIDC preferred over tokens
- [ ] Path filters updated if package dependencies changed
- [ ] No `continue-on-error` added to hide a failure
- [ ] Workflow changes confirmed with the user before merging
