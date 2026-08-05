---
applyTo: ".github/workflows/**"
---

# CI/CD

Follow the `ci-cd` skill in `.agents/skills/ci-cd/`, and ask before changing
anything here — a broken workflow blocks every branch.

- Pin third-party actions to a full commit SHA, never a tag.
- Workflows get the narrowest `permissions:` block that works.
- Never put a secret in a workflow file; use `secrets.*` or OIDC.
- Any check added here must also be runnable locally via `make`.
