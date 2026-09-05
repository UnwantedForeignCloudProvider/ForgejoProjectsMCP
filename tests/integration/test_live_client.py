"""Client operations, filters and error handling against a live instance.

The live counterpart of the operation half of ``tests/test_client.py``. Where
the offline tests assert the exact request the client builds, these assert that
Forgejo answers it the way the client expects: that the id it recovers is real,
that a filter narrows what actually comes back, and that a genuine failure
carries the status and code the tool layer relies on.

``tests/integration/test_live_projects.py`` covers the board lifecycles; this
file covers everything around them.
"""

from __future__ import annotations

import time

import pytest

from forgejo_projects_mcp.client import ForgejoError
from forgejo_projects_mcp.compat import Version

from .helpers import (
    add_comment,
    create_issue,
    set_issue_state,
    unique,
    watch_requests,
)


# --------------------------------------------------------------- repositories
def test_list_repositories_returns_the_full_repository_shape(
    live_client, seeded_repo, run_async
):
    """Every field the tool layer exposes is present and typed as promised."""
    repos = run_async(live_client.list_repositories(query=seeded_repo.name))

    assert len(repos) == 1
    assert repos[0] == {
        "full_name": seeded_repo.full_name,
        "owner": seeded_repo.owner,
        "name": seeded_repo.name,
        "description": repos[0]["description"],
        "private": False,
        "archived": False,
        "empty": False,
        "fork": False,
    }
    assert isinstance(repos[0]["description"], str)


def test_list_repositories_paginates(live_client, seeded_repo, offset_repo, run_async):
    """limit and page reach the search route rather than being silently dropped."""
    first = run_async(live_client.list_repositories(limit=1, page=1))
    second = run_async(live_client.list_repositories(limit=1, page=2))

    assert len(first) == len(second) == 1
    assert first[0]["full_name"] != second[0]["full_name"], (
        "page 2 should not repeat page 1"
    )
    assert {seeded_repo.full_name, offset_repo.full_name} <= {
        r["full_name"] for r in run_async(live_client.list_repositories(limit=50))
    }


# -------------------------------------------------------------------- projects
@pytest.mark.parametrize("card_type", ["text", "images_and_text"])
def test_projects_can_be_created_with_each_card_type(
    live_client, seeded_repo, run_async, writable, card_type
):
    """Both card types are accepted by the real form, and the id is recovered."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique("Cards")

    created = run_async(
        live_client.create_project(owner, repo, title, "desc", card_type=card_type)
    )

    project = created["project"]
    assert created["created"] is True
    assert project["title"] == title
    assert isinstance(project["id"], int) and project["id"] > 0
    run_async(live_client.delete_project(owner, repo, project["id"]))


def test_list_projects_all_merges_open_and_closed(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo has no "all" view, so the client merges the two real states."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    open_title, closed_title = unique("Open"), unique("Closed")
    open_id = int(
        run_async(live_client.create_project(owner, repo, open_title))["project"]["id"]
    )
    closed_id = int(
        run_async(live_client.create_project(owner, repo, closed_title))["project"]["id"]
    )
    run_async(live_client.close_project(owner, repo, closed_id))

    try:
        listed_open = [p["id"] for p in run_async(live_client.list_projects(owner, repo, "open"))]
        listed_closed = [
            p["id"] for p in run_async(live_client.list_projects(owner, repo, "closed"))
        ]
        listed_all = [p["id"] for p in run_async(live_client.list_projects(owner, repo, "all"))]

        assert open_id in listed_open and closed_id not in listed_open
        assert closed_id in listed_closed and open_id not in listed_closed
        assert {open_id, closed_id} <= set(listed_all)
        assert listed_all == sorted(listed_all)  # merged in id order
    finally:
        run_async(live_client.reopen_project(owner, repo, closed_id))
        run_async(live_client.delete_project(owner, repo, closed_id))
        run_async(live_client.delete_project(owner, repo, open_id))


def test_a_missing_project_is_an_http_error_with_a_code(
    live_client, seeded_repo, run_async
):
    """A real failure carries the status and code the tool layer maps from."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.get_project(owner, repo, 999_999))

    assert exc.value.status == 404
    assert exc.value.code == "HTTP_404"


# --------------------------------------------------------------------- columns
def test_the_default_column_cannot_be_deleted(
    live_client, seeded_repo, live_project, run_async
):
    """Forgejo refuses, and the client turns the bare failure into a hint.

    Forgejo reports the refusal as a plain server error with no explanation of
    what went wrong, so the client supplies the reason itself. That hint is
    only correct if Forgejo really does refuse, which is what this checks --
    and it does not refuse on the 1.x line, where the delete simply succeeds.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    column_id = int(
        run_async(live_client.create_column(owner, repo, project_id, "Default"))[
            "column"
        ]["id"]
    )
    run_async(live_client.set_default_column(owner, repo, project_id, column_id))
    def delete():
        return live_client.delete_column(owner, repo, project_id, column_id)

    if live_client.version < Version(7, 0, 0):
        # Forgejo 1.20/1.21 allow it and fall back to the uncategorized column.
        assert run_async(delete())["deleted"] is True
        return

    with pytest.raises(ForgejoError) as exc:
        run_async(delete())

    assert "default column cannot be deleted" in str(exc.value)


# ------------------------------------------------------------------ milestones
def test_list_milestones_all_merges_open_and_closed(
    live_client, seeded_repo, run_async, writable
):
    """The same merge as for projects, over the milestones page."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    closed_id = int(
        run_async(live_client.create_milestone(owner, repo, unique("Closed")))[
            "milestone"
        ]["id"]
    )
    run_async(live_client.close_milestone(owner, repo, closed_id))

    try:
        listed_open = [m["id"] for m in run_async(live_client.list_milestones(owner, repo, "open"))]
        listed_closed = [
            m["id"] for m in run_async(live_client.list_milestones(owner, repo, "closed"))
        ]
        listed_all = [m["id"] for m in run_async(live_client.list_milestones(owner, repo, "all"))]

        assert seeded_repo.milestone_id in listed_open
        assert closed_id in listed_closed and closed_id not in listed_open
        assert {seeded_repo.milestone_id, closed_id} <= set(listed_all)
    finally:
        run_async(live_client.delete_milestone(owner, repo, closed_id))


def test_a_missing_milestone_is_reported_as_not_found(
    live_client, seeded_repo, run_async
):
    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.read_milestone_content(seeded_repo.owner, seeded_repo.name, 999_999))

    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert exc.value.status == 404


# ----------------------------------------------------------------------- cards
def test_resolve_issue_id_maps_numbers_to_global_ids(
    live_client, seeded_repo, run_async
):
    """Card operations address issues by global id, not by repo-local number."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    first, second = seeded_repo.issue_numbers[0], seeded_repo.issue_numbers[1]

    first_id = run_async(live_client.resolve_issue_id(owner, repo, first))
    second_id = run_async(live_client.resolve_issue_id(owner, repo, second))

    assert first_id > 0 and second_id > 0
    assert first_id != second_id
    assert second_id > first_id  # ids are allocated in creation order


def test_resolving_a_missing_issue_is_not_found(live_client, seeded_repo, run_async):
    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.resolve_issue_id(seeded_repo.owner, seeded_repo.name, 99_999))

    assert exc.value.code in ("ISSUE_NOT_FOUND", "HTTP_404")


# --------------------------------------------------------------------- reading
def test_read_issue_returns_body_milestone_and_comments(
    live_client, seeded_repo, forgejo_target, run_async, writable
):
    """One issue, read whole: the shape read_card returns to a caller."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    number = seeded_repo.issue_numbers[0]  # attached to the seeded milestone
    add_comment(forgejo_target, owner, repo, number, "A **live** comment.")

    issue = run_async(live_client.read_issue(owner, repo, number))

    assert issue["number"] == number
    assert issue["title"] == "Seeded issue 1"
    assert issue["state"] == "open"
    assert issue["body"] == "Body of seeded issue 1."
    assert issue["milestone"] == {
        "id": seeded_repo.milestone_id,
        "title": seeded_repo.milestone_title,
    }
    assert {"author": forgejo_target.username, "body": "A **live** comment."} in (
        issue["comments"]
    )


def test_bulk_read_preserves_order_and_inlines_failures(
    live_client, seeded_repo, run_async
):
    """A missing issue is reported in place, and the order asked for is kept."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    first, second = seeded_repo.issue_numbers[0], seeded_repo.issue_numbers[1]
    missing = 99_999

    results = run_async(live_client.bulk_read_issues(owner, repo, [first, missing, second]))

    assert [r["number"] for r in results] == [first, missing, second]
    assert "error" not in results[0]
    assert "error" in results[1]
    assert "error" not in results[2]


def test_bulk_read_filters_by_state(
    live_client, seeded_repo, forgejo_target, run_async, writable
):
    """Post-filtering by state reflects the real state of real issues."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    open_number = create_issue(forgejo_target, owner, repo, unique("Open issue"))
    closed_number = create_issue(forgejo_target, owner, repo, unique("Closed issue"))
    set_issue_state(forgejo_target, owner, repo, closed_number, "closed")
    both = [open_number, closed_number]

    def numbers(state):
        read = run_async(live_client.bulk_read_issues(owner, repo, both, state))
        return [issue["number"] for issue in read]

    assert numbers("open") == [open_number]
    assert numbers("closed") == [closed_number]
    assert set(numbers("all")) == set(both)


def test_an_invalid_state_is_rejected_before_any_request(
    live_client, seeded_repo, run_async
):
    """Bad input fails fast, without troubling the instance."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    for call in (
        lambda: live_client.list_projects(owner, repo, "bogus"),
        lambda: live_client.read_project_content(owner, repo, 1, state="nope"),
        lambda: live_client.bulk_read_issues(owner, repo, [1], "weird"),
    ):
        with pytest.raises(ForgejoError) as exc:
            run_async(call())
        assert exc.value.code == "INVALID_STATE"
        assert exc.value.status == 400

    assert log.calls == []


def test_reading_a_missing_column_is_not_found(
    live_client, seeded_repo, live_project, run_async
):
    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.read_column_content(
                seeded_repo.owner, seeded_repo.name, live_project["id"], 999_999
            )
        )

    assert exc.value.code == "COLUMN_NOT_FOUND"


# ------------------------------------------------------------ board filtering
@pytest.fixture
def board_with_cards(live_client, seeded_repo, live_project, run_async):
    """A board holding two milestone issues and one issue without a milestone."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    on_milestone = list(seeded_repo.issue_numbers[:2])
    off_milestone = seeded_repo.issue_numbers[2]
    numbers = [*on_milestone, off_milestone]

    column_id = int(
        run_async(live_client.create_column(owner, repo, project_id, "Cards"))["column"]["id"]
    )
    run_async(live_client.add_issues_to_project(owner, repo, project_id, numbers))
    run_async(live_client.move_card(owner, repo, project_id, column_id, numbers))
    try:
        yield {
            "project_id": project_id,
            "column_id": column_id,
            "on_milestone": on_milestone,
            "off_milestone": off_milestone,
            "numbers": numbers,
        }
    finally:
        run_async(live_client.remove_issues_from_project(owner, repo, numbers))


def test_reading_a_board_without_filters_skips_the_issue_query(
    live_client, seeded_repo, board_with_cards, run_async
):
    """With nothing to filter on, the extra issue-list request is not made."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    content = run_async(
        live_client.read_project_content(owner, repo, board_with_cards["project_id"])
    )

    assert content["total"] == len(board_with_cards["numbers"])
    assert log.find("GET", f"/{owner}/{repo}/issues") is None


def test_reading_a_board_filtered_by_milestone(
    live_client, seeded_repo, board_with_cards, run_async
):
    """A milestone filter is applied server-side and narrows the cards returned."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    content = run_async(
        live_client.read_project_content(
            owner, repo, board_with_cards["project_id"],
            milestone=seeded_repo.milestone_id,
        )
    )

    query = log.require("GET", f"/{owner}/{repo}/issues")
    assert query["params"]["project"] == str(board_with_cards["project_id"])
    assert query["params"]["milestone"] == str(seeded_repo.milestone_id)
    returned = {c["number"] for col in content["columns"] for c in col["cards"]}
    assert returned == set(board_with_cards["on_milestone"])
    assert board_with_cards["off_milestone"] not in returned


def test_reading_a_board_paginates(live_client, seeded_repo, board_with_cards, run_async):
    """limit/offset walk the cards without changing the reported total."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = board_with_cards["project_id"]
    total = len(board_with_cards["numbers"])

    first = run_async(live_client.read_project_content(owner, repo, project_id, limit=1))
    rest = run_async(
        live_client.read_project_content(owner, repo, project_id, limit=None, offset=1)
    )

    assert first["total"] == total
    assert first["returned"] == 1
    assert first["truncated"] is True
    assert rest["returned"] == total - 1
    assert rest["truncated"] is False


def test_reading_a_column_paginates(live_client, seeded_repo, board_with_cards, run_async):
    owner, repo = seeded_repo.owner, seeded_repo.name

    page = run_async(
        live_client.read_column_content(
            owner, repo, board_with_cards["project_id"], board_with_cards["column_id"],
            limit=1, offset=0,
        )
    )

    assert page["column"]["title"] == "Cards"
    assert page["total"] == len(board_with_cards["numbers"])
    assert page["returned"] == 1
    assert page["truncated"] is True


def test_reading_a_milestone_passes_project_and_state(
    live_client, seeded_repo, board_with_cards, run_async
):
    """The milestone reader forwards every filter to the issue list."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    content = run_async(
        live_client.read_milestone_content(
            owner, repo, seeded_repo.milestone_id,
            state="open", project=board_with_cards["project_id"],
        )
    )

    query = log.require("GET", f"/{owner}/{repo}/issues")
    assert query["params"]["milestone"] == str(seeded_repo.milestone_id)
    assert query["params"]["project"] == str(board_with_cards["project_id"])
    assert query["params"]["state"] == "open"
    assert {i["number"] for i in content["issues"]} == set(board_with_cards["on_milestone"])
    assert content["error_count"] == 0


# -------------------------------------------------------------------- throttle
def test_requests_are_spaced_by_the_throttle(live_client, seeded_repo, run_async):
    """The politeness limiter really does pace requests at a live instance."""
    from forgejo_projects_mcp.client import _REQUESTS_PER_SECOND

    owner, repo = seeded_repo.owner, seeded_repo.name
    run_async(live_client.ensure())  # keep login out of the measurement
    requests = 4

    started = time.monotonic()
    for _ in range(requests):
        run_async(live_client.list_projects(owner, repo, "open"))
    elapsed = time.monotonic() - started

    assert elapsed >= (requests - 1) / _REQUESTS_PER_SECOND
