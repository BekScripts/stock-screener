# domain

Screening rules and filter models, free of I/O.

## Use

```python
from domain import something
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["domain"]

[tool.uv.sources]
domain = { workspace = true }
```

## Boundaries

TODO: state what belongs in this package and what does not.
