"""Behavioural tests for ForgejoClient against a fake request context.

These assert the exact HTTP method / path / body each operation issues (the part
most likely to break on a Forgejo upgrade) and that responses are parsed right.
"""

import asyncio
import re

import pytest
from conftest import FakeResponse, make_client
from playwright.async_api import Error as PlaywrightError

from forgejo_projects_mcp.client import AuthError, ForgejoError

REPO = "/o/r"


def run(coro):
    return asyncio.run(coro)


def find_call(client, method, path):
    for ctx in client._pw.contexts + [client._ctx]:
        for c in ctx.calls:
            if c["method"] == method and c["path"] == path:
                return c
    return None


# ----------------------------------------------------------------- auth / login
def test_login_persists_session(tmp_state):
    state = {"logged_in": False}

    def handler(method, path, kw):
        if path == "/user/login" and method == "POST":
            state["logged_in"] = True
            return FakeResponse(status=303, headers={"location": "/"}, url="/")
        if path == "/user/settings":
            return FakeResponse(status=200 if state["logged_in"] else 302)
        return FakeResponse(status=200)

    c = make_client(handler, authed=False)
    result = run(c.login())
    assert result["authenticated"] is True
    # storage_state written on the freshly-authenticated context
    assert any(ctx.storage_saved for ctx in c._pw.contexts)


def test_bad_credentials_raise(tmp_state):
    def handler(method, path, kw):
        if path == "/user/login":
            return FakeResponse(status=200)  # re-rendered login = failure
        if path == "/user/settings":
            return FakeResponse(status=302)
        return FakeResponse(status=200)

    c = make_client(handler, authed=False)
    with pytest.raises(AuthError):
        run(c.login())


def test_request_retries_after_session_bounce():
    state = {"bounced": False}

    def handler(method, path, kw):
        if path == "/user/login":
            return FakeResponse(status=303, headers={"location": "/"}, url="/")
        if path == "/user/settings":
            return FakeResponse(status=200)
        if path == "/repo/search":
            if not state["bounced"]:
                state["bounced"] = True
                # simulate a logged-out bounce to the login page
                return FakeResponse(status=200, url="https://forge.test/user/login")
            return FakeResponse(status=200, json_data={"data": []})
        return FakeResponse(status=200, json_data={"data": []})

    c = make_client(handler, authed=True)
    repos = run(c.list_repositories())
    assert repos == []
    assert state["bounced"] is True  # the first attempt bounced, then retried


# ---------------------------------------------------------------- repositories
def test_list_repositories_parses_json():
    def handler(method, path, kw):
        assert path == "/repo/search"
        return FakeResponse(
            status=200,
            json_data={
                "data": [
                    {"repository": {"full_name": "o/r", "private": True,
                                    "archived": False, "empty": False, "fork": False,
                                    "description": "d"}}
                ]
            },
        )

    c = make_client(handler)
    repos = run(c.list_repositories(query="r"))
    assert repos == [{
        "full_name": "o/r", "owner": "o", "name": "r", "description": "d",
        "private": True, "archived": False, "empty": False, "fork": False,
    }]


# --------------------------------------------------------------------- projects
def _projects_handler(list_html):
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/projects/new":
            return FakeResponse(status=200)
        if method == "GET" and path == f"{REPO}/projects":
            return FakeResponse(status=200, text=list_html)
        return FakeResponse(status=200)
    return handler


def test_create_project_form_and_id_recovery():
    html = '<a href="/o/r/projects/3" class="t">My Board</a>'
    c = make_client(_projects_handler(html))
    out = run(c.create_project("o", "r", "My Board", "desc", card_type="text"))
    call = find_call(c, "POST", f"{REPO}/projects/new")
    assert call["form"]["title"] == "My Board"
    assert call["form"]["content"] == "desc"
    assert call["form"]["card_type"] == "1"      # text -> 1
    assert out["project"] == {"id": 3, "title": "My Board"}


def test_create_project_card_type_images():
    c = make_client(_projects_handler('<a href="/o/r/projects/1">x</a>'))
    run(c.create_project("o", "r", "x", card_type="images_and_text"))
    call = find_call(c, "POST", f"{REPO}/projects/new")
    assert call["form"]["card_type"] == "2"


def test_close_reopen_delete_project_paths():
    seen = []

    def handler(method, path, kw):
        seen.append((method, path))
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.close_project("o", "r", 3))
    run(c.reopen_project("o", "r", 3))
    run(c.delete_project("o", "r", 3))
    assert ("POST", f"{REPO}/projects/3/close") in seen
    assert ("POST", f"{REPO}/projects/3/open") in seen
    assert ("POST", f"{REPO}/projects/3/delete") in seen


# ---------------------------------------------------------------------- columns
BOARD = """
<div class="project-column" data-id="5"><span class="project-column-title-label">To Do</span>
<div class="ui cards" id="board_5"></div></div>
"""


def test_create_column_form_and_recovery():
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200)
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=BOARD)
        return FakeResponse(status=200)

    c = make_client(handler)
    out = run(c.create_column("o", "r", 1, "To Do", color="#fff"))
    call = find_call(c, "POST", f"{REPO}/projects/1")
    assert call["form"] == {"title": "To Do", "color": "#fff"}
    assert out["column"]["id"] == 5


def test_edit_column_uses_put():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))
    run(c.edit_column("o", "r", 1, 5, title="Doing", color="#000"))
    call = find_call(c, "PUT", f"{REPO}/projects/1/5")
    assert call is not None
    assert call["form"] == {"title": "Doing", "color": "#000"}


def test_delete_column_uses_delete():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))
    out = run(c.delete_column("o", "r", 1, 5))
    assert out == {"deleted": True, "column_id": 5}
    assert find_call(c, "DELETE", f"{REPO}/projects/1/5") is not None


def test_delete_default_column_gives_hint():
    def handler(method, path, kw):
        return FakeResponse(status=500, headers={"content-type": "text/html"},
                            text="<p>Internal server error</p>")

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.delete_column("o", "r", 1, 5))
    assert "default column cannot be deleted" in str(exc.value)


def test_set_default_column_path():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))
    run(c.set_default_column("o", "r", 1, 5))
    assert find_call(c, "POST", f"{REPO}/projects/1/5/default") is not None


# ------------------------------------------------------------------------ cards
def _issue_page(issue_id):
    return f'<div data-issue-id="{issue_id}"></div>'


def test_resolve_issue_id():
    c = make_client(lambda m, p, kw: FakeResponse(status=200, text=_issue_page(42)))
    assert run(c.resolve_issue_id("o", "r", 7)) == 42


def test_move_card_builds_json_payload():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        if path == f"{REPO}/issues/8":
            return FakeResponse(status=200, text=_issue_page(43))
        if path == f"{REPO}/projects/1/9/move":
            return FakeResponse(status=200, json_data={"ok": True})
        return FakeResponse(status=200)

    c = make_client(handler)
    out = run(c.move_card("o", "r", 1, 9, [7, 8]))
    call = find_call(c, "POST", f"{REPO}/projects/1/9/move")
    assert call["data"] == {"issues": [
        {"issueID": 42, "sorting": 0},
        {"issueID": 43, "sorting": 1},
    ]}
    assert out["result"] == {"ok": True}


def test_add_issues_to_project_form():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.add_issues_to_project("o", "r", 1, [7]))
    call = find_call(c, "POST", f"{REPO}/issues/projects")
    assert call["form"] == {"id": "1", "issue_ids": "42"}


def test_remove_issues_uses_project_zero():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.remove_issues_from_project("o", "r", [7]))
    call = find_call(c, "POST", f"{REPO}/issues/projects")
    assert call["form"] == {"id": "0", "issue_ids": "42"}


def test_create_issue_parses_redirect():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/new":
            return FakeResponse(status=200, headers={"content-type": "application/json"},
                                json_data={"redirect": "/o/r/issues/11"})
        return FakeResponse(status=200)

    c = make_client(handler)
    out = run(c.create_issue("o", "r", "T", body="b", project_id=1))
    assert out["number"] == 11
    call = find_call(c, "POST", f"{REPO}/issues/new")
    assert call["form"]["project_id"] == "1"


# -------------------------------------------------------------------- milestones
def test_milestone_paths():
    seen = []

    def handler(method, path, kw):
        seen.append((method, path))
        if path == f"{REPO}/milestones":
            return FakeResponse(status=200, text='<a href="/o/r/milestone/1">Sprint</a>')
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.create_milestone("o", "r", "Sprint", deadline="2026-12-31"))
    run(c.edit_milestone("o", "r", 1, title="Sprint 2"))
    run(c.close_milestone("o", "r", 1))
    run(c.reopen_milestone("o", "r", 1))
    run(c.delete_milestone("o", "r", 1))
    assert ("POST", f"{REPO}/milestones/new") in seen
    assert ("POST", f"{REPO}/milestones/1/edit") in seen
    assert ("POST", f"{REPO}/milestones/1/close") in seen
    assert ("POST", f"{REPO}/milestones/1/open") in seen
    # delete uses the collection route with ?id=N, NOT /milestones/{id}/delete
    assert ("POST", f"{REPO}/milestones/delete") in seen
    assert ("POST", f"{REPO}/milestones/1/delete") not in seen


def test_delete_milestone_route_and_form():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))
    run(c.delete_milestone("o", "r", 5))
    call = find_call(c, "POST", f"{REPO}/milestones/delete")
    assert call is not None
    assert call["form"] == {"id": "5"}
    assert find_call(c, "POST", f"{REPO}/milestones/5/delete") is None


def _state_handler(prefix, open_id, closed_id):
    def handler(method, path, kw):
        state = (kw.get("params") or {}).get("state")
        if state == "open":
            return FakeResponse(status=200, text=f'<a href="/o/r/{prefix}/{open_id}" class="t">Open</a>')
        if state == "closed":
            return FakeResponse(status=200, text=f'<a href="/o/r/{prefix}/{closed_id}" class="t">Closed</a>')
        return FakeResponse(status=200, text="")
    return handler


def test_list_projects_all_merges_open_and_closed():
    c = make_client(_state_handler("projects", 1, 2))
    got = run(c.list_projects("o", "r", "all"))
    assert [p["id"] for p in got] == [1, 2]
    # merging "all" must actually query both concrete states
    assert find_call(c, "GET", f"{REPO}/projects") is not None


def test_list_milestones_all_merges_open_and_closed():
    c = make_client(_state_handler("milestone", 3, 4))
    got = run(c.list_milestones("o", "r", "all"))
    assert [m["id"] for m in got] == [3, 4]


# ------------------------------------------------------- error / shutdown hardening
def test_network_failure_becomes_forgejo_error():
    def handler(method, path, kw):
        raise PlaywrightError("connect ECONNREFUSED")

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.list_repositories())
    assert exc.value.code == "NETWORK_ERROR"
    assert exc.value.status is None


def test_http_error_carries_status_and_code():
    def handler(method, path, kw):
        return FakeResponse(status=404, headers={"content-type": "text/html"},
                            text="<p>Not found</p>")

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.get_project("o", "r", 1))
    assert exc.value.status == 404
    assert exc.value.code == "HTTP_404"


def test_close_is_idempotent_and_never_raises():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))
    run(c.close())
    run(c.close())  # second call is a no-op
    assert c._ctx is None
    assert c._pw is None


# --------------------------------------------------------------- bulk / reading
def _issue_html(n, title="T", state="open"):
    octicon = "octicon-issue-closed" if state == "closed" else "octicon-issue-opened"
    return (
        f'<meta property="og:title" content="{title}">'
        f'<span class="index">#{n}</span>'
        f'<div data-issue-id="{1000 + n}"></div>'
        f'<div class="issue-state-label"><svg class="svg {octicon}"></svg></div>'
        f'<div id="issue-{n}-raw" class="raw-content">body {n}</div>'
    )


_READ_BOARD = """
<div class="project-column" data-id="5"><span class="project-column-title-label">To Do</span>
  <div class="ui cards" id="board_5">
    <div class="issue-card" data-issue="1042"><a href="/o/r/issues/42">C1</a></div>
  </div></div>
<div class="project-column" data-id="6"><span class="project-column-title-label">Done</span>
  <div class="ui cards" id="board_6">
    <div class="issue-card" data-issue="1043"><a href="/o/r/issues/43">C2</a></div>
  </div></div>
"""


def _read_handler(method, path, kw):
    if path == f"{REPO}/projects/1":
        return FakeResponse(status=200, text=_READ_BOARD)
    if path == f"{REPO}/issues":
        return FakeResponse(status=200,
                            text='<a href="/o/r/issues/42">a</a><a href="/o/r/issues/43">b</a>')
    if path == f"{REPO}/milestones":
        return FakeResponse(status=200, text='<a href="/o/r/milestone/7">Sprint 7</a>')
    m = re.fullmatch(rf"{REPO}/issues/(\d+)", path)
    if m:
        n = int(m.group(1))
        return FakeResponse(status=200, text=_issue_html(n, f"Card {n}"))
    return FakeResponse(status=200)


def test_read_issue_full():
    c = make_client(_read_handler)
    got = run(c.read_issue("o", "r", 42))
    assert got["number"] == 42
    assert got["title"] == "Card 42"
    assert got["body"] == "body 42"


def test_bulk_read_issues_returns_all_and_inlines_errors():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/99":
            raise PlaywrightError("boom")
        return _read_handler(method, path, kw)

    c = make_client(handler)
    got = run(c.bulk_read_issues("o", "r", [42, 99, 43]))
    assert [g["number"] for g in got] == [42, 99, 43]
    assert got[0]["title"] == "Card 42"
    assert "error" in got[1]              # failed one is inlined, not fatal
    assert got[2]["title"] == "Card 43"


def test_read_column_content():
    c = make_client(_read_handler)
    got = run(c.read_column_content("o", "r", 1, 5))
    assert got["column"] == {"id": 5, "title": "To Do"}
    assert got["total"] == 1
    assert got["returned"] == 1
    assert got["error_count"] == 0
    assert got["truncated"] is False
    assert got["issues"][0]["number"] == 42


def test_read_column_content_missing_column():
    c = make_client(_read_handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.read_column_content("o", "r", 1, 999))
    assert exc.value.code == "COLUMN_NOT_FOUND"


def test_read_project_content():
    c = make_client(_read_handler)
    got = run(c.read_project_content("o", "r", 1))
    assert got["total"] == 2
    assert got["returned"] == 2
    assert got["error_count"] == 0
    cols = {c["title"]: c for c in got["columns"]}
    assert cols["To Do"]["cards"][0]["number"] == 42
    assert cols["Done"]["cards"][0]["body"] == "body 43"


def test_read_project_limit_offset_paginates():
    c = make_client(_read_handler)
    got = run(c.read_project_content("o", "r", 1, limit=1))
    assert got["total"] == 2
    assert got["returned"] == 1
    assert got["truncated"] is True
    cols = {c["title"]: c for c in got["columns"]}
    assert [x["number"] for x in cols["To Do"]["cards"]] == [42]
    assert cols["Done"]["cards"] == []


def test_read_milestone_content():
    c = make_client(_read_handler)
    got = run(c.read_milestone_content("o", "r", 7))
    assert got["milestone"] == {"id": 7, "title": "Sprint 7"}
    assert {i["number"] for i in got["issues"]} == {42, 43}


def test_read_milestone_not_found():
    c = make_client(_read_handler)   # milestones page only lists id 7
    with pytest.raises(ForgejoError) as exc:
        run(c.read_milestone_content("o", "r", 999))
    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert exc.value.status == 404


def test_invalid_state_is_rejected():
    c = make_client(_read_handler)
    for call in (
        lambda: c.list_projects("o", "r", "bogus"),
        lambda: c.read_project_content("o", "r", 1, state="nope"),
        lambda: c.bulk_read_issues("o", "r", [1], "weird"),
    ):
        with pytest.raises(ForgejoError) as exc:
            run(call())
        assert exc.value.code == "INVALID_STATE"
        assert exc.value.status == 400


def test_bulk_read_issues_state_filter():
    def handler(method, path, kw):
        m = re.fullmatch(rf"{REPO}/issues/(\d+)", path)
        if m:
            n = int(m.group(1))
            st = "closed" if n == 43 else "open"
            return FakeResponse(status=200, text=_issue_html(n, f"C{n}", st))
        return FakeResponse(status=200)

    c = make_client(handler)
    assert [i["number"] for i in run(c.bulk_read_issues("o", "r", [42, 43], "open"))] == [42]
    assert [i["number"] for i in run(c.bulk_read_issues("o", "r", [42, 43], "closed"))] == [43]


def test_read_project_no_filter_skips_issue_query():
    c = make_client(_read_handler)
    got = run(c.read_project_content("o", "r", 1))
    assert got["returned"] == 2
    assert find_call(c, "GET", f"{REPO}/issues") is None   # no server-side filter needed


def test_read_project_milestone_filter():
    def handler(method, path, kw):
        if path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=_READ_BOARD)
        if path == f"{REPO}/issues":
            params = kw.get("params") or {}
            body = '<a href="/o/r/issues/42">x</a>'
            if params.get("milestone") != "7":
                body += '<a href="/o/r/issues/43">y</a>'
            return FakeResponse(status=200, text=body)
        m = re.fullmatch(rf"{REPO}/issues/(\d+)", path)
        if m:
            n = int(m.group(1))
            return FakeResponse(status=200, text=_issue_html(n, f"C{n}"))
        return FakeResponse(status=200)

    c = make_client(handler)
    got = run(c.read_project_content("o", "r", 1, milestone=7))
    call = find_call(c, "GET", f"{REPO}/issues")
    assert call["params"]["project"] == "1"       # project is fixed by the tool
    assert call["params"]["milestone"] == "7"
    assert got["returned"] == 1
    cols = {col["title"]: col for col in got["columns"]}
    assert [x["number"] for x in cols["To Do"]["cards"]] == [42]
    assert cols["Done"]["cards"] == []            # card 43 filtered out


def test_read_milestone_passes_project_and_state():
    c = make_client(_read_handler)
    run(c.read_milestone_content("o", "r", 7, state="open", project=5))
    call = find_call(c, "GET", f"{REPO}/issues")
    assert call["params"]["milestone"] == "7"
    assert call["params"]["project"] == "5"
    assert call["params"]["state"] == "open"


def test_bulk_move_cards_groups_by_column_and_builds_payloads():
    moves = [
        {"issue_number": 42, "column_id": 5},
        {"issue_number": 43, "column_id": 5},
        {"issue_number": 44, "column_id": 6},
    ]
    c = make_client(_read_handler)
    out = run(c.bulk_move_cards("o", "r", 1, moves))
    assert out["moved_count"] == 3
    col5 = find_call(c, "POST", f"{REPO}/projects/1/5/move")
    col6 = find_call(c, "POST", f"{REPO}/projects/1/6/move")
    assert col5["data"] == {"issues": [
        {"issueID": 1042, "sorting": 0},
        {"issueID": 1043, "sorting": 1},
    ]}
    assert col6["data"] == {"issues": [{"issueID": 1044, "sorting": 0}]}


def test_throttle_advances_schedule():
    c = make_client(lambda m, p, kw: FakeResponse(status=200))

    async def go():
        before = c._next_request
        await c._throttle()
        await c._throttle()
        return c._next_request > before

    assert run(go()) is True
