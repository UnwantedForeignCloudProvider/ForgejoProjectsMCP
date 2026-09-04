"""The server registers exactly the expected tool set (offline)."""

import asyncio

from forgejo_projects_mcp.server import mcp

EXPECTED_TOOLS = {
    "forgejo_status", "authenticate", "list_repositories",
    "list_projects", "create_project", "get_project", "update_project",
    "close_project", "reopen_project", "delete_project",
    "create_column", "edit_column", "delete_column", "set_default_column",
    "create_issue", "add_issues_to_project", "remove_issues_from_project",
    "move_card", "delete_issue",
    "list_milestones", "create_milestone", "edit_milestone",
    "close_milestone", "reopen_milestone", "delete_milestone",
}


def test_server_registers_all_tools():
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    assert mcp.name == "forgejo-projects-mcp"
    assert names == EXPECTED_TOOLS


def test_required_params_present_on_key_tools():
    tools = {t.name: t for t in asyncio.run(mcp.list_tools())}
    required = set(tools["move_card"].input_schema.get("required", []))
    assert {"owner", "repo", "project_id", "column_id", "issue_numbers"} <= required
