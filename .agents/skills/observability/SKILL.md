---
name: observability
description: Add logging, error handling, or metrics. Covers structured logging with structlog, log levels, what to log and what never to log, exception handling patterns, and adding context to a request. Use when the task mentions logging, debugging output, error handling, monitoring, tracing, or when code needs to report what it is doing.
---

# Observability

## Structured logging

`structlog` is configured once in `src/app/logging.py`. Everywhere else:

```python
import structlog

log = structlog.get_logger(__name__)

log.info("invoice sent", invoice_id=invoice.id, recipient_domain=domain)
```

**Pass context as keyword arguments, never format it into the message.** The
message is a stable identifier you can search and aggregate on; the kwargs are
queryable fields. `f"Sent invoice {id} to {email}"` produces a million distinct
strings you cannot group.

```python
log.info("invoice sent", invoice_id=42)  # good
log.info(f"Sent invoice 42")  # bad
print("sent")  # rejected by Ruff (T20)
```

## Levels

| Level | Use for | Someone gets paged? |
| --- | --- | --- |
| `debug` | detail useful while diagnosing | no |
| `info` | a business event happened | no |
| `warning` | degraded but handled — retry succeeded, fallback used | no, but it's reviewed |
| `error` | the operation failed and a human should know | yes |

Don't log at `error` for something you handled and recovered from — that trains
people to ignore errors. Don't log at `info` in a hot loop.

## Request context

Bind values once and every subsequent log line in that unit of work carries
them, without threading a logger through every call:

```python
structlog.contextvars.bind_contextvars(request_id=req_id, user_id=user.id)
# ... all logs from here carry request_id and user_id
structlog.contextvars.clear_contextvars()  # at the end of the unit of work
```

## Never log

- Credentials, tokens, API keys, passwords, session cookies
- Personal data: full email addresses, names, addresses, payment details. Log an
  identifier or a derived field (`recipient_domain`) instead.
- Whole request or response bodies — they contain both of the above

The rule is: assume every log line is readable by anyone with dashboard access,
and retained for a year.

## Errors

Log an exception where you **handle** it, once, with the stack:

```python
try:
    client.send(invoice)
except ServiceUnavailableError:
    log.warning("invoice send failed, will retry", invoice_id=invoice.id)
    raise
except ClientError:
    log.exception("invoice rejected", invoice_id=invoice.id)
    return Failure(...)
```

- `log.exception()` inside an `except` block — it attaches the traceback.
- Log **or** re-raise, not both, unless you're adding context the caller lacks.
  Logging at every level of the stack produces five copies of one incident.
- Never `except Exception: pass`. If a failure is genuinely ignorable, catch the
  specific type and log at `debug` with a comment saying why.
- Never swallow an exception to keep a process alive without recording it.

## Checklist

- [ ] `structlog.get_logger(__name__)`, no `print()`
- [ ] Static message, context in kwargs
- [ ] Level matches the severity; `error` means someone should act
- [ ] No credentials or personal data in any field
- [ ] `log.exception()` used inside `except`, at the handling site only
- [ ] No bare `except`, no silent swallow
