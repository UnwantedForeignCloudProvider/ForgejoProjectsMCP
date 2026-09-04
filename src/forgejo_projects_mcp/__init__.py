"""forgejo_projects_mcp — an MCP server for managing Forgejo Projects/Kanban boards.

Forgejo does not expose Projects over its REST API, so this server drives the
internal web routes with a persisted session cookie (via Playwright's HTTP
request context — no browser binary needed).
"""

from importlib.metadata import PackageNotFoundError, version

from . import _env  # noqa: F401  (imported first: loads .env before env is read)
from .server import main, mcp

try:
    __version__ = version("forgejo-projects-mcp")
except PackageNotFoundError:  # not installed (e.g. running from a source tree)
    __version__ = "0.0.0"

__all__ = ["__version__", "main", "mcp"]
