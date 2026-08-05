# 0002. Use uv workspaces for shared packages

- Status: Accepted
- Date: 2026-08-01

## Context

The project needs shared libraries that several parts of the codebase import,
each with its own dependencies and tests, without publishing them to a package
index or maintaining a separate repository per library.

The constraints: one command to install everything, a single resolved
dependency set so two packages cannot disagree about a version, edits to a
library visible immediately to its consumers without a reinstall, and each
package independently testable.

## Decision

We will use a uv workspace. The repository root is both the application package
and the workspace root; shared libraries live in `packages/*` and are declared
as members:

```toml
[tool.uv.workspace]
members = ["packages/*"]

[tool.uv.sources]
data-access = { workspace = true }
```

Members are installed editable, share one `uv.lock` and one virtualenv, and are
imported by their normal distribution name.

## Alternatives considered

**Separate repositories per library, published to a private index.** Correct at
large scale, and far too heavy here: every cross-cutting change becomes a
publish, a version bump, and a coordinated merge across repos. Chosen against
until the repo has more than one team.

**Path dependencies without a workspace** (`{ path = "../data-access" }`). Works,
but each package resolves independently, so two packages can end up on
different versions of the same transitive dependency and only find out in
production. The workspace's shared lockfile is precisely the property we want.

**A single flat package with internal modules.** Simplest, and the right answer
for a small project — but it gives no enforced boundary. Nothing stops the
persistence layer importing a web handler, and the day that matters is the day
it is expensive to fix.

**Poetry or PDM workspaces.** Both viable. uv resolves and installs roughly an
order of magnitude faster, and Ruff is already in the toolchain from the same
vendor, so the configuration stays in one ecosystem.

## Consequences

One `uv sync` installs everything; changes to a package are immediately live in
its consumers. Each package declares its own dependencies, so the boundary is
visible in metadata rather than convention alone.

The costs are the ones uv documents. All members share a single
`requires-python`, so no package can require a newer Python than the rest.
Members that need genuinely conflicting dependency versions cannot coexist and
would force a split. And uv cannot enforce import boundaries — Python will
happily let `data-access` import from `app` even though nothing declares it, so
that rule is enforced by review and by the `python-package` skill, not by tooling.
