"""forgejo_projects_mcp — an MCP server for managing Forgejo Projects/Kanban boards.

Forgejo does not expose Projects over its REST API, so this server drives the
internal web routes with a persisted session cookie (via Playwright's HTTP
request context — no browser binary needed).
"""

from .server import main, mcp

__all__ = ["main", "mcp"]
