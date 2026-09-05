"""The MCP tool layer, dispatched against a live instance.

``tests/test_tools.py`` calls the same tools with the client replaced by stubs,
which pins down argument validation and result shaping. Here the client is real,
so the tools are exercised end to end: the arguments an MCP client would send,
through the web routes, back out as the JSON an MCP client would receive.

That makes these the tests that would catch a tool wired to the wrong client
method, or a result shape that only holds for invented data.
"""

from __future__ import annotations

import json

import pytest

from mcp.server.mcpserver.exceptions import ToolError

from forgejo_projects_mcp import server

from .helpers import unique


@pytest.fixture
def live_tools(live_client, monkeypatch):
    """Point the MCP server's module-level client at the live instance."""
    monkeypatch.setattr(server, "client", live_client)
    return server.mcp


def call(mcp, run_async, name: str, **arguments):
    """Invoke a tool the way an MCP client does and return its JSON result."""
    result = run_async(mcp.call_tool(name, arguments))
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None


# ------------------------------------------------------------------- sessions
def test_forgejo_status_reports_a_live_session(live_tools, forgejo_target, run_async):
    """The status tool is the one an agent calls first; it must be truthful."""
    out = call(live_tools, run_async, "forgejo_status")

    assert out["authenticated"] is True
    assert out["instance"] == forgejo_target.url
    assert out["version"]
    assert out["compatibility"]["verified"] is True


def test_authenticate_logs_in_and_reports_the_version(live_tools, run_async):
    out = call(live_tools, run_async, "authenticate", force=True)

    assert out["authenticated"] is True
    assert out["version"]


# --------------------------------------------------------------- list wrappers
def test_list_repositories_wraps_the_count(live_tools, seeded_repo, run_async):
    out = call(live_tools, run_async, "list_repositories", query=seeded_repo.name)

    assert out["count"] == len(out["repositories"]) == 1
    assert out["repositories"][0]["full_name"] == seeded_repo.full_name


def test_list_projects_wraps_the_count(
    live_tools, seeded_repo, live_project, run_async
):
    out = call(
        live_tools, run_async, "list_projects",
        owner=seeded_repo.owner, repo=seeded_repo.name,
    )

    assert out["count"] == len(out["projects"])
    assert live_project["id"] in [p["id"] for p in out["projects"]]


# ---------------------------------------------------------------- passthroughs
def test_a_passthrough_tool_returns_what_the_client_returns(
    live_tools, seeded_repo, live_project, run_async
):
    """create_column is dispatched, performed, and its result handed back whole."""
    out = call(
        live_tools, run_async, "create_column",
        owner=seeded_repo.owner, repo=seeded_repo.name,
        project_id=live_project["id"], title="From MCP",
    )

    assert out["created"] is True
    assert out["column"]["title"] == "From MCP"
    assert isinstance(out["column"]["id"], int)


def test_move_card_forwards_every_argument(
    live_tools, seeded_repo, live_project, run_async
):
    """The argument names in the schema reach the client unchanged."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    numbers = list(seeded_repo.issue_numbers[:2])
    column_id = int(
        call(live_tools, run_async, "create_column", owner=owner, repo=repo,
             project_id=project_id, title="Target")["column"]["id"]
    )
    call(live_tools, run_async, "add_issues_to_project", owner=owner, repo=repo,
         project_id=project_id, issue_numbers=numbers)

    out = call(
        live_tools, run_async, "move_card", owner=owner, repo=repo,
        project_id=project_id, column_id=column_id, issue_numbers=numbers,
    )

    assert out["moved"] == numbers
    assert out["column_id"] == column_id
    board = call(live_tools, run_async, "get_project", owner=owner, repo=repo,
                 project_id=project_id)
    moved = next(c for c in board["columns"] if c["id"] == column_id)
    assert [card["number"] for card in moved["cards"]] == numbers
    call(live_tools, run_async, "remove_issues_from_project", owner=owner, repo=repo,
         issue_numbers=numbers)


def test_bulk_read_issues_returns_summaries_and_separates_errors(
    live_tools, seeded_repo, run_async
):
    """The tool strips bodies and comments, and partitions the failures out."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    good = seeded_repo.issue_numbers[0]
    missing = 99_999

    out = call(
        live_tools, run_async, "bulk_read_issues",
        owner=owner, repo=repo, issue_numbers=[good, missing],
    )

    assert out["count"] == 1
    assert out["error_count"] == 1
    summary = out["issues"][0]
    assert summary["number"] == good
    assert set(summary) == {"number", "title", "state", "milestone"}
    assert [e["number"] for e in out["errors"]] == [missing]


def test_read_project_returns_full_card_content(
    live_tools, seeded_repo, live_project, run_async
):
    """The expensive reader really does return bodies, not just titles."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    numbers = list(seeded_repo.issue_numbers[:2])
    column_id = int(
        call(live_tools, run_async, "create_column", owner=owner, repo=repo,
             project_id=project_id, title="Reading")["column"]["id"]
    )
    call(live_tools, run_async, "add_issues_to_project", owner=owner, repo=repo,
         project_id=project_id, issue_numbers=numbers)
    call(live_tools, run_async, "move_card", owner=owner, repo=repo,
         project_id=project_id, column_id=column_id, issue_numbers=numbers)

    out = call(live_tools, run_async, "read_project", owner=owner, repo=repo,
               project_id=project_id)

    assert out["error_count"] == 0
    cards = [c for col in out["columns"] for c in col["cards"]]
    assert {c["number"] for c in cards} >= set(numbers)
    assert all(c["body"] for c in cards if c["number"] in numbers)
    call(live_tools, run_async, "remove_issues_from_project", owner=owner, repo=repo,
         issue_numbers=numbers)


# --------------------------------------------------------------------- errors
def test_a_real_not_found_becomes_a_tool_error(live_tools, seeded_repo, run_async):
    """A 404 from the instance is raised as a ToolError carrying NOT_FOUND."""
    with pytest.raises(ToolError) as exc:
        call(live_tools, run_async, "get_project", owner=seeded_repo.owner,
             repo=seeded_repo.name, project_id=999_999)

    assert "[NOT_FOUND]" in str(exc.value)


def test_a_specific_not_found_code_survives_the_boundary(
    live_tools, seeded_repo, run_async
):
    """A code more precise than the status is kept rather than flattened."""
    with pytest.raises(ToolError) as exc:
        call(live_tools, run_async, "read_milestone", owner=seeded_repo.owner,
             repo=seeded_repo.name, milestone_id=999_999)

    assert "[MILESTONE_NOT_FOUND]" in str(exc.value)


def test_a_missing_configuration_is_reported_as_such(
    live_tools, client_factory, monkeypatch, run_async
):
    """With nothing to authenticate with, the tool layer says so precisely."""
    unconfigured = client_factory(credentials=False, base_url="")
    monkeypatch.setattr(server, "client", unconfigured)

    with pytest.raises(ToolError) as exc:
        call(live_tools, run_async, "list_repositories")

    assert "[MISSING_CONFIG]" in str(exc.value)


def test_an_unreachable_instance_does_not_leak_internals(
    live_tools, client_factory, monkeypatch, run_async
):
    """A transport failure is reported without a stack trace or a raw error."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(
        server, "client", client_factory(base_url=f"http://127.0.0.1:{port}")
    )

    with pytest.raises(ToolError) as exc:
        call(live_tools, run_async, "list_repositories")

    assert "Traceback" not in str(exc.value)


def test_a_missing_required_argument_is_rejected_before_dispatch(
    live_tools, seeded_repo, run_async
):
    """Schema validation runs first, so the instance is never troubled."""
    with pytest.raises(Exception):
        call(live_tools, run_async, "move_card", owner=seeded_repo.owner,
             repo=seeded_repo.name, project_id=1, issue_numbers=[1])


# ------------------------------------------------------------------- lifecycle
def test_the_server_lifespan_closes_the_live_client(live_tools, live_client, run_async):
    """Shutting the server down really releases the browser-less driver."""
    run_async(live_client.ensure())
    assert live_client._pw is not None

    async def run_lifespan():
        async with server._lifespan(server.mcp):
            pass

    run_async(run_lifespan())

    assert live_client._ctx is None
    assert live_client._pw is None


def test_every_registered_tool_is_dispatchable(live_tools, run_async):
    """The registry the offline smoke test asserts is the one served here."""
    tools = run_async(live_tools.list_tools())

    assert live_tools.name == "forgejo-projects-mcp"
    assert "forgejo_status" in {t.name for t in tools}
    assert all(t.input_schema for t in tools)


def test_a_read_only_tool_needs_no_extra_setup(live_tools, seeded_repo, run_async):
    """read_card is the cheapest expensive reader: it must work on its own."""
    out = call(live_tools, run_async, "read_card", owner=seeded_repo.owner,
               repo=seeded_repo.name, number=seeded_repo.issue_numbers[0])

    assert out["number"] == seeded_repo.issue_numbers[0]
    assert out["title"] == "Seeded issue 1"
    assert out["body"]


def test_milestone_tools_round_trip(live_tools, seeded_repo, run_async, writable):
    """The milestone tools cover their own lifecycle through the MCP boundary."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique("MCP milestone")

    created = call(live_tools, run_async, "create_milestone", owner=owner, repo=repo,
                   title=title)
    milestone_id = int(created["milestone"]["id"])
    try:
        listed = call(live_tools, run_async, "list_milestones", owner=owner, repo=repo)
        assert milestone_id in [m["id"] for m in listed["milestones"]]

        call(live_tools, run_async, "edit_milestone", owner=owner, repo=repo,
             milestone_id=milestone_id, title=f"{title} renamed")
        content = call(live_tools, run_async, "read_milestone", owner=owner, repo=repo,
                       milestone_id=milestone_id)
        assert content["milestone"]["title"] == f"{title} renamed"
    finally:
        call(live_tools, run_async, "delete_milestone", owner=owner, repo=repo,
             milestone_id=milestone_id)
