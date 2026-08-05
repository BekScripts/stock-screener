---
type: reference
title: Configuration
---

# Configuration

Every value the application reads from the environment. Defined as typed fields
on `Settings` in `src/app/config.py`; nothing else in the codebase reads the
environment.

Values are read from environment variables, falling back to a `.env` file in the
working directory. Field names map to upper-case variables: `log_level` reads
`LOG_LEVEL`.

| Variable | Type | Default | Description |
| --- | --- | --- | --- |
| `ENVIRONMENT` | `local` \| `test` \| `staging` \| `production` | `local` | Deployment environment. |
| `LOG_LEVEL` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` | `INFO` | Minimum level emitted. |
| `LOG_JSON` | bool | `false` | Emit JSON logs instead of console-formatted. |

A field without a default is required: the process fails at startup if the
variable is absent.

## Behaviour

`Settings` is frozen after construction and `extra="forbid"` rejects unknown
keyword arguments. Undeclared environment variables are ignored rather than
rejected — a typo'd variable name is silently unused.

`get_settings()` caches a single instance for the process lifetime. Tests call
`get_settings.cache_clear()` to reset it.

## Related

- [Add a configuration setting](../how-to/add-a-setting.md)
