# `forgejo_projects_mcp.cli`

Source: [`src/forgejo_projects_mcp/cli.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/cli.py)

The CLI is an argparse front end over the same MCP tools exposed by `server.py`. It does not open an MCP transport. Instead, it lists the registered tools, generates subcommands from their input schemas, and calls `mcp.call_tool` in-process.

Entry point: `forgejo-projects-cli`.

## Public functions

### `build_parser`

```python
build_parser(tools) -> argparse.ArgumentParser
```

Builds the root parser and one required subcommand per tool. For each tool it reads:

- `tool.name` for the subcommand;
- the first line of `tool.description` for subcommand help;
- `tool.input_schema["properties"]` for options; and
- `tool.input_schema["required"]` for required flags.

JSON-schema types are mapped as follows:

| Schema type | CLI behavior |
|---|---|
| `integer` | `int` |
| `number` | `float` |
| `boolean` | `_bool`, displayed as `true\|false` |
| `array` or `object` | `json.loads` on a JSON string |
| other / omitted | argparse's default string behavior |

Optional schema defaults are passed through to argparse.

### `_bool`

```python
_bool(value: str) -> bool
```

Returns `True` for `1`, `true`, `yes`, or `on`, ignoring case and surrounding whitespace. Other values return `False`.

### `_extract`

```python
_extract(result) -> str
```

Prefers a result's structured dictionary and serializes it as indented UTF-8 JSON. If no structured content exists, it returns the first textual content block, or the string `"null"` if neither is present.

### `_invoke`

```python
async _invoke(name: str, arguments: dict) -> tuple[str, bool]
```

Calls `mcp.call_tool(name, arguments)`, extracts the result, and returns `(output, is_error)`. Exceptions are converted to a JSON object such as `{"error": "[NOT_FOUND] ..."}`. The shared client is closed in a `finally` block after every CLI invocation.

### `main`

```python
main(argv: list[str] | None = None) -> int
```

Lists the registered tools, parses `argv` (or `sys.argv`), invokes the selected tool, prints its output to stdout, and returns:

- `0` for a successful call; or
- `1` when the tool raised an error.

When called as a script, the module raises `SystemExit(main())`.

## Examples

```bash
forgejo-projects-cli --help
forgejo-projects-cli list_projects --owner team --repo platform
forgejo-projects-cli bulk_read_issues \
  --owner team --repo platform --issue_numbers '[1, 2, 3]'
```

Collection options such as `--issue_numbers` and `--moves` must contain valid JSON. The CLI's complete command and option list is generated from the server tool definitions, so the [usage guide](../usage.md) is the behavioral reference and this page explains the adapter.

