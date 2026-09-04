# `forgejo_projects_mcp._env`

Source: [`src/forgejo_projects_mcp/_env.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/_env.py)

This small internal module loads a local dotenv file before any other module reads environment variables.

## `load_env`

```python
load_env() -> None
```

Calls `find_dotenv(usecwd=True)` and then `load_dotenv(...)`:

- the search starts at the process current working directory and can walk upward;
- values already in the environment are not overwritten; and
- a missing `.env` is harmless.

`load_env()` is invoked once at module import. It is not a long-running watcher: editing `.env` after the process has started does not update an existing client.

## Why this module is separate

The module is imported first from `forgejo_projects_mcp.__init__`. Keeping dotenv loading in a dependency-light module makes the startup ordering explicit and avoids duplicating dotenv setup in the MCP server and CLI entry points.

