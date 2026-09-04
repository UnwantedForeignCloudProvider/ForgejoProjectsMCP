"""Unit tests for the HTML/JSON parsing helpers (pure, offline)."""

import asyncio

from conftest import FakeResponse

from forgejo_projects_mcp.client import ForgejoClient

BOARD_HTML = """
<div id="project-board"><div class="board sortable">
  <div class="project-column" data-id="5" data-url="/o/r/projects/1/5">
    <div class="project-column-header">
      <span class="project-column-title-label">To Do</span>
    </div>
    <div class="ui cards" id="board_5">
      <div class="issue-card" data-issue="42">
        <a class="x" href="/o/r/issues/7">Card A</a>
      </div>
      <div class="issue-card" data-issue="43">
        <a class="x" href="/o/r/issues/8">Card B</a>
      </div>
    </div>
  </div>
  <div class="project-column" data-id="6">
    <span class="project-column-title-label">Done</span>
    <div class="ui cards" id="board_6"></div>
  </div>
</div></div>
"""


def test_parse_board_columns_and_cards():
    board = ForgejoClient._parse_board(BOARD_HTML)
    cols = {c["title"]: c for c in board["columns"]}
    assert set(cols) == {"To Do", "Done"}
    assert cols["To Do"]["id"] == 5
    assert cols["Done"]["id"] == 6
    assert cols["Done"]["cards"] == []
    assert cols["To Do"]["cards"] == [
        {"issue_id": 42, "number": 7, "title": "Card A"},
        {"issue_id": 43, "number": 8, "title": "Card B"},
    ]


def test_parse_board_ignores_new_column_modal():
    # The "new-project-column-modal" must not be mistaken for a real column.
    html = (
        '<div class="ui modal new-project-column-modal" id="new-project-column-item">'
        '<span class="project-column-title-label">Name</span></div>' + BOARD_HTML
    )
    board = ForgejoClient._parse_board(html)
    assert {c["id"] for c in board["columns"]} == {5, 6}


def test_parse_projects_list():
    html = """
    <a href="/o/r/projects/2"><svg class="icon"/></a>
    <a href="/o/r/projects/2" class="title">Alpha</a>
    <a href="/o/r/projects/9" class="title">Beta board</a>
    """
    got = ForgejoClient._parse_projects_list(html)
    assert got == [
        {"id": 2, "title": "Alpha"},
        {"id": 9, "title": "Beta board"},
    ]


def test_parse_milestones():
    html = """
    <a href="/o/r/milestone/1">Sprint 1</a>
    <a href="/o/r/milestone/1/edit">Edit</a>
    <a href="/o/r/milestone/4">Sprint 2</a>
    """
    got = ForgejoClient._parse_milestones(html)
    assert got == [
        {"id": 1, "title": "Sprint 1"},
        {"id": 4, "title": "Sprint 2"},
    ]


ISSUE_HTML = """
<meta property="og:title" content="My Card">
<h1 class="tw-break-anywhere"> My Card <span class="index">#42</span> </h1>
<div class="ui green label issue-state-label"><svg viewBox="0 0 16 16" class="svg octicon-issue-opened"></svg>Open</div>
<a href="/o/r/milestone/7">Sprint 7</a>
<div id="issue-42-raw" class="raw-content">Line one.

Line two.</div>
<div class="timeline-item comment" id="issuecomment-9">
  <a class="author text black" href="/alice">alice</a>
  <div id="issuecomment-9-raw" class="raw-content">A **comment**.</div>
</div>
<div class="timeline-item event" id="issuecomment-10">changed the milestone</div>
"""


def test_parse_issue_full_content():
    got = ForgejoClient._parse_issue(ISSUE_HTML)
    assert got["number"] == 42
    assert got["title"] == "My Card"
    assert got["state"] == "open"
    assert got["milestone"] == {"id": 7, "title": "Sprint 7"}
    assert got["body"] == "Line one.\n\nLine two."
    # only the real comment, not the timeline event
    assert got["comments"] == [{"author": "alice", "body": "A **comment**."}]


def test_parse_issue_closed_state():
    html = (
        '<meta property="og:title" content="Done">'
        '<span class="index">#5</span>'
        '<div class="ui red label issue-state-label"><svg class="svg octicon-issue-closed"></svg>Closed</div>'
        '<div id="issue-5-raw" class="raw-content">x</div>'
    )
    got = ForgejoClient._parse_issue(html)
    assert got["state"] == "closed"
    assert got["comments"] == []


def test_error_detail_json():
    r = FakeResponse(
        status=500,
        headers={"content-type": "application/json"},
        json_data={"message": "boom"},
    )
    assert asyncio.run(ForgejoClient._error_detail(r)) == ": boom"


def test_error_detail_html_is_not_dumped():
    r = FakeResponse(
        status=500,
        headers={"content-type": "text/html"},
        text="<html><body><p>Internal server error</p></body></html>",
    )
    assert asyncio.run(ForgejoClient._error_detail(r)) == ": Internal server error"
