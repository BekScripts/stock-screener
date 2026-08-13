# api-clients

Outbound HTTP clients for market data providers.

## Use

```python
from api_clients import something
```

To depend on this package, add it in two places in the consuming
`pyproject.toml`:

```toml
[project]
dependencies = ["api-clients"]

[tool.uv.sources]
api-clients = { workspace = true }
```

## Boundaries

TODO: state what belongs in this package and what does not.
