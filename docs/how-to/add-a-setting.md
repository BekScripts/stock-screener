---
type: how-to
title: Add a configuration setting
---

# Add a configuration setting

Expose a new environment-driven value to the application.

## Steps

1. Add a typed field to `Settings` in `src/stock_screener/config.py`. Use the narrowest
   type that fits — `Literal` for a fixed set, not `str`:

   ```python
   class Settings(BaseSettings):
       request_timeout: float = 10.0
       queue_backend: Literal["redis", "sqs"] = "redis"
   ```

   Omit the default to make it required: the app then fails at startup with a
   clear message instead of at 3am on first use.

2. For a credential, use `SecretStr` so it can't leak through `repr()` or a
   logged settings object:

   ```python
   api_token: SecretStr
   # read it with: settings.api_token.get_secret_value()
   ```

3. Add the variable to `.env.example` with a **placeholder**, never a real
   value:

   ```bash
   REQUEST_TIMEOUT=10.0
   QUEUE_BACKEND=redis
   ```

4. Add the real value to your local `.env` and to each deployment environment.

5. Read it through `get_settings()` — never `os.environ`:

   ```python
   from stock_screener.config import get_settings

   timeout = get_settings().request_timeout
   ```

6. Verify:

   ```bash
   make check
   ```

## Related

- `config-and-secrets` skill — the full rules, including what must never be
  committed
- [Configuration reference](../reference/configuration.md) — every setting
