# CLI

For harnesses that can't speak MCP, `forgejo-projects-cli` exposes **every tool as
a subcommand**, generated from the same tool definitions and dispatched
in-process (no MCP transport). It reads the same env vars, prints the JSON result
to **stdout**, logs to **stderr**, and exits **non-zero on error**.

When stdin and stderr are attached to a terminal, the CLI prompts for missing
credentials and retries rejected credentials up to three times. Password input
is hidden and remains in memory; the authenticated session state and the
non-secret config file are cached. Noninteractive invocations never prompt and
require the relevant environment, `.env`, CLI options, or a persisted config.
Use `forgejo-projects-cli authenticate --force true` to replace a cached session
explicitly.

## Credentials

Credentials come from the same env vars as the MCP server
(`FORGEJO_URL` / `FORGEJO_USERNAME` / `FORGEJO_PASSWORD`) and may also be passed
as options, accepted **either before or after the tool name**:

| Option | Notes |
|---|---|
| `--forgejo-url URL` | Overrides `FORGEJO_URL` and the persisted config. |
| `--forgejo-username NAME` | Overrides `FORGEJO_USERNAME` and the persisted config. |
| `--forgejo-password PASSWORD` | **Insecure**: visible in process lists and shell history. |
| `--forgejo-password-stdin` | Reads the password from the first line of stdin (preferred). |

Precedence is **CLI option > environment variable > persisted config file**.
`--forgejo-password` and `--forgejo-password-stdin` are mutually exclusive. After
the first successful login the URL and username are persisted, so later runs need
no configuration (see [Configuration](configuration.md#persisted-settings-and-session-cache)).

```bash
# Explicit credentials, password piped in rather than placed in argv:
printf '%s\n' "$FORGEJO_PW" | forgejo-projects-cli \
    --forgejo-url https://forge.example.com --forgejo-username me \
    --forgejo-password-stdin list_repositories
```

## Discover

```bash
forgejo-projects-cli --help          # list every subcommand (tool)
forgejo-projects-cli <tool> --help   # options for one tool
```

Options mirror each tool's parameters as `--flag`. List/object parameters take a
**JSON string**.

## Examples

```bash
forgejo-projects-cli list_repositories --query kanban
forgejo-projects-cli list_projects --owner o --repo r --state all
forgejo-projects-cli get_project   --owner o --repo r --project_id 3

forgejo-projects-cli create_project --owner o --repo r --title "Q3 Roadmap"
forgejo-projects-cli create_column  --owner o --repo r --project_id 3 --title "To Do"
forgejo-projects-cli create_issue   --owner o --repo r --title "Fix login" --project_id 3

forgejo-projects-cli move_card --owner o --repo r --project_id 3 --column_id 11 \
    --issue_numbers '[5]'
forgejo-projects-cli bulk_move_cards --owner o --repo r --project_id 3 \
    --moves '[{"issue_number":5,"column_id":11},{"issue_number":6,"column_id":12}]'
```

## Reading content — mind the cost

Prefer cheap options first — `list_projects` / `get_project` for structure, and
`bulk_read_issues` for lightweight summaries:

```bash
forgejo-projects-cli bulk_read_issues --owner o --repo r --issue_numbers '[5,6,7]'
```

The full readers (`read_card`, `read_column`, `read_milestone`, `read_project`)
fetch full content and are expensive — cap them with `--limit`/`--offset` and
narrow with `--state` / `--milestone` / `--project`:

```bash
forgejo-projects-cli read_project --owner o --repo r --project_id 3 \
    --state open --limit 20 --offset 0
```

## Errors and exit codes

A failed call prints `{"error": "[CODE] message"}` and exits non-zero. Parse the
JSON on stdout; ignore stderr for parsing (it carries logs). Set
`FORGEJO_MCP_LOG_LEVEL=DEBUG` for verbose diagnostics.
