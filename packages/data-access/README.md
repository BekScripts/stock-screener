# data-access

Persistence and caching for securities data.

## Use

```python
from data_access import something
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["data-access"]

[tool.uv.sources]
data-access = { workspace = true }
```

## Boundaries

TODO: state what belongs in this package and what does not.
