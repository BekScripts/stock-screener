---
name: documentation
description: Write docstrings, READMEs, or anything under docs/. Covers the Diátaxis classification (tutorial, how-to, reference, explanation), which type a new page should be, Google-style docstrings, and the rule that doc changes ship with the code change. Use when writing or updating documentation, adding docstrings, classifying a document, or when a code change alters documented behaviour.
---

# Documentation

## Classify before you write

Every page in `docs/` is exactly one of four types, following
[Diátaxis](https://diataxis.fr). The type is declared in frontmatter and decides
which directory it lives in:

```markdown
---
type: how-to
title: Add a shared package
---
```

| Type | Directory | Reader is | Written as |
| --- | --- | --- | --- |
| `tutorial` | `docs/tutorials/` | learning, doesn't know the questions yet | a lesson that always works |
| `how-to` | `docs/how-to/` | achieving a goal, already knows the basics | a recipe |
| `reference` | `docs/reference/` | looking something up | dry, complete, no opinions |
| `explanation` | `docs/explanation/` | trying to understand | discussion of the why |

ADRs in `docs/adr/` are a fifth, separate thing: a dated, immutable record of one
decision. See the `adr` skill.

### Choosing the type

Ask what the reader is doing, not what the content is about. The same subject
produces four different pages:

| The reader… | Type | Example |
| --- | --- | --- |
| has never used this and needs a win | tutorial | "Build your first endpoint" |
| knows the project, needs a job done | how-to | "Add a shared package" |
| needs to know what a flag does | reference | "Configuration" |
| wonders why it's built this way | explanation | "Architecture" |

If a page seems to be two types, it is two pages. Split it and link them.

### Don't mix modes

This is the rule the whole framework exists for:

- **A tutorial that explains trade-offs loses the beginner.** They cannot
  evaluate options yet. Make every choice for them and move on.
- **A how-to that teaches wastes the reader's time.** They came for a recipe.
  Link to the explanation instead of inlining it.
- **A reference page with a worked example goes stale.** Describe the machinery;
  put the example in a how-to. If you write "you should" on a reference page,
  that sentence belongs elsewhere.
- **An explanation with step-by-step instructions is a how-to wearing a hat.**

## The layers outside docs/

| Layer | Answers | Lives in |
| --- | --- | --- |
| docstring | how do I call this? | the code |
| package README | what is this for, and what is it not? | `packages/*/README.md` |
| root README | how do I run this project? | `README.md` |
| docs site | the four types above | `docs/` |

## Docstrings

Google style, enforced by Ruff's `D` rules. Required on every public module,
class, and function. Private helpers (`_leading_underscore`) need one only when
the logic is non-obvious.

```python
def retry(operation: Callable[[], T], *, attempts: int = 3) -> T:
    """Run an operation, retrying transient failures with backoff.

    Args:
        operation: The callable to run. Must be safe to execute more than once.
        attempts: Maximum number of tries, including the first.

    Returns:
        Whatever `operation` returns on its first successful call.

    Raises:
        ServiceUnavailableError: If every attempt fails.
    """
```

- Summary is one imperative line: "Run an operation…", not "This function runs…".
- Document every argument, the return value, and every exception a caller is
  expected to handle.
- Say what the signature cannot: units, valid ranges, side effects, idempotency,
  whether it blocks, who owns the transaction.
- **Never restate the signature.** `identifier: The identifier.` is noise. If
  there is nothing to add, improve the name instead.

## READMEs

Every package README covers, in order: what it does, how to depend on
it, a usage example that actually runs, and — most importantly — **what it
explicitly does not do**. That last section is what stops a package accumulating
unrelated code.

The root README is for getting running in under five minutes. Architecture goes
in `docs/explanation/`, not the README.

## The rule that matters

**Documentation changes ship in the same commit as the code change.** A
docstring describing behaviour the function no longer has is worse than none: it
is trusted and wrong. When you change a signature, a default, an error type, or
an env var, update the docstring, the README, `.env.example`, and the reference
page in that same change.

Write down what is surprising, not what is obvious. Nobody needs a comment
saying a loop iterates. People need to know the retry is not idempotent.

## Adding a page

1. Decide the type from what the reader is doing.
2. Create it in the matching directory with `type:` and `title:` frontmatter.
3. Add it to `nav:` in `mkdocs.yml` — `mkdocs build --strict` fails on orphans.
4. Link it from that section's `index.md`.
5. `make docs-build` to verify strict mode passes.

## Checklist

- [ ] Page declares `type:` and sits in the matching directory
- [ ] Exactly one type — no teaching in a how-to, no examples in reference
- [ ] Added to `mkdocs.yml` nav and its section index
- [ ] Public functions, classes, and modules have Google-style docstrings
- [ ] Args, Returns, and Raises documented; no restated signatures
- [ ] New package README includes a "does not do" section
- [ ] Changed behaviour reflected in docstring, README, `.env.example`, reference
- [ ] `make docs-build` and `make lint` pass
