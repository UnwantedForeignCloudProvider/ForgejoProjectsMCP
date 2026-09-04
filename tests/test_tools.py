"""Tests for the MCP tool layer via mcp.call_tool, with the client mocked.

These exercise the request/response path a real MCP client uses (argument
validation, result serialization) without any network.
"""

import asyncio
import json

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from forgejo_projects_mcp import server
from forgejo_projects_mcp.client import AuthError, ForgejoError


def call(name, **args):
    return asyncio.run(server.mcp.call_tool(name, args))


def result_json(res):
    for c in getattr(res, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


def patch(monkeypatch, name, fn):
    monkeypatch.setattr(server.client, name, fn)


def test_stdio_server_client_has_no_credential_provider():
    assert server.client._credential_provider is None


def test_list_repositories_wraps_count(monkeypatch):
    async def fake(query="", limit=50, page=1):
        return [{"full_name": "o/r"}, {"full_name": "o/s"}]

    patch(monkeypatch, "list_repositories", fake)
    out = result_json(call("list_repositories"))
    assert out["count"] == 2
    assert out["repositories"][0]["full_name"] == "o/r"


def test_list_projects_wraps_count(monkeypatch):
    async def fake(owner, repo, state="open"):
        return [{"id": 1, "title": "A"}]

    patch(monkeypatch, "list_projects", fake)
    out = result_json(call("list_projects", owner="o", repo="r"))
    assert out == {"count": 1, "projects": [{"id": 1, "title": "A"}]}


def test_passthrough_tool_returns_client_dict(monkeypatch):
    async def fake(owner, repo, project_id, title, color=""):
        return {"created": True, "column": {"id": 5, "title": title}}

    patch(monkeypatch, "create_column", fake)
    out = result_json(call("create_column", owner="o", repo="r",
                           project_id=1, title="To Do"))
    assert out["column"]["id"] == 5


def test_error_raises_tool_error_with_code(monkeypatch):
    # A 404 is surfaced as a raised ToolError (MCP isError:true), not a value.
    async def boom(*a, **k):
        raise ForgejoError("boom", status=404, code="HTTP_404")

    patch(monkeypatch, "create_project", boom)
    with pytest.raises(ToolError) as exc:
        call("create_project", owner="o", repo="r", title="x")
    assert "[NOT_FOUND]" in str(exc.value)


def test_specific_not_found_code_is_preserved(monkeypatch):
    async def boom(*a, **k):
        raise ForgejoError("no such milestone", status=404, code="MILESTONE_NOT_FOUND")

    patch(monkeypatch, "read_milestone_content", boom)
    with pytest.raises(ToolError) as exc:
        call("read_milestone", owner="o", repo="r", milestone_id=9)
    assert "[MILESTONE_NOT_FOUND]" in str(exc.value)


def test_auth_error_raises_with_code(monkeypatch):
    async def boom(*a, **k):
        raise AuthError("no creds", code="MISSING_CONFIG")

    patch(monkeypatch, "list_repositories", boom)
    with pytest.raises(ToolError) as exc:
        call("list_repositories")
    assert "[MISSING_CONFIG]" in str(exc.value)


def test_unexpected_error_is_generic_and_does_not_leak(monkeypatch):
    async def boom(*a, **k):
        raise ValueError("internal detail that must not leak")

    patch(monkeypatch, "list_projects", boom)
    with pytest.raises(ToolError) as exc:
        call("list_projects", owner="o", repo="r")
    assert "[INTERNAL_ERROR]" in str(exc.value)
    assert "internal detail" not in str(exc.value)


def test_move_card_forwards_arguments(monkeypatch):
    captured = {}

    async def fake(owner, repo, project_id, column_id, issue_numbers):
        captured.update(locals())
        return {"moved": issue_numbers, "column_id": column_id, "result": {"ok": True}}

    patch(monkeypatch, "move_card", fake)
    out = result_json(call("move_card", owner="o", repo="r",
                           project_id=1, column_id=9, issue_numbers=[7, 8]))
    assert captured["issue_numbers"] == [7, 8]
    assert out["result"] == {"ok": True}


def test_missing_required_argument_is_rejected():
    # move_card requires column_id; omitting it must fail validation.
    with pytest.raises(Exception):
        call("move_card", owner="o", repo="r", project_id=1, issue_numbers=[1])


def test_bulk_read_issues_tool_returns_summaries(monkeypatch):
    async def fake(owner, repo, numbers, state="all"):
        # client returns full content; the tool must strip body/comments
        return [
            {"number": 1, "title": "A", "state": "open", "milestone": None,
             "body": "big body", "comments": [{"author": "x", "body": "c"}]},
            {"number": 2, "error": "boom"},
        ]

    patch(monkeypatch, "bulk_read_issues", fake)
    out = result_json(call("bulk_read_issues", owner="o", repo="r", issue_numbers=[1, 2]))
    assert out["count"] == 1               # successes only (was ambiguous before)
    assert out["error_count"] == 1
    assert out["issues"][0] == {"number": 1, "title": "A", "state": "open", "milestone": None}
    assert "body" not in out["issues"][0] and "comments" not in out["issues"][0]
    assert out["errors"] == [{"number": 2, "error": "boom"}]   # failures separated


def test_lifespan_closes_client_on_shutdown(monkeypatch):
    closed = {"n": 0}

    async def fake_close():
        closed["n"] += 1

    monkeypatch.setattr(server.client, "close", fake_close)

    async def run_lifespan():
        async with server._lifespan(server.mcp):
            pass

    asyncio.run(run_lifespan())
    assert closed["n"] == 1
