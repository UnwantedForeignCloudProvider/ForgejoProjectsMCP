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
(FORGEJO_URL / FORGEJO_USERNAME / FORGEJO_PASSWORD).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from .server import client, mcp


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


async def _invoke(name: str, arguments: dict) -> tuple[str, bool]:
    # Tool failures surface as raised ToolErrors (MCP isError). Convert them to a
    # JSON error payload on stdout + a non-zero exit code for the harness.
    try:
        try:
            result = await mcp.call_tool(name, arguments)
            return _extract(result), False
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
    output, is_error = asyncio.run(_invoke(namespace.tool, arguments))
    print(output)
    return 1 if is_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
