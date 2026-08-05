# 0001. Record architecture decisions

- Status: Accepted
- Date: 2026-08-01

## Context

Structural decisions — which datastore, how packages are bounded, why a
convention exists — get made once, in a conversation, and then lost. Six months
later the reasoning is gone and only the consequence remains. New contributors,
human or agent, see a constraint with no rationale and one of two things
happens: they work around it, or they revert it and rediscover the original
problem the hard way.

Code comments do not solve this. They explain what the code does, not which
alternatives were weighed and discarded.

## Decision

We will record significant architecture decisions as ADRs in `docs/adr/`, using
the format described in the `adr` skill: context, decision, alternatives
considered, consequences.

Records are numbered sequentially and immutable once accepted. A decision that
turns out to be wrong is superseded by a new record that links back to it.

## Alternatives considered

**A wiki or Confluence space.** Drifts out of sync with the code immediately,
because it is not in the diff and nobody updates it during review. Also
invisible to agents working in the repository.

**Comments in the code.** Good for local "why", useless for cross-cutting
decisions that belong to no single file. A choice of datastore has no natural
home in a module.

**Nothing — rely on git history and PR discussion.** Commit messages capture
what changed, and PR threads capture the argument, but neither is discoverable
by someone asking "why is it like this?" two years on. Searching a decade of
PRs is not a documentation strategy.

## Consequences

Decisions become discoverable and citable. Newcomers and AI agents can read why
a constraint exists before deciding to change it, and `.agents/skills/` can
reference records rather than restating rationale.

The cost is real: writing an ADR takes twenty minutes of honest thinking, and
there is a standing temptation to write them for trivia. The `adr` skill draws
that line — if a competent newcomer would not be tempted to change it back, it
does not need a record.

Superseded records stay in the tree forever, so the directory only grows. That
is acceptable; the archive of what was wrong is the most useful part.
