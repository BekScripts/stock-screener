---
applyTo: "tests/**,packages/*/tests/**"
---

# Tests

Follow the `testing` skill in `.agents/skills/testing/`.

- One behaviour per test. The name states the behaviour, not the function.
- Mark every test `unit`, `integration`, or `slow`.
- Tests never reach the network or a real database unless marked `integration`.
- Assert on outcomes, not on how the code got there.
- Never weaken an assertion or add a skip to make a suite pass.
