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
from html import unescape

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

    # Forgejo stores whatever the dropdown posts and normalises anything it
    # does not recognise to 0 ("Text only") without complaining, so the only
    # way to know the requested type was stored is to read it back off the
    # edit form. This is what caught the two names being mapped to each
    # other's values.
    html = run_async(live_client._get_text(
        live_client._route(
            "project_edit", owner=owner, repo=repo, project_id=project["id"])
    ))
    stored = live_client._profile.search("project_edit_card_type", html)
    assert stored is not None, "the edit form no longer exposes card_type"
    assert stored.group(1) == live_client._profile.card_types[card_type]

    run_async(live_client.delete_project(owner, repo, project["id"]))


def test_a_blank_project_title_is_refused_before_forgejo_sees_it(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo accepts a blank title and then omits the project it created.

    The board is reachable by id but absent from every list page, so an agent
    that made one cannot find it again. The client refuses the write instead.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    before = run_async(live_client.list_projects(owner, repo, "all"))

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.create_project(owner, repo, "   "))

    assert exc.value.code == "INVALID_INPUT"
    assert run_async(live_client.list_projects(owner, repo, "all")) == before


def test_an_unknown_card_type_is_refused_before_forgejo_sees_it(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo silently stores an unrecognised card type as "Text only"."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    before = run_async(live_client.list_projects(owner, repo, "all"))

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.create_project(owner, repo, unique("CT"), card_type="bogus")
        )

    assert exc.value.code == "INVALID_INPUT"
    assert run_async(live_client.list_projects(owner, repo, "all")) == before


def test_update_project_keeps_the_description_it_was_not_given(
    live_client, seeded_repo, run_async, writable
):
    """A partial edit must not clear the fields the caller did not name."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique("Keep")
    created = run_async(
        live_client.create_project(owner, repo, title, "keep this description")
    )
    project_id = int(created["project"]["id"])
    try:
        run_async(
            live_client.update_project(owner, repo, project_id, title=title + " v2")
        )

        html = run_async(live_client._get_text(
            live_client._route(
                "project_edit", owner=owner, repo=repo, project_id=project_id)
        ))
        description = live_client._profile.search("project_edit_description", html)
        assert description is not None
        assert description.group(1) == "keep this description"
    finally:
        run_async(live_client.delete_project(owner, repo, project_id))


def test_a_partial_update_does_not_mangle_escaped_characters(
    live_client, seeded_repo, run_async, writable
):
    """Carried-over values come out of HTML, so they have to be decoded first.

    Re-posting them as they were parsed would turn `&` into `&amp;` a little
    more on every partial edit.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique('A & B "quoted" <tag>')
    created = run_async(
        live_client.create_project(owner, repo, title, "keep & carry <on>")
    )
    project_id = int(created["project"]["id"])
    try:
        run_async(
            live_client.update_project(
                owner, repo, project_id, card_type="images_and_text")
        )

        listed = [
            p for p in run_async(live_client.list_projects(owner, repo, "open"))
            if p["id"] == project_id
        ]
        assert listed and listed[0]["title"] == title

        html = run_async(live_client._get_text(
            live_client._route(
                "project_edit", owner=owner, repo=repo, project_id=project_id)
        ))
        description = live_client._profile.search("project_edit_description", html)
        assert unescape(description.group(1)) == "keep & carry <on>"
    finally:
        run_async(live_client.delete_project(owner, repo, project_id))


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


# Forgejo paginates these list pages at 20 entries, so one past that proves the
# client reads beyond the first page. Kept minimal: every entry is a real write.
_PAST_FIRST_PAGE = 21


def test_listing_projects_returns_the_ones_past_the_first_page(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo shows 20 per page and publishes no total.

    Page 1 holds the *newest* projects, so it is the first one created that
    falls off the end -- and a single-page read reported it as simply absent.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    before = {p["id"] for p in run_async(live_client.list_projects(owner, repo, "all"))}
    created = []
    try:
        for index in range(_PAST_FIRST_PAGE):
            made = run_async(
                live_client.create_project(owner, repo, unique(f"Page{index:02d}"))
            )
            created.append(int(made["project"]["id"]))

        listed = {p["id"] for p in run_async(live_client.list_projects(owner, repo, "open"))}

        assert set(created) <= listed, (
            f"{len(set(created) - listed)} of {len(created)} projects were not listed"
        )
        assert len(listed) >= len(before | set(created))
    finally:
        for project_id in created:
            try:
                run_async(live_client.delete_project(owner, repo, project_id))
            except Exception:  # cleanup is best-effort on a disposable instance
                pass


def test_listing_milestones_returns_the_ones_past_the_first_page(
    live_client, seeded_repo, run_async, writable, forgejo_target
):
    """The milestones page orders the other way: new ones land on page 2.

    That is what made a create beyond the boundary fail its own verification
    and report CREATE_UNVERIFIED for a milestone Forgejo had really created.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    created = []
    try:
        for index in range(_PAST_FIRST_PAGE):
            made = run_async(
                live_client.create_milestone(owner, repo, unique(f"MS{index:02d}"))
            )
            assert made["milestone"], "create returned no milestone"
            created.append(int(made["milestone"]["id"]))

        listed = {m["id"] for m in run_async(
            live_client.list_milestones(owner, repo, "open"))}
        assert set(created) <= listed

        # The client's view must match Forgejo's own count, not a page of it.
        via_api = {
            m["id"] for m in forgejo_target.api(
                "GET", f"/repos/{owner}/{repo}/milestones?state=open")
        }
        assert listed == via_api
    finally:
        for milestone_id in created:
            try:
                run_async(live_client.delete_milestone(owner, repo, milestone_id))
            except Exception:  # cleanup is best-effort on a disposable instance
                pass


def test_a_batch_that_sends_one_card_to_two_columns_is_refused(
    live_client, seeded_repo, live_project, run_async
):
    """Both writes used to be applied, and the reply claimed both columns."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    first = int(run_async(
        live_client.create_column(owner, repo, project_id, "One"))["column"]["id"])
    second = int(run_async(
        live_client.create_column(owner, repo, project_id, "Two"))["column"]["id"])
    number = seeded_repo.issue_numbers[0]
    run_async(live_client.add_issues_to_project(owner, repo, project_id, [number]))

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.bulk_move_cards(owner, repo, project_id, [
            {"issue_number": number, "column_id": first},
            {"issue_number": number, "column_id": second},
        ]))

    assert exc.value.code == "INVALID_INPUT"


def test_an_unusable_reference_on_issue_create_is_named(
    live_client, seeded_repo, live_project, run_async, forgejo_target
):
    """Forgejo answers an unusable milestone or project with a bare 500.

    Forgejo 16 does answer an unknown project with 404, but 1.20 puts it in the
    same 500 bucket, so both are diagnosed rather than trusted to the status.
    A milestone that exists in *another* repository fails the same way, which is
    why the check is scoped to this repository's milestones.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    before = len(forgejo_target.api("GET", f"/repos/{owner}/{repo}/issues?state=all"))

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.create_issue(owner, repo, unique("BadMS"), milestone_id=999_999)
        )
    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert exc.value.status == 404

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.create_issue(owner, repo, unique("BadPr"), project_id=999_999)
        )
    assert exc.value.code == "PROJECT_NOT_FOUND"

    after = len(forgejo_target.api("GET", f"/repos/{owner}/{repo}/issues?state=all"))
    assert after == before, "a refused create must not leave an issue behind"


def test_a_500_from_a_reference_that_checks_out_keeps_its_error(
    live_client, seeded_repo, run_async, writable
):
    """An unusable assignee cannot be looked up through any route we have.

    The milestone here is real, so the diagnosis finds nothing missing and must
    hand back Forgejo's own error with a note, rather than blaming the milestone.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.create_issue(
            owner, repo, unique("BadAssignee"),
            milestone_id=seeded_repo.milestone_id, assignee_ids=[999_999]))

    assert exc.value.code != "MILESTONE_NOT_FOUND"
    assert "assignee_ids" in str(exc.value)


def test_moving_an_issue_that_is_not_on_the_board_says_so(
    live_client, seeded_repo, live_project, run_async
):
    """Forgejo answers this with a bare HTTP 500 that names nothing.

    An agent cannot tell "this issue is not on that board" from "the server
    broke", which are very different things to do next.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    column = int(run_async(live_client.create_column(
        owner, repo, live_project["id"], "Target"))["column"]["id"])
    detached = seeded_repo.issue_numbers[1]

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.move_card(
            owner, repo, live_project["id"], column, [detached]))

    assert exc.value.code == "CARD_NOT_FOUND"
    assert exc.value.status == 404


def test_re_adding_an_issue_already_on_the_board_leaves_it_where_it_is(
    live_client, seeded_repo, live_project, run_async
):
    """Attaching an issue that is already on this project is a placement no-op.

    Worth pinning down: it would be easy for the route to reset the card to the
    default column, and a QA report claimed it did. It does not, on any release
    the suite runs against.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    default_column = int(run_async(live_client.create_column(
        owner, repo, project_id, "Default"))["column"]["id"])
    other_column = int(run_async(live_client.create_column(
        owner, repo, project_id, "Elsewhere"))["column"]["id"])
    run_async(live_client.set_default_column(owner, repo, project_id, default_column))
    number = seeded_repo.issue_numbers[0]

    run_async(live_client.add_issues_to_project(owner, repo, project_id, [number]))
    run_async(live_client.move_card(owner, repo, project_id, other_column, [number]))

    def column_of(num):
        board = run_async(live_client.get_project(owner, repo, project_id))
        return next((col["id"] for col in board["columns"]
                     for card in col["cards"] if card["number"] == num), None)

    assert column_of(number) == other_column
    run_async(live_client.add_issues_to_project(owner, repo, project_id, [number]))
    assert column_of(number) == other_column, "the re-add must not reset placement"


def test_move_card_refuses_a_repeated_issue_number(
    live_client, seeded_repo, live_project, run_async
):
    """The same rule bulk moves get: one card cannot hold two positions."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    column = int(run_async(live_client.create_column(
        owner, repo, live_project["id"], "Dupes"))["column"]["id"])
    number = seeded_repo.issue_numbers[0]

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.move_card(
            owner, repo, live_project["id"], column, [number, number]))

    assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("color", ["#e01e5a", "#E01E5A", "#000000"])
def test_the_column_colors_the_client_allows_are_the_ones_forgejo_takes(
    live_client, seeded_repo, live_project, run_async, color
):
    """The validator must not be stricter *or* looser than Forgejo.

    Looser is the dangerous direction: the CSS three-digit shorthand (`#fff`)
    reads like a valid color and was allowed through at first, but Forgejo
    answers it with exactly the HTTP 500 this check exists to prevent.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name

    created = run_async(live_client.create_column(
        owner, repo, live_project["id"], unique("Col"), color=color))

    assert created["created"] is True
    assert created["column"]["id"]


def test_an_unparseable_column_color_is_refused_before_forgejo_sees_it(
    live_client, seeded_repo, live_project, run_async
):
    """Forgejo answers a color it cannot parse with a bare HTTP 500."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.create_column(
                owner, repo, live_project["id"], "Colored", color="not-a-color")
        )

    assert exc.value.code == "INVALID_INPUT"
    assert exc.value.status == 400


def test_attaching_an_issue_to_a_second_project_moves_it(
    live_client, seeded_repo, live_project, run_async
):
    """Forgejo stores one project per issue, so "add" is really "move".

    Asserted rather than fixed: the route takes a single project assignment and
    offers no way to hold an issue on two boards. The tool documents it.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    first = live_project["id"]
    second_project = run_async(live_client.create_project(owner, repo, unique("Second")))
    second = int(second_project["project"]["id"])
    number = seeded_repo.issue_numbers[0]
    try:
        run_async(live_client.add_issues_to_project(owner, repo, first, [number]))
        board = run_async(live_client.get_project(owner, repo, first))
        assert any(c["number"] == number for col in board["columns"] for c in col["cards"])

        run_async(live_client.add_issues_to_project(owner, repo, second, [number]))

        first_board = run_async(live_client.get_project(owner, repo, first))
        second_board = run_async(live_client.get_project(owner, repo, second))
        assert not any(
            c["number"] == number
            for col in first_board["columns"] for c in col["cards"]
        ), "the issue is expected to leave the first board"
        assert any(
            c["number"] == number
            for col in second_board["columns"] for c in col["cards"]
        )
    finally:
        run_async(live_client.delete_project(owner, repo, second))


def _milestone_fields(instance, owner, repo, milestone_id):
    """Read one milestone back through Forgejo's own REST API.

    Milestones, unlike projects, have documented API coverage. Verifying an
    edit through it rather than through the client keeps the assertion
    independent of the parser the edit itself relies on.
    """
    for milestone in instance.api("GET", f"/repos/{owner}/{repo}/milestones?state=all"):
        if milestone["id"] == milestone_id:
            return milestone
    raise AssertionError(f"milestone {milestone_id} is gone")


def test_editing_one_milestone_field_leaves_the_others_alone(
    live_client, seeded_repo, run_async, writable, forgejo_target
):
    """The edit route replaces the whole milestone, so the rest is carried over.

    A deadline-only edit used to submit an empty title, which Forgejo refuses
    outright -- the client reported success and nothing changed at all.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique("Sprint")
    created = run_async(
        live_client.create_milestone(owner, repo, title, "keep me", "2029-12-31")
    )
    milestone_id = int(created["milestone"]["id"])
    try:
        run_async(
            live_client.edit_milestone(owner, repo, milestone_id, deadline="2030-01-15")
        )
        after = _milestone_fields(forgejo_target, owner, repo, milestone_id)
        assert after["due_on"].startswith("2030-01-15"), "the deadline did not persist"
        assert after["title"] == title
        assert after["description"] == "keep me"

        run_async(
            live_client.edit_milestone(owner, repo, milestone_id, title=title + " v2")
        )
        after = _milestone_fields(forgejo_target, owner, repo, milestone_id)
        assert after["title"] == title + " v2"
        assert after["description"] == "keep me"
        assert after["due_on"].startswith("2030-01-15")
    finally:
        run_async(live_client.delete_milestone(owner, repo, milestone_id))


def test_a_milestone_field_is_cleared_only_when_asked(
    live_client, seeded_repo, run_async, writable, forgejo_target
):
    """An empty string clears a field; ``None`` leaves it as it was."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    created = run_async(
        live_client.create_milestone(
            owner, repo, unique("Clear"), "drop me", "2029-12-31")
    )
    milestone_id = int(created["milestone"]["id"])
    try:
        run_async(
            live_client.edit_milestone(
                owner, repo, milestone_id, description="", deadline="")
        )

        after = _milestone_fields(forgejo_target, owner, repo, milestone_id)
        assert after["description"] == ""
        assert after["due_on"] is None
    finally:
        run_async(live_client.delete_milestone(owner, repo, milestone_id))


def test_an_impossible_deadline_is_reported_as_a_rejection(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo refuses this by re-rendering the form as HTTP 200.

    That is below 400, so it used to arrive as ``created: true`` with no
    milestone attached.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = unique("BadDate")

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.create_milestone(owner, repo, title, deadline="2026-02-30")
        )

    assert exc.value.code == "REJECTED"
    assert "yyyy-mm-dd" in str(exc.value)
    assert not [
        m for m in run_async(live_client.list_milestones(owner, repo, "all"))
        if m["title"] == title
    ]


def test_a_blank_milestone_title_is_refused_before_forgejo_sees_it(
    live_client, seeded_repo, run_async, writable
):
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.create_milestone(owner, repo, "  "))
    assert exc.value.code == "INVALID_INPUT"

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.edit_milestone(
                owner, repo, seeded_repo.milestone_id, title="")
        )
    assert exc.value.code == "INVALID_INPUT"


def test_deleting_a_milestone_that_is_not_there_is_not_a_success(
    live_client, seeded_repo, run_async, writable
):
    """Forgejo answers this route with 200 whether or not it deleted anything."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.delete_milestone(owner, repo, 999_999))

    assert exc.value.code == "MILESTONE_NOT_FOUND"
    assert exc.value.status == 404


def test_deleting_the_same_milestone_twice_reports_the_second_as_gone(
    live_client, seeded_repo, run_async, writable
):
    owner, repo = seeded_repo.owner, seeded_repo.name
    created = run_async(live_client.create_milestone(owner, repo, unique("Once")))
    milestone_id = int(created["milestone"]["id"])

    assert run_async(live_client.delete_milestone(owner, repo, milestone_id)) == {
        "deleted": True, "milestone_id": milestone_id
    }
    with pytest.raises(ForgejoError) as exc:
        run_async(live_client.delete_milestone(owner, repo, milestone_id))
    assert exc.value.code == "MILESTONE_NOT_FOUND"


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


def test_an_unknown_filter_id_is_named_instead_of_returning_nothing(
    live_client, seeded_repo, live_project, run_async
):
    """These used to answer total: 0, which reads as 'there is nothing there'."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    column_id = int(
        run_async(live_client.create_column(owner, repo, project_id, "Col"))["column"]["id"]
    )

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.read_project_content(owner, repo, project_id, milestone=999_999)
        )
    assert exc.value.code == "MILESTONE_NOT_FOUND"

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.read_column_content(
                owner, repo, project_id, column_id, milestone=999_999
            )
        )
    assert exc.value.code == "MILESTONE_NOT_FOUND"

    with pytest.raises(ForgejoError) as exc:
        run_async(
            live_client.read_milestone_content(
                owner, repo, seeded_repo.milestone_id, project=999_999
            )
        )
    assert exc.value.code == "PROJECT_NOT_FOUND"


def test_an_unfiltered_board_read_does_not_look_a_milestone_up(
    live_client, seeded_repo, live_project, run_async
):
    """The confirming lookup must only be paid for when a filter is passed."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    run_async(live_client.read_project_content(owner, repo, live_project["id"]))

    assert f"/{owner}/{repo}/milestones" not in log.paths("GET")


def test_a_repeated_issue_number_is_read_once(live_client, seeded_repo, run_async):
    """A duplicate used to be fetched again and counted again."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    number = seeded_repo.issue_numbers[0]
    log = watch_requests(live_client, run_async)
    log.clear()

    got = run_async(live_client.bulk_read_issues(owner, repo, [number, number]))

    assert [g["number"] for g in got] == [number]
    assert log.paths("GET").count(f"/{owner}/{repo}/issues/{number}") == 1


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
