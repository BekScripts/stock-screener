---
name: python-app
description: Write and structure application code in src/. Covers module layout, the layering rule (entrypoint to service to package), dependency injection over globals, error handling, and when to extract code into a shared package. Use when adding a feature, a module, an endpoint, a CLI command, or any code under src/.
---

# Application code

`src/app/` is the deployable, and there is exactly one per repository. It
composes shared packages into something that runs. Logic that could serve a
second consumer belongs in `packages/` instead — see the `python-package` skill.

A genuinely separate deployable — a queue worker, a scheduled job, a second
service — belongs in its own repository, with any shared code published as a
package. Do not invent a second app directory here.

## Layering

```
__main__.py      entrypoint: parse input, build dependencies, delegate. Thin.
config.py        typed settings. The only place that reads the environment.
logging.py       observability setup.
<feature>/       one directory per feature, not per technical layer
```

Organise by **feature**, not by type. `billing/` containing its own service,
schemas, and handlers beats parallel `services/`, `schemas/`, `handlers/` trees
that force every change to touch three directories.

Dependencies point one way and never back:

```
__main__  →  feature modules  →  packages/*
```

## Rules

**Entrypoints stay thin.** `__main__.py` wires things together and returns an
exit code. If it contains a branch on business state, that branch belongs in a
module.

**Inject dependencies, don't reach for globals.** Pass a session, a client, or a
settings object in as an argument. The only cached global is `get_settings()`.
Code that constructs its own database session is untestable without a database.

```python
# good
def send_invoice(client: MailClient, invoice: Invoice) -> None: ...


# bad — untestable, hides the dependency
def send_invoice(invoice: Invoice) -> None:
    client = MailClient(os.environ["MAIL_URL"])
```

**Type every signature.** mypy runs strict. No untyped defs, no bare `Any`. Use
`from __future__ import annotations` and put type-only imports under
`if TYPE_CHECKING:`.

**Raise specific errors.** Define an exception per failure mode and let it
propagate to the entrypoint, which decides how to report it. Never
`except Exception: pass`. Never return `None` to signal failure when an
exception says it better.

**No side effects at import time.** No network calls, no file reads, no config
loading at module level. Import must be free — put the work in a function.

**No `print()`.** Use `structlog.get_logger(__name__)` and pass context as
keyword arguments. Ruff enforces this (`T20`).

## When to extract into a package

Extract when a second consumer appears, when the code is a coherent domain with
its own vocabulary, or when it needs to be tested against a real dependency in
isolation. Do not extract for tidiness alone. Follow the `python-package` skill.

## Checklist

- [ ] Organised by feature, not by technical layer
- [ ] Entrypoint contains no business logic
- [ ] Dependencies passed in, not constructed inside
- [ ] Every function fully annotated
- [ ] Specific exceptions, no bare `except`
- [ ] No import-time side effects, no `print()`
- [ ] Config read via `get_settings()`, never `os.environ`
- [ ] `make check` passes
