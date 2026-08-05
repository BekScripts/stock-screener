---
name: config-and-secrets
description: Read configuration, add an environment variable, or handle a credential. Covers the typed settings pattern, .env files, secret handling rules, per-environment configuration, and what must never be committed. Use when the task mentions an env var, a config value, an API key, a connection string, a token, or a .env file.
---

# Configuration and secrets

## One entry point

`src/app/config.py` is the **only** place in the codebase that reads the
environment. Everywhere else calls `get_settings()`.

```python
from app.config import get_settings

settings = get_settings()
if settings.is_production:
    ...
```

Never `os.environ["X"]` or `os.getenv("X")` outside `config.py`. A scattered
`os.getenv` is untyped, unvalidated, undiscoverable, and fails at the moment it
is first read rather than at startup.

## Adding a setting

Three edits, all required:

1. **A typed field** in `Settings` — as narrow a type as possible. Use `Literal`
   for fixed sets, not `str`:

   ```python
   database_url: str
   max_retries: int = 3
   environment: Literal["local", "test", "staging", "production"] = "local"
   ```

2. **A line in `.env.example`** with a placeholder — never a real value. This
   file is the documentation of what the app needs to run.

3. **The real value** in your local `.env` and in the deployment environment.

Fields without a default are required: the app fails at startup with a clear
message rather than at 3am on first use. Give a default only when the fallback
is genuinely safe in production.

## Secrets

**Never commit a secret.** Not in code, not in a test fixture, not in a
docstring, not in a comment, not in `.env.example`, not "temporarily".

- `.env` is gitignored. Confirm before writing anything into it.
- Use `SecretStr` for credentials so they don't leak through `repr()` or a
  logged settings object:

  ```python
  from pydantic import SecretStr

  api_token: SecretStr
  # use: settings.api_token.get_secret_value()
  ```
- Never log a credential, and never include one in an exception message. Log
  that authentication failed, not what was sent.
- In CI, values come from `secrets.*` or OIDC — never a literal in a workflow.

**If a secret is already committed, rotating it is the fix.** Removing it from
the working tree does nothing; it stays in history and in every clone. Say so
immediately rather than quietly deleting the line.

## Per-environment values

Same variable names everywhere, different values per environment. Don't branch
on environment in application code:

```python
# bad — behaviour differs invisibly between environments
timeout = 1 if settings.environment == "local" else 30

# good — the difference is in configuration, and visible
timeout = settings.request_timeout
```

The `is_production` property exists for genuine operational differences (log
format, error verbosity), not business logic.

## Checklist

- [ ] Setting is a typed field on `Settings`, as narrow as possible
- [ ] `.env.example` updated with a placeholder value
- [ ] No `os.environ` / `os.getenv` outside `config.py`
- [ ] Credentials typed `SecretStr`
- [ ] No secret in code, tests, docs, or `.env.example`
- [ ] No credential reachable from a log line or exception
- [ ] Required settings have no default; app fails fast at startup
