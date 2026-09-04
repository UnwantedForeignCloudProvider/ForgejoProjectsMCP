# Usage

The package exposes the same operations through an MCP stdio server and a generated CLI. MCP clients should use the tools by name; shell users and harnesses can use the equivalent subcommands.

## Registering the MCP server

Install the package and make sure `forgejo-projects-mcp` is on PATH. Add a local stdio server to your MCP client:

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

If the client does not inherit your shell PATH, use the absolute executable path. See [Getting started](getting-started.md) for client-specific registration examples.

## Using the CLI

List all generated commands:

```bash
forgejo-projects-cli --help
```

Inspect one command and its generated options:

```bash
forgejo-projects-cli read_project --help
```

Scalar options use ordinary flags. Arrays and objects are JSON strings:

```bash
forgejo-projects-cli bulk_read_issues \
  --owner team \
  --repo platform \
  --issue_numbers '[12, 13, 14]' \
  --state open

forgejo-projects-cli bulk_move_cards \
  --owner team \
  --repo platform \
  --project_id 12 \
  --moves '[{"issue_number": 12, "column_id": 8}, {"issue_number": 13, "column_id": 9}]'
```

Boolean flags accept `1`, `true`, `yes`, and `on` (case-insensitive) as true; any other value is false. The CLI prints a JSON result to stdout and returns exit code `1` when the MCP call fails.

## Recommended workflow

1. Call `forgejo_status` or the CLI equivalent.
2. Call `list_repositories` if the repository is not already known.
3. Call `list_projects` with `state="all"` when a closed board may be relevant.
4. Call `get_project` to obtain project and column IDs.
5. Use the smallest operation that answers the task.
6. Use `read_card` or a full reader only when body/comment content is needed.
7. Re-read the board after destructive or structural changes when confirmation matters.

Use a throwaway repository while validating compatibility with a new Forgejo release.

## Tool reference

The following table is the complete MCP surface. Required arguments have no default. Optional arguments and defaults are shown explicitly.

### Session and discovery

| Tool | Arguments | Result |
|---|---|---|
| `forgejo_status` | none | Authentication status, instance, username, and cache state. Errors are returned as `authenticated: false`. |
| `authenticate` | `force: bool = false` | Logs in and caches the session. `force=true` skips the existing cache. |
| `list_repositories` | `query: str = ""`, `limit: int = 50`, `page: int = 1` | `{count, repositories}`. Each repository includes `full_name`, `owner`, `name`, `description`, `private`, `archived`, `empty`, and `fork`. |

### Projects

| Tool | Arguments | Result / behavior |
|---|---|---|
| `list_projects` | `owner`, `repo`, `state: open\|closed\|all = open` | `{count, projects}` where each project has `id` and `title`. `all` combines separate open and closed requests. |
| `create_project` | `owner`, `repo`, `title`, `description: str = ""`, `card_type: text\|images_and_text = text` | `{created, project}`. The new project ID is recovered by reading the projects page. |
| `get_project` | `owner`, `repo`, `project_id` | Board `{id, title, columns}`. Each column contains `id`, `title`, and cards with `issue_id`, repository `number`, and `title`. |
| `update_project` | `owner`, `repo`, `project_id`, `title: str\|null = null`, `description: str\|null = null`, `card_type: str\|null = null` | `{updated, project_id}`. Title and card type are read from the edit form when omitted. The current implementation sends an empty description when `description` is omitted, so provide the desired description if it must be retained. |
| `close_project` | `owner`, `repo`, `project_id` | Archives the project and returns `{close: true, project_id}`. |
| `reopen_project` | `owner`, `repo`, `project_id` | Reopens the project and returns `{open: true, project_id}`. |
| `delete_project` | `owner`, `repo`, `project_id` | Permanently deletes the project. Issues survive. Returns `{deleted: true, project_id}`. |

### Columns

| Tool | Arguments | Result / behavior |
|---|---|---|
| `create_column` | `owner`, `repo`, `project_id`, `title`, `color: str = ""` | `{created, column}`. `color` may be a hex value such as `#e01e5a`. |
| `edit_column` | `owner`, `repo`, `project_id`, `column_id`, `title: str\|null = null`, `color: str\|null = null` | `{updated, column_id}`. Only supplied fields are sent. |
| `delete_column` | `owner`, `repo`, `project_id`, `column_id` | `{deleted, column_id}`. Cards return to the default/uncategorized location. The default column cannot be deleted; set another default first. |
| `set_default_column` | `owner`, `repo`, `project_id`, `column_id` | `{default_column, project_id}`. New attached cards land in this column. |

### Issues and cards

| Tool | Arguments | Result / behavior |
|---|---|---|
| `create_issue` | `owner`, `repo`, `title`, `body: str = ""`, `project_id: int\|null = null`, `milestone_id: int\|null = null`, `label_ids: list[int]\|null = null`, `assignee_ids: list[int]\|null = null` | Creates an issue. A project ID places it on that board; optional numeric milestone, label, and assignee IDs are passed to Forgejo. Returns `{created, number, title}`. |
| `add_issues_to_project` | `owner`, `repo`, `project_id`, `issue_numbers: list[int]` | Attaches existing repository issues as cards and returns `{attached, project_id}`. New attachments use the project's default column. |
| `remove_issues_from_project` | `owner`, `repo`, `issue_numbers: list[int]` | Detaches cards from a project while leaving the issues intact. Returns `{detached}`. |
| `move_card` | `owner`, `repo`, `project_id`, `column_id`, `issue_numbers: list[int]` | Moves one or more attached cards to one column. List order controls the submitted sorting order. |
| `delete_issue` | `owner`, `repo`, `number` | Permanently deletes the issue, not merely the card. Use `remove_issues_from_project` to preserve the issue. |

Issue arguments use repository numbers such as `42`, not internal issue IDs. The client fetches each issue page to resolve the ID required by Forgejo's project routes.

### Bulk and full reads

| Tool | Arguments | Result / behavior |
|---|---|---|
| `bulk_move_cards` | `owner`, `repo`, `project_id`, `moves: list[{issue_number, column_id}]` | Moves many cards concurrently. Items targeting the same column preserve input order. Returns `{moved_count, columns}`. |
| `bulk_read_issues` | `owner`, `repo`, `issue_numbers`, `state: open\|closed\|all = all` | Lightweight summaries only: `{count, error_count, issues, errors}`. Body and comments are omitted. Individual failures are reported in `errors`. |
| `read_card` | `owner`, `repo`, `number` | **Expensive.** Full issue content: `number`, `title`, `state`, `body`, `milestone`, and `comments`. |
| `read_column` | `owner`, `repo`, `project_id`, `column_id`, `state = all`, `milestone: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content for one column plus `{filters, total, returned, truncated, error_count, issues}`. |
| `read_milestone` | `owner`, `repo`, `milestone_id`, `state = all`, `project: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content for a milestone plus pagination and filter metadata. |
| `read_project` | `owner`, `repo`, `project_id`, `state = all`, `milestone: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content grouped under every board column plus pagination and filter metadata. |

All full readers accept direct numeric IDs for their optional `milestone` or `project` filter. A reader's `limit` applies after the matching card/issue numbers have been collected, and `offset` is zero-based. Use `truncated=true` to know that another page remains.

Full issue content has this shape:

```json
{
  "number": 42,
  "title": "Prepare release notes",
  "state": "open",
  "body": "Document the changes before tagging.",
  "milestone": {"id": 7, "title": "v1.2"},
  "comments": [
    {"author": "maintainer", "body": "Please include migration notes."}
  ]
}
```

`milestone` is `null` when there is no milestone. A reader can return partial results: `error_count` reports issue pages that failed, and the failed issue is retained with an `error` field where the client can represent it.

### Milestones

| Tool | Arguments | Result / behavior |
|---|---|---|
| `list_milestones` | `owner`, `repo`, `state: open\|closed\|all = open` | `{count, milestones}` with `{id, title}` entries. `all` merges open and closed pages. |
| `create_milestone` | `owner`, `repo`, `title`, `description: str = ""`, `deadline: str = ""` | Creates a milestone. `deadline` is `YYYY-MM-DD`. Returns `{created, milestone}`. |
| `edit_milestone` | `owner`, `repo`, `milestone_id`, `title: str\|null = null`, `description: str\|null = null`, `deadline: str\|null = null` | Updates milestone fields and returns `{updated, milestone_id}`. The current implementation sends empty strings for omitted values; supply the desired values when preserving content matters. |
| `close_milestone` | `owner`, `repo`, `milestone_id` | Closes a milestone and returns `{close: true, milestone_id}`. |
| `reopen_milestone` | `owner`, `repo`, `milestone_id` | Reopens a milestone and returns `{open: true, milestone_id}`. |
| `delete_milestone` | `owner`, `repo`, `milestone_id` | Deletes a milestone and returns `{deleted: true, milestone_id}`. |

## Error handling

### MCP

Tool failures are raised as MCP `ToolError` results, so MCP clients receive an error result with `isError: true`. The text starts with a stable code:

```text
[NOT_FOUND] GET /team/platform/projects/999 -> HTTP 404
```

Common codes include:

| Code | Meaning |
|---|---|
| `MISSING_CONFIG` | One or more required environment variables are empty. |
| `AUTH_FAILED` | Login failed or did not establish a valid session. |
| `NETWORK_ERROR` | The Forgejo instance could not be reached. |
| `INVALID_STATE` | `state` was not `open`, `closed`, or `all`. |
| `ISSUE_NOT_FOUND` | A repository issue page did not expose its internal ID. |
| `COLUMN_NOT_FOUND` | A requested column is not on the project board. |
| `MILESTONE_NOT_FOUND` | A requested milestone is not present. |
| `NOT_FOUND` | A generic upstream HTTP 404. |
| `FORBIDDEN` / `BAD_REQUEST` | Generic upstream 4xx responses. |
| `UPSTREAM_ERROR` | Upstream HTTP 5xx response. |
| `INTERNAL_ERROR` | Unexpected server-side failure; details are in logs, not exposed to the caller. |

Network and upstream 5xx errors include a suggested `retry_after` value in the internal classification. The MCP-facing text remains concise.

### CLI

The CLI prints a JSON error object and returns a non-zero exit code:

```json
{"error": "[NOT_FOUND] ..."}
```

This makes it suitable for shell scripts: treat exit code `0` as success and any other code as failure.

## Cost and consistency notes

- `bulk_read_issues` is the preferred way to obtain many lightweight summaries.
- `read_card`, `read_column`, `read_milestone`, and `read_project` fetch full HTML issue pages and are network- and token-expensive.
- Project and milestone `state="all"` requests are implemented as separate open and closed page requests because Forgejo's page silently behaves like open when given all.
- Writes are not transactional. A multi-step operation can make progress before a later request fails.
- The web routes are undocumented and can change with Forgejo upgrades. Re-test the tool after an upgrade.

