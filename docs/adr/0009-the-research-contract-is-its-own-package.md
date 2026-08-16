# 0009. The AI research contract is its own package, and holds no model client

- Status: Accepted
- Date: 2026-08-15

## Context

Phase 3 adds an AI research layer over the deterministic ranking. Before any
provider is wired up, the contract had to be settled: what evidence a model is
given, what shape its answer takes, and which of its answers are acceptable.

Three properties of that contract decided where the code should live.

It is **pure**. Checking that a citation resolves, that a figure already exists
in the supplied evidence, and that a section's claims carry a permitted basis are
all functions of two values. None of them needs a database, a network call or a
clock.

It has **more than one consumer**. `data-access` must persist a report;
`src/stock_screener` must assemble a brief from stored rows, call a provider and
render what comes back; a later dashboard reads the same models. A contract that
lived in the application could not be imported by persistence without inverting
the dependency direction the workspace already enforces.

It is **the thing most worth testing**. The failure mode of an AI research tool
is not a wrong number — it is a fluent, well-cited, wrong number. The checks that
catch that are worth exercising against hand-written evidence, and anything
requiring a database or a provider to construct its inputs would not be.

The alternative placements were both defensible, and one was recommended.

## Decision

We will keep the research contract in a new workspace package, `packages/research`,
containing three modules and no I/O:

- `brief` — the evidence a report may rest on, with every citable item carrying a
  namespaced id, and a fingerprint over the whole brief that serves as the cache
  key.
- `report` — `Claim` and its `Basis`, the thirteen sections, `DraftReport` for
  unvalidated model output and `ResearchReport` for what was accepted.
- `validation` — the rules between the two.

Two boundaries come with it.

**The model client stays in `api-clients`.** An LLM provider is an outbound HTTP
dependency like Alpaca, FMP and EDGAR, and belongs where the other three already
are — behind an adapter that translates vendor payloads into workspace models.
`research` therefore depends only on `domain` and pydantic, and can be tested
with no transport at all.

**`ResearchReport` has no score field.** Not a score, a rating, a category, a
target or a component number. The requirement that the AI must never recalculate
or override the CompounderScore is met structurally, by giving it nowhere to put
one, rather than by asking a prompt to behave.

## Alternatives considered

**Put the models in `domain`, as `domain/research.py`.** This was the
recommendation. `domain` is already the shared, I/O-free vocabulary, it is
already imported by `data-access` and the application, and adding a module to it
costs no new pyproject, README, AGENTS.md or workspace registration. Rejected in
favour of the separation: `domain` states what a company is worth and why, and
every rule in it is arithmetic that can be checked against a hand-worked number.
The research contract is a set of rules about what a language model is allowed to
have said. Both are pure, but a reader who opens `domain` looking for the scoring
formula should not find prompt-shaped concepts next to it, and a change to the
research contract should not be able to touch the file that decides a score.

**Keep it in `src/stock_screener/research/` until a second consumer appears.**
The repository's own rule is one caller means `src/`, and extracting later is
cheap. Rejected because the second consumer is immediate rather than
hypothetical: persisting a report is `data-access`'s job, and a package may not
import from the application. The extraction would have happened in the next step
of the same phase.

**Fold validation into the provider adapter in `api-clients`.** Tempting, since
parsing a response and checking it are adjacent. Rejected because it would make
every test of the numeric whitelist require a fake transport, and because the
rules must apply to any provider — they are a property of the contract, not of
whoever answers it.

**Let the report carry the model's own view of the score, for comparison.** A
field holding what the model thought the company deserved, displayed next to the
real one, is genuinely interesting. Rejected: a number in a stored report is a
number that will end up in a ranking, a CSV or a chart, and at that point the
distinction between the score and a commentary on it survives only by convention.
Disagreement belongs in the bear case, as prose, with its basis marked
`INTERPRETATION`.

## Consequences

The workspace gains a fourth package, a fourth set of files to keep current, and
a fourth entry in the CI matrix. The name `research` is taken on PyPI — as
`domain`, `api-clients` and `data-access` already are — so the
`[tool.uv.sources]` entry in the root `pyproject.toml` is load-bearing: removing
it makes uv resolve a stranger's package from PyPI instead of this one, and the
failure appears as confusing import errors rather than as a missing dependency.

The root `pyproject.toml` declares `research` as a dependency before anything in
`src/` imports it. That is deliberate — the wiring is easy to forget and
expensive to debug — but it means the declaration runs ahead of the code for one
step.

The numeric whitelist will produce false positives. A model writing a figure that
is correct but absent from the brief gets its claim dropped, and the honest fix is
to add the figure to Phase 1 or Phase 2 deterministically rather than to loosen
the check. Bare integers are matched permissively against every digit sequence in
the brief to keep counts and years from being flagged, which is a deliberate
widening and the weakest part of the rule.

Because filing text is not extracted yet, several sections will legitimately
answer `UNKNOWN` and confidence will rarely reach `HIGH`. That is the contract
reporting a real limitation, and it should not be read as a defect in the prompt
or the model.
