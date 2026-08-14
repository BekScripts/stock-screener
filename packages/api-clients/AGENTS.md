# api-clients

Outbound HTTP clients for market data providers.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** vendor authentication, request building, pagination, rate limiting,
retry, and translation of vendor payloads into `domain` models.

**Not here:** database access, eligibility rules, metric calculations, or any
judgement about what the data means.

## Invariants

- Every public method returns `domain` models. Returning a dict or a vendor
  payload is a bug, not a shortcut — it moves the normalisation problem instead
  of solving it.
- A field the vendor omitted or sent unparseably becomes `None`, never `0.0`.
- Normalise sign conventions at the boundary. Capital expenditure is stored as a
  positive outflow.
- Never put a credential in an exception message, a log line, or a test fixture.
  The API key travels in a query string for FMP, so never echo a URL either.
- Rate limits and 5xx are retried with backoff; 401/403 are not.

## Testing

Every adapter test drives the real adapter through `httpx.MockTransport`. No
test may open a socket. Inject `sleep` and `clock` rather than waiting.

When adding an adapter, assert that no vendor field name appears in the returned
models.

## Public API

`src/api_clients/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

httpx, pydantic, and the `domain` workspace package. It must not depend on
`data-access` or import from the root app.
