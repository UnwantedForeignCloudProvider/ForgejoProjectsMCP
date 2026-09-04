"""The argparse CLI mirrors the MCP tools and dispatches through them."""

import asyncio
import json

from forgejo_projects_mcp import cli, server
from forgejo_projects_mcp.client import ForgejoError


def test_parser_has_a_subcommand_per_tool():
    tools = asyncio.run(server.mcp.list_tools())
    parser = cli.build_parser(tools)
    # argparse stores subcommands in the choices of the subparsers action
    sub = next(a for a in parser._actions if a.dest == "tool")
    assert set(sub.choices) == {t.name for t in tools}


def test_cli_dispatches_and_prints_json(monkeypatch, capsys):
    async def fake(owner, repo, state="open"):
        return [{"id": 1, "title": "A"}]

    monkeypatch.setattr(server.client, "list_projects", fake)
    rc = cli.main(["list_projects", "--owner", "o", "--repo", "r"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"count": 1, "projects": [{"id": 1, "title": "A"}]}


def test_cli_parses_json_list_argument(monkeypatch, capsys):
    seen = {}

    async def fake(owner, repo, numbers, state="all"):
        seen["numbers"] = numbers
        return [{"number": n, "title": "t", "state": "open", "milestone": None} for n in numbers]

    monkeypatch.setattr(server.client, "bulk_read_issues", fake)
    rc = cli.main(["bulk_read_issues", "--owner", "o", "--repo", "r",
                   "--issue_numbers", "[1, 2, 3]"])
    assert rc == 0
    assert seen["numbers"] == [1, 2, 3]      # parsed from JSON
    assert json.loads(capsys.readouterr().out)["count"] == 3


def test_cli_returns_nonzero_on_error(monkeypatch, capsys):
    async def boom(*a, **k):
        raise ForgejoError("nope", status=404, code="HTTP_404")

    monkeypatch.setattr(server.client, "create_project", boom)
    rc = cli.main(["create_project", "--owner", "o", "--repo", "r", "--title", "x"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "NOT_FOUND" in out["error"]   # error is a "[CODE] message" string
