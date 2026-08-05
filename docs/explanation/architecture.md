---
type: explanation
title: Architecture
---

# Architecture

> TODO: replace this with the real architecture once the project has one.

## Shape

```
src/app/            the application
  config.py         the only place that reads the environment
  logging.py        structlog setup
  <feature>/        one directory per feature

packages/<name>/    shared libraries, one bounded concern each
```

The workspace is a single uv workspace: one lockfile, one virtualenv, every
package installed editable.

## One deployable per repository

The application lives at `src/`, not in a directory alongside its siblings.
There is no `apps/` tree, because a repository that builds one thing should not
carry a directory implying it builds several.

A genuinely separate deployable — a queue worker, a scheduled job, a companion
service — gets its own repository, with shared code extracted into a package.
That boundary is enforced by deployment reality rather than by convention, which
makes it far harder to erode than a directory split.

The cost is that splitting a service out later means creating a repository
rather than a directory. In exchange, nothing in this repo can quietly grow a
second lifecycle.

Organising one app's code is not a reason to reach for another top-level
directory. That is what modules inside `src/` are for.

## Dependency direction

```
src/app  →  packages/*  →  packages/*
```

Strictly one-way. A package never imports from `src/app`, and package
dependencies never form a cycle. When a package needs something the application
has, the dependency is inverted: the shared piece moves down into a package, or
the application passes it in.

uv cannot enforce this — Python will happily import anything on the path. It is
held up by review and by the `python-package` skill.

## Boundaries that matter

- **Configuration has one door.** `src/app/config.py` is the only reader of the
  environment; everything else receives a `Settings` object. Scattered
  `os.getenv` calls are untyped, unvalidated, and fail at first use rather than
  at startup.
- **A package's public API is its `__init__.py`.** Reaching into a submodule from
  outside is a bug, not a shortcut — it turns an internal detail into something
  you can't change without breaking a caller you don't know about.
- **Entry points stay thin.** `__main__.py` wires dependencies together and
  returns an exit code. A branch on business state there is a branch in the
  wrong place.

## Decisions

Significant, hard-to-reverse choices are recorded in [ADRs](../adr/index.md).
