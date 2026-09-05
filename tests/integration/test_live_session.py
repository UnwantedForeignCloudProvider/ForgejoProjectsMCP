"""Session, version detection and compatibility checks against a live instance.

These are read-only: they authenticate and read, so they are safe even against
an instance the suite does not own.

Run against one or more versions::

    uv run pytest -m integration --forgejo-version 16
    uv run pytest -m integration --forgejo-version 8 --forgejo-version 16
"""

from __future__ import annotations

from pathlib import Path

from forgejo_projects_mcp.compat import CSRF_ORIGIN, CSRF_TOKEN, Version


def test_status_authenticates(live_client, forgejo_target, run_async):
    """A fresh login against the live instance succeeds over plain HTTP."""
    result = run_async(live_client.status())

    assert result["authenticated"] is True
    assert result["username"] == live_client.username
    assert result["instance"] == forgejo_target.url


def test_status_reports_the_instance_version(live_client, forgejo_target, run_async):
    """The session check also returns the version the instance reports itself.

    The client reads the version out of the same response that proves the
    session, so this asserts the two-in-one probe agrees with the REST API's
    own answer.
    """
    result = run_async(live_client.status())

    detected = Version.parse(result["version"])
    reported = Version.parse(forgejo_target.reported_version)
    assert detected is not None, f"no version detected: {result}"
    assert detected == reported


def test_session_probe_costs_no_extra_request(live_client, run_async):
    """Version detection piggybacks on the authentication probe.

    The client is asked only to authenticate; it must know the version
    afterwards without having made a dedicated version request.
    """
    assert live_client.version is None

    run_async(live_client.ensure())

    assert live_client.version is not None


def test_compatibility_profile_matches_the_version(live_client, run_async):
    """The resolved profile reflects the CSRF rules of the running version.

    Forgejo began accepting a matching Origin header in place of a CSRF token
    in 14.0; older releases reject such a write with HTTP 400.
    """
    result = run_async(live_client.status())
    version = Version.parse(result["version"])
    assert version is not None

    expected = CSRF_TOKEN if version < Version(14, 0, 0) else CSRF_ORIGIN
    compatibility = result["compatibility"]
    assert compatibility["csrf_mode"] == expected
    assert live_client.profile.csrf_mode == expected


def test_login_persists_session_and_config(live_client, run_async):
    """Login writes the isolated session state and non-secret config files."""
    result = run_async(live_client.login())

    assert result["authenticated"] is True
    assert live_client.base_url.startswith("http")  # plain HTTP is supported
    assert Path(result["state_file"]).exists()
    assert Path(result["config_file"]).exists()
    assert result["version"], "login should report the instance version too"


def test_list_repositories_sees_the_seeded_repo(live_client, seeded_repo, run_async):
    """The repo-search web route parses, and finds the repository we seeded."""
    repos = run_async(live_client.list_repositories(query=seeded_repo.name))

    assert [r["full_name"] for r in repos] == [seeded_repo.full_name]
    assert repos[0]["owner"] == seeded_repo.owner
    assert repos[0]["name"] == seeded_repo.name
