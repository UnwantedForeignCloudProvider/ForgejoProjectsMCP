"""How the client detects an instance's version and adapts its behavior to it.

The session probe is the pivot: one request proves the session, reveals the
version, and (on versions that need one) yields the CSRF token. Everything the
client does afterwards is driven by the profile that version resolves to.
"""

import asyncio

import pytest
from conftest import FakeResponse, make_client

from forgejo_projects_mcp.client import ForgejoClient, ForgejoError
from forgejo_projects_mcp.compat import CSRF_ORIGIN, CSRF_TOKEN, Version, profile_for


def run(coro):
    return asyncio.run(coro)


def page(version: str, *, csrf: str | None = None) -> str:
    """The part of a rendered Forgejo page the client reads itself out of."""
    token = f"csrfToken: '{csrf}'," if csrf else ""
    return (
        "<script>window.config = {"
        f"assetVersionEncoded: encodeURIComponent('{version}'), {token}"
        "};</script>"
    )


def probing_client(version: str, *, csrf: str | None = None, writes=None):
    """A client whose probe page advertises ``version`` (and maybe a token)."""
    responses = writes if writes is not None else {}

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=200, text=page(version, csrf=csrf))
        if path in responses:
            return responses[path].pop(0)
        return FakeResponse(status=303, headers={"location": "/"})

    return make_client(handler, authed=False)


# ----------------------------------------------------------- detection
def test_the_session_probe_also_reports_the_version():
    client = probing_client("16.0.3~gitea-1.22.0")

    result = run(client.status())

    assert result["authenticated"] is True
    assert result["version"] == "16.0.3~gitea-1.22.0"
    assert result["compatibility"]["version_short"] == "16.0.3"
    assert client.version == Version(16, 0, 3)


def test_version_detection_costs_no_extra_request():
    """Only the probe itself is issued -- no separate version lookup."""
    client = probing_client("16.0.3~gitea-1.22.0")

    run(client.status())

    paths = [c["path"] for ctx in client._pw.contexts + [client._ctx] for c in ctx.calls]
    assert paths == ["/user/settings"]


def test_an_unreadable_version_leaves_the_newest_behavior_in_place():
    """A probe body with no version marker must not break anything."""

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=200, text="<html>no markers</html>")
        return FakeResponse(status=200)

    client = make_client(handler, authed=False)

    result = run(client.status())

    assert result["authenticated"] is True
    assert result["version"] is None
    assert client.profile.csrf_mode == CSRF_ORIGIN
    assert result["compatibility"]["verified"] is False


def test_a_failed_probe_reports_no_version():
    def handler(method, path, kw):
        return FakeResponse(status=302)

    client = make_client(handler, authed=False)
    client.username = ""
    client.password = ""

    result = run(client.status())

    assert result["authenticated"] is False
    assert result["version"] is None


def test_login_reports_the_version_it_just_observed():
    state = {"logged_in": False}

    def handler(method, path, kw):
        if path == "/user/login" and method == "POST":
            state["logged_in"] = True
            return FakeResponse(status=303, headers={"location": "/"}, url="/")
        if path == "/user/settings":
            if not state["logged_in"]:
                return FakeResponse(status=302)
            return FakeResponse(status=200, text=page("14.0.5~gitea-1.22.0"))
        return FakeResponse(status=200)

    client = make_client(handler, authed=False)

    result = run(client.login())

    assert result["version"] == "14.0.5~gitea-1.22.0"
    assert result["compatibility"]["csrf_mode"] == CSRF_ORIGIN


# ---------------------------------------------------------------- csrf
def find_write(client, path):
    for ctx in client._pw.contexts + [client._ctx]:
        for call in ctx.calls:
            if call["path"] == path and call["method"] != "GET":
                return call
    raise AssertionError(f"no write recorded for {path}")


def test_old_versions_send_the_csrf_token_they_were_given():
    client = probing_client("13.0.5~gitea-1.22.0", csrf="tok-13")

    run(client.create_milestone("o", "r", "M"))

    assert client.profile.csrf_mode == CSRF_TOKEN
    assert find_write(client, "/o/r/milestones/new")["headers"]["X-Csrf-Token"] == (
        "tok-13"
    )


def test_new_versions_send_no_csrf_token():
    """From 14.0 a matching Origin header is enough, so no token is needed."""
    client = probing_client("16.0.3~gitea-1.22.0")

    run(client.create_milestone("o", "r", "M"))

    assert client.profile.csrf_mode == CSRF_ORIGIN
    assert "headers" not in find_write(client, "/o/r/milestones/new")


def test_reads_never_carry_a_csrf_token():
    client = probing_client("13.0.5~gitea-1.22.0", csrf="tok-13")

    run(client.list_projects("o", "r", "open"))

    reads = [
        call
        for ctx in client._pw.contexts + [client._ctx]
        for call in ctx.calls
        if call["path"] == "/o/r/projects"
    ]
    assert reads and all("headers" not in call for call in reads)


def test_a_csrf_rejection_is_recovered_and_remembered():
    """An instance that demands a token despite its version is adapted to.

    This is the safety net for a build whose behavior does not match what its
    version implies: the rejected write is retried with a token, and the rest
    of the session keeps sending one.
    """
    attempts = {"count": 0}

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(
                status=200, text=page("16.0.3~gitea-1.22.0", csrf="tok-16")
            )
        if method == "POST" and path == "/o/r/milestones/new":
            attempts["count"] += 1
            if attempts["count"] == 1:
                return FakeResponse(status=400, text="Invalid CSRF token.")
            return FakeResponse(status=303, headers={"location": "/"})
        return FakeResponse(status=200, text="")

    client = make_client(handler, authed=False)
    assert client.profile.csrf_mode == CSRF_ORIGIN

    run(client.create_milestone("o", "r", "M"))

    assert attempts["count"] == 2
    assert client.profile.csrf_mode == CSRF_TOKEN
    retried = [
        call
        for ctx in client._pw.contexts + [client._ctx]
        for call in ctx.calls
        if call["path"] == "/o/r/milestones/new"
    ]
    assert retried[-1]["headers"]["X-Csrf-Token"] == "tok-16"


def test_a_csrf_rejection_is_retried_only_once():
    """A persistent rejection surfaces as an error instead of looping."""
    attempts = {"count": 0}

    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(
                status=200, text=page("16.0.3~gitea-1.22.0", csrf="tok-16")
            )
        if method == "POST":
            attempts["count"] += 1
            return FakeResponse(status=400, text="Invalid CSRF token.")
        return FakeResponse(status=200, text="")

    client = make_client(handler, authed=False)

    with pytest.raises(ForgejoError) as excinfo:
        run(client.create_milestone("o", "r", "M"))

    assert excinfo.value.status == 400
    assert attempts["count"] == 2


# ------------------------------------------------------------- parsing
BOARD = """
<title>testadmin/kanban - Forgejo: Beyond coding. We forge.</title>
<h2 class="tw-mb-0 tw-flex-1 tw-break-anywhere">Roadmap</h2>
<div class="project-column" data-id="5">
  <span class="project-column-title-label">To Do</span>
</div>
"""


def test_the_board_title_comes_from_the_project_heading():
    """The heading is the title on every version; <title> is not."""
    board = ForgejoClient._parse_board(BOARD, profile_for(Version(16, 0, 3)))

    assert board["title"] == "Roadmap"


def test_old_versions_never_fall_back_to_the_page_title():
    """Below 10.0 the page <title> is 'owner/repo' and must not be trusted."""
    without_heading = BOARD.replace(
        '<h2 class="tw-mb-0 tw-flex-1 tw-break-anywhere">Roadmap</h2>', ""
    )

    old = ForgejoClient._parse_board(without_heading, profile_for(Version(9, 0, 3)))
    new = ForgejoClient._parse_board(without_heading, profile_for(Version(16, 0, 3)))

    assert old["title"] == ""
    assert new["title"] == "testadmin/kanban"


def test_parsers_default_to_the_newest_profile():
    """The parse helpers stay usable without a profile argument."""
    assert ForgejoClient._parse_board(BOARD)["title"] == "Roadmap"


LEGACY_BOARD = """
<h2 class="project-title">Roadmap</h2>
<div class="ui segment board-column" data-id="4" data-sorting="0">
  <div class="ui large label board-label gt-py-2">
    <div class="ui small circular grey label board-card-cnt">1</div>
    Backlog
  </div>
  <div class="card board-card" data-issue="42">
    <a class="project-board-title" href="/o/r/issues/7">Card A</a>
  </div>
</div>
"""


def test_forgejo_1_20_board_markup_is_parsed_by_its_quirk():
    """Before 1.21 columns were 'boards' in the markup, with no title label."""
    board = ForgejoClient._parse_board(LEGACY_BOARD, profile_for(Version(1, 20, 6)))

    assert board["title"] == "Roadmap"
    assert [(c["id"], c["title"]) for c in board["columns"]] == [(4, "Backlog")]
    assert board["columns"][0]["cards"] == [
        {"issue_id": 42, "number": 7, "title": "Card A"}
    ]


def test_the_newest_profile_does_not_understand_1_20_markup():
    """The legacy patterns are scoped to the versions that need them."""
    board = ForgejoClient._parse_board(LEGACY_BOARD, profile_for(Version(16, 0, 3)))

    assert board["columns"] == []


# --------------------------------------------------- new-issue response shapes
def issue_creating_client(response: FakeResponse):
    def handler(method, path, kw):
        if path == "/user/settings":
            return FakeResponse(status=200, text=page("16.0.3~gitea-1.22.0"))
        if path == "/o/r/issues/new":
            return response
        return FakeResponse(status=200, text="")

    return make_client(handler, authed=False)


def test_the_new_issue_number_is_read_from_a_json_redirect():
    client = issue_creating_client(
        FakeResponse(status=200, json_data={"redirect": "/o/r/issues/12"})
    )

    assert run(client.create_issue("o", "r", "T"))["number"] == 12


def test_the_new_issue_number_is_read_from_a_location_header():
    """Forgejo below 1.21 answers 303 instead of returning JSON."""
    client = issue_creating_client(
        FakeResponse(status=303, headers={"location": "/o/r/issues/9"})
    )

    assert run(client.create_issue("o", "r", "T"))["number"] == 9


def test_a_new_issue_response_carrying_neither_reports_no_number():
    client = issue_creating_client(FakeResponse(status=303, headers={}))

    result = run(client.create_issue("o", "r", "T"))

    assert result["created"] is True
    assert result["number"] is None
