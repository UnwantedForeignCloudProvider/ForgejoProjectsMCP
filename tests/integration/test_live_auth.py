"""Authentication, session caching and configuration against a live instance.

These are the live counterparts of the auth half of ``tests/test_client.py``.
The offline versions assert what the client *sends* when a fake transport plays
along; these assert that a real Forgejo accepts it -- that a real login is
persisted and replayed, that a real rejection surfaces as ``AuthError``, and
that the credential provider recovers from both.

Most of them only read, but they log in repeatedly and write the session cache,
so they take ``writable`` where they would otherwise disturb a shared instance.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

import forgejo_projects_mcp.client as client_mod
from forgejo_projects_mcp import _env
from forgejo_projects_mcp.client import AuthError, ForgejoClient, ForgejoError

from .helpers import expire_session_after_next_probe, watch_requests

EMPTY_STATE = json.dumps({"cookies": [], "origins": []})


def _closed_port() -> int:
    """A port with nothing listening on it, for the connection-refused path."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --------------------------------------------------------------- login basics
def test_login_persists_the_session_state(live_client, isolated_config, run_async):
    """A real login writes a storage state that carries the session cookie."""
    result = run_async(live_client.login())

    assert result["authenticated"] is True
    state_file = Path(result["state_file"])
    assert state_file.exists()
    state = json.loads(state_file.read_text())
    assert state["cookies"], "the persisted state should carry session cookies"


def test_login_persists_only_non_secret_config(live_client, forgejo_target, run_async):
    """The URL and username are persisted; the password never is."""
    run_async(live_client.login())

    saved = json.loads(client_mod.CONFIG_FILE.read_text())
    assert saved == {
        "base_url": forgejo_target.url,
        "username": forgejo_target.username,
    }
    assert forgejo_target.password not in client_mod.CONFIG_FILE.read_text()
    # ...and not in the session state either, which holds cookies, not secrets.
    assert forgejo_target.password not in client_mod.STATE_FILE.read_text()


def test_saved_config_supplies_url_and_username(
    live_client, forgejo_target, run_async, monkeypatch
):
    """A second client reads the URL and username a previous login persisted."""
    run_async(live_client.login())
    for var in ("FORGEJO_URL", "FORGEJO_USERNAME", "FORGEJO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    reloaded = ForgejoClient()

    assert reloaded.base_url == forgejo_target.url
    assert reloaded.username == forgejo_target.username
    assert reloaded.password == ""  # never loaded from the config file


def test_env_overrides_saved_config(live_client, forgejo_target, run_async, monkeypatch):
    """FORGEJO_URL wins over the persisted value, and still authenticates."""
    run_async(live_client.login())
    client_mod.CONFIG_FILE.write_text(
        json.dumps({"base_url": "https://stale.invalid", "username": "someone-else"})
    )
    monkeypatch.setenv("FORGEJO_URL", forgejo_target.url)
    monkeypatch.setenv("FORGEJO_USERNAME", forgejo_target.username)
    monkeypatch.setenv("FORGEJO_PASSWORD", forgejo_target.password)

    reloaded = ForgejoClient()
    try:
        result = run_async(reloaded.status())
    finally:
        run_async(reloaded.close())

    assert reloaded.base_url == forgejo_target.url
    assert result["authenticated"] is True


def test_corrupt_config_file_is_ignored(forgejo_target, isolated_config, run_async,
                                        monkeypatch):
    """An unreadable config file is discarded, not fatal: the env still works."""
    client_mod.CONFIG_FILE.write_text("{not valid json")
    monkeypatch.setenv("FORGEJO_URL", forgejo_target.url)
    monkeypatch.setenv("FORGEJO_USERNAME", forgejo_target.username)
    monkeypatch.setenv("FORGEJO_PASSWORD", forgejo_target.password)

    client = ForgejoClient()
    try:
        assert run_async(client.status())["authenticated"] is True
    finally:
        run_async(client.close())


def test_a_dotenv_file_configures_the_client(forgejo_target, isolated_config,
                                             run_async, monkeypatch, tmp_path):
    """The whole configuration path works from a .env in the working directory.

    This is the live counterpart of ``tests/test_env.py``: there the loader is
    checked in isolation, here the values it loads are used to authenticate.
    """
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text(
        f"FORGEJO_URL={forgejo_target.url}\n"
        f"FORGEJO_USERNAME={forgejo_target.username}\n"
        f"FORGEJO_PASSWORD={forgejo_target.password}\n"
    )
    monkeypatch.chdir(workdir)
    for var in ("FORGEJO_URL", "FORGEJO_USERNAME", "FORGEJO_PASSWORD"):
        monkeypatch.delenv(var, raising=False)

    _env.load_env()
    client = ForgejoClient()
    try:
        assert run_async(client.status())["authenticated"] is True
    finally:
        run_async(client.close())


def test_a_real_environment_variable_beats_the_dotenv_file(
    forgejo_target, isolated_config, run_async, monkeypatch, tmp_path
):
    """A .env never overrides what the environment already says.

    The live counterpart of the precedence half of ``tests/test_env.py``: the
    .env here points at nothing, so if it won, nothing would authenticate.
    """
    workdir = tmp_path / "precedence"
    workdir.mkdir()
    (workdir / ".env").write_text("FORGEJO_URL=http://192.0.2.1:9\n")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("FORGEJO_URL", forgejo_target.url)
    monkeypatch.setenv("FORGEJO_USERNAME", forgejo_target.username)
    monkeypatch.setenv("FORGEJO_PASSWORD", forgejo_target.password)

    _env.load_env()
    client = ForgejoClient()
    try:
        assert client.base_url == forgejo_target.url
        assert run_async(client.status())["authenticated"] is True
    finally:
        run_async(client.close())


def test_bad_credentials_raise(client_factory, run_async):
    """A real rejected login raises AuthError rather than a silent failure."""
    client = client_factory(password="definitely-not-the-password")

    with pytest.raises(AuthError):
        run_async(client.login())


def test_a_failed_login_reports_no_version(client_factory, run_async):
    """status() on a rejected session reports neither authentication nor version."""
    client = client_factory(username="", password="")

    result = run_async(client.status())

    assert result["authenticated"] is False
    assert result["version"] is None
    assert "error" in result


# -------------------------------------------------------------- session cache
def test_cached_session_only_requires_the_url(client_factory, run_async):
    """A second client with no credentials reuses the persisted session."""
    run_async(client_factory().login())

    cached = client_factory(credentials=False)
    log = watch_requests(cached, run_async)

    assert log.find("POST", "/user/login") is None
    assert run_async(cached.list_repositories()) is not None


def test_non_forced_login_reuses_the_cache_without_credentials(
    client_factory, run_async
):
    """login() is satisfied by a valid cached session and does not re-post."""
    run_async(client_factory().login())

    cached = client_factory(credentials=False)
    run_async(cached.ensure())
    log = watch_requests(cached, run_async)
    result = run_async(cached.login())

    assert result["authenticated"] is True
    assert log.find("POST", "/user/login") is None


def test_an_invalid_cached_session_requires_credentials(
    client_factory, isolated_config, run_async
):
    """A stale session with no credentials to fall back on fails loudly."""
    client_mod.STATE_FILE.write_text(EMPTY_STATE)
    client = client_factory(credentials=False)

    with pytest.raises(AuthError) as exc:
        run_async(client.ensure())

    assert exc.value.code == "MISSING_CONFIG"
    assert "FORGEJO_USERNAME" in str(exc.value)
    assert "FORGEJO_PASSWORD" in str(exc.value)


def test_forced_login_requires_credentials_even_with_a_valid_session(
    client_factory, run_async
):
    """force=True must not silently accept the cached session instead."""
    run_async(client_factory().login())
    client = client_factory(credentials=False)

    with pytest.raises(AuthError) as exc:
        run_async(client.login(force=True))

    assert exc.value.code == "MISSING_CONFIG"


# ------------------------------------------------------- credential recovery
def test_credential_provider_recovers_from_a_real_rejection(
    client_factory, forgejo_target, run_async
):
    """A rejected password is replaced through the provider and the login retried."""
    client = client_factory(password="wrong-password")
    recoveries: list[str] = []

    def provide(error):
        recoveries.append(error.code)
        return forgejo_target.url, forgejo_target.username, forgejo_target.password

    client.set_credential_provider(provide)
    result = run_async(client.login())

    assert result["authenticated"] is True
    assert recoveries == ["AUTH_FAILED"]


def test_credential_provider_recovers_a_missing_configuration(
    client_factory, forgejo_target, isolated_config, run_async
):
    """A stale cache with no credentials is recovered through the provider."""
    client_mod.STATE_FILE.write_text(EMPTY_STATE)
    client = client_factory(credentials=False)
    recoveries: list[str] = []

    def provide(error):
        recoveries.append(error.code)
        return forgejo_target.url, forgejo_target.username, forgejo_target.password

    client.set_credential_provider(provide)
    repos = run_async(client.list_repositories())

    assert recoveries == ["MISSING_CONFIG"]
    assert isinstance(repos, list)


def test_the_provider_can_point_the_client_at_another_url(
    client_factory, forgejo_target, run_async
):
    """Recovering with a different URL disposes the context and rebuilds it.

    The offline test can only check that a new context was created. Here both
    URLs are real spellings of the same instance, so the rebuilt context has to
    actually reach the new one and log in through it.
    """
    if "localhost" not in forgejo_target.url:
        pytest.skip("needs a second spelling of the instance URL")
    first_url = forgejo_target.url.replace("localhost", "127.0.0.1")
    client = client_factory(base_url=first_url, password="wrong-password")
    recoveries: list[str] = []

    def provide(error):
        recoveries.append(error.code)
        return forgejo_target.url, forgejo_target.username, forgejo_target.password

    client.set_credential_provider(provide)
    result = run_async(client.login())

    assert recoveries == ["AUTH_FAILED"]
    assert result["authenticated"] is True
    assert client.base_url == forgejo_target.url != first_url


# ---------------------------------------------------------------- resilience
def test_an_expired_session_is_reestablished(live_client, run_async):
    """A session that dies mid-flight is re-established and the request retried.

    The client probes the session, the instance then really does invalidate it
    (a genuine logout, not a simulated one), and the next request comes back
    bounced to the login page. The client must log in again and complete the
    original request without the caller noticing.
    """
    log = watch_requests(live_client, run_async)
    expire_session_after_next_probe(live_client)

    # A route that requires a session: without one, Forgejo bounces it to the
    # login page, which is exactly what the client has to notice and undo.
    protected = live_client.profile.route("auth_probe")
    response = run_async(live_client._request("GET", protected))

    assert response.status == 200
    assert log.find("POST", "/user/login") is not None, (
        "the client should have re-authenticated after the bounce"
    )


def test_unreachable_instance_becomes_a_network_error(client_factory, run_async):
    """A refused connection is reported as NETWORK_ERROR, not a stack trace."""
    client = client_factory(base_url=f"http://127.0.0.1:{_closed_port()}")

    with pytest.raises(ForgejoError) as exc:
        run_async(client.list_repositories())

    assert exc.value.code == "NETWORK_ERROR"
    assert exc.value.status is None


def test_close_is_idempotent_against_a_live_session(live_client, run_async):
    """Closing a client that really opened a connection never raises."""
    run_async(live_client.ensure())

    run_async(live_client.close())
    run_async(live_client.close())  # second call is a no-op

    assert live_client._ctx is None
    assert live_client._pw is None
