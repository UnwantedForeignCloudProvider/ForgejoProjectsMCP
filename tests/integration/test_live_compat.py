"""Version detection and per-version adaptation, verified against the real thing.

``tests/test_versioning.py`` proves the client *would* adapt if an instance
behaved a certain way. These prove the instances actually do behave that way:
that the version in the page is the version Forgejo reports, that the CSRF rule
each release enforces is the one the profile predicts, and that the runtime
recovery works when a real server really does reject a write.
"""

from __future__ import annotations

import pytest

from forgejo_projects_mcp.compat import (
    CSRF_ORIGIN,
    CSRF_TOKEN,
    NEWEST_VERIFIED,
    OLDEST_VERIFIED,
    Version,
    detect_csrf_token,
    detect_version,
    profile_for,
)

from .helpers import unique, watch_requests

CSRF_HEADER = "X-Csrf-Token"


def _csrf_headers(call: dict) -> dict[str, str]:
    return {k.lower(): v for k, v in call["headers"].items()}


# ------------------------------------------------------------------ detection
def test_one_request_proves_the_session_and_reveals_the_version(
    live_client, run_async
):
    """The two-in-one probe, measured on the wire.

    The offline test counts requests against a fake transport. Here the count
    is of real requests: the client is asked to re-establish what it knows, and
    exactly one round trip must answer both questions.
    """
    run_async(live_client.ensure())
    log = watch_requests(live_client, run_async)
    live_client._version = None
    log.clear()

    authenticated = run_async(live_client._is_authenticated())

    assert authenticated is True
    assert log.paths() == [live_client.profile.route("auth_probe")]
    assert live_client.version is not None


def test_the_detected_version_matches_what_the_instance_reports(
    live_client, forgejo_target, run_async
):
    """Version read from the HTML agrees with the documented REST answer."""
    detected = Version.parse(run_async(live_client.status())["version"])
    reported = Version.parse(forgejo_target.reported_version)

    assert detected is not None
    assert detected == reported


def test_any_rendered_page_carries_the_version(
    live_client, seeded_repo, run_async
):
    """Detection does not depend on the probe page in particular."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    html = run_async(
        live_client._get_text(live_client.profile.route("projects", owner=owner, repo=repo))
    )

    assert detect_version(html) == live_client.version


def test_a_csrf_token_is_published_by_the_versions_that_demand_one(
    live_client, run_async
):
    """Token mode is only viable if the instance actually hands a token out.

    Releases that accept an Origin header stopped publishing one at all, so this
    is deliberately one-directional: where the profile says a token is needed,
    the probe page must carry one for the client to find.
    """
    run_async(live_client.ensure())
    html = run_async(live_client._get_text(live_client.profile.route("auth_probe")))

    if live_client.profile.csrf_mode == CSRF_TOKEN:
        assert detect_csrf_token(html), (
            "this release requires a CSRF token but publishes none"
        )
        assert live_client._csrf_token, "the probe should have absorbed the token"


def test_the_running_version_is_one_the_suite_claims_to_support(
    live_client, run_async
):
    """`verified` is a promise about the range this suite exercises: check it."""
    described = run_async(live_client.status())["compatibility"]
    version = live_client.version

    # The window is expressed in majors at the top end: any patch of the newest
    # verified major counts as verified.
    assert OLDEST_VERIFIED <= version
    assert version.major <= NEWEST_VERIFIED.major
    assert described["verified"] is True
    assert described["quirks"] == list(profile_for(version).quirks)


# ----------------------------------------------------------------------- CSRF
def test_a_write_carries_a_token_exactly_when_the_version_needs_one(
    live_client, seeded_repo, run_async, writable
):
    """The CSRF rule the profile predicts is the rule the instance enforces.

    Below Forgejo 14 a write must carry the session token; from 14 a matching
    Origin header is enough. Either way the write has to succeed, which is what
    makes this more than a restatement of the profile.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    created = run_async(live_client.create_milestone(owner, repo, unique("CSRF")))
    milestone_id = int(created["milestone"]["id"])

    try:
        write = log.require("POST", f"/{owner}/{repo}/milestones/new")
        needs_token = live_client.version < Version(14, 0, 0)
        assert live_client.profile.csrf_mode == (CSRF_TOKEN if needs_token else CSRF_ORIGIN)
        assert (CSRF_HEADER.lower() in _csrf_headers(write)) is needs_token
    finally:
        run_async(live_client.delete_milestone(owner, repo, milestone_id))


def test_reads_never_carry_a_csrf_token(live_client, seeded_repo, run_async):
    """A token on a read would leak it into logs and caches for no benefit."""
    owner, repo = seeded_repo.owner, seeded_repo.name
    log = watch_requests(live_client, run_async)
    log.clear()

    run_async(live_client.list_projects(owner, repo, "open"))

    reads = [c for c in log.calls if c["method"] == "GET"]
    assert reads
    assert all(CSRF_HEADER.lower() not in _csrf_headers(c) for c in reads)


def test_a_real_csrf_rejection_is_recovered_and_remembered(
    live_client, seeded_repo, run_async, writable
):
    """The safety net, triggered by an instance that really does reject the write.

    The client is put into the wrong CSRF mode on purpose -- the same state it
    would be in if it had misread the version -- and the instance answers the
    write with a genuine rejection. The client must pick up a token, retry, and
    keep using one for the rest of the session.

    The window is narrow, and both edges were measured rather than assumed.
    From 14.0 an Origin header is accepted, so there is nothing to recover from.
    Below 9.0 the rejection is undetectable: instead of ``400 Invalid CSRF
    token`` those releases answer ``303`` to the dashboard and drop the write
    silently, which is indistinguishable from the redirect a successful write
    returns. See the note in the automation reference.
    """
    run_async(live_client.ensure())
    if live_client.version >= Version(14, 0, 0):
        pytest.skip("this release accepts an Origin header, so it never rejects")
    if live_client.version < Version(9, 0, 0):
        pytest.skip(
            "this release drops a token-less write silently (303) instead of "
            "rejecting it, so there is no rejection to detect"
        )

    owner, repo = seeded_repo.owner, seeded_repo.name
    live_client._profile = live_client.profile.with_csrf_mode(CSRF_ORIGIN)
    log = watch_requests(live_client, run_async)
    log.clear()

    created = run_async(live_client.create_milestone(owner, repo, unique("Rejected")))
    milestone_id = int(created["milestone"]["id"])

    try:
        attempts = [
            c for c in log.calls if c["path"] == f"/{owner}/{repo}/milestones/new"
        ]
        assert len(attempts) == 2, "the rejected write should have been retried once"
        assert CSRF_HEADER.lower() not in _csrf_headers(attempts[0])
        assert CSRF_HEADER.lower() in _csrf_headers(attempts[1])
        assert live_client.profile.csrf_mode == CSRF_TOKEN
    finally:
        run_async(live_client.delete_milestone(owner, repo, milestone_id))


def test_the_board_markup_matches_the_profile_for_this_version(
    live_client, seeded_repo, live_project, run_async
):
    """The column patterns this version resolves to match its real markup.

    This is the check that catches a release changing its board vocabulary: the
    profile's own patterns are run against the page the instance rendered.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    project_id = live_project["id"]
    run_async(live_client.create_column(owner, repo, project_id, "Matched"))

    html = run_async(
        live_client._get_text(
            live_client.profile.route("project", owner=owner, repo=repo,
                                      project_id=project_id)
        )
    )
    profile = live_client.profile

    assert profile.search("board_column_open", html), (
        f"no column pattern in the {profile.describe()['version_short']} profile "
        "matches this instance's board markup"
    )
    assert profile.search("board_title", html)
    board = run_async(live_client.get_project(owner, repo, project_id))
    assert [c["title"] for c in board["columns"] if c["title"] == "Matched"]
