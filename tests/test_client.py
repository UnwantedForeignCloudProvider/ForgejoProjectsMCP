"""Behavioural tests for ForgejoClient against a fake request context.

These assert the exact HTTP method / path / body each operation issues (the part
most likely to break on a Forgejo upgrade) and that responses are parsed right.

Most of these have a live counterpart in ``tests/integration/test_live_client.py``
or ``test_live_projects.py``. A few cannot, because a real Forgejo will not
produce the situation they cover, and they are only meaningful here:

* ``test_every_request_context_carries_an_explicit_timeout`` -- provoking it
  live would need a host that accepts connections and never answers, which no
  test instance can be relied on to be.
* ``test_listing_stops_when_a_page_stops_adding_entries`` -- guards against a
  server that answers an out-of-range page by clamping to the last one. Every
  release from 1.20 to 16 returns an empty page instead, so the guard is for a
  hypothetical future one.
* ``test_create_project_reports_a_refused_form_instead_of_succeeding`` -- Forgejo
  accepts every project title, blank included, so a refusal cannot be provoked
  on this route. The mechanism is covered live through ``create_milestone``,
  which Forgejo does refuse (an impossible deadline).
* ``test_create_project_never_returns_an_unrelated_project`` and
  ``test_create_column_that_cannot_be_found_is_not_reported_as_created`` -- these
  need a create that succeeds but cannot be found afterwards. Client-side
  validation and full-list paging now prevent exactly that, so it is
  unreachable against a real instance.
* ``test_an_error_with_no_missing_reference_keeps_its_own_meaning`` -- needs a
  4xx from ``issues/new`` whose cause is not a missing reference.
* ``test_an_attachment_that_produced_no_card_is_not_reported_as_attached`` --
  needs Forgejo to accept an attach and then not show the card. It attaches
  reliably on every release from 1.20 to 16, so the guard is for a future one
  that stops; the successful path is covered live.
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


def test_login_persists_non_secret_config(tmp_state):
    import json

    import forgejo_projects_mcp.client as client_mod

    state = {"logged_in": False}

    def handler(method, path, kw):
        if path == "/user/login" and method == "POST":
            state["logged_in"] = True
            return FakeResponse(status=303, headers={"location": "/"}, url="/")
        if path == "/user/settings":
            return FakeResponse(status=200 if state["logged_in"] else 302)
        return FakeResponse(status=200)

    c = make_client(handler, authed=False)
    c.base_url = "https://forge.test"
    c.username = "alice"
    c.password = "secret"

    run(c.login())

    saved = json.loads(client_mod.CONFIG_FILE.read_text())
    assert saved == {"base_url": "https://forge.test", "username": "alice"}
    # the password is never written to disk
    assert "secret" not in client_mod.CONFIG_FILE.read_text()


def test_saved_config_supplies_url_and_username(tmp_state, monkeypatch):
    import json

    import forgejo_projects_mcp.client as client_mod

    client_mod.CONFIG_FILE.write_text(
        json.dumps({"base_url": "https://saved.test", "username": "bob"})
    )
    for var in ("FORGEJO_URL", "FORGEJO_USERNAME", "FORGEJO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    c = client_mod.ForgejoClient()

    assert c.base_url == "https://saved.test"
    assert c.username == "bob"
    assert c.password == ""  # never loaded from the config file


def test_env_overrides_saved_config(tmp_state, monkeypatch):
    import json

    import forgejo_projects_mcp.client as client_mod

    client_mod.CONFIG_FILE.write_text(
        json.dumps({"base_url": "https://saved.test", "username": "bob"})
    )
    monkeypatch.setenv("FORGEJO_URL", "https://env.test")
    monkeypatch.delenv("FORGEJO_USERNAME", raising=False)

    c = client_mod.ForgejoClient()

    assert c.base_url == "https://env.test"   # env wins
    assert c.username == "bob"                # falls back to saved


def test_corrupt_config_file_is_ignored(tmp_state, monkeypatch):
    import forgejo_projects_mcp.client as client_mod

    client_mod.CONFIG_FILE.write_text("{not valid json")
    for var in ("FORGEJO_URL", "FORGEJO_USERNAME"):
        monkeypatch.delenv(var, raising=False)

    c = client_mod.ForgejoClient()

    assert c.base_url == ""
    assert c.username == ""


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


def test_cached_session_only_requires_url(tmp_state):
    (tmp_state / "storage_state.json").write_text("{}")

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=200)
        raise AssertionError(f"unexpected request: {method} {path}")

    c = make_client(handler, authed=False)
    c.username = ""
    c.password = ""
    c._ctx = None

    run(c.ensure())

    assert len(c._pw.contexts) == 1
    assert c._pw.contexts[0].new_context_kwargs["storage_state"].endswith(
        "storage_state.json"
    )
    assert find_call(c, "POST", "/user/login") is None


def test_non_forced_login_reuses_cache_without_login_credentials(tmp_state):
    (tmp_state / "storage_state.json").write_text("{}")

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=200)
        raise AssertionError(f"unexpected request: {method} {path}")

    c = make_client(handler, authed=False)
    c.username = ""
    c.password = ""
    c._ctx = None

    result = run(c.login())

    assert result["authenticated"] is True
    assert find_call(c, "POST", "/user/login") is None


def test_invalid_cached_session_requires_login_credentials(tmp_state):
    (tmp_state / "storage_state.json").write_text("{}")

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=302)
        raise AssertionError(f"unexpected request: {method} {path}")

    c = make_client(handler, authed=False)
    c.username = ""
    c.password = ""
    c._ctx = None

    with pytest.raises(AuthError) as exc:
        run(c.ensure())

    assert exc.value.code == "MISSING_CONFIG"
    assert "FORGEJO_USERNAME" in str(exc.value)
    assert "FORGEJO_PASSWORD" in str(exc.value)
    assert find_call(c, "POST", "/user/login") is None


def test_force_login_requires_credentials_even_with_valid_session():
    c = make_client(lambda method, path, kw: FakeResponse(status=200), authed=False)
    c.username = ""
    c.password = ""

    with pytest.raises(AuthError) as exc:
        run(c.login(force=True))

    assert exc.value.code == "MISSING_CONFIG"


def test_credential_provider_recovers_and_recreates_context_for_new_url(tmp_state):
    logged_in = False

    def handler(method, path, kw):
        nonlocal logged_in
        if method == "POST" and path == "/user/login":
            if kw["form"]["password"] == "good-password":
                logged_in = True
                return FakeResponse(status=303)
            return FakeResponse(status=200)
        if path == "/user/settings":
            return FakeResponse(status=200 if logged_in else 302)
        return FakeResponse(status=200)

    c = make_client(handler, authed=False)
    c.username = "wrong-user"
    c.password = "wrong-password"
    old_url = c.base_url
    recoveries = []

    def provide(error):
        recoveries.append(error.code)
        return "https://other-forge.test/", "good-user", "good-password"

    c.set_credential_provider(provide)
    result = run(c.login())

    assert result["authenticated"] is True
    assert recoveries == ["AUTH_FAILED"]
    assert c.base_url != old_url
    assert c._pw.contexts[0].disposed is True
    assert c._pw.contexts[-1].new_context_kwargs["base_url"] == c.base_url
    assert any(ctx.storage_saved for ctx in c._pw.contexts)


def test_every_request_context_carries_an_explicit_timeout(tmp_state):
    """An unanswered request must fail, not stall for Playwright's own default.

    Without this the bound was implicit (30s) and could not be shortened, so an
    instance that accepts connections and never replies looked like a hang.
    """
    from forgejo_projects_mcp import client as client_mod

    c = make_client(lambda m, p, kw: FakeResponse(status=200), authed=True)
    c._ctx = None  # force the real context-creation path
    run(c.list_projects("o", "r", "open"))

    kwargs = c._pw.contexts[-1].new_context_kwargs
    assert kwargs["timeout"] == client_mod._TIMEOUT_SECONDS * 1000
    assert kwargs["timeout"] > 0


def test_session_bounce_can_recover_missing_login_credentials(tmp_state):
    bounced = False
    logged_in = False

    def handler(method, path, kw):
        nonlocal bounced, logged_in
        if method == "POST" and path == "/user/login":
            logged_in = True
            return FakeResponse(status=303)
        if path == "/user/settings":
            return FakeResponse(status=200 if logged_in else 302)
        if path == "/repo/search" and not bounced:
            bounced = True
            return FakeResponse(status=200, url="https://forge.test/user/login")
        if path == "/repo/search":
            return FakeResponse(status=200, json_data={"data": []})
        return FakeResponse(status=200)

    c = make_client(handler, authed=True)
    c.username = ""
    c.password = ""
    recoveries = []

    def provide(error):
        recoveries.append(error.code)
        return c.base_url, "user", "password"

    c.set_credential_provider(provide)
    repos = run(c.list_repositories())

    assert repos == []
    assert recoveries == ["MISSING_CONFIG"]
    assert logged_in is True


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
    """A fake that answers the create route the way Forgejo answers a success.

    Forgejo redirects an accepted project write and re-renders the form with
    HTTP 200 when it refuses one, so the fake has to redirect for the client to
    treat the write as done.
    """
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/projects/new":
            return FakeResponse(status=303, headers={"location": f"{REPO}/projects"})
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
    assert call["form"]["card_type"] == "0"      # text -> 0
    assert out["project"] == {"id": 3, "title": "My Board"}


def test_create_project_card_type_images():
    c = make_client(_projects_handler('<a href="/o/r/projects/1">x</a>'))
    run(c.create_project("o", "r", "x", card_type="images_and_text"))
    call = find_call(c, "POST", f"{REPO}/projects/new")
    assert call["form"]["card_type"] == "1"


def test_create_project_rejects_a_blank_title_without_asking_forgejo():
    """Forgejo accepts a blank title and then hides the project it created."""
    c = make_client(_projects_handler(""))
    with pytest.raises(ForgejoError) as e:
        run(c.create_project("o", "r", "   "))
    assert e.value.code == "INVALID_INPUT"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_create_project_rejects_an_unknown_card_type():
    c = make_client(_projects_handler(""))
    with pytest.raises(ForgejoError) as e:
        run(c.create_project("o", "r", "Board", card_type="bogus"))
    assert e.value.code == "INVALID_INPUT"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_create_project_reports_a_refused_form_instead_of_succeeding():
    """A re-rendered form is a rejection, not a created project."""
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/projects/new":
            return FakeResponse(
                status=200,
                text='<div class="ui negative message flash-message flash-error"'
                     ' hx-swap-oob="true">\n\t\t<p>Title cannot be empty.</p>'
                     '\n\t</div>',
            )
        return FakeResponse(status=200, text="")

    c = make_client(handler)
    with pytest.raises(ForgejoError) as e:
        run(c.create_project("o", "r", "Board"))
    assert e.value.code == "REJECTED"
    assert "Title cannot be empty." in str(e.value)


def test_create_project_never_returns_an_unrelated_project():
    """The old fallback returned the last listed project; that misdirects edits."""
    other = '<a href="/o/r/projects/9" class="t">Someone else\'s board</a>'
    c = make_client(_projects_handler(other))
    with pytest.raises(ForgejoError) as e:
        run(c.create_project("o", "r", "My Board"))
    assert e.value.code == "CREATE_UNVERIFIED"


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
    out = run(c.create_column("o", "r", 1, "To Do", color="#ffffff"))
    call = find_call(c, "POST", f"{REPO}/projects/1")
    assert call["form"] == {"title": "To Do", "color": "#ffffff"}
    assert out["column"]["id"] == 5


def test_edit_column_uses_put():
    c = make_client(_board_handler())
    run(c.edit_column("o", "r", 1, 5, title="Doing", color="#000000"))
    call = find_call(c, "PUT", f"{REPO}/projects/1/5")
    assert call is not None
    assert call["form"] == {"title": "Doing", "color": "#000000"}


def _board_handler(board=None, responses=None):
    """Serve the board read that column operations now make first."""
    responses = responses or {}

    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=BOARD if board is None else board)
        return responses.get((method, path), FakeResponse(status=200))
    return handler


def test_delete_column_uses_delete():
    c = make_client(_board_handler())
    out = run(c.delete_column("o", "r", 1, 5))
    assert out == {"deleted": True, "column_id": 5}
    assert find_call(c, "DELETE", f"{REPO}/projects/1/5") is not None


def test_delete_default_column_gives_hint():
    """A column that exists but cannot go is still explained as the default."""
    c = make_client(_board_handler(responses={
        ("DELETE", f"{REPO}/projects/1/5"): FakeResponse(
            status=500, headers={"content-type": "text/html"},
            text="<p>Internal server error</p>"),
    }))
    with pytest.raises(ForgejoError) as exc:
        run(c.delete_column("o", "r", 1, 5))
    assert "default column cannot be deleted" in str(exc.value)


def test_delete_unknown_column_is_not_found_not_a_500():
    """Forgejo answers an unknown id with the same 500 as the default column."""
    c = make_client(_board_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.delete_column("o", "r", 1, 999))
    assert exc.value.code == "COLUMN_NOT_FOUND"
    assert exc.value.status == 404
    assert find_call(c, "DELETE", f"{REPO}/projects/1/999") is None


@pytest.mark.parametrize("color", ["red", "e01e5a", "#12345", "#gggggg", "#", "#fff"])
def test_column_colors_are_validated_before_forgejo_answers_500(color):
    """Forgejo answers an unparseable color with a bare HTTP 500."""
    c = make_client(_board_handler())

    with pytest.raises(ForgejoError) as exc:
        run(c.create_column("o", "r", 1, "Col", color=color))
    assert exc.value.code == "INVALID_INPUT"

    with pytest.raises(ForgejoError) as exc:
        run(c.edit_column("o", "r", 1, 5, color=color))
    assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("color", ["", "#e01e5a", "#E01E5A"])
def test_valid_column_colors_are_passed_through(color):
    c = make_client(_board_handler())
    run(c.edit_column("o", "r", 1, 5, color=color))
    assert find_call(c, "PUT", f"{REPO}/projects/1/5")["form"]["color"] == color


def test_create_column_rejects_a_blank_title():
    """Forgejo makes an unnamed column that no reader can tell apart."""
    c = make_client(_board_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.create_column("o", "r", 1, "   "))
    assert exc.value.code == "INVALID_INPUT"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_create_column_that_cannot_be_found_is_not_reported_as_created():
    c = make_client(_board_handler(board="<div></div>"))
    with pytest.raises(ForgejoError) as exc:
        run(c.create_column("o", "r", 1, "To Do"))
    assert exc.value.code == "CREATE_UNVERIFIED"


def test_update_project_carries_over_escaped_values_decoded():
    """Form values arrive HTML-escaped; re-posting them raw would double-escape."""
    form_page = (
        '<input name="title" value="A &amp; B &#34;quoted&#34;" required>'
        '<input type="hidden" name="card_type" value="1">'
        '<textarea name="content" placeholder="Description">x &amp; y</textarea>'
    )

    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1/edit":
            return FakeResponse(status=200, text=form_page)
        return FakeResponse(status=303, headers={"location": f"{REPO}/projects"})

    c = make_client(handler)
    run(c.update_project("o", "r", 1, card_type="text"))

    call = find_call(c, "POST", f"{REPO}/projects/1/edit")
    assert call["form"]["title"] == 'A & B "quoted"'
    assert call["form"]["content"] == "x & y"
    assert call["form"]["card_type"] == "0"


def test_edit_column_rejects_a_blank_title():
    """Argument checks come before the board read, so this costs no request."""
    c = make_client(_board_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.edit_column("o", "r", 1, 5, title=" "))
    assert exc.value.code == "INVALID_INPUT"
    assert not c._ctx.calls


def test_set_default_column_path():
    c = make_client(_board_handler())
    run(c.set_default_column("o", "r", 1, 5))
    assert find_call(c, "POST", f"{REPO}/projects/1/5/default") is not None


@pytest.mark.parametrize(
    ("operation", "method", "path"),
    [
        ("edit_column", "PUT", f"{REPO}/projects/1/999"),
        ("delete_column", "DELETE", f"{REPO}/projects/1/999"),
        ("set_default_column", "POST", f"{REPO}/projects/1/999/default"),
    ],
)
def test_every_column_operation_rejects_an_unknown_id_the_same_way(
    operation, method, path
):
    """Forgejo answers all three with a bare HTTP 500 that explains nothing."""
    c = make_client(_board_handler())

    with pytest.raises(ForgejoError) as exc:
        run(getattr(c, operation)("o", "r", 1, 999))

    assert exc.value.code == "COLUMN_NOT_FOUND"
    assert exc.value.status == 404
    assert find_call(c, method, path) is None


# ------------------------------------------------------------------------ cards
def _issue_page(issue_id):
    return f'<div data-issue-id="{issue_id}"></div>'


def test_resolve_issue_id():
    c = make_client(lambda m, p, kw: FakeResponse(status=200, text=_issue_page(42)))
    assert run(c.resolve_issue_id("o", "r", 7)) == 42


def _issue_create_handler(status, milestones=(), projects=()):
    """A new-issue route that fails, over a repo with the given references."""
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/issues/new":
            return FakeResponse(status=status, headers={"content-type": "text/html"},
                                text="<p>Internal server error</p>")
        if method == "GET" and path == f"{REPO}/milestones":
            page = (kw.get("params") or {}).get("page", "1")
            rows = milestones if page == "1" else ()
            return FakeResponse(status=200, text="".join(
                f'<a href="/o/r/milestone/{i}" class="t">M{i}</a>' for i in rows))
        if method == "GET" and path == f"{REPO}/projects":
            page = (kw.get("params") or {}).get("page", "1")
            rows = projects if page == "1" else ()
            return FakeResponse(status=200, text="".join(
                f'<a href="/o/r/projects/{i}" class="t">P{i}</a>' for i in rows))
        return FakeResponse(status=200, text="")
    return handler


def test_an_unknown_milestone_on_issue_create_is_named_not_a_500():
    """Forgejo answers an unusable milestone with a bare upstream 500."""
    c = make_client(_issue_create_handler(500, milestones=(1, 2)))

    with pytest.raises(ForgejoError) as exc:
        run(c.create_issue("o", "r", "T", milestone_id=999))

    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert exc.value.status == 404
    assert "was not created" in str(exc.value)


def test_an_unknown_project_on_issue_create_is_named_not_a_500():
    """Forgejo 16 answers 404 here, but 1.20 uses the same 500."""
    c = make_client(_issue_create_handler(500, projects=(1,)))

    with pytest.raises(ForgejoError) as exc:
        run(c.create_issue("o", "r", "T", project_id=999))

    assert exc.value.code == "PROJECT_NOT_FOUND"
    assert exc.value.status == 404


def test_a_500_with_valid_references_keeps_the_upstream_error_and_explains():
    """An unusable assignee cannot be looked up, so the note lists the fields."""
    c = make_client(_issue_create_handler(500, milestones=(5,)))

    with pytest.raises(ForgejoError) as exc:
        run(c.create_issue("o", "r", "T", milestone_id=5, assignee_ids=[999]))

    assert exc.value.status == 500
    assert "assignee_ids" in str(exc.value)
    assert "was not created" in str(exc.value)


def test_the_same_code_is_reported_whichever_status_forgejo_chose():
    """Forgejo 16 answers an unknown project 404, Forgejo 1.20 uses 500."""
    codes = set()
    for status in (404, 500):
        c = make_client(_issue_create_handler(status, projects=(1,)))
        with pytest.raises(ForgejoError) as exc:
            run(c.create_issue("o", "r", "T", project_id=999))
        codes.add(exc.value.code)

    assert codes == {"PROJECT_NOT_FOUND"}


def test_an_error_with_no_missing_reference_keeps_its_own_meaning():
    """A 404 from an unknown repository must not be relabelled."""
    c = make_client(_issue_create_handler(404, projects=(7,)))

    with pytest.raises(ForgejoError) as exc:
        run(c.create_issue("o", "r", "T", project_id=7))

    assert exc.value.status == 404
    assert exc.value.code == "HTTP_404"


def test_a_batch_that_sends_one_card_to_two_columns_is_refused():
    """Both moves used to run concurrently and the reply claimed both columns.

    The card ends up wherever the last write landed, so there is no coherent
    final state to report for such a batch.
    """
    c = make_client(lambda m, p, kw: FakeResponse(status=200, text=_issue_page(42)))

    with pytest.raises(ForgejoError) as exc:
        run(c.bulk_move_cards("o", "r", 1, [
            {"issue_number": 2, "column_id": 1},
            {"issue_number": 2, "column_id": 2},
        ]))

    assert exc.value.code == "INVALID_INPUT"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_moving_an_issue_that_is_not_on_the_board_says_so():
    """Forgejo answers a move for a detached issue with a bare HTTP 500.

    That reads as a server fault rather than the addressing mistake it is.
    """
    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=MOVE_BOARD)
        return FakeResponse(status=200, text=_issue_page(42))

    c = make_client(handler)

    with pytest.raises(ForgejoError) as exc:
        run(c.move_card("o", "r", 1, 9, [7, 99]))

    assert exc.value.code == "CARD_NOT_FOUND"
    assert exc.value.status == 404
    assert "99" in str(exc.value)
    assert find_call(c, "POST", f"{REPO}/projects/1/9/move") is None


def test_bulk_moving_an_issue_that_is_not_on_the_board_says_so():
    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=MOVE_BOARD)
        return FakeResponse(status=200, text=_issue_page(42))

    c = make_client(handler)

    with pytest.raises(ForgejoError) as exc:
        run(c.bulk_move_cards("o", "r", 1, [{"issue_number": 99, "column_id": 9}]))

    assert exc.value.code == "CARD_NOT_FOUND"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_move_card_refuses_a_repeated_issue_number():
    c = make_client(lambda m, p, kw: FakeResponse(status=200, text=_issue_page(42)))

    with pytest.raises(ForgejoError) as exc:
        run(c.move_card("o", "r", 1, 5, [7, 8, 7]))

    assert exc.value.code == "INVALID_INPUT"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


# A board carrying issues 7 and 8 as cards, which the move guard reads first.
MOVE_BOARD = """
<div class="project-column" data-id="9"><span class="project-column-title-label">Doing</span>
<div data-issue="42"><a href="/o/r/issues/7">Seven</a></div>
<div data-issue="43"><a href="/o/r/issues/8">Eight</a></div>
</div>
"""


def test_move_card_builds_json_payload():
    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=MOVE_BOARD)
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


_ATTACHED_BOARD = (
    '<div class="project-column" data-id="5">'
    '<span class="project-column-title-label">Uncategorized</span>'
    '<div class="issue-card" data-issue="42"><a href="/o/r/issues/7">C</a></div>'
    "</div>"
)


def test_add_issues_to_project_form():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        if path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=_ATTACHED_BOARD)
        return FakeResponse(status=200)

    c = make_client(handler)
    got = run(c.add_issues_to_project("o", "r", 1, [7]))
    call = find_call(c, "POST", f"{REPO}/issues/projects")
    assert call["form"] == {"id": "1", "issue_ids": "42"}
    # The board is read back, so the reply names where the card landed.
    assert got["cards"] == [
        {"number": 7, "column_id": 5, "column_title": "Uncategorized"}
    ]


def test_remove_issues_uses_project_zero():
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.remove_issues_from_project("o", "r", [7]))
    call = find_call(c, "POST", f"{REPO}/issues/projects")
    assert call["form"] == {"id": "0", "issue_ids": "42"}


def test_an_attachment_that_produced_no_card_is_not_reported_as_attached():
    """Forgejo answers this route the same way whether or not anything changed."""
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        if path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=_READ_BOARD)  # card 7 is absent
        return FakeResponse(status=200)

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.add_issues_to_project("o", "r", 1, [7]))
    assert exc.value.code == "ATTACH_UNVERIFIED"
    assert "7" in str(exc.value)


def test_a_guarded_detach_refuses_an_issue_that_is_on_another_board():
    """Naming the wrong board used to clear the assignment anyway."""
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        if path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=_READ_BOARD)  # card 7 is absent
        return FakeResponse(status=200)

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.remove_issues_from_project("o", "r", [7], project_id=1))
    assert exc.value.code == "CARD_NOT_FOUND"
    assert not [x for x in c._ctx.calls if x["method"] == "POST"]


def test_an_unguarded_detach_still_costs_no_board_read():
    """The guard is opt-in: without it the call is what it always was."""
    def handler(method, path, kw):
        if path == f"{REPO}/issues/7":
            return FakeResponse(status=200, text=_issue_page(42))
        return FakeResponse(status=200)

    c = make_client(handler)
    run(c.remove_issues_from_project("o", "r", [7]))
    assert find_call(c, "GET", f"{REPO}/projects/1") is None


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
# The milestone edit form, which the client reads before editing so that a
# field the caller left unset survives the write.
MILESTONE_FORM = """
<input name="title" placeholder="Title" value="Sprint" autofocus required maxlength="50">
<input type="date" id="deadline" name="deadline" value="2026-12-31">
<textarea class="markdown-text-editor" name="content" aria-label="Description">Ship it</textarea>
"""


def _milestone_handler(seen=None, form=MILESTONE_FORM, listed="Sprint"):
    """Answer the milestone routes the way Forgejo does: redirect on success."""
    def handler(method, path, kw):
        if seen is not None:
            seen.append((method, path))
        if method == "GET" and path == f"{REPO}/milestones/1/edit":
            return FakeResponse(status=200, text=form)
        if path == f"{REPO}/milestones":
            return FakeResponse(
                status=200, text=f'<a href="/o/r/milestone/1">{listed}</a>')
        if method == "POST" and path in (
            f"{REPO}/milestones/new", f"{REPO}/milestones/1/edit"
        ):
            return FakeResponse(status=303, headers={"location": f"{REPO}/milestones"})
        return FakeResponse(status=200)
    return handler


def test_milestone_paths():
    seen = []
    c = make_client(_milestone_handler(seen))
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
    c = make_client(_milestone_handler())
    run(c.delete_milestone("o", "r", 1))
    call = find_call(c, "POST", f"{REPO}/milestones/delete")
    assert call is not None
    assert call["form"] == {"id": "1"}
    assert find_call(c, "POST", f"{REPO}/milestones/1/delete") is None


def test_delete_milestone_that_does_not_exist_is_not_reported_as_deleted():
    """Forgejo answers this route with 200 whether or not anything was there."""
    c = make_client(_milestone_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.delete_milestone("o", "r", 999))
    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert find_call(c, "POST", f"{REPO}/milestones/delete") is None


def test_edit_milestone_keeps_the_fields_it_was_not_given():
    """A deadline-only edit used to blank the title, which Forgejo then refused."""
    c = make_client(_milestone_handler())
    run(c.edit_milestone("o", "r", 1, deadline="2030-01-15"))
    call = find_call(c, "POST", f"{REPO}/milestones/1/edit")
    assert call["form"] == {
        "title": "Sprint",            # carried over from the edit form
        "content": "Ship it",         # carried over, not cleared
        "deadline": "2030-01-15",     # the only field the caller named
    }


def test_edit_milestone_clears_a_field_when_asked_explicitly():
    """An empty string is a request to clear; None means leave alone."""
    c = make_client(_milestone_handler())
    run(c.edit_milestone("o", "r", 1, deadline="", description=""))
    call = find_call(c, "POST", f"{REPO}/milestones/1/edit")
    assert call["form"] == {"title": "Sprint", "content": "", "deadline": ""}


def test_edit_milestone_refuses_to_blank_the_title():
    c = make_client(_milestone_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.edit_milestone("o", "r", 1, title=""))
    assert exc.value.code == "INVALID_INPUT"
    assert find_call(c, "POST", f"{REPO}/milestones/1/edit") is None


def test_create_milestone_reports_a_refused_form():
    """An impossible deadline is refused with a re-rendered form, not a 4xx."""
    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/milestones/new":
            return FakeResponse(
                status=200,
                text='<div class="ui negative message flash-message flash-error">'
                     '\n\t\t<p>Due date format must be &#34;yyyy-mm-dd&#34;.</p>'
                     '\n\t</div>',
            )
        return FakeResponse(status=200, text="")

    c = make_client(handler)
    with pytest.raises(ForgejoError) as exc:
        run(c.create_milestone("o", "r", "Sprint", deadline="2026-02-30"))
    assert exc.value.code == "REJECTED"
    assert 'Due date format must be "yyyy-mm-dd".' in str(exc.value)


def test_create_milestone_rejects_a_blank_title():
    c = make_client(_milestone_handler())
    with pytest.raises(ForgejoError) as exc:
        run(c.create_milestone("o", "r", " "))
    assert exc.value.code == "INVALID_INPUT"


def _state_handler(prefix, open_id, closed_id):
    def handler(method, path, kw):
        state = (kw.get("params") or {}).get("state")
        if state == "open":
            return FakeResponse(status=200, text=f'<a href="/o/r/{prefix}/{open_id}" class="t">Open</a>')
        if state == "closed":
            return FakeResponse(status=200, text=f'<a href="/o/r/{prefix}/{closed_id}" class="t">Closed</a>')
        return FakeResponse(status=200, text="")
    return handler


# ------------------------------------------------------------------ paging
def _paged_handler(path, link, pages):
    """Serve a paginated list page: {page number: [(id, title), ...]}."""
    def handler(method, path_, kw):
        if method == "GET" and path_ == path:
            page = (kw.get("params") or {}).get("page", "1")
            rows = pages.get(page, [])
            html = "".join(
                f'<a href="/o/r/{link}/{i}" class="t">{t}</a>' for i, t in rows
            )
            return FakeResponse(status=200, text=html)
        return FakeResponse(status=200, text="")
    return handler


def _pages_requested(client, path):
    return [
        (call.get("params") or {}).get("page")
        for call in client._ctx.calls
        if call["method"] == "GET" and call["path"] == path
    ]


def test_list_projects_reads_every_page_not_just_the_first():
    """Forgejo shows 20 per page and no total, so page 1 is only a prefix."""
    c = make_client(_paged_handler(f"{REPO}/projects", "projects", {
        "1": [(i, f"P{i}") for i in range(1, 21)],
        "2": [(i, f"P{i}") for i in range(21, 26)],
        "3": [],
    }))

    got = run(c.list_projects("o", "r", "open"))

    assert [p["id"] for p in got] == list(range(1, 26))
    assert _pages_requested(c, f"{REPO}/projects") == ["1", "2", "3"]


def test_list_milestones_reads_every_page_not_just_the_first():
    c = make_client(_paged_handler(f"{REPO}/milestones", "milestone", {
        "1": [(i, f"M{i}") for i in range(1, 21)],
        "2": [(i, f"M{i}") for i in range(21, 23)],
        "3": [],
    }))

    got = run(c.list_milestones("o", "r", "open"))

    assert [m["id"] for m in got] == list(range(1, 23))


def test_listing_stops_when_a_page_stops_adding_entries():
    """A server that clamped an out-of-range page would otherwise loop forever."""
    same = [(i, f"P{i}") for i in range(1, 21)]
    c = make_client(_paged_handler(f"{REPO}/projects", "projects", {
        "1": same, "2": same, "3": same, "4": same,
    }))

    got = run(c.list_projects("o", "r", "open"))

    assert [p["id"] for p in got] == list(range(1, 21))
    # Page 2 repeated page 1, so paging stopped there rather than continuing.
    assert _pages_requested(c, f"{REPO}/projects") == ["1", "2"]


def test_a_short_first_page_still_ends_the_walk():
    c = make_client(_paged_handler(f"{REPO}/projects", "projects", {
        "1": [(1, "Only")], "2": [],
    }))

    assert [p["id"] for p in run(c.list_projects("o", "r", "open"))] == [1]


def test_creating_past_the_first_page_still_finds_the_new_resource():
    """The create verifies itself against this list, so a prefix broke it.

    A milestone created once 20 already existed landed on page 2, was not found
    by a single-page read, and a successful create was reported as
    CREATE_UNVERIFIED.
    """
    pages = {
        "1": [(i, f"M{i}") for i in range(1, 21)],
        "2": [(21, "Twenty-first")],
        "3": [],
    }

    def handler(method, path, kw):
        if method == "POST" and path == f"{REPO}/milestones/new":
            return FakeResponse(status=303, headers={"location": f"{REPO}/milestones"})
        if method == "GET" and path == f"{REPO}/milestones":
            rows = pages.get((kw.get("params") or {}).get("page", "1"), [])
            return FakeResponse(status=200, text="".join(
                f'<a href="/o/r/milestone/{i}" class="t">{t}</a>' for i, t in rows))
        return FakeResponse(status=200, text="")

    c = make_client(handler)
    out = run(c.create_milestone("o", "r", "Twenty-first"))

    assert out["milestone"] == {"id": 21, "title": "Twenty-first"}


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
    if path == f"{REPO}/projects":
        return FakeResponse(
            status=200,
            text='<a href="/o/r/projects/1">Board</a><a href="/o/r/projects/5">Other</a>',
        )
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
        if path == f"{REPO}/milestones":
            return FakeResponse(
                status=200, text='<a href="/o/r/milestone/7">Sprint 7</a>'
            )
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


@pytest.mark.parametrize(
    ("reader", "kwargs", "code"),
    [
        ("read_project_content", {"milestone": 999}, "MILESTONE_NOT_FOUND"),
        ("read_column_content", {"milestone": 999}, "MILESTONE_NOT_FOUND"),
        ("read_milestone_content", {"project": 999}, "PROJECT_NOT_FOUND"),
    ],
)
def test_an_unknown_filter_id_is_named_instead_of_returning_nothing(
    reader, kwargs, code
):
    """An unusable filter used to read as a board with no matching issues."""
    c = make_client(_read_handler)
    args = {
        "read_project_content": ("o", "r", 1),
        "read_column_content": ("o", "r", 1, 5),
        "read_milestone_content": ("o", "r", 7),
    }[reader]

    with pytest.raises(ForgejoError) as exc:
        run(getattr(c, reader)(*args, **kwargs))

    assert exc.value.code == code
    assert exc.value.status == 404
    assert "999" in str(exc.value)
    # The filter is confirmed before the issues list is queried with it.
    assert find_call(c, "GET", f"{REPO}/issues") is None


def test_a_board_read_does_not_re_confirm_its_own_project():
    """get_project already resolved it; checking again would cost a list walk."""
    c = make_client(_read_handler)
    run(c.read_project_content("o", "r", 1, milestone=7))
    assert find_call(c, "GET", f"{REPO}/projects") is None


def test_a_repeated_issue_number_is_read_once():
    """Duplicates were fetched once per occurrence and counted once per occurrence."""
    c = make_client(_read_handler)
    got = run(c.bulk_read_issues("o", "r", [42, 42, 43]))
    assert [g["number"] for g in got] == [42, 43]
    reads = [x for x in c._ctx.calls if x["path"] == f"{REPO}/issues/42"]
    assert len(reads) == 1


def test_bulk_move_cards_groups_by_column_and_builds_payloads():
    moves = [
        {"issue_number": 42, "column_id": 5},
        {"issue_number": 43, "column_id": 5},
        {"issue_number": 44, "column_id": 6},
    ]
    # The board has to carry all three cards: moving one that is not on it is
    # a addressing error the client now refuses, not a payload to build.
    board = _READ_BOARD + (
        '<div class="project-column" data-id="7">'
        '<span class="project-column-title-label">Later</span>'
        '<div class="issue-card" data-issue="1044">'
        '<a href="/o/r/issues/44">C3</a></div></div>'
    )

    def handler(method, path, kw):
        if method == "GET" and path == f"{REPO}/projects/1":
            return FakeResponse(status=200, text=board)
        return _read_handler(method, path, kw)

    c = make_client(handler)
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
