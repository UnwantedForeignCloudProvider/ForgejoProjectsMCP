"""The HTML parsers, run against pages a real Forgejo actually rendered.

``tests/test_parsing.py`` feeds the same functions hand-written fixtures. That
is the right way to pin down parsing logic, but it can only ever prove the
parser matches the fixture -- if Forgejo changes its markup, the fixture and the
parser stay in happy agreement while the product breaks.

These tests close that gap: they fetch the real page, run the real parser over
it, and assert the result describes what the test itself created. Every
assumption the offline fixtures encode (that a modal is present and must be
skipped, that milestone edit links must not be counted, that a body lives in a
raw element keyed by the issue's global id) is checked here against the markup
of every supported release.
"""

from __future__ import annotations

import pytest

from forgejo_projects_mcp.client import ForgejoClient

from .helpers import add_comment, create_issue, set_issue_state, unique


def fetch(client, run_async, name: str, **params) -> str:
    """The raw HTML of one page, through the client's own routing."""
    return run_async(client._get_text(client.profile.route(name, **params)))


# ----------------------------------------------------------------- board page
def test_the_board_page_parses_into_columns_and_cards(
    live_client, seeded_repo, live_project, run_async
):
    """A board built through the client reads back out of its own HTML."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    numbers = list(seeded_repo.issue_numbers[:2])
    todo = int(
        run_async(live_client.create_column(owner, repo, project_id, "To Do"))["column"]["id"]
    )
    done = int(
        run_async(live_client.create_column(owner, repo, project_id, "Done"))["column"]["id"]
    )
    run_async(live_client.add_issues_to_project(owner, repo, project_id, numbers))
    run_async(live_client.move_card(owner, repo, project_id, todo, numbers))

    html = fetch(live_client, run_async, "project", owner=owner, repo=repo,
                 project_id=project_id)
    board = ForgejoClient._parse_board(html, live_client.profile)

    columns = {c["title"]: c for c in board["columns"]}
    assert board["title"] == live_project["title"]
    assert {"To Do", "Done"} <= set(columns)
    assert columns["To Do"]["id"] == todo
    assert columns["Done"]["id"] == done
    assert columns["Done"]["cards"] == []
    assert [c["number"] for c in columns["To Do"]["cards"]] == numbers
    for card in columns["To Do"]["cards"]:
        assert isinstance(card["issue_id"], int) and card["issue_id"] > 0
        assert card["title"]

    run_async(live_client.remove_issues_from_project(owner, repo, numbers))


def test_the_new_column_modal_is_not_mistaken_for_a_column(
    live_client, seeded_repo, live_project, run_async
):
    """The real board page ships a hidden "new column" form, and it is skipped.

    The offline suite asserts this against invented markup. Here the modal is
    the one Forgejo actually renders, so the test also confirms the offline
    fixture is still describing something real.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    created = int(
        run_async(live_client.create_column(owner, repo, project_id, "Only column"))[
            "column"
        ]["id"]
    )

    html = fetch(live_client, run_async, "project", owner=owner, repo=repo,
                 project_id=project_id)
    board = ForgejoClient._parse_board(html, live_client.profile)

    # Below 1.21 the form is named after the old "board" vocabulary, the same
    # rename the parser's quirk accounts for.
    legacy = "legacy-board-vocabulary" in live_client.profile.quirks
    marker = "new-board-modal" if legacy else "new-project-column"
    assert marker in html, (
        f"expected the board page to carry a hidden new-column form ({marker})"
    )
    assert created in [c["id"] for c in board["columns"]]
    # Forgejo 1.x renders the default column as an "Uncategorized" pseudo-column
    # with id 0, so ids are non-negative rather than positive.
    assert all(c["id"] >= 0 for c in board["columns"])
    # The real point: the hidden form did not become an extra (duplicate) column.
    assert len(board["columns"]) == len({c["id"] for c in board["columns"]})


# -------------------------------------------------------------- project lists
def test_the_projects_page_parses_into_a_list(
    live_client, seeded_repo, live_project, run_async
):
    """Every project link on the real page is read once, with its title."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    html = fetch(live_client, run_async, "projects", owner=owner, repo=repo)
    projects = ForgejoClient._parse_projects_list(html, live_client.profile)

    by_id = {p["id"]: p["title"] for p in projects}
    assert by_id.get(live_project["id"]) == live_project["title"]
    assert len(projects) == len(by_id), "each project should be listed once"


# ------------------------------------------------------------- milestone list
def test_the_milestones_page_parses_into_a_list(
    live_client, seeded_repo, run_async
):
    """Milestone links parse to (id, title), and edit links are not counted."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    html = fetch(live_client, run_async, "milestones", owner=owner, repo=repo)
    milestones = ForgejoClient._parse_milestones(html, live_client.profile)

    by_id = {m["id"]: m["title"] for m in milestones}
    assert by_id.get(seeded_repo.milestone_id) == seeded_repo.milestone_title
    assert len(milestones) == len(by_id), "each milestone should be listed once"


# ------------------------------------------------------------------ issue page
def test_the_issue_page_parses_into_full_content(
    live_client, seeded_repo, forgejo_target, run_async, writable
):
    """Title, number, state, body, milestone and comments, from real markup."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    number = seeded_repo.issue_numbers[1]
    add_comment(forgejo_target, owner, repo, number, "First **comment**.")
    add_comment(forgejo_target, owner, repo, number, "Second comment.")

    html = fetch(live_client, run_async, "issue", owner=owner, repo=repo, number=number)
    issue = ForgejoClient._parse_issue(html, live_client.profile)

    assert issue["number"] == number
    assert issue["title"] == "Seeded issue 2"
    assert issue["state"] == "open"
    assert issue["body"] == "Body of seeded issue 2."
    assert issue["milestone"] == {
        "id": seeded_repo.milestone_id,
        "title": seeded_repo.milestone_title,
    }
    assert [c["body"] for c in issue["comments"]] == [
        "First **comment**.",
        "Second comment.",
    ]
    assert {c["author"] for c in issue["comments"]} == {forgejo_target.username}


def test_a_closed_issue_parses_as_closed(
    live_client, seeded_repo, forgejo_target, run_async, writable
):
    """The state label is read from the icon Forgejo really renders."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    number = create_issue(forgejo_target, owner, repo, unique("To close"), "closing this")
    set_issue_state(forgejo_target, owner, repo, number, "closed")

    html = fetch(live_client, run_async, "issue", owner=owner, repo=repo, number=number)
    issue = ForgejoClient._parse_issue(html, live_client.profile)

    assert issue["state"] == "closed"
    assert issue["milestone"] is None
    assert issue["comments"] == []


def test_an_issue_body_is_found_when_its_id_differs_from_its_number(
    live_client, offset_repo, forgejo_target, run_async, writable
):
    """The body element is keyed by the issue's global id, not by its number.

    The two coincide only in the first repository an instance ever creates, so
    a suite that seeds one repository cannot tell the difference. This uses a
    second repository, where they are guaranteed to diverge, which is the
    situation every real instance is in.
    """
    owner, repo = offset_repo.owner, offset_repo.name
    number = offset_repo.issue_numbers[0]

    issue_id = run_async(live_client.resolve_issue_id(owner, repo, number))
    issue = run_async(live_client.read_issue(owner, repo, number))

    assert issue_id != number, "expected the global id and the number to diverge"
    assert issue["number"] == number
    assert issue["body"] == "Body of seeded issue 1."


# --------------------------------------------------------------- error bodies
def test_a_real_json_error_is_summarized(live_client, run_async):
    """The JSON branch of the error reader, on a response Forgejo really sent."""
    run_async(live_client.ensure())
    response = run_async(live_client._ctx.get("/api/v1/repos/nobody/nothing"))
    detail = run_async(ForgejoClient._error_detail(response))

    assert response.status == 404
    assert detail.startswith(": ")
    assert "<" not in detail


def test_a_real_html_error_page_is_not_dumped(live_client, seeded_repo, run_async):
    """An HTML error page is reduced to a sentence, never echoed wholesale."""
    run_async(live_client.ensure())
    response = run_async(
        live_client._ctx.get(f"/{seeded_repo.owner}/{seeded_repo.name}/projects/999999")
    )
    detail = run_async(ForgejoClient._error_detail(response))

    assert response.status == 404
    assert len(detail) < 220
    assert "<" not in detail and "</" not in detail


@pytest.mark.parametrize("state", ["open", "closed"])
def test_the_issues_page_parses_issue_numbers(
    live_client, seeded_repo, run_async, state
):
    """The filtered issue list yields exactly the numbers Forgejo shows."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    numbers = run_async(
        live_client._filtered_issue_numbers(owner, repo, state=state)
    )

    assert numbers == sorted(set(numbers))
    assert all(isinstance(n, int) and n > 0 for n in numbers)
    if state == "open":
        assert set(seeded_repo.issue_numbers) <= set(numbers)
