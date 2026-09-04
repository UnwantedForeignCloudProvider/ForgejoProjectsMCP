"""Debug logging coverage, including protection against payload disclosure."""

import asyncio
import logging

import pytest
from conftest import FakeResponse, make_client

import forgejo_projects_mcp.client as client_mod
from forgejo_projects_mcp.client import ForgejoClient


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def debug_logs(caplog):
    """Capture the client logger, whose parent intentionally does not propagate."""
    caplog.set_level(logging.DEBUG, logger=client_mod.logger.name)
    client_mod.logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        client_mod.logger.removeHandler(caplog.handler)


def test_request_logs_metadata_status_and_timing_without_values(debug_logs):
    secret = "do-not-log-this-value"
    c = make_client(lambda method, path, kw: FakeResponse(status=201))

    run(
        c._request(
            "POST",
            f"/o/r/issues/new?token={secret}",
            form={"title": secret, "password": secret},
            params={"q": secret},
        )
    )

    assert "HTTP request method=POST path=/o/r/issues/new" in debug_logs.text
    assert "param_keys=['q']" in debug_logs.text
    assert "form_fields=['password', 'title']" in debug_logs.text
    assert "Request slot acquired method=POST path=/o/r/issues/new" in debug_logs.text
    assert "HTTP response method=POST path=/o/r/issues/new status=201" in debug_logs.text
    assert "elapsed_ms=" in debug_logs.text
    assert secret not in debug_logs.text


def test_throttle_logs_wait_duration(monkeypatch, debug_logs):
    waits = []

    async def record_sleep(delay):
        waits.append(delay)

    async def exercise():
        c = ForgejoClient()
        c._next_request = asyncio.get_running_loop().time() + 0.05
        await c._throttle()

    monkeypatch.setattr(client_mod.asyncio, "sleep", record_sleep)
    run(exercise())

    assert waits and waits[0] > 0
    assert "Request throttle waiting wait_ms=" in debug_logs.text
    assert "requests_per_second=" in debug_logs.text


def test_rate_limit_retry_logs_attempt_and_responses(debug_logs):
    responses = iter(
        [
            FakeResponse(status=429, headers={"retry-after": "0"}),
            FakeResponse(status=200),
        ]
    )
    c = make_client(lambda method, path, kw: next(responses))

    async def no_throttle():
        return None

    c._throttle = no_throttle
    response = run(c._request("GET", "/repo/search"))

    assert response.status == 200
    assert "HTTP response method=GET path=/repo/search status=429" in debug_logs.text
    assert "Rate-limit retry scheduled method=GET path=/repo/search" in debug_logs.text
    assert "retries_remaining=1" in debug_logs.text
    assert "HTTP response method=GET path=/repo/search status=200" in debug_logs.text


def test_authentication_logs_decisions_without_credentials(tmp_state, debug_logs):
    secret = "credential-must-stay-private"
    logged_in = False

    def handler(method, path, kw):
        nonlocal logged_in
        if method == "POST" and path == "/user/login":
            logged_in = True
            return FakeResponse(status=303)
        if path == "/user/settings":
            return FakeResponse(status=200 if logged_in else 302)
        return FakeResponse(status=200)

    c = make_client(handler, authed=False)
    c.password = secret
    result = run(c.login())

    assert result["authenticated"] is True
    assert "Explicit authentication requested force=False" in debug_logs.text
    assert "Authentication probe status=302 authenticated=False" in debug_logs.text
    assert "Login attempt started" in debug_logs.text
    assert "Login response status=303" in debug_logs.text
    assert "Login completed session_state_persisted=true" in debug_logs.text
    assert secret not in debug_logs.text


def test_parsers_log_only_sizes_and_counts(debug_logs):
    secret = "private-content-must-not-be-logged"
    projects = f'<a href="/o/r/projects/2">{secret}</a>'
    board = (
        '<div class="project-column" data-id="5">'
        f'<span class="project-column-title-label">{secret}</span>'
        '<a data-issue="42" href="/o/r/issues/7">card</a></div>'
    )
    issue = (
        f'<meta property="og:title" content="{secret}">'
        '<span class="index">#7</span>'
        f'<div id="issue-7-raw">{secret}</div>'
    )
    milestones = f'<a href="/o/r/milestone/3">{secret}</a>'

    ForgejoClient._parse_projects_list(projects)
    ForgejoClient._parse_board(board)
    ForgejoClient._parse_issue(issue)
    ForgejoClient._parse_milestones(milestones)

    assert "Parsed projects list" in debug_logs.text
    assert "projects=1" in debug_logs.text
    assert "Parsed project board" in debug_logs.text
    assert "columns=1" in debug_logs.text
    assert "Parsed issue" in debug_logs.text
    assert "Parsed milestones list" in debug_logs.text
    assert "milestones=1" in debug_logs.text
    assert secret not in debug_logs.text
