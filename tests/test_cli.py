"""The argparse CLI mirrors the MCP tools and dispatches through them."""

import asyncio
import io
import json

import pytest
from conftest import FakeResponse, make_client

from forgejo_projects_mcp import cli, server
from forgejo_projects_mcp.client import AuthError, ForgejoError


def test_parser_has_a_subcommand_per_tool():
    tools = asyncio.run(server.mcp.list_tools())
    parser = cli.build_parser(tools)
    # argparse stores subcommands in the choices of the subparsers action
    sub = next(a for a in parser._actions if a.dest == "tool")
    assert set(sub.choices) == {t.name for t in tools}


def test_help_does_not_check_interactivity(monkeypatch, capsys):
    monkeypatch.setattr(
        cli,
        "_is_interactive",
        lambda: pytest.fail("help must not initialize interactive authentication"),
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["--help"])

    assert exc.value.code == 0
    assert "forgejo-projects-cli" in capsys.readouterr().out


def test_cli_dispatches_and_prints_json(monkeypatch, capsys):
    async def fake(owner, repo, state="open"):
        return [{"id": 1, "title": "A"}]

    monkeypatch.setattr(server.client, "list_projects", fake)
    rc = cli.main(["list_projects", "--owner", "o", "--repo", "r"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == {"count": 1, "projects": [{"id": 1, "title": "A"}]}


def test_cli_parses_json_list_argument(monkeypatch, capsys):
    seen = {}

    async def fake(owner, repo, numbers, state="all"):
        seen["numbers"] = numbers
        return [{"number": n, "title": "t", "state": "open", "milestone": None} for n in numbers]

    monkeypatch.setattr(server.client, "bulk_read_issues", fake)
    rc = cli.main(["bulk_read_issues", "--owner", "o", "--repo", "r",
                   "--issue_numbers", "[1, 2, 3]"])
    assert rc == 0
    assert seen["numbers"] == [1, 2, 3]      # parsed from JSON
    assert json.loads(capsys.readouterr().out)["count"] == 3


def test_cli_returns_nonzero_on_error(monkeypatch, capsys):
    async def boom(*a, **k):
        raise ForgejoError("nope", status=404, code="HTTP_404")

    monkeypatch.setattr(server.client, "create_project", boom)
    rc = cli.main(["create_project", "--owner", "o", "--repo", "r", "--title", "x"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert "NOT_FOUND" in out["error"]   # error is a "[CODE] message" string


def test_cli_credential_args_override_client(monkeypatch, capsys):
    client = make_client(lambda method, path, kw: FakeResponse(status=200))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    async def fake(owner, repo, state="open"):
        return []

    monkeypatch.setattr(server.client, "list_projects", fake)
    rc = cli.main([
        "--forgejo-url", "https://arg.test/",
        "--forgejo-username", "argu",
        "--forgejo-password", "argp",
        "list_projects", "--owner", "o", "--repo", "r",
    ])

    assert rc == 0
    assert client.base_url == "https://arg.test"   # trailing slash stripped
    assert client.username == "argu"
    assert client.password == "argp"


def test_cli_credential_args_work_after_tool_name(monkeypatch, capsys):
    client = make_client(lambda method, path, kw: FakeResponse(status=200))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)

    async def fake(owner, repo, state="open"):
        return []

    monkeypatch.setattr(server.client, "list_projects", fake)
    rc = cli.main([
        "list_projects", "--owner", "o", "--repo", "r",
        "--forgejo-url", "https://after.test",
    ])

    assert rc == 0
    assert client.base_url == "https://after.test"


def test_cli_password_stdin(monkeypatch, capsys):
    client = make_client(lambda method, path, kw: FakeResponse(status=200))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("from-stdin\n"))

    async def fake(owner, repo, state="open"):
        return []

    monkeypatch.setattr(server.client, "list_projects", fake)
    rc = cli.main([
        "--forgejo-password-stdin", "list_projects", "--owner", "o", "--repo", "r",
    ])

    assert rc == 0
    assert client.password == "from-stdin"


def test_cli_password_and_stdin_are_mutually_exclusive(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    with pytest.raises(SystemExit) as exc:
        cli.main([
            "--forgejo-password", "x", "--forgejo-password-stdin",
            "forgejo_status",
        ])
    assert exc.value.code != 0
    assert "mutually exclusive" in capsys.readouterr().err


def test_credential_args_not_forwarded_as_tool_arguments(monkeypatch, capsys):
    seen = {}

    async def fake(owner, repo, state="open"):
        seen["kwargs_ok"] = True
        return []

    client = make_client(lambda method, path, kw: FakeResponse(status=200))
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(server.client, "list_projects", fake)

    rc = cli.main([
        "--forgejo-url", "https://arg.test", "list_projects",
        "--owner", "o", "--repo", "r",
    ])

    assert rc == 0
    assert seen.get("kwargs_ok") is True   # call succeeded => no unexpected kwargs


def test_interactive_cli_prompts_for_missing_credentials(
    monkeypatch, capsys, tmp_state
):
    logged_in = False

    def handler(method, path, kw):
        nonlocal logged_in
        if method == "POST" and path == "/user/login":
            logged_in = True
            return FakeResponse(status=303)
        if path == "/user/settings":
            return FakeResponse(status=200 if logged_in else 302)
        return FakeResponse(status=200)

    client = make_client(handler, authed=False)
    client.base_url = ""
    client.username = ""
    client.password = ""
    client._ctx = None
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("https://forge.test\nuser\n"))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt, stream: "secret-password")

    rc = cli.main(["forgejo_status"])
    captured = capsys.readouterr()

    assert rc == 0
    assert json.loads(captured.out)["authenticated"] is True
    assert "Forgejo URL" in captured.err
    assert "Forgejo username" in captured.err
    assert "secret-password" not in captured.out
    assert "secret-password" not in captured.err
    assert client._credential_provider is None


def test_interactive_cli_retries_rejected_credentials(monkeypatch, capsys, tmp_state):
    posts = 0
    logged_in = False

    def handler(method, path, kw):
        nonlocal posts, logged_in
        if method == "POST" and path == "/user/login":
            posts += 1
            if kw["form"]["password"] == "good-password":
                logged_in = True
                return FakeResponse(status=303)
            return FakeResponse(status=200)
        if path == "/user/settings":
            return FakeResponse(status=200 if logged_in else 302)
        return FakeResponse(status=200)

    client = make_client(handler, authed=False)
    client.password = "bad-from-env"
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n\n\n\n"))
    passwords = iter(("still-bad", "good-password"))
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt, stream: next(passwords)
    )

    rc = cli.main(["forgejo_status"])
    captured = capsys.readouterr()

    assert rc == 0
    assert json.loads(captured.out)["authenticated"] is True
    assert captured.err.count("Forgejo authentication failed") == 2
    assert posts == 3


def test_interactive_cli_stops_after_three_attempts(monkeypatch, capsys):
    client = make_client(
        lambda method, path, kw: FakeResponse(
            status=302 if path == "/user/settings" else 200
        ),
        authed=False,
    )
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("\n\n" * 3))
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt, stream: "still-bad")

    rc = cli.main(["forgejo_status"])
    captured = capsys.readouterr()

    assert rc == 1
    assert json.loads(captured.out)["authenticated"] is False
    assert captured.err.count("Forgejo authentication failed") == 3


def test_noninteractive_cli_never_installs_prompt(monkeypatch, capsys):
    client = make_client(lambda method, path, kw: FakeResponse(status=200))
    client.base_url = ""
    monkeypatch.setattr(server, "client", client)
    monkeypatch.setattr(cli, "client", client)
    monkeypatch.setattr(cli, "_is_interactive", lambda: False)
    monkeypatch.setattr(
        cli.getpass,
        "getpass",
        lambda prompt, stream: pytest.fail("noninteractive CLI prompted"),
    )

    rc = cli.main(["forgejo_status"])

    assert rc == 1
    assert json.loads(capsys.readouterr().out)["authenticated"] is False


def test_credential_prompt_cancels_cleanly_on_eof(monkeypatch, capsys):
    monkeypatch.setattr(cli.client, "base_url", "")
    monkeypatch.setattr(cli.client, "username", "")
    monkeypatch.setattr(cli.client, "password", "")
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO(""))

    provider = cli._make_credential_provider()
    result = provider(AuthError("missing", code="MISSING_CONFIG"))

    assert result is None
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "authentication cancelled" in captured.err


def test_missing_password_prompt_does_not_repeat_configured_fields(
    monkeypatch, capsys
):
    monkeypatch.setattr(cli.client, "base_url", "https://forge.test")
    monkeypatch.setattr(cli.client, "username", "user")
    monkeypatch.setattr(cli.client, "password", "")
    monkeypatch.setattr(
        cli.getpass, "getpass", lambda prompt, stream: "secret-password"
    )

    provider = cli._make_credential_provider()
    result = provider(AuthError("missing", code="MISSING_CONFIG"))

    assert result == ("https://forge.test", "user", "secret-password")
    captured = capsys.readouterr()
    assert "Forgejo URL" not in captured.err
    assert "Forgejo username" not in captured.err
    assert "secret-password" not in captured.err
