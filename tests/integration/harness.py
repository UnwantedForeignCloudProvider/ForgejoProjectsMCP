"""Automation for the throwaway Forgejo instances the integration suite uses.

The suite is meant to run from nothing: this module starts a container for a
requested Forgejo version, waits for it to become healthy, creates an admin
user, and seeds a repository with issues and a milestone. Everything it creates
is disposable and local, and it tears down whatever it started.

Two escape hatches keep it usable during development:

* an instance that is *already* running for the requested version is reused and
  never torn down, so an edit-run loop does not pay the boot cost each time; and
* ``FORGEJO_TEST_URL`` points the suite at an externally managed instance, in
  which case no container is touched at all.

Repository, issue and milestone seeding goes through Forgejo's documented REST
API, which is stable across versions. Projects have no API at all -- they are
the reason this project exists -- so project seeding goes through
:class:`~forgejo_projects_mcp.client.ForgejoClient` itself. A failure while
seeding a project is therefore a genuine product failure, and the fixtures
report it as an error rather than a skip.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPOSE_FILE = Path(__file__).resolve().parents[1] / "composes" / "docker-compose.yaml"

# Defaults shared with tests/composes/README.md. Throwaway credentials for
# disposable local instances only.
DEFAULT_USERNAME = "testadmin"
DEFAULT_PASSWORD = "testadmin-password-123"  # noqa: S105 - test-instance default
DEFAULT_EMAIL = "testadmin@example.com"

# Base for the per-version host port, clear of Forgejo's own default 3000.
PORT_BASE = int(os.environ.get("FORGEJO_TEST_PORT_BASE", "3300"))

BOOT_TIMEOUT = float(os.environ.get("FORGEJO_TEST_BOOT_TIMEOUT", "180"))


class HarnessError(RuntimeError):
    """Raised when the throwaway instance could not be prepared."""


def docker_available() -> str | None:
    """Return why Docker cannot be used, or ``None`` when it can."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    probe = subprocess.run(
        ["docker", "compose", "version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        return "docker compose is unavailable (is the daemon running?)"
    return None


def port_for(version: str) -> int:
    """Host port for a version tag, so several versions can run at once.

    Forgejo numbers its releases 1.20, 1.21, then 7.0 onwards, so the major
    number identifies every release except the two in the 1.x line, which are
    told apart by their minor: 7-16 map to 3307-3316 and 1.20/1.21 to
    3320/3321. Requesting a future major 20 or 21 alongside a 1.x release is
    the only collision this can produce, and the suite rejects that pairing
    with a clear message rather than letting the ports clash.
    """
    parts = version.split(".")
    if not parts[0].isdigit():
        raise HarnessError(f"cannot derive a port from version {version!r}")
    major = int(parts[0])
    if major == 1:
        minor = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        return PORT_BASE + minor
    return PORT_BASE + major


@dataclass
class ForgejoInstance:
    """One throwaway Forgejo container, addressed by version tag."""

    version: str
    port: int = 0
    username: str = DEFAULT_USERNAME
    password: str = DEFAULT_PASSWORD
    email: str = DEFAULT_EMAIL
    # Set for an instance someone else runs: its URL is taken verbatim (it need
    # not even be local) and its lifecycle is never touched.
    url_override: str = ""
    # True when this process started the container and is responsible for it.
    started_here: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if not self.url_override:
            self.port = self.port or port_for(self.version)

    @property
    def managed(self) -> bool:
        """Whether this harness may start and stop the instance."""
        return not self.url_override

    # ------------------------------------------------------------- addressing
    @property
    def url(self) -> str:
        return self.url_override or f"http://localhost:{self.port}"

    @property
    def project_name(self) -> str:
        return f"forgejo-test-{self.version}"

    def _env(self) -> dict[str, str]:
        return {
            **os.environ,
            "FORGEJO_VERSION": self.version,
            "FORGEJO_PORT": str(self.port),
        }

    def _compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
            env=self._env(),
            capture_output=True,
            text=True,
            check=check,
        )

    # -------------------------------------------------------------- lifecycle
    def up(self) -> None:
        """Start the instance (or adopt a healthy one) and seed the admin user."""
        if not self.managed:
            raise HarnessError("an externally managed instance is not started here")
        if self.healthy():
            # Someone else owns this container; leave it running afterwards.
            self.ensure_admin()
            return
        try:
            self._compose("up", "-d", "--wait")
        except subprocess.CalledProcessError as exc:
            raise HarnessError(
                f"could not start Forgejo {self.version}: "
                f"{(exc.stderr or exc.stdout or '').strip()[:400]}"
            ) from exc
        self.started_here = True
        self.wait_healthy()
        self.ensure_admin()

    def down(self) -> None:
        """Stop and remove the instance, but only if this process started it."""
        if not self.started_here or not self.managed:
            return
        self._compose("down", "--remove-orphans", check=False)
        self.started_here = False

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}/api/healthz", timeout=2) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError, TimeoutError):
            return False

    def wait_healthy(self, timeout: float = BOOT_TIMEOUT) -> None:
        """Block until the instance answers its health check."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            time.sleep(1.0)
        raise HarnessError(
            f"Forgejo {self.version} did not become healthy at {self.url} "
            f"within {timeout:.0f}s"
        )

    def ensure_admin(self) -> None:
        """Create the admin user, tolerating one that already exists."""
        if self._can_authenticate():
            return
        self._compose(
            "exec",
            "-u",
            "git",
            "-T",
            "server",
            "forgejo",
            "admin",
            "user",
            "create",
            "--admin",
            "--username",
            self.username,
            "--password",
            self.password,
            "--email",
            self.email,
            "--must-change-password=false",
            check=False,
        )
        if not self._can_authenticate():
            raise HarnessError(
                f"could not authenticate as {self.username!r} on Forgejo "
                f"{self.version} after creating the admin user"
            )

    def _can_authenticate(self) -> bool:
        try:
            self.api("GET", "/user")
        except HarnessError:
            return False
        return True

    # -------------------------------------------------------------- REST API
    def api(self, method: str, path: str, payload: Any | None = None) -> Any:
        """Call the documented REST API as the admin user.

        Used only for seeding the parts of a fixture that Forgejo *does* expose
        (repositories, issues, milestones), so a seeding failure never hides
        behind the web-route scraping under test.
        """
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            f"{self.url}/api/v1{path}", data=data, method=method
        )
        request.add_header("Content-Type", "application/json")
        token = base64.b64encode(f"{self.username}:{self.password}".encode()).decode()
        request.add_header("Authorization", f"Basic {token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise HarnessError(f"{method} {path} -> HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise HarnessError(f"{method} {path} -> {type(exc).__name__}: {exc}") from exc

    @property
    def reported_version(self) -> str:
        """The version the instance reports over its REST API."""
        return str(self.api("GET", "/version")["version"])


@dataclass(frozen=True)
class SeededRepo:
    """A freshly created repository with predictable contents."""

    owner: str
    name: str
    issue_numbers: tuple[int, ...]
    milestone_id: int
    milestone_title: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


def seed_repository(
    instance: ForgejoInstance,
    name: str,
    *,
    issues: int = 4,
    milestone_title: str = "Sprint 1",
) -> SeededRepo:
    """Create a repository with issues and a milestone through the REST API.

    The first two issues are attached to the milestone, giving live tests a
    non-trivial filter to exercise without having to create state themselves.
    """
    instance.api("POST", "/user/repos", {"name": name, "auto_init": True})
    milestone = instance.api(
        "POST",
        f"/repos/{instance.username}/{name}/milestones",
        {"title": milestone_title},
    )
    numbers: list[int] = []
    for index in range(1, issues + 1):
        payload: dict[str, Any] = {
            "title": f"Seeded issue {index}",
            "body": f"Body of seeded issue {index}.",
        }
        if index <= 2:
            payload["milestone"] = milestone["id"]
        created = instance.api(
            "POST", f"/repos/{instance.username}/{name}/issues", payload
        )
        numbers.append(int(created["number"]))
    return SeededRepo(
        owner=instance.username,
        name=name,
        issue_numbers=tuple(numbers),
        milestone_id=int(milestone["id"]),
        milestone_title=milestone_title,
    )
