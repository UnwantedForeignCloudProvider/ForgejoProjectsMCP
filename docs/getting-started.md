# Getting started

This guide takes a new installation from zero to a verified Forgejo connection and a first project operation.

## 1. Install the executable

The shortest path is a uv tool installation:

```bash
uv tool install forgejo-projects-mcp
```

For alternatives, including a local editable checkout, see [Installation](installation.md).

If the command is not found after installation, let uv update its shell integration and restart the shell:

```bash
uv tool update-shell
```

## 2. Configure Forgejo credentials

Create a `.env` in the directory from which the process will start:

```dotenv
FORGEJO_URL='https://forge.example.com'
FORGEJO_USERNAME='your-username'
FORGEJO_PASSWORD='your-password'
```

The package loads `.env` automatically from the current working directory or a parent directory. Existing environment variables and an MCP client's explicit `env` block take precedence. Keep this file private; it contains a real account password. As an alternative for a manual first run, the dedicated CLI can prompt for these values without saving the password.

For all optional settings and the session-cache location, see [Configuration](configuration.md).

## 3. Verify authentication

The CLI is convenient for a first smoke test because it prints a JSON result:

```bash
forgejo-projects-cli forgejo_status
```

A successful result looks like:

```json
{
  "authenticated": true,
  "instance": "https://forge.example.com",
  "username": "your-username",
  "state_file": "/home/you/.config/forgejo_projects_mcp/storage_state.json",
  "state_cached": true
}
```

`forgejo_status` authenticates if necessary, so the first call may prompt in an
interactive terminal, log in, and write the session cache. It makes at most
three prompted login attempts. A failed status response contains
`authenticated: false`, an explanatory `error` field, and a nonzero CLI exit
code. The stdio MCP server and redirected/piped CLI executions never prompt.

## 4. Find a repository

Use the repository search tool to identify the exact owner and repository name:

```bash
forgejo-projects-cli list_repositories --query platform --limit 10 --page 1
```

The result includes `full_name`, `owner`, `name`, description, and repository flags such as `private` and `archived`.

## 5. Inspect or create a board

List existing open projects:

```bash
forgejo-projects-cli list_projects --owner team --repo platform --state open
```

Create a board if needed:

```bash
forgejo-projects-cli create_project \
  --owner team \
  --repo platform \
  --title "Release board" \
  --description "Work for the next release"
```

The returned project object contains the numeric `id`. Use that ID to inspect the board and learn its column IDs:

```bash
forgejo-projects-cli get_project \
  --owner team \
  --repo platform \
  --project_id 12
```

A new board includes Forgejo's implicit default / uncategorized column. Add a named column when useful:

```bash
forgejo-projects-cli create_column \
  --owner team \
  --repo platform \
  --project_id 12 \
  --title "In progress" \
  --color '#e01e5a'
```

## 6. Create and move a card

Create an issue and place it on the board in one operation:

```bash
forgejo-projects-cli create_issue \
  --owner team \
  --repo platform \
  --title "Prepare release notes" \
  --body "Document the changes before tagging." \
  --project_id 12
```

Or attach an existing issue by its repository issue number:

```bash
forgejo-projects-cli add_issues_to_project \
  --owner team \
  --repo platform \
  --project_id 12 \
  --issue_numbers '[42, 43]'
```

Move cards by repository issue number. The order in the list becomes the order sent to the target column:

```bash
forgejo-projects-cli move_card \
  --owner team \
  --repo platform \
  --project_id 12 \
  --column_id 8 \
  --issue_numbers '[42, 43]'
```

## 7. Register the MCP server

Use the executable installed by uv in your MCP client's local-server configuration. A generic JSON configuration is:

```json
{
  "mcpServers": {
    "forgejo-projects-mcp": {
      "command": "forgejo-projects-mcp",
      "env": {
        "FORGEJO_URL": "https://forge.example.com",
        "FORGEJO_USERNAME": "your-username",
        "FORGEJO_PASSWORD": "your-password"
      }
    }
  }
}
```

Prefer the MCP client's secret-management mechanism when it has one. Do not place credentials in a committed project configuration. If the MCP client does not inherit your PATH, replace `forgejo-projects-mcp` with the absolute path reported by `which forgejo-projects-mcp` or `where forgejo-projects-mcp` on Windows.

The package also documents ready-to-copy examples for Claude Code, Codex, Qwen Code, OpenClaw, opencode, and Hermes in the [README](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/README.md#agent-installation).

## Common first-run problems

### `MISSING_CONFIG`

Set `FORGEJO_URL`. Also set `FORGEJO_USERNAME` and `FORGEJO_PASSWORD` when no
valid cached session exists or a forced login is requested. The interactive CLI
can collect missing values; MCP clients and noninteractive scripts must provide
them through the environment or `.env`.

### The password is changed but the old account still works

The cached session may still be valid. Force a fresh login:

```bash
forgejo-projects-cli authenticate --force true
```

### `NETWORK_ERROR`

Check the URL, DNS, TLS certificate, VPN, proxy, and whether the Forgejo instance is reachable from the process environment. The client does not use `/api/v1` for Projects operations.

### A project or column ID is unknown

Call `get_project` and use its `id` and `columns[].id` values. Do not substitute an issue number for a project or column ID.

## Safe verification checklist

Before using the server on important work:

1. run `forgejo_status`;
2. list the target repository and verify its `full_name`;
3. use a throwaway project and issue;
4. create, read, move, detach, and delete only the test objects; and
5. confirm the results in Forgejo's web UI.

The automated test suite is offline and does not replace a live-instance compatibility check.
