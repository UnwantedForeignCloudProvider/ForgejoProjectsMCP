"""MCP server exposing Forgejo Projects/Kanban management tools.

Run with:  forgejo-projects-mcp   (stdio transport)
Requires FORGEJO_URL in the environment. FORGEJO_USERNAME and FORGEJO_PASSWORD
are also required when no valid cached session is available.
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field, ValidationError

from .client import AuthError, ForgejoClient, ForgejoError

# Every numeric argument is a Forgejo identifier, and pydantic's default lax
# mode coerces "7", 7.0 and even true into one. That turned a malformed call
# into a well-formed call against the wrong resource -- true reads issue 1 --
# so identifiers are matched strictly. The published JSON schema is unchanged
# ({"type": "integer"}), so MCP clients and the generated CLI see no difference.
Id = Annotated[int, Field(strict=True)]

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


class _ToolServer(MCPServer):
    """An MCPServer that reports a bad argument with a code, like every other
    failure.

    Argument validation runs inside ``call_tool``, before the tool body and so
    before ``_safe`` can classify anything, and a rejection arrived as a raw
    pydantic dump ending in a docs URL -- the one failure in this surface with
    no ``[CODE]`` to branch on. Both transports reach a tool through this
    method (the MCP wire handler calls it, and the CLI dispatches through it),
    so normalising here covers both.
    """

    async def call_tool(self, name, arguments, context=None):
        try:
            return await super().call_tool(name, arguments, context)
        except ToolError as e:
            cause = e.__cause__
            # Only argument validation is rewritten; a ToolError raised by a
            # tool already carries its own code from _safe.
            if not isinstance(cause, ValidationError):
                raise
            detail = "; ".join(
                f"{'.'.join(str(p) for p in d['loc'])}: {d['msg']}"
                for d in cause.errors()
            )
            raise ToolError(f"[INVALID_INPUT] {name}: {detail}") from e


mcp = _ToolServer("forgejo-projects-mcp", lifespan=_lifespan)


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
        raw = getattr(exc, "code", None)
        # A meaningful code from the client wins; raw "HTTP_<n>" is generic and
        # gets prettified to a category label below.
        specific = raw if raw and not raw.startswith("HTTP_") else None
        if specific == "NETWORK_ERROR":
            return {"category": "external_error", "code": "NETWORK_ERROR",
                    "message": str(exc), "retry_after": 30}
        if status is not None and status >= 500:
            return {"category": "server_error", "code": specific or "UPSTREAM_ERROR",
                    "message": str(exc), "retry_after": 30}
        if status in (401, 403):
            return {"category": "client_error", "code": specific or "FORBIDDEN",
                    "message": str(exc)}
        if status == 404:
            return {"category": "client_error", "code": specific or "NOT_FOUND",
                    "message": str(exc)}
        if status is not None and 400 <= status < 500:
            return {"category": "client_error", "code": specific or "BAD_REQUEST",
                    "message": str(exc)}
        # ForgejoError without an HTTP status (e.g. a parse failure).
        return {"category": "server_error", "code": specific or "FORGEJO_ERROR",
                "message": str(exc)}
    return {
        "category": "server_error",
        "code": "INTERNAL_ERROR",
        "message": "An unexpected error occurred; see server logs.",
    }


async def _safe(coro: Awaitable) -> Any:
    """Boundary handler: run a client coroutine; on failure raise a ToolError so
    the MCP result is flagged ``isError: true`` (agents can detect failure without
    parsing content). The message is ``[CODE] message`` with a stable code.

    ``BaseException`` (KeyboardInterrupt / cancellation) is intentionally *not*
    caught so shutdown and cancellation propagate.
    """
    try:
        return await coro
    except (AuthError, ForgejoError) as e:
        info = _classify(e)
        logger.info("Tool error: %s", e)
        raise ToolError(f"[{info['code']}] {info['message']}") from e
    except Exception as e:  # boundary: convert to a flagged tool error, not a crash
        logger.exception("Unhandled tool error")
        info = _classify(e)
        raise ToolError(f"[{info['code']}] {info['message']}") from e


# --------------------------------------------------------------- auth / repos
@mcp.tool()
async def forgejo_status() -> dict:
    """Report authentication, the Forgejo version, and how the tools adapt to it.

    Reads credentials from FORGEJO_URL / FORGEJO_USERNAME / FORGEJO_PASSWORD and
    authenticates if needed, persisting the session under the OS config dir
    (e.g. ~/.config/forgejo_projects_mcp/).

    The session check reads the instance version out of the same response, so
    ``version`` costs no extra request. ``compatibility`` reports the behavior
    resolved for that version: its ``quirks`` and whether it is ``verified``
    (inside the range the integration suite exercises). An unverified or
    undetectable version is not an error -- the newest verified behavior is used.
    """
    return await client.status()


@mcp.tool()
async def authenticate(force: bool = False) -> dict:
    """Authenticate to Forgejo and cache the session.

    Returns the detected instance ``version`` and the ``compatibility`` profile
    resolved for it, alongside the session details.

    Args:
        force: If true, ignore any cached session and log in fresh.
    """
    return await _safe(client.login(force=force))


@mcp.tool()
async def list_repositories(query: str = "", limit: Id = 50, page: Id = 1) -> dict:
    """List repositories the current user can access (for choosing where to manage projects).

    ``description``, ``archived`` and ``empty`` are always null: the search
    route Forgejo offers hard-codes them, so they are reported as unknown rather
    than as an empty description on an unarchived, non-empty repository.
    ``private`` and ``fork`` are real.

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
        title: Project title. Must not be blank: Forgejo accepts an empty title
            and creates a board its own list pages then omit.
        description: Optional description.
        card_type: 'text' or 'images_and_text'.
    """
    return await _safe(client.create_project(owner, repo, title, description, card_type))


@mcp.tool()
async def get_project(owner: str, repo: str, project_id: Id) -> dict:
    """Get a project board: its columns and the cards (issues) in each.

    Returns column ids/titles and, per card, the issue id, number and title.
    """
    return await _safe(client.get_project(owner, repo, project_id))


@mcp.tool()
async def update_project(
    owner: str, repo: str, project_id: Id,
    title: str | None = None, description: str | None = None,
    card_type: str | None = None,
) -> dict:
    """Update a project's title, description and/or card type.

    Fields left unset keep their current value; pass an empty string to clear
    one deliberately.
    """
    return await _safe(
        client.update_project(owner, repo, project_id, title, description, card_type)
    )


@mcp.tool()
async def close_project(owner: str, repo: str, project_id: Id) -> dict:
    """Close (archive) a project."""
    return await _safe(client.close_project(owner, repo, project_id))


@mcp.tool()
async def reopen_project(owner: str, repo: str, project_id: Id) -> dict:
    """Reopen a closed project."""
    return await _safe(client.reopen_project(owner, repo, project_id))


@mcp.tool()
async def delete_project(owner: str, repo: str, project_id: Id) -> dict:
    """Permanently delete a project. This does not delete its issues."""
    return await _safe(client.delete_project(owner, repo, project_id))


# --------------------------------------------------------------------- columns
@mcp.tool()
async def create_column(
    owner: str, repo: str, project_id: Id, title: str, color: str = ""
) -> dict:
    """Add a column to a project board.

    Args:
        title: Column title. Must not be blank: Forgejo accepts an empty title
            and renders a column nothing can tell apart from another.
        color: Optional hex color: six digits after a '#', e.g. '#e01e5a'. The
            '#fff' shorthand is not accepted. Anything else is refused here;
            Forgejo answers it with HTTP 500.
    """
    return await _safe(client.create_column(owner, repo, project_id, title, color))


@mcp.tool()
async def edit_column(
    owner: str, repo: str, project_id: Id, column_id: Id,
    title: str | None = None, color: str | None = None,
) -> dict:
    """Edit a column's title and/or color.

    Only the fields given are sent. A blank title is refused.
    """
    return await _safe(
        client.edit_column(owner, repo, project_id, column_id, title, color)
    )


@mcp.tool()
async def delete_column(
    owner: str, repo: str, project_id: Id, column_id: Id
) -> dict:
    """Delete a column. Its cards return to the default/uncategorized column."""
    return await _safe(client.delete_column(owner, repo, project_id, column_id))


@mcp.tool()
async def set_default_column(
    owner: str, repo: str, project_id: Id, column_id: Id
) -> dict:
    """Set a column as the project's default (where new cards land)."""
    return await _safe(client.set_default_column(owner, repo, project_id, column_id))


# ----------------------------------------------------------------------- cards
@mcp.tool()
async def create_issue(
    owner: str, repo: str, title: str, body: str = "",
    project_id: Id | None = None, milestone_id: Id | None = None,
    label_ids: list[Id] | None = None, assignee_ids: list[Id] | None = None,
) -> dict:
    """Create an issue, optionally placing it directly on a project board.

    Args:
        project_id: If given, the issue is added to that project as a card. An
            unknown id is reported as not-found and no issue is created.
        milestone_id: Optional milestone to attach. An unknown id is reported as
            not-found and no issue is created.
        label_ids / assignee_ids: Optional numeric ids. An unknown assignee is an
            error; an unknown label is silently ignored by Forgejo, which creates
            the issue without it, so read the issue back if labels matter.
    """
    return await _safe(
        client.create_issue(
            owner, repo, title, body, project_id, milestone_id, label_ids, assignee_ids
        )
    )


@mcp.tool()
async def add_issues_to_project(
    owner: str, repo: str, project_id: Id, issue_numbers: list[Id]
) -> dict:
    """Move existing issues (by their repo issue numbers) onto a project board.

    Named "add" because it creates a card, but an issue holds a single project
    assignment: attaching one that already sits on another board REMOVES it from
    that board. There is no way to keep an issue on two boards at once. Cards
    land in the project's default column.

    The board is read back before this reports success, so ``cards`` names the
    column each issue actually landed in. An attachment Forgejo accepted but
    that produced no card is ATTACH_UNVERIFIED rather than a reported success.
    """
    return await _safe(
        client.add_issues_to_project(owner, repo, project_id, issue_numbers)
    )


@mcp.tool()
async def remove_issues_from_project(
    owner: str, repo: str, issue_numbers: list[Id], project_id: Id | None = None
) -> dict:
    """Detach issues (by number) from EVERY project board they are on.

    Despite the name, this is not scoped to one project: Forgejo's route clears
    an issue's project assignment outright, so an issue on two boards is removed
    from both. There is no project-scoped alternative -- an issue only ever
    holds one project assignment, so there is no such thing to offer.
    The issues themselves survive.

    The returned list describes the resulting state, not what changed: an issue
    that was already on no board is listed as detached too.

    Args:
        project_id: Optional guard, not a scope. When given, every issue must
            already be a card on that project or the call is refused with
            CARD_NOT_FOUND and nothing is detached. Pass it whenever you know
            which board the issues are on: it is the only way to be sure this
            does not clear an assignment you did not mean to touch.
    """
    return await _safe(
        client.remove_issues_from_project(owner, repo, issue_numbers, project_id)
    )


@mcp.tool()
async def move_card(
    owner: str, repo: str, project_id: Id, column_id: Id, issue_numbers: list[Id]
) -> dict:
    """Move one or more cards (issues, by number) into a column, in the given order.

    Every issue must already be a card on this project and the column must be
    on its board; either mistake is reported as not-found (CARD_NOT_FOUND or
    COLUMN_NOT_FOUND) before anything is written. An issue number may appear
    only once per call.
    """
    return await _safe(
        client.move_card(owner, repo, project_id, column_id, issue_numbers)
    )


@mcp.tool()
async def delete_issue(owner: str, repo: str, number: Id) -> dict:
    """Permanently delete an issue by number."""
    return await _safe(client.delete_issue(owner, repo, number))


# --------------------------------------------------------------- bulk / reading
def _summary(issue: dict) -> dict:
    """Lightweight projection of a parsed issue (drops body and comments)."""
    if "error" in issue:
        return issue
    return {k: issue.get(k) for k in ("number", "title", "state", "milestone")}


@mcp.tool()
async def bulk_move_cards(
    owner: str, repo: str, project_id: Id, moves: list[dict[str, Id]]
) -> dict:
    """Move many cards at once, each to its own column.

    Args:
        moves: list of {"issue_number": N, "column_id": C}. Cards going to the
            same column are placed in the order listed. Runs concurrently, so an
            issue number may appear only once per call -- a card cannot be sent
            to two columns in one batch.

    Every card and destination column is checked against the board before
    anything is written, so an unknown column is COLUMN_NOT_FOUND and nothing
    moves. Past that check the batch is not atomic: Forgejo moves one column per
    request, with no undo. If one of those requests fails after others landed,
    the call fails with BULK_MOVE_PARTIAL, naming the columns that moved and the
    ones that did not; re-read the board before retrying.
    """
    return await _safe(client.bulk_move_cards(owner, repo, project_id, moves))


@mcp.tool()
async def bulk_read_issues(
    owner: str, repo: str, issue_numbers: list[Id], state: str = "all"
) -> dict:
    """Read a summary (number, title, state, milestone) of many issues at once.

    Lightweight — bodies and comments are omitted. Use read_card for full content.

    Args:
        state: 'open', 'closed', or 'all' (default) — filters the returned issues.

    Returns ``count`` (successfully read), ``error_count``, the ``issues``
    summaries, and any ``errors`` (issues that could not be read).
    """
    res = await _safe(client.bulk_read_issues(owner, repo, issue_numbers, state))
    ok = [i for i in res if "error" not in i]
    errors = [i for i in res if "error" in i]
    return {
        "count": len(ok),
        "error_count": len(errors),
        "issues": [_summary(i) for i in ok],
        "errors": errors,
    }


@mcp.tool()
async def read_card(owner: str, repo: str, number: Id) -> dict:
    """Full content of one card/issue: title, state, body, milestone, and all comments.

    ⚠️ Network- and token-expensive. Avoid unless asked or necessary.
    """
    return await _safe(client.read_issue(owner, repo, number))


@mcp.tool()
async def read_column(
    owner: str, repo: str, project_id: Id, column_id: Id,
    state: str = "all", milestone: Id | None = None,
    limit: Id | None = None, offset: Id = 0,
) -> dict:
    """Full content of every card in a column.

    Args:
        state: 'open', 'closed', or 'all' (default).
        milestone: optional milestone id to restrict to.
        limit: max cards to read this call (None = no limit); offset to page.

    ⚠️ Network- and token-expensive. Avoid unless asked or necessary; use
    limit/offset to cap cost.
    """
    return await _safe(
        client.read_column_content(
            owner, repo, project_id, column_id, state, milestone, limit, offset
        )
    )


@mcp.tool()
async def read_milestone(
    owner: str, repo: str, milestone_id: Id,
    state: str = "all", project: Id | None = None,
    limit: Id | None = None, offset: Id = 0,
) -> dict:
    """Full content of every issue attached to a milestone.

    Args:
        state: 'open', 'closed', or 'all' (default).
        project: optional project id to restrict to.
        limit: max issues to read this call (None = no limit); offset to page.

    ⚠️ Network- and token-expensive. Avoid unless asked or necessary; use
    limit/offset to cap cost.
    """
    return await _safe(
        client.read_milestone_content(
            owner, repo, milestone_id, state, project, limit, offset
        )
    )


@mcp.tool()
async def read_project(
    owner: str, repo: str, project_id: Id,
    state: str = "all", milestone: Id | None = None,
    limit: Id | None = None, offset: Id = 0,
) -> dict:
    """Full content of an entire board: every column and every card's content.

    Args:
        state: 'open', 'closed', or 'all' (default).
        milestone: optional milestone id to restrict to.
        limit: max cards to read this call (None = no limit); offset to page.

    ⚠️ Network- and token-expensive. Avoid unless asked or necessary; use
    limit/offset to cap cost.
    """
    return await _safe(
        client.read_project_content(
            owner, repo, project_id, state, milestone, limit, offset
        )
    )


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
        deadline: Optional due date, 'YYYY-MM-DD'. A date Forgejo will not
            accept is reported as an error, not as a created milestone.
    """
    return await _safe(
        client.create_milestone(owner, repo, title, description, deadline)
    )


@mcp.tool()
async def edit_milestone(
    owner: str, repo: str, milestone_id: Id,
    title: str | None = None, description: str | None = None,
    deadline: str | None = None,
) -> dict:
    """Edit a milestone's title, description and/or deadline ('YYYY-MM-DD').

    Fields left unset keep their current value; pass an empty string to clear
    one deliberately.
    """
    return await _safe(
        client.edit_milestone(owner, repo, milestone_id, title, description, deadline)
    )


@mcp.tool()
async def close_milestone(owner: str, repo: str, milestone_id: Id) -> dict:
    """Close a milestone."""
    return await _safe(client.close_milestone(owner, repo, milestone_id))


@mcp.tool()
async def reopen_milestone(owner: str, repo: str, milestone_id: Id) -> dict:
    """Reopen a closed milestone."""
    return await _safe(client.reopen_milestone(owner, repo, milestone_id))


@mcp.tool()
async def delete_milestone(owner: str, repo: str, milestone_id: Id) -> dict:
    """Delete a milestone.

    A milestone that does not exist is an error rather than a reported success.
    """
    return await _safe(client.delete_milestone(owner, repo, milestone_id))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
