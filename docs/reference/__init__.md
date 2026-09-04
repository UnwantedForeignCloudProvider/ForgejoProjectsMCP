# `forgejo_projects_mcp.__init__`

Source: [`src/forgejo_projects_mcp/__init__.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/__init__.py)

The package entry point describes the project, loads environment variables before configuration is read, imports the MCP server, and exposes the package version.

## Import behavior

Importing `forgejo_projects_mcp` imports `._env` first. That import runs `.env` loading as a deliberate side effect. The order matters because `client.py` reads configuration while its module-level constants are initialized.

The package then imports:

```python
from .server import main, mcp
```

If package metadata is available, `__version__` comes from the installed distribution. When running directly from a source tree without installed metadata, it falls back to `"0.0.0"`.

## Public exports

```python
__all__ = ["__version__", "main", "mcp"]
```

- `__version__: str` — installed or fallback package version;
- `main() -> None` — runs the stdio MCP server; and
- `mcp: MCPServer` — the registered server object, useful for introspection and tests.

The package also installs the console scripts `forgejo-projects-mcp` and `forgejo-projects-cli`; see [`server.py`](server.md) and [`cli.py`](cli.md).

