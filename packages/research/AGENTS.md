# research

The AI research contract: what the model is given, and what it is allowed to
return.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

**Here:** the input a model receives, the shape of its answer, and every check
applied to that answer before it is accepted. Pure functions over an
already-assembled brief.

**Not here:** anything that performs I/O or reads configuration. No HTTP, no
database, no filesystem, no `datetime.now()`, no `os.environ`. An LLM client is
an external provider and belongs in `api-clients`; selecting candidates,
assembling a brief from stored rows, and persisting a report are application
concerns.

**Here too:** the prompt. It lives beside the validator because every rule it
states has a check that enforces it — split across packages they would drift, and
the failure would be silent.

**Not here yet:** filing-text extraction. That is a later step of Phase 3 and
should not be started here without being asked for.

## Invariants

- `ResearchReport` carries **no** score, rating, category or target field. The
  contract's central guarantee is structural: the model cannot override the
  CompounderScore because there is nowhere to put one. Adding such a field is
  the one change to this package that is always wrong.
- Every number in a claim must already appear in the brief, subject only to
  display rounding. No new ratios, percentages, growth rates, multiples, price
  targets or return estimates. If a derived figure would be useful, add it
  deterministically in `domain` and supply it in the brief.
- A figure written with a unit — `%`, `pp`, `x`, a currency symbol — must match a
  brief value of the compatible unit. Only bare numbers may match a bare literal.
- A missing value is `None` and renders as `unknown`. Never `0.0`, and never
  omitted.
- An unresolvable citation is invented evidence, not weak evidence. The claim is
  dropped, never repaired.
- `UNKNOWN` is legal in every section and is the answer a section falls back to.
  A validated report never has an empty section.
- Validation lowers confidence and never raises it. The ceiling is computed from
  the brief, so a model cannot argue past thin data.
- `validate_report` never raises on bad model output — bad output produces a
  `PARTIAL` report. A provider failure is a `FAILED` report and must never stop a
  scan, a scoring run or a ranking.
- Changing which inputs are supplied, which sections exist, or how a claim must
  be evidenced means a new `CURRENT_CONTRACT_VERSION`, never an edit to an
  existing one.

A new validation rule needs tests for the case it catches, the case it must not
catch, and the boundary between them. The numeric whitelist in particular is
where a false positive is expensive: a spurious demotion teaches the reader to
ignore the flag.

## Public API

`src/research/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

`domain` and pydantic. This package must not import `data-access`, `api-clients`
or the root app — a contract that depended on persistence could not be tested
against a hand-written brief, which is the whole point of keeping it separate.
