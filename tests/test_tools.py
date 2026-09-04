"""Tests for the MCP tool layer via mcp.call_tool, with the client mocked.

These exercise the request/response path a real MCP client uses (argument
validation, result serialization) without any network.
"""

import asyncio
import json

import pytest

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


def test_error_is_surfaced_not_raised(monkeypatch):
    async def boom(*a, **k):
        raise ForgejoError("boom", status=404, code="HTTP_404")

    patch(monkeypatch, "create_project", boom)
    out = result_json(call("create_project", owner="o", repo="r", title="x"))
    assert out["error"]["category"] == "client_error"
    assert out["error"]["code"] == "NOT_FOUND"
    assert out["error"]["message"] == "boom"


def test_network_error_classified_external(monkeypatch):
    async def boom(*a, **k):
        raise ForgejoError("unreachable", code="NETWORK_ERROR")

    patch(monkeypatch, "list_projects", boom)
    out = result_json(call("list_projects", owner="o", repo="r"))
    assert out["error"]["category"] == "external_error"
    assert out["error"]["retry_after"] == 30


def test_server_5xx_classified_server_error(monkeypatch):
    async def boom(*a, **k):
        raise ForgejoError("upstream", status=500, code="HTTP_500")

    patch(monkeypatch, "get_project", boom)
    out = result_json(call("get_project", owner="o", repo="r", project_id=1))
    assert out["error"]["category"] == "server_error"


def test_auth_error_classified_client(monkeypatch):
    async def boom(*a, **k):
        raise AuthError("no creds", code="MISSING_CONFIG")

    patch(monkeypatch, "list_repositories", boom)
    out = result_json(call("list_repositories"))
    assert out["error"] == {
        "category": "client_error",
        "code": "MISSING_CONFIG",
        "message": "no creds",
    }


def test_unexpected_error_is_generic(monkeypatch):
    async def boom(*a, **k):
        raise ValueError("internal detail that must not leak")

    patch(monkeypatch, "list_projects", boom)
    out = result_json(call("list_projects", owner="o", repo="r"))
    assert out["error"]["category"] == "server_error"
    assert out["error"]["code"] == "INTERNAL_ERROR"
    assert "internal detail" not in out["error"]["message"]


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
