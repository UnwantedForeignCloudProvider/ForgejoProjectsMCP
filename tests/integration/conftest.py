"""Fixtures for the integration suite, which runs against real Forgejo instances.

The suite is opt-in and self-contained: name one or more Forgejo versions and it
starts a throwaway container per version, seeds an admin, a repository, issues
and a milestone, and runs every test against each of them.

    uv run pytest -m integration --forgejo-version 16
    uv run pytest -m integration --forgejo-version 1.20 --forgejo-version 16
    FORGEJO_TEST_VERSIONS=1.20,10,13,16 uv run pytest -m integration

Nothing runs by default: with no version requested and no ``FORGEJO_TEST_URL``,
every integration test skips, so a plain ``uv run pytest`` stays offline.

Pointing ``FORGEJO_TEST_URL`` at an instance you manage yourself uses that
instead of Docker. Such an instance is treated as *not* disposable: tests that
create or delete anything skip unless ``FORGEJO_TEST_ALLOW_WRITES=1`` says
otherwise, so the suite cannot damage a shared forge by accident.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import forgejo_projects_mcp.client as client_mod
from forgejo_projects_mcp.client import ForgejoClient

from .harness import (
    DEFAULT_PASSWORD,
    DEFAULT_USERNAME,
    ForgejoInstance,
    HarnessError,
    SeededRepo,
    docker_available,
    port_for,
    seed_repository,
)

_EXTERNAL = "external"


def pytest_collection_modifyitems(config, items) -> None:
    """Mark tests under tests/integration/ as ``integration``.

    The hook runs once for every collected item, so it must restrict itself to
    this directory rather than marking the offline suite too.
    """
    here = Path(__file__).parent
    for item in items:
        item_path = Path(str(getattr(item, "fspath", "")))
        if here == item_path.parent or here in item_path.parents:
            item.add_marker(pytest.mark.integration)


def _requested_versions(config) -> list[str]:
    """Versions to exercise, from the CLI option or FORGEJO_TEST_VERSIONS."""
    from_cli = [v.strip() for v in config.getoption("--forgejo-version") if v.strip()]
    if from_cli:
        return from_cli
    raw = os.environ.get("FORGEJO_TEST_VERSIONS", "")
    return [v.strip() for v in raw.split(",") if v.strip()]


def pytest_generate_tests(metafunc) -> None:
    """Run every integration test once per requested Forgejo version."""
    if "forgejo_target" not in metafunc.fixturenames:
        return
    if os.environ.get("FORGEJO_TEST_URL"):
        metafunc.parametrize(
            "forgejo_target", [_EXTERNAL], indirect=True, scope="session"
        )
        return
    versions = _requested_versions(metafunc.config)
    if not versions:
        skipped = pytest.param(
            "",
            marks=pytest.mark.skip(
                reason=(
                    "no Forgejo instance requested: pass --forgejo-version, set "
                    "FORGEJO_TEST_VERSIONS, or point FORGEJO_TEST_URL at an "
                    "instance you manage"
                )
            ),
        )
        metafunc.parametrize(
            "forgejo_target", [skipped], indirect=True, scope="session"
        )
        return
    _reject_port_collisions(versions)
    metafunc.parametrize(
        "forgejo_target",
        versions,
        indirect=True,
        scope="session",
        ids=[f"forgejo{v}" for v in versions],
    )


def _reject_port_collisions(versions: list[str]) -> None:
    """Fail loudly when two requested versions would share a host port."""
    by_port: dict[int, str] = {}
    for version in versions:
        port = port_for(version)
        if port in by_port:
            raise pytest.UsageError(
                f"Forgejo {version} and {by_port[port]} both map to host port "
                f"{port}; run them in separate sessions or set "
                f"FORGEJO_TEST_PORT_BASE for one of them."
            )
        by_port[port] = version


@pytest.fixture(scope="session")
def forgejo_target(request) -> Iterator[ForgejoInstance]:
    """A live Forgejo instance: started here, adopted, or externally managed."""
    spec = request.param
    if spec == _EXTERNAL:
        yield _external_instance()
        return
    reason = docker_available()
    if reason:
        pytest.skip(f"cannot start a Forgejo container: {reason}")
    instance = ForgejoInstance(version=spec)
    try:
        instance.up()
    except HarnessError as exc:
        pytest.fail(f"could not prepare Forgejo {spec}: {exc}")
    try:
        yield instance
    finally:
        if not request.config.getoption("--forgejo-keep"):
            instance.down()


def _external_instance() -> ForgejoInstance:
    """Describe an instance the caller manages, addressed by FORGEJO_TEST_URL."""
    url = os.environ["FORGEJO_TEST_URL"].rstrip("/")
    instance = ForgejoInstance(
        version=_EXTERNAL,
        url_override=url,
        username=os.environ.get("FORGEJO_TEST_USERNAME", DEFAULT_USERNAME),
        password=os.environ.get("FORGEJO_TEST_PASSWORD", DEFAULT_PASSWORD),
    )
    if not instance.healthy():
        pytest.skip(f"no Forgejo instance reachable at {url}")
    return instance


@pytest.fixture(scope="session")
def disposable(forgejo_target: ForgejoInstance) -> bool:
    """Whether the target may be written to freely."""
    if forgejo_target.managed:
        return True
    return os.environ.get("FORGEJO_TEST_ALLOW_WRITES") == "1"


@pytest.fixture
def writable(disposable: bool) -> None:
    """Skip a test that creates or deletes state on a non-disposable instance."""
    if not disposable:
        pytest.skip(
            "target is externally managed; set FORGEJO_TEST_ALLOW_WRITES=1 to "
            "let the suite create and delete data on it"
        )


@pytest.fixture(scope="session")
def seeded_repo(forgejo_target: ForgejoInstance, disposable: bool) -> SeededRepo:
    """A repository with issues and a milestone, created once per instance."""
    if not disposable:
        pytest.skip(
            "seeding a repository needs a disposable instance; set "
            "FORGEJO_TEST_ALLOW_WRITES=1 to allow it"
        )
    name = f"kanban-{uuid.uuid4().hex[:8]}"
    try:
        return seed_repository(forgejo_target, name)
    except HarnessError as exc:
        pytest.fail(f"could not seed {name} on {forgejo_target.url}: {exc}")


@pytest.fixture(scope="session")
def offset_repo(
    forgejo_target: ForgejoInstance, seeded_repo: SeededRepo
) -> SeededRepo:
    """A second seeded repository, where issue ids and numbers diverge.

    Issue numbers restart at 1 in every repository while issue *ids* keep
    counting across the whole instance, so the two are equal only in the first
    repository an instance creates. Anything that keys off one or the other
    looks correct against a single seeded repository and breaks everywhere
    else; this fixture is what tells the difference.
    """
    name = f"offset-{uuid.uuid4().hex[:8]}"
    try:
        return seed_repository(forgejo_target, name)
    except HarnessError as exc:
        pytest.fail(f"could not seed {name} on {forgejo_target.url}: {exc}")


@pytest.fixture
def run_async() -> Iterator[Callable[[Awaitable[Any]], Any]]:
    """Run coroutines for one test on a single event loop.

    A live test makes several client calls, and the client's asyncio primitives
    bind to the loop that first uses them -- so every call in a test (fixture
    setup and teardown included) has to share one loop rather than getting a
    fresh one from ``asyncio.run`` each time.
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop.run_until_complete
    finally:
        loop.close()


@pytest.fixture
def isolated_config(monkeypatch, tmp_path: Path) -> Path:
    """Redirect the session-state and config files into the test's tmp dir.

    A live test performs a real login, which persists both files. Redirecting
    them keeps the developer's own ``~/.config`` untouched and lets each test
    start from a known cache state.
    """
    monkeypatch.setattr(client_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(client_mod, "STATE_FILE", tmp_path / "storage_state.json")
    monkeypatch.setattr(client_mod, "CONFIG_FILE", tmp_path / "config.json")
    return tmp_path


@pytest.fixture
def client_factory(
    forgejo_target: ForgejoInstance,
    run_async: Callable[[Awaitable[Any]], Any],
    isolated_config: Path,
) -> Iterator[Callable[..., ForgejoClient]]:
    """Build extra clients that share this test's isolated config directory.

    Session-cache behavior can only be exercised with more than one client: one
    logs in and persists the state, a second is expected to reuse it. Every
    client built here is closed when the test ends.
    """
    built: list[ForgejoClient] = []

    def build(*, credentials: bool = True, **overrides: Any) -> ForgejoClient:
        client = ForgejoClient()
        client.base_url = overrides.pop("base_url", forgejo_target.url)
        client.username = forgejo_target.username if credentials else ""
        client.password = forgejo_target.password if credentials else ""
        for name, value in overrides.items():
            setattr(client, name, value)
        built.append(client)
        return client

    try:
        yield build
    finally:
        for client in built:
            run_async(client.close())


@pytest.fixture
def live_client(client_factory: Callable[..., ForgejoClient]) -> ForgejoClient:
    """A ForgejoClient pointed at the live instance, with an isolated cache."""
    return client_factory()


@pytest.fixture
def live_project(
    live_client: ForgejoClient,
    seeded_repo: SeededRepo,
    run_async: Callable[[Awaitable[Any]], Any],
    writable: None,
) -> Iterator[dict]:
    """A project board created for one test and deleted afterwards.

    Projects have no REST API, so this is created through the client under
    test: if board creation regresses, tests using this fixture fail loudly
    rather than skipping.
    """
    owner, repo = seeded_repo.owner, seeded_repo.name
    title = f"Board {uuid.uuid4().hex[:8]}"
    created = run_async(live_client.create_project(owner, repo, title, "seeded"))
    project = created.get("project")
    assert project, f"create_project returned no project: {created}"
    try:
        yield {"id": int(project["id"]), "title": title}
    finally:
        try:
            run_async(live_client.delete_project(owner, repo, int(project["id"])))
        except Exception:  # cleanup is best-effort; the instance is disposable
            pass
