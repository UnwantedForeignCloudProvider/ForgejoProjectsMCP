"""MCP server exposing Forgejo Projects/Kanban management tools.

Run with:  forgejo-projects-mcp   (stdio transport)
Requires FORGEJO_URL, FORGEJO_USERNAME, FORGEJO_PASSWORD in the environment.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import AuthError, ForgejoClient, ForgejoError

# --- logging: stdio transport owns stdout, so all logs MUST go to stderr ------
logger = logging.getLogger("forgejo_projects_mcp")
if not logger.handlers:
    _handler = logging.StreamHandler(sys.stderr)
    _handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    logger.addHandler(_handler)
    logger.setLevel(os.environ.get("FORGEJO_MCP_LOG_LEVEL", "INFO").upper())
    logger.propagate = False

client = ForgejoClient()


@asynccontextmanager
async def _lifespan(_server: MCPServer):
    """Guarantee the Playwright driver is torn down when the server stops."""
    logger.info("forgejo-projects-mcp started")
    try:
        yield
    finally:
        logger.info("forgejo-projects-mcp shutting down; releasing resources")
        await client.close()


mcp = MCPServer("forgejo-projects-mcp", lifespan=_lifespan)


def _classify(exc: Exception) -> dict[str, Any]:
    """Map an exception to a structured, non-leaking error payload.

    Categories follow the MCP guidance: client_error (caller can fix),
    server_error (our/Forgejo fault), external_error (dependency unreachable).
    """
    if isinstance(exc, AuthError):
        return {
            "category": "client_error",
            "code": getattr(exc, "code", "AUTH_FAILED"),
            "message": str(exc),
        }
    if isinstance(exc, ForgejoError):
        status = getattr(exc, "status", None)
        code = getattr(exc, "code", None)
        if code == "NETWORK_ERROR":
            return {
                "category": "external_error",
                "code": "NETWORK_ERROR",
                "message": str(exc),
                "retry_after": 30,
            }
        if status is not None and status >= 500:
            return {
                "category": "server_error",
                "code": code or "UPSTREAM_ERROR",
                "message": str(exc),
                "retry_after": 30,
            }
        if status in (401, 403):
            return {"category": "client_error", "code": "FORBIDDEN", "message": str(exc)}
        if status == 404:
            return {"category": "client_error", "code": "NOT_FOUND", "message": str(exc)}
        if status is not None and 400 <= status < 500:
            return {
                "category": "client_error",
                "code": code or "BAD_REQUEST",
                "message": str(exc),
            }
        # ForgejoError without an HTTP status (e.g. a parse failure).
        return {
            "category": "server_error",
            "code": code or "FORGEJO_ERROR",
            "message": str(exc),
        }
    return {
        "category": "server_error",
        "code": "INTERNAL_ERROR",
        "message": "An unexpected error occurred; see server logs.",
    }


async def _safe(coro: Awaitable) -> Any:
    """Boundary handler: run a client coroutine and never leak a raw exception.

    Known errors become structured ``{"error": {...}}`` payloads the agent can
    reason about; unexpected ones are logged server-side and returned as a
    generic internal error. ``BaseException`` (KeyboardInterrupt / cancellation)
    is intentionally *not* caught so shutdown and cancellation propagate.
    """
    try:
        return await coro
    except (AuthError, ForgejoError) as e:
        logger.info("Tool error: %s", e)
        return {"error": _classify(e)}
    except Exception as e:  # boundary must convert, not crash
        logger.exception("Unhandled tool error")
        return {"error": _classify(e)}


# --------------------------------------------------------------- auth / repos
@mcp.tool()
async def forgejo_status() -> dict:
    """Report whether the MCP is authenticated to the Forgejo instance.

    Reads credentials from FORGEJO_URL / FORGEJO_USERNAME / FORGEJO_PASSWORD and
    authenticates if needed, persisting the session under the OS config dir
    (e.g. ~/.config/forgejo_projects_mcp/).
    """
    return await client.status()


@mcp.tool()
async def authenticate(force: bool = False) -> dict:
    """Authenticate to Forgejo and cache the session.

    Args:
        force: If true, ignore any cached session and log in fresh.
    """
    return await _safe(client.login(force=force))


@mcp.tool()
async def list_repositories(query: str = "", limit: int = 50, page: int = 1) -> dict:
    """List repositories the current user can access (for choosing where to manage projects).

    Args:
        query: Optional name filter.
        limit: Max results (default 50).
        page: Page number (default 1).
    """
    repos = await _safe(client.list_repositories(query=query, limit=limit, page=page))
    if isinstance(repos, dict):  # error
        return repos
    return {"count": len(repos), "repositories": repos}


# -------------------------------------------------------------------- projects
@mcp.tool()
async def list_projects(owner: str, repo: str, state: str = "open") -> dict:
    """List projects (kanban boards) in a repository.

    Args:
        owner: Repository owner (user or org).
        repo: Repository name.
        state: 'open', 'closed', or 'all'.
    """
    res = await _safe(client.list_projects(owner, repo, state))
    if isinstance(res, dict):
        return res
    return {"count": len(res), "projects": res}


@mcp.tool()
async def create_project(
    owner: str, repo: str, title: str,
    description: str = "", card_type: str = "text",
) -> dict:
    """Create a new project/kanban board in a repository.

    Args:
        owner: Repository owner.
        repo: Repository name.
        title: Project title.
        description: Optional description.
        card_type: 'text' or 'images_and_text'.
    """
    return await _safe(client.create_project(owner, repo, title, description, card_type))


@mcp.tool()
async def get_project(owner: str, repo: str, project_id: int) -> dict:
    """Get a project board: its columns and the cards (issues) in each.

    Returns column ids/titles and, per card, the issue id, number and title.
    """
    return await _safe(client.get_project(owner, repo, project_id))


@mcp.tool()
async def update_project(
    owner: str, repo: str, project_id: int,
    title: str | None = None, description: str | None = None,
    card_type: str | None = None,
) -> dict:
    """Update a project's title, description and/or card type."""
    return await _safe(
        client.update_project(owner, repo, project_id, title, description, card_type)
    )


@mcp.tool()
async def close_project(owner: str, repo: str, project_id: int) -> dict:
    """Close (archive) a project."""
    return await _safe(client.close_project(owner, repo, project_id))


@mcp.tool()
async def reopen_project(owner: str, repo: str, project_id: int) -> dict:
    """Reopen a closed project."""
    return await _safe(client.reopen_project(owner, repo, project_id))


@mcp.tool()
async def delete_project(owner: str, repo: str, project_id: int) -> dict:
    """Permanently delete a project. This does not delete its issues."""
    return await _safe(client.delete_project(owner, repo, project_id))


# --------------------------------------------------------------------- columns
@mcp.tool()
async def create_column(
    owner: str, repo: str, project_id: int, title: str, color: str = ""
) -> dict:
    """Add a column to a project board.

    Args:
        color: Optional hex color, e.g. '#e01e5a'.
    """
    return await _safe(client.create_column(owner, repo, project_id, title, color))


@mcp.tool()
async def edit_column(
    owner: str, repo: str, project_id: int, column_id: int,
    title: str | None = None, color: str | None = None,
) -> dict:
    """Edit a column's title and/or color."""
    return await _safe(
        client.edit_column(owner, repo, project_id, column_id, title, color)
    )


@mcp.tool()
async def delete_column(
    owner: str, repo: str, project_id: int, column_id: int
) -> dict:
    """Delete a column. Its cards return to the default/uncategorized column."""
    return await _safe(client.delete_column(owner, repo, project_id, column_id))


@mcp.tool()
async def set_default_column(
    owner: str, repo: str, project_id: int, column_id: int
) -> dict:
    """Set a column as the project's default (where new cards land)."""
    return await _safe(client.set_default_column(owner, repo, project_id, column_id))


# ----------------------------------------------------------------------- cards
@mcp.tool()
async def create_issue(
    owner: str, repo: str, title: str, body: str = "",
    project_id: int | None = None, milestone_id: int | None = None,
    label_ids: list[int] | None = None, assignee_ids: list[int] | None = None,
) -> dict:
    """Create an issue, optionally placing it directly on a project board.

    Args:
        project_id: If given, the issue is added to that project as a card.
        milestone_id: Optional milestone to attach.
        label_ids / assignee_ids: Optional numeric ids.
    """
    return await _safe(
        client.create_issue(
            owner, repo, title, body, project_id, milestone_id, label_ids, assignee_ids
        )
    )


@mcp.tool()
async def add_issues_to_project(
    owner: str, repo: str, project_id: int, issue_numbers: list[int]
) -> dict:
    """Add existing issues (by their repo issue numbers) to a project as cards."""
    return await _safe(
        client.add_issues_to_project(owner, repo, project_id, issue_numbers)
    )


@mcp.tool()
async def remove_issues_from_project(
    owner: str, repo: str, issue_numbers: list[int]
) -> dict:
    """Remove issues (by number) from any project board. The issues themselves survive."""
    return await _safe(
        client.remove_issues_from_project(owner, repo, issue_numbers)
    )


@mcp.tool()
async def move_card(
    owner: str, repo: str, project_id: int, column_id: int, issue_numbers: list[int]
) -> dict:
    """Move one or more cards (issues, by number) into a column, in the given order."""
    return await _safe(
        client.move_card(owner, repo, project_id, column_id, issue_numbers)
    )


@mcp.tool()
async def delete_issue(owner: str, repo: str, number: int) -> dict:
    """Permanently delete an issue by number."""
    return await _safe(client.delete_issue(owner, repo, number))


# ------------------------------------------------------------------ milestones
@mcp.tool()
async def list_milestones(owner: str, repo: str, state: str = "open") -> dict:
    """List milestones in a repository ('open', 'closed', or 'all')."""
    res = await _safe(client.list_milestones(owner, repo, state))
    if isinstance(res, dict):
        return res
    return {"count": len(res), "milestones": res}


@mcp.tool()
async def create_milestone(
    owner: str, repo: str, title: str,
    description: str = "", deadline: str = "",
) -> dict:
    """Create a milestone.

    Args:
        deadline: Optional due date, 'YYYY-MM-DD'.
    """
    return await _safe(
        client.create_milestone(owner, repo, title, description, deadline)
    )


@mcp.tool()
async def edit_milestone(
    owner: str, repo: str, milestone_id: int,
    title: str | None = None, description: str | None = None,
    deadline: str | None = None,
) -> dict:
    """Edit a milestone's title, description and/or deadline ('YYYY-MM-DD')."""
    return await _safe(
        client.edit_milestone(owner, repo, milestone_id, title, description, deadline)
    )


@mcp.tool()
async def close_milestone(owner: str, repo: str, milestone_id: int) -> dict:
    """Close a milestone."""
    return await _safe(client.close_milestone(owner, repo, milestone_id))


@mcp.tool()
async def reopen_milestone(owner: str, repo: str, milestone_id: int) -> dict:
    """Reopen a closed milestone."""
    return await _safe(client.reopen_milestone(owner, repo, milestone_id))


@mcp.tool()
async def delete_milestone(owner: str, repo: str, milestone_id: int) -> dict:
    """Delete a milestone."""
    return await _safe(client.delete_milestone(owner, repo, milestone_id))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
