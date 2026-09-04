"""Argparse CLI over the same tools the MCP server exposes.

For harnesses that can't speak MCP: every registered tool becomes a subcommand
whose options are generated from the tool's input schema, and each invocation is
dispatched in-process through ``mcp.call_tool`` (no MCP transport involved). The
JSON result is printed to stdout; the exit code is non-zero when the result is an
error payload.

    forgejo-projects-cli --help
    forgejo-projects-cli list_repositories --query kanban
    forgejo-projects-cli read_project --owner o --repo r --project_id 3 --state open
    forgejo-projects-cli bulk_move_cards --owner o --repo r --project_id 3 \
        --moves '[{"issue_number": 5, "column_id": 12}]'

Credentials come from the same env vars as the server
(FORGEJO_URL / FORGEJO_USERNAME / FORGEJO_PASSWORD). Interactive terminal
invocations request missing or rejected values without persisting the password.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from typing import Any

from .client import AuthError, CredentialProvider
from .server import client, mcp

_MAX_AUTH_ATTEMPTS = 3


def _bool(value: str) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _add_argument(parser: argparse.ArgumentParser, name: str, spec: dict, required: bool) -> None:
    kind = spec.get("type")
    help_text = spec.get("description", "") or ""
    kwargs: dict[str, Any] = {"required": required, "help": help_text}
    if kind == "integer":
        kwargs["type"] = int
    elif kind == "number":
        kwargs["type"] = float
    elif kind == "boolean":
        kwargs["type"] = _bool
        kwargs["metavar"] = "true|false"
    elif kind in ("array", "object"):
        kwargs["type"] = json.loads
        kwargs["help"] = (help_text + " (JSON)").strip()
    if not required and "default" in spec:
        kwargs["default"] = spec["default"]
    parser.add_argument(f"--{name}", **kwargs)


def build_parser(tools) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forgejo-projects-cli",
        description="Manage Forgejo Projects/Kanban from the command line "
        "(same tools as the MCP server).",
    )
    subparsers = parser.add_subparsers(dest="tool", required=True, metavar="TOOL")
    for tool in sorted(tools, key=lambda t: t.name):
        doc = (tool.description or "").strip()
        sub = subparsers.add_parser(
            tool.name, help=doc.split("\n", 1)[0], description=doc
        )
        schema = tool.input_schema or {}
        required = set(schema.get("required", []))
        for pname, pspec in schema.get("properties", {}).items():
            _add_argument(sub, pname, pspec, pname in required)
    return parser


def _extract(result) -> str:
    structured = getattr(result, "structured_content", None)
    if isinstance(structured, dict):
        return json.dumps(structured, indent=2, ensure_ascii=False, default=str)
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if text:
            return text
    return "null"


def _read_text(prompt: str, current: str = "") -> str:
    default = f" [{current}]" if current else ""
    print(f"{prompt}{default}: ", end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if not value:
        raise EOFError
    entered = value.rstrip("\r\n").strip()
    return entered or current


def _make_credential_provider() -> CredentialProvider:
    attempts = 0

    def provide(error: AuthError) -> tuple[str, str, str] | None:
        nonlocal attempts
        if attempts >= _MAX_AUTH_ATTEMPTS:
            return None
        attempts += 1
        try:
            if error.code == "AUTH_FAILED":
                print(
                    "Forgejo authentication failed; enter updated credentials.",
                    file=sys.stderr,
                )
                base_url = _read_text("Forgejo URL", client.base_url)
                username = _read_text("Forgejo username", client.username)
                password = getpass.getpass("Forgejo password: ", stream=sys.stderr)
                return base_url, username, password

            base_url = client.base_url
            username = client.username
            password = client.password
            if not base_url:
                base_url = _read_text("Forgejo URL")
            if not username:
                username = _read_text("Forgejo username")
            if not password:
                password = getpass.getpass("Forgejo password: ", stream=sys.stderr)
            return base_url, username, password
        except (EOFError, KeyboardInterrupt):
            print("\nForgejo authentication cancelled.", file=sys.stderr)
            return None

    return provide


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


async def _invoke(name: str, arguments: dict) -> tuple[str, bool]:
    # Tool failures surface as raised ToolErrors (MCP isError). Convert them to a
    # JSON error payload on stdout + a non-zero exit code for the harness.
    try:
        try:
            result = await mcp.call_tool(name, arguments)
            output = _extract(result)
            status = None
            if name == "forgejo_status":
                try:
                    status = json.loads(output)
                except json.JSONDecodeError:
                    pass
            status_failed = (
                name == "forgejo_status"
                and isinstance(status, dict)
                and status.get("authenticated") is False
            )
            return output, status_failed
        except Exception as e:  # CLI boundary: report, never crash
            return json.dumps({"error": str(e)}, ensure_ascii=False), True
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    tools = asyncio.run(mcp.list_tools())
    namespace = build_parser(tools).parse_args(argv)
    arguments = {
        k: v for k, v in vars(namespace).items() if k != "tool" and v is not None
    }
    provider = _make_credential_provider() if _is_interactive() else None
    client.set_credential_provider(provider)
    try:
        output, is_error = asyncio.run(_invoke(namespace.tool, arguments))
    finally:
        client.set_credential_provider(None)
    print(output)
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
