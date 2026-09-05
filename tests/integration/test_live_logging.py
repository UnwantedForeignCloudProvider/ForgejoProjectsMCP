"""Debug logging against a live instance, and what must never appear in it.

``tests/test_logging.py`` asserts the same properties over invented responses.
Running them for real matters because the values at risk here are real: an
actual password, an actual issue body, an actual repository name typed by a
user. Debug logs are the likeliest place for those to escape, and a fake
transport can only ever leak what the test itself put there.

One offline test has no counterpart here: a stock Forgejo will not answer 429,
so the rate-limit retry logging cannot be provoked against a real instance and
stays covered offline only.
"""

from __future__ import annotations

import logging
import uuid

import pytest

import forgejo_projects_mcp.client as client_mod
from forgejo_projects_mcp.client import AuthError, ForgejoError

from .helpers import add_comment, create_issue


@pytest.fixture
def debug_logs(caplog):
    """Capture the client logger, whose parent intentionally does not propagate."""
    caplog.set_level(logging.DEBUG, logger=client_mod.logger.name)
    client_mod.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        client_mod.logger.removeHandler(caplog.handler)


def secret(label: str) -> str:
    """A value distinctive enough that finding it in a log is unambiguous."""
    return f"{label}-{uuid.uuid4().hex}"


def test_a_real_request_logs_metadata_but_never_values(
    live_client, seeded_repo, run_async, debug_logs, writable
):
    """Field names, status and timing are logged; the values are not."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    query = secret("search")
    title = secret("milestone-title")

    run_async(live_client.list_repositories(query=query))
    created = run_async(live_client.create_milestone(owner, repo, title))
    run_async(
        live_client.delete_milestone(owner, repo, int(created["milestone"]["id"]))
    )

    text = debug_logs.text
    assert "HTTP request method=GET path=/repo/search" in text
    assert "param_keys=" in text
    assert "form_fields=" in text
    assert "HTTP response method=GET path=/repo/search status=200" in text
    assert "elapsed_ms=" in text
    assert query not in text, "a search term must not reach the logs"
    assert title not in text, "a form value must not reach the logs"


def test_a_real_login_logs_decisions_but_never_credentials(
    client_factory, forgejo_target, run_async, debug_logs
):
    """The login handshake is traceable without the password being in the trace."""
    client = client_factory()

    result = run_async(client.login())

    text = debug_logs.text
    assert result["authenticated"] is True
    assert "Explicit authentication requested force=False" in text
    assert "Login attempt started" in text
    assert "Login response status=" in text
    assert "Login completed session_state_persisted=true" in text
    assert forgejo_target.password not in text


def test_a_rejected_login_logs_the_failure_without_the_password(
    client_factory, run_async, debug_logs
):
    """The failure path is the one most likely to echo what was sent."""
    wrong = secret("rejected-password")
    client = client_factory(password=wrong)

    with pytest.raises(AuthError):
        run_async(client.login())

    text = debug_logs.text
    assert "Login rejected status=" in text or "Authentication probe status=" in text
    assert wrong not in text


def test_the_throttle_logs_its_wait_against_a_live_instance(
    live_client, seeded_repo, run_async, debug_logs
):
    """Consecutive real requests are spaced, and the wait is recorded."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    run_async(live_client.ensure())

    for _ in range(3):
        run_async(live_client.list_projects(owner, repo, "open"))

    text = debug_logs.text
    assert "Request slot acquired" in text
    assert "Request throttle waiting wait_ms=" in text
    assert "requests_per_second=" in text


def test_parsers_log_only_sizes_and_counts(
    live_client, seeded_repo, live_project, forgejo_target, run_async, debug_logs,
    writable,
):
    """Real page content is summarized in the logs, never reproduced in them."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    column_title = secret("column")
    issue_title = secret("issue-title")
    issue_body = secret("issue-body")
    comment_body = secret("comment-body")

    number = create_issue(forgejo_target, owner, repo, issue_title, issue_body)
    add_comment(forgejo_target, owner, repo, number, comment_body)
    run_async(live_client.create_column(owner, repo, project_id, column_title))
    debug_logs.clear()

    run_async(live_client.list_projects(owner, repo, "open"))
    board = run_async(live_client.get_project(owner, repo, project_id))
    issue = run_async(live_client.read_issue(owner, repo, number))
    run_async(live_client.list_milestones(owner, repo, "open"))

    # The content really was read: this is what must not have been logged.
    assert column_title in [c["title"] for c in board["columns"]]
    assert issue["title"] == issue_title
    assert issue["body"] == issue_body

    text = debug_logs.text
    assert "Parsed projects list" in text and "projects=" in text
    assert "Parsed project board" in text and "columns=" in text
    assert "Parsed issue" in text
    assert "Parsed milestones list" in text and "milestones=" in text
    for value in (column_title, issue_title, issue_body, comment_body):
        assert value not in text


def test_a_failing_read_logs_the_error_shape_not_the_page(
    live_client, seeded_repo, run_async, debug_logs
):
    """An error response is described by format and presence, not by content."""
    owner, repo = seeded_repo.owner, seeded_repo.name

    with pytest.raises(ForgejoError):
        run_async(live_client.get_project(owner, repo, 999_999))

    text = debug_logs.text
    assert "Parsed error response format=" in text
    assert "<html" not in text.lower(), "an error page must not be dumped into the logs"


def test_the_version_is_announced_once_per_session(
    client_factory, run_async, debug_logs
):
    """The version is announced at info level once, not on every probe."""
    def announcements() -> list[str]:
        return [
            record.getMessage()
            for record in debug_logs.records
            if record.levelno >= logging.INFO
            and "Forgejo version detected" in record.getMessage()
        ]

    client = client_factory()

    run_async(client.ensure())
    announced = announcements()
    debug_logs.clear()
    run_async(client.ensure())  # already authenticated: nothing new to announce

    assert announced, "the detected version should be announced once"
    assert client.version is not None
    assert client.version.short in announced[0]
    assert announcements() == []


def test_the_session_state_file_holds_no_credentials(
    live_client, forgejo_target, run_async
):
    """Whatever else is cached, the password is not part of it."""
    run_async(live_client.login())

    state = client_mod.STATE_FILE.read_text()
    config = client_mod.CONFIG_FILE.read_text()

    assert forgejo_target.password not in state
    assert forgejo_target.password not in config
