---
name: testing
description: Write, structure, and debug tests. Covers unit vs integration boundaries, pytest markers, naming, fixtures, parametrisation, what to mock and what not to, coverage expectations, and how to respond to a failing test. Use when writing any test, changing test setup, investigating a CI failure, or asked whether something is tested.
---

# Testing

## Where tests live

| Code under test | Tests |
| --- | --- |
| `src/app/` | `tests/unit/`, `tests/integration/` |
| `packages/billing/` | `packages/billing/tests/` |

A member's tests ship with the member. Never test a package from the root
suite — it must be verifiable on its own.

## Markers — every test gets exactly one

| Marker | Means | Constraint |
| --- | --- | --- |
| `unit` | pure logic, in-memory | no network, no DB, no filesystem, no clock |
| `integration` | touches a real dependency | may be slow, may need docker |
| `slow` | takes over a second | run it, but expect the wait |

```bash
uv run pytest -m unit           # the fast loop you run constantly
uv run pytest -m "not slow"     # what you run before pushing
make test                       # everything, with coverage
```

## Naming

The test name states the behaviour and the condition, so a failure report reads
as a sentence without opening the file:

```python
def test_rejects_unknown_environment() -> None: ...  # good
def test_returns_empty_list_when_no_matches() -> None: ...  # good
def test_config() -> None: ...  # useless when it fails
def test_get_settings_2() -> None: ...  # worse
```

## Structure

One behaviour per test, arranged as arrange / act / assert with a blank line
between the phases. If a test needs a comment to explain what phase you're in,
it is testing too much.

```python
@pytest.mark.unit
def test_raises_when_record_missing() -> None:
    repo = UserRepository(FakeSession())

    with pytest.raises(NotFoundError, match="User not found: 99"):
        repo.get(99)
```

- Assert on the **outcome**, not the implementation. Asserting that a private
  method was called means the test breaks on every refactor while catching no
  bugs.
- `pytest.raises` always takes `match=` — otherwise it passes on the wrong error.
- Parametrise repetitive cases with `@pytest.mark.parametrize`; don't loop
  inside a test, because the first failure hides the rest.

## Fixtures

Put shared setup in `conftest.py` at the narrowest scope that works. Fixtures
build test data; they don't assert. If a fixture is used by one test, inline it.

Tests are isolated by default — `tests/conftest.py` moves each test to a temp
directory and clears the settings env vars, so no test sees your local `.env`.
`pytest-randomly` shuffles order, so any test depending on another's leftover
state will fail loudly. That is the point; fix the dependency, don't disable the
plugin.

## What to mock

Mock at the **boundary you own**: the HTTP transport, the clock, the filesystem.
Do not mock the code under test, and do not mock a type you don't control deep
in its internals — that pins your test to a library's private behaviour.

```python
transport = httpx.MockTransport(handler)  # good — real client code still runs
mock.patch("app.billing.charge")  # bad — you're testing the mock
```

Prefer a small fake class over `MagicMock`. A fake fails when the interface
changes; a `MagicMock` cheerfully accepts anything and passes forever.

## When a test fails

**Understand the failure before changing anything.** The failing assertion is
evidence. Then, in order of preference:

1. Fix the code — the test found a real bug.
2. Fix the test — the behaviour intentionally changed, so update the assertion
   to the new correct value and say so.

Never: loosen an assertion, add `@pytest.mark.skip`, widen an exception match,
or delete the test to get green. If a test is genuinely obsolete, remove it in
its own change with the reason stated.

## Coverage

80% minimum, enforced by `make test`. Coverage is a floor, not a goal — an
untested error path at 95% coverage is still a production incident. Cover the
unhappy paths: empty input, missing record, timeout, malformed data,
concurrent access.

## Checklist

- [ ] Test lives beside the code it tests (package tests inside the package)
- [ ] Exactly one marker: `unit`, `integration`, or `slow`
- [ ] Name states the behaviour and the condition
- [ ] One behaviour per test; asserts on outcomes
- [ ] `pytest.raises` has `match=`
- [ ] Mocks at an owned boundary; fakes over `MagicMock`
- [ ] Unhappy paths covered, not just the happy one
- [ ] No skips, no weakened assertions, no failing tests left behind
