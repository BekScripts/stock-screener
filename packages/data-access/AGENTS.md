# data-access

Persistence and caching for securities data.

Agents working in this directory follow the repository-wide AGENTS.md plus the
`python-package` skill.

## Boundaries

TODO: what belongs here, and what must not.

## Public API

`src/data_access/__init__.py` is the only supported import surface. Adding to
`__all__` is a contract; removing from it is a breaking change.

## Dependencies

TODO: which other packages this may depend on. It must not import from the root
app.
