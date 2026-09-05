"""End-to-end board management against a live, disposable Forgejo instance.

Everything here writes: projects, columns, cards, issues and milestones are
created, changed and deleted. That is why these tests require a disposable
instance -- one the suite started itself, or an external one explicitly opened
up with ``FORGEJO_TEST_ALLOW_WRITES=1``. The ``writable`` fixture enforces it.

Each test creates what it needs inside a repository seeded once per instance,
and the ``live_project`` fixture deletes the board it made afterwards, so tests
stay independent of each other and of run order.
"""

from __future__ import annotations


def test_project_lifecycle(live_client, seeded_repo, run_async, writable):
    """Create, list, read, rename, close, reopen and delete a board."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    created = run_async(
        live_client.create_project(owner, repo, "Lifecycle board", "described")
    )
    assert created["created"] is True
    project_id = int(created["project"]["id"])

    listed = run_async(live_client.list_projects(owner, repo, "open"))
    assert project_id in [p["id"] for p in listed]

    board = run_async(live_client.get_project(owner, repo, project_id))
    assert board["id"] == project_id
    assert board["title"] == "Lifecycle board"

    run_async(live_client.update_project(owner, repo, project_id, title="Renamed"))
    assert run_async(live_client.get_project(owner, repo, project_id))["title"] == (
        "Renamed"
    )

    run_async(live_client.close_project(owner, repo, project_id))
    closed = run_async(live_client.list_projects(owner, repo, "closed"))
    assert project_id in [p["id"] for p in closed]

    run_async(live_client.reopen_project(owner, repo, project_id))
    reopened = run_async(live_client.list_projects(owner, repo, "open"))
    assert project_id in [p["id"] for p in reopened]

    run_async(live_client.delete_project(owner, repo, project_id))
    remaining = run_async(live_client.list_projects(owner, repo, "all"))
    assert project_id not in [p["id"] for p in remaining]


def test_column_lifecycle(live_client, seeded_repo, live_project, run_async):
    """Add a column, rename it, make it the default, and delete another."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]

    created = run_async(
        live_client.create_column(owner, repo, project_id, "In progress", "#e01e5a")
    )
    assert created["created"] is True
    column_id = int(created["column"]["id"])

    run_async(
        live_client.edit_column(owner, repo, project_id, column_id, title="Doing")
    )
    board = run_async(live_client.get_project(owner, repo, project_id))
    titles = {c["id"]: c["title"] for c in board["columns"]}
    assert titles[column_id] == "Doing"

    # A default column cannot be deleted, so promote the new one first and
    # delete a second column instead.
    run_async(live_client.set_default_column(owner, repo, project_id, column_id))
    spare = run_async(live_client.create_column(owner, repo, project_id, "Spare"))
    spare_id = int(spare["column"]["id"])

    run_async(live_client.delete_column(owner, repo, project_id, spare_id))
    after = run_async(live_client.get_project(owner, repo, project_id))
    assert spare_id not in [c["id"] for c in after["columns"]]


def test_cards_move_between_columns(live_client, seeded_repo, live_project, run_async):
    """Attach seeded issues to a board and move them across columns."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    first, second = seeded_repo.issue_numbers[0], seeded_repo.issue_numbers[1]

    todo = int(
        run_async(live_client.create_column(owner, repo, project_id, "Todo"))["column"][
            "id"
        ]
    )
    done = int(
        run_async(live_client.create_column(owner, repo, project_id, "Done"))["column"][
            "id"
        ]
    )

    run_async(
        live_client.add_issues_to_project(owner, repo, project_id, [first, second])
    )
    board = run_async(live_client.get_project(owner, repo, project_id))
    on_board = {c["number"] for column in board["columns"] for c in column["cards"]}
    assert {first, second} <= on_board

    run_async(live_client.move_card(owner, repo, project_id, todo, [first, second]))
    board = run_async(live_client.get_project(owner, repo, project_id))
    in_todo = next(c for c in board["columns"] if c["id"] == todo)
    assert [card["number"] for card in in_todo["cards"]] == [first, second]

    run_async(
        live_client.bulk_move_cards(
            owner,
            repo,
            project_id,
            [
                {"issue_number": first, "column_id": done},
                {"issue_number": second, "column_id": todo},
            ],
        )
    )
    board = run_async(live_client.get_project(owner, repo, project_id))
    by_column = {
        c["id"]: [card["number"] for card in c["cards"]] for c in board["columns"]
    }
    assert by_column[done] == [first]
    assert by_column[todo] == [second]

    run_async(live_client.remove_issues_from_project(owner, repo, [first, second]))
    board = run_async(live_client.get_project(owner, repo, project_id))
    still_on_board = {c["number"] for col in board["columns"] for c in col["cards"]}
    assert not {first, second} & still_on_board


def test_issue_created_straight_onto_a_board(
    live_client, seeded_repo, live_project, run_async
):
    """create_issue with a project id lands the new issue on that board."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]

    created = run_async(
        live_client.create_issue(
            owner, repo, "Straight to the board", "body", project_id=project_id
        )
    )
    assert created["created"] is True
    number = created["number"]
    assert number is not None, "the new-issue response should carry the number"

    board = run_async(live_client.get_project(owner, repo, project_id))
    on_board = {c["number"] for column in board["columns"] for c in column["cards"]}
    assert number in on_board

    issue = run_async(live_client.read_issue(owner, repo, number))
    assert issue["title"] == "Straight to the board"
    assert issue["body"] == "body"
    assert issue["state"] == "open"

    run_async(live_client.delete_issue(owner, repo, number))


def test_reading_a_board_returns_card_content(
    live_client, seeded_repo, live_project, run_async
):
    """read_project and read_column return full issue content per column."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    numbers = list(seeded_repo.issue_numbers[:3])

    column_id = int(
        run_async(live_client.create_column(owner, repo, project_id, "Review"))[
            "column"
        ]["id"]
    )
    run_async(live_client.add_issues_to_project(owner, repo, project_id, numbers))
    run_async(live_client.move_card(owner, repo, project_id, column_id, numbers))

    content = run_async(live_client.read_project_content(owner, repo, project_id))
    assert content["error_count"] == 0
    assert content["total"] >= len(numbers)
    read_numbers = {
        card["number"] for column in content["columns"] for card in column["cards"]
    }
    assert set(numbers) <= read_numbers

    column = run_async(
        live_client.read_column_content(owner, repo, project_id, column_id)
    )
    assert column["column"]["title"] == "Review"
    assert {i["number"] for i in column["issues"]} == set(numbers)
    assert all(i["body"] for i in column["issues"])

    paged = run_async(
        live_client.read_column_content(
            owner, repo, project_id, column_id, limit=1, offset=0
        )
    )
    assert paged["returned"] == 1
    assert paged["truncated"] is True


def test_milestone_lifecycle_and_content(live_client, seeded_repo, run_async, writable):
    """Create, edit, close, reopen and delete a milestone, and read its issues."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    seeded = run_async(live_client.list_milestones(owner, repo, "all"))
    assert seeded_repo.milestone_id in [m["id"] for m in seeded]

    content = run_async(
        live_client.read_milestone_content(owner, repo, seeded_repo.milestone_id)
    )
    assert content["milestone"]["title"] == seeded_repo.milestone_title
    # seed_repository attaches the first two issues to the milestone.
    assert content["total"] == 2
    assert content["error_count"] == 0

    created = run_async(live_client.create_milestone(owner, repo, "Temp milestone"))
    milestone_id = int(created["milestone"]["id"])

    run_async(live_client.edit_milestone(owner, repo, milestone_id, title="Renamed"))
    assert "Renamed" in [
        m["title"] for m in run_async(live_client.list_milestones(owner, repo, "open"))
    ]

    run_async(live_client.close_milestone(owner, repo, milestone_id))
    assert milestone_id in [
        m["id"] for m in run_async(live_client.list_milestones(owner, repo, "closed"))
    ]

    run_async(live_client.reopen_milestone(owner, repo, milestone_id))
    assert milestone_id in [
        m["id"] for m in run_async(live_client.list_milestones(owner, repo, "open"))
    ]

    run_async(live_client.delete_milestone(owner, repo, milestone_id))
    assert milestone_id not in [
        m["id"] for m in run_async(live_client.list_milestones(owner, repo, "all"))
    ]


def test_bulk_read_reports_per_issue_failures(live_client, seeded_repo, run_async):
    """A missing issue is reported inline instead of failing the whole read."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    missing = 10_000

    results = run_async(
        live_client.bulk_read_issues(
            owner, repo, [seeded_repo.issue_numbers[0], missing]
        )
    )

    by_number = {r["number"]: r for r in results}
    assert "error" not in by_number[seeded_repo.issue_numbers[0]]
    assert "error" in by_number[missing]
