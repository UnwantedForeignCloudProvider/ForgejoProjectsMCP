"""The command-line interface, driven against a live instance.

``tests/test_cli.py`` checks the CLI's plumbing with the client stubbed out.
These run the same entry point for real: credentials off the command line or a
prompt, a tool dispatched over the web routes, and JSON on stdout with an exit
code a shell can branch on. Two of them go further and run the CLI as a separate
process, which is the only way to test what a user actually types.

``cli.main`` owns its event loop and closes the client when it returns, so every
test here uses a client of its own rather than the shared ``live_client``.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from forgejo_projects_mcp import cli, server

from .helpers import unique

CLI_MODULE = "forgejo_projects_mcp.cli"


@pytest.fixture
def run_cli(client_factory, monkeypatch, isolated_config):
    """Run ``cli.main`` against the live instance with a client of its own.

    ``credentials`` decides whether the client starts already configured, so a
    test can force the CLI to get them from arguments, stdin or a prompt.
    """

    def run(argv, *, credentials=True, interactive=False, **overrides):
        client = client_factory(credentials=credentials, **overrides)
        monkeypatch.setattr(server, "client", client)
        monkeypatch.setattr(cli, "client", client)
        monkeypatch.setattr(cli, "_is_interactive", lambda: interactive)
        return cli.main(argv), client

    return run


def printed(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# ---------------------------------------------------------------- dispatching
def test_the_cli_dispatches_a_tool_and_prints_json(
    run_cli, seeded_repo, live_project, capsys
):
    """The everyday case: a subcommand, real work, JSON on stdout, exit 0."""
    rc, _ = run_cli(
        ["list_projects", "--owner", seeded_repo.owner, "--repo", seeded_repo.name]
    )

    out = printed(capsys)
    assert rc == 0
    assert out["count"] == len(out["projects"])
    assert live_project["id"] in [p["id"] for p in out["projects"]]


def test_the_cli_parses_a_json_list_argument(run_cli, seeded_repo, capsys):
    """Array-typed options are read as JSON, not as a bare string."""
    numbers = list(seeded_repo.issue_numbers[:2])

    rc, _ = run_cli([
        "bulk_read_issues", "--owner", seeded_repo.owner, "--repo", seeded_repo.name,
        "--issue_numbers", json.dumps(numbers),
    ])

    out = printed(capsys)
    assert rc == 0
    assert out["count"] == len(numbers)
    assert [i["number"] for i in out["issues"]] == numbers


def test_the_cli_reports_a_real_failure_with_a_nonzero_exit(
    run_cli, seeded_repo, capsys
):
    """A genuine 404 becomes an error payload and a shell-visible failure."""
    rc, _ = run_cli([
        "get_project", "--owner", seeded_repo.owner, "--repo", seeded_repo.name,
        "--project_id", "999999",
    ])

    out = printed(capsys)
    assert rc == 1
    assert "NOT_FOUND" in out["error"]


def test_a_write_tool_works_from_the_command_line(run_cli, seeded_repo, capsys,
                                                  writable):
    """Writes go through the same CSRF handling when driven by the CLI."""
    title = unique("CLI milestone")

    rc, _ = run_cli([
        "create_milestone", "--owner", seeded_repo.owner, "--repo", seeded_repo.name,
        "--title", title,
    ])
    created = printed(capsys)
    assert rc == 0
    assert created["created"] is True

    milestone_id = str(created["milestone"]["id"])
    rc, _ = run_cli([
        "delete_milestone", "--owner", seeded_repo.owner, "--repo", seeded_repo.name,
        "--milestone_id", milestone_id,
    ])
    assert rc == 0


# --------------------------------------------------------------- credentials
def test_credential_arguments_authenticate_the_session(
    run_cli, forgejo_target, capsys
):
    """URL, username and password given as options are what get used."""
    rc, client = run_cli(
        [
            "--forgejo-url", forgejo_target.url + "/",  # trailing slash is stripped
            "--forgejo-username", forgejo_target.username,
            "--forgejo-password", forgejo_target.password,
            "forgejo_status",
        ],
        credentials=False,
        base_url="",
    )

    assert rc == 0
    assert printed(capsys)["authenticated"] is True
    assert client.base_url == forgejo_target.url


def test_credential_arguments_work_after_the_tool_name(
    run_cli, forgejo_target, capsys
):
    """The connection options are accepted on either side of the subcommand."""
    rc, client = run_cli(
        [
            "forgejo_status",
            "--forgejo-url", forgejo_target.url,
            "--forgejo-username", forgejo_target.username,
            "--forgejo-password", forgejo_target.password,
        ],
        credentials=False,
        base_url="",
    )

    assert rc == 0
    assert printed(capsys)["authenticated"] is True
    assert client.base_url == forgejo_target.url


def test_the_password_can_be_read_from_stdin(
    run_cli, forgejo_target, monkeypatch, capsys
):
    """The recommended way to pass a password: never in the process list."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(forgejo_target.password + "\n"))

    # Everything is configured except the password, which arrives on stdin.
    rc, client = run_cli(["--forgejo-password-stdin", "forgejo_status"], password="")

    assert rc == 0
    assert printed(capsys)["authenticated"] is True
    assert client.password == forgejo_target.password


def test_credential_arguments_are_not_forwarded_to_the_tool(
    run_cli, seeded_repo, forgejo_target, capsys
):
    """Connection options must not leak into the tool's argument set."""
    rc, _ = run_cli([
        "--forgejo-url", forgejo_target.url,
        "list_projects", "--owner", seeded_repo.owner, "--repo", seeded_repo.name,
    ])

    assert rc == 0
    assert "projects" in printed(capsys)


# -------------------------------------------------------------- interactively
def test_an_interactive_run_prompts_for_missing_credentials(
    run_cli, forgejo_target, monkeypatch, capsys
):
    """A terminal user is asked for what is missing, and the login really works."""
    monkeypatch.setattr(
        cli.sys, "stdin",
        io.StringIO(f"{forgejo_target.url}\n{forgejo_target.username}\n"),
    )
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt, stream: forgejo_target.password
    )

    rc, client = run_cli(
        ["forgejo_status"], credentials=False, base_url="", interactive=True
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["authenticated"] is True
    assert "Forgejo URL" in captured.err
    assert "Forgejo username" in captured.err
    assert forgejo_target.password not in captured.out
    assert forgejo_target.password not in captured.err
    assert client._credential_provider is None  # uninstalled again afterwards


def test_an_interactive_run_retries_a_password_the_instance_rejected(
    run_cli, forgejo_target, monkeypatch, capsys
):
    """A real rejection is reported and re-prompted until it is accepted."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n\n\n\n"))
    passwords = iter(("still-wrong", forgejo_target.password))
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt, stream: next(passwords)
    )

    rc, _ = run_cli(
        ["forgejo_status"], interactive=True, password="wrong-from-the-environment"
    )

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["authenticated"] is True
    assert captured.err.count("Forgejo authentication failed") == 2


def test_an_interactive_run_gives_up_after_three_rejections(
    run_cli, monkeypatch, capsys
):
    """Wrong credentials do not loop forever against the instance."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n\n" * 3))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt, stream: "still-wrong")

    rc, _ = run_cli(["forgejo_status"], interactive=True, password="wrong")

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out)["authenticated"] is False
    assert captured.err.count("Forgejo authentication failed") == 3


def test_a_noninteractive_run_never_prompts(run_cli, monkeypatch, capsys):
    """Piped or scripted invocations fail cleanly instead of blocking on input."""
    monkeypatch.setattr(
        cli.getpass, "getpass",
        lambda prompt, stream: pytest.fail("a non-interactive CLI must not prompt"),
    )

    rc, _ = run_cli(["forgejo_status"], credentials=False, base_url="")

    assert rc == 1
    assert printed(capsys)["authenticated"] is False


def test_a_prompt_only_asks_for_what_is_missing(
    run_cli, forgejo_target, monkeypatch, capsys
):
    """With the URL and username already configured, only the password is asked."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt, stream: forgejo_target.password
    )

    rc, _ = run_cli(["forgejo_status"], interactive=True, password="")

    captured = capsys.readouterr()
    assert rc == 0
    assert json.loads(captured.out)["authenticated"] is True
    assert "Forgejo URL" not in captured.err
    assert "Forgejo username" not in captured.err


def test_a_cancelled_prompt_stops_cleanly(run_cli, monkeypatch, capsys):
    """Ctrl-D at the prompt aborts without a traceback and without retrying."""
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))

    def eof(prompt, stream):
        raise EOFError

    monkeypatch.setattr(cli.getpass, "getpass", eof)

    rc, _ = run_cli(
        ["forgejo_status"], credentials=False, base_url="", interactive=True
    )

    captured = capsys.readouterr()
    assert rc == 1
    assert json.loads(captured.out)["authenticated"] is False
    assert "authentication cancelled" in captured.err
    assert "Traceback" not in captured.err


# ------------------------------------------------------------- as a subprocess
def _subprocess_env(forgejo_target, home: Path) -> dict[str, str]:
    """A clean environment: this instance's settings and nothing inherited."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGEJO_")}
    env["XDG_CONFIG_HOME"] = str(home)
    env["FORGEJO_URL"] = forgejo_target.url
    env["FORGEJO_USERNAME"] = forgejo_target.username
    env["FORGEJO_PASSWORD"] = forgejo_target.password
    return env


def _run(args: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """Run the CLI with no terminal attached.

    stdin is closed deliberately: inherited from a terminal the CLI would decide
    it is interactive and block on a credential prompt, so the test would pass
    or hang depending on how the suite was launched.
    """
    return subprocess.run(
        args,
        env=env,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_the_cli_runs_as_a_real_process(forgejo_target, seeded_repo, tmp_path):
    """The whole thing, the way a user runs it: a process, env vars, stdout.

    Nothing in this test is patched -- a separate interpreter starts the CLI,
    reads its configuration from the environment, talks to the instance and
    exits. It runs in an empty directory so no stray ``.env`` is picked up.
    """
    env = _subprocess_env(forgejo_target, tmp_path / "config")
    result = _run(
        [sys.executable, "-m", CLI_MODULE, "list_repositories",
         "--query", seeded_repo.name],
        env, tmp_path,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["repositories"][0]["full_name"] == seeded_repo.full_name


def test_the_installed_console_script_works(forgejo_target, tmp_path):
    """The entry point declared in pyproject.toml is the one users invoke."""
    script = shutil.which(
        "forgejo-projects-cli", path=str(Path(sys.executable).parent)
    ) or shutil.which("forgejo-projects-cli")
    if script is None:
        pytest.skip("forgejo-projects-cli is not installed in this environment")

    env = _subprocess_env(forgejo_target, tmp_path / "config")
    result = _run([script, "forgejo_status"], env, tmp_path)

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is True
    assert payload["version"]
    assert forgejo_target.password not in result.stdout + result.stderr


def test_the_subprocess_persists_and_reuses_its_session(forgejo_target, tmp_path):
    """A second run reuses the session the first one cached, with no password."""
    home = tmp_path / "config"
    env = _subprocess_env(forgejo_target, home)
    first = _run([sys.executable, "-m", CLI_MODULE, "forgejo_status"], env, tmp_path)
    assert first.returncode == 0, first.stderr

    without_password = {k: v for k, v in env.items() if k != "FORGEJO_PASSWORD"}
    without_password.pop("FORGEJO_USERNAME", None)
    without_password.pop("FORGEJO_URL", None)
    second = _run(
        [sys.executable, "-m", CLI_MODULE, "forgejo_status"], without_password, tmp_path
    )

    assert second.returncode == 0, second.stderr
    payload = json.loads(second.stdout)
    assert payload["authenticated"] is True
    assert payload["state_cached"] is True
    assert payload["config_cached"] is True


def test_the_subprocess_fails_cleanly_without_configuration(tmp_path):
    """With nothing configured the CLI exits non-zero and says why, in JSON."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGEJO_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    result = _run([sys.executable, "-m", CLI_MODULE, "forgejo_status"], env, tmp_path)

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["authenticated"] is False
    assert "FORGEJO_URL" in payload["error"]


def test_the_subprocess_help_lists_every_tool(tmp_path):
    """--help works with nothing configured and names the tools it can run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("FORGEJO_")}
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")

    result = _run([sys.executable, "-m", CLI_MODULE, "--help"], env, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "forgejo-projects-cli" in result.stdout
    for tool in ("forgejo_status", "list_projects", "move_card", "read_project"):
        assert tool in result.stdout


def test_the_subprocess_rejects_two_password_sources(forgejo_target, tmp_path):
    """--forgejo-password and --forgejo-password-stdin cannot be combined."""
    env = _subprocess_env(forgejo_target, tmp_path / "config")

    result = _run(
        [sys.executable, "-m", CLI_MODULE, "--forgejo-password", "x",
         "--forgejo-password-stdin", "forgejo_status"],
        env, tmp_path,
    )

    assert result.returncode != 0
    assert "mutually exclusive" in result.stderr
