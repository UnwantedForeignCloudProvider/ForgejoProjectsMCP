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

Boolean flags accept `1`, `true`, `yes`, and `on` (case-insensitive) as true; any other value is false. The CLI prints a JSON result to stdout and returns exit code `1` when the MCP call fails or `forgejo_status` reports `authenticated: false`.

When stdin and stderr are terminals, the CLI requests missing credentials and
re-prompts after rejected logins, up to three prompted attempts. Prompts and
diagnostics use stderr so stdout remains valid JSON. Passwords are hidden and
remain in memory; only the authenticated session state is cached. This recovery
is deliberately unavailable to the stdio MCP server and noninteractive CLI
invocations.

## Recommended workflow

1. Call `forgejo_status` or the CLI equivalent.
2. Call `list_repositories` if the repository is not already known.
3. Call `list_projects` with `state="all"` when a closed board may be relevant.
4. Call `get_project` to obtain project and column IDs.
5. Use the smallest operation that answers the task.
6. Use `read_card` or a full reader only when body/comment content is needed.
7. Re-read the board after destructive or structural changes when confirmation matters.

`forgejo_status` reports the instance version and the behavior resolved for it:

```json
{
  "authenticated": true,
  "instance": "https://forge.example.com",
  "username": "your-username",
  "version": "16.0.3~gitea-1.22.0",
  "compatibility": {
    "version_short": "16.0.3",
    "csrf_mode": "origin",
    "quirks": [],
    "verified": true
  }
}
```

The version costs nothing extra: it is read from the same response that proves
the session. `quirks` names the version-specific behavior in force, and
`verified: false` means the release is outside the range the integration suite
exercises — the client still runs, using its newest verified behavior. See
[Architecture](architecture.md#version-adaptation).

Use a throwaway repository while validating compatibility with a new Forgejo release.

## Tool reference

The following table is the complete MCP surface. Required arguments have no default. Optional arguments and defaults are shown explicitly.

### Session and discovery

| Tool | Arguments | Result |
|---|---|---|
| `forgejo_status` | none | Authentication status, instance, username, cache state, the detected Forgejo `version`, and the `compatibility` profile resolved for it. Errors are returned as `authenticated: false`, never raised — a missing `FORGEJO_URL` is reported immediately, without a connection attempt. If the configured instance accepts connections but does not answer, this waits up to `FORGEJO_MCP_TIMEOUT` (default 30s) before reporting `NETWORK_ERROR`. |
| `authenticate` | `force: bool = false` | Logs in and caches the session; also returns `version` and `compatibility`. `force=true` skips the existing cache. |
| `list_repositories` | `query: str = ""`, `limit: int = 50`, `page: int = 1` | `{count, repositories}`. Each repository includes `full_name`, `owner`, `name`, `description`, `private`, `archived`, `empty`, and `fork`. `limit` and `page` are passed to Forgejo unchanged, so its boundaries apply: a `limit` of `0` or less yields Forgejo's own default page size rather than an empty or unbounded list, and `page` `0` behaves as page `1`. The endpoint publishes no total, so fewer results than `limit` is the only end-of-list signal. |

### Projects

| Tool | Arguments | Result / behavior |
|---|---|---|
| `list_projects` | `owner`, `repo`, `state: open\|closed\|all = open` | `{count, projects}` where each project has `id` and `title`. Complete: Forgejo paginates this page at 20 and the client walks every page. `all` combines separate open and closed requests. |
| `create_project` | `owner`, `repo`, `title`, `description: str = ""`, `card_type: text\|images_and_text = text` | `{created, project}`. The new project ID is recovered by reading the projects page. A blank title and an unknown `card_type` are refused as `[INVALID_INPUT]`; if the new project cannot be found afterwards the call raises `[CREATE_UNVERIFIED]` rather than returning another project. |
| `get_project` | `owner`, `repo`, `project_id` | Board `{id, title, columns}`. Each column contains `id`, `title`, and cards with `issue_id`, repository `number`, and `title`. |
| `update_project` | `owner`, `repo`, `project_id`, `title: str\|null = null`, `description: str\|null = null`, `card_type: str\|null = null` | `{updated, project_id}`. Title, description and card type are read from the edit form when omitted, so a partial edit preserves what it does not name. Pass an empty string to clear a field deliberately. |
| `close_project` | `owner`, `repo`, `project_id` | Archives the project and returns `{close: true, project_id}`. |
| `reopen_project` | `owner`, `repo`, `project_id` | Reopens the project and returns `{open: true, project_id}`. |
| `delete_project` | `owner`, `repo`, `project_id` | Permanently deletes the project. Issues survive. Returns `{deleted: true, project_id}`. |

### Columns

| Tool | Arguments | Result / behavior |
|---|---|---|
| `create_column` | `owner`, `repo`, `project_id`, `title`, `color: str = ""` | `{created, column}`. `color` must be six hex digits, e.g. `#e01e5a`; anything else is `[INVALID_INPUT]` (Forgejo answers it with HTTP 500, and does not accept the `#fff` shorthand). A blank title is refused the same way; a column that cannot be found afterwards raises `[CREATE_UNVERIFIED]`. |
| `edit_column` | `owner`, `repo`, `project_id`, `column_id`, `title: str\|null = null`, `color: str\|null = null` | `{updated, column_id}`. Only supplied fields are sent. A blank title is refused as `[INVALID_INPUT]`. |
| `delete_column` | `owner`, `repo`, `project_id`, `column_id` | `{deleted, column_id}`. Cards return to the default/uncategorized location. The default column cannot be deleted; set another default first. A column id that is not on the board is `[COLUMN_NOT_FOUND]`. |
| `set_default_column` | `owner`, `repo`, `project_id`, `column_id` | `{default_column, project_id}`. New attached cards land in this column. |

### Issues and cards

| Tool | Arguments | Result / behavior |
|---|---|---|
| `create_issue` | `owner`, `repo`, `title`, `body: str = ""`, `project_id: int\|null = null`, `milestone_id: int\|null = null`, `label_ids: list[int]\|null = null`, `assignee_ids: list[int]\|null = null` | Creates an issue. A project ID places it on that board; optional numeric milestone, label, and assignee IDs are passed to Forgejo. An unknown `project_id` or `milestone_id` is reported as `[PROJECT_NOT_FOUND]`/`[MILESTONE_NOT_FOUND]` and no issue is created — Forgejo itself answers these with an unnamed 500 (or a 404, depending on the release). An unknown `label_id` is **silently ignored by Forgejo**: the issue is created without the label and nothing reports it, so read the issue back if labels matter. Returns `{created, number, title}`. |
| `add_issues_to_project` | `owner`, `repo`, `project_id`, `issue_numbers: list[int]` | **Moves** existing repository issues onto this board and returns `{attached, project_id, cards}`. An issue holds a single project assignment, so attaching one that is already on another board removes it from that board — there is no way to keep an issue on two boards. New cards use the project's default column. The board is read back before this reports success, so `cards` names the `column_id` and `column_title` each issue actually landed in; an attachment Forgejo accepted that produced no card is `[ATTACH_UNVERIFIED]` rather than a reported success. This costs one extra board read per call. |
| `remove_issues_from_project` | `owner`, `repo`, `issue_numbers: list[int]`, `project_id: int\|null = null` | Detaches each issue from **every** project board it is on, leaving the issues intact. Despite the name this is not project-scoped — Forgejo clears the issue's project assignment outright and the route takes no project argument. There is no project-scoped alternative to offer, because an issue only ever holds one project assignment. Returns `{detached}`, which is the resulting state rather than a record of changes: an issue that was already on no board is listed too. `project_id` is a **guard, not a scope**: when given, every issue must already be a card on that board or the call is refused with `[CARD_NOT_FOUND]` and nothing is detached. Pass it whenever you know which board the issues are on — it is the only way to be sure this does not clear an assignment you did not mean to touch. |
| `move_card` | `owner`, `repo`, `project_id`, `column_id`, `issue_numbers: list[int]` | Moves one or more attached cards to one column. List order controls the submitted sorting order. An issue that is not a card on the project is `[CARD_NOT_FOUND]` — Forgejo answers that with an unnamed HTTP 500. Unlike `add_issues_to_project` this does not read the board back: the move route answers with an explicit success body, which the client returns in `result`. Read the board back yourself if the final ordering matters. |
| `delete_issue` | `owner`, `repo`, `number` | Permanently deletes the issue, not merely the card. Use `remove_issues_from_project` to preserve the issue. |

Issue arguments use repository numbers such as `42`, not internal issue IDs. The client fetches each issue page to resolve the ID required by Forgejo's project routes.

### Bulk and full reads

| Tool | Arguments | Result / behavior |
|---|---|---|
| `bulk_move_cards` | `owner`, `repo`, `project_id`, `moves: list[{issue_number, column_id}]` | Moves many cards concurrently. Items targeting the same column preserve input order. An issue number may appear only once per call — a repeated one is `[INVALID_INPUT]`, since the moves run concurrently and a card cannot be in two columns. Returns `{moved_count, columns}`. |
| `bulk_read_issues` | `owner`, `repo`, `issue_numbers`, `state: open\|closed\|all = all` | Lightweight summaries only: `{count, error_count, issues, errors}`. Body and comments are omitted. Individual failures are reported in `errors`. A repeated issue number is read once, so `count` is the number of *distinct* issues read and may be lower than the length of `issue_numbers`. |
| `read_card` | `owner`, `repo`, `number` | **Expensive.** Full issue content: `number`, `title`, `state`, `body`, `milestone`, and `comments`. |
| `read_column` | `owner`, `repo`, `project_id`, `column_id`, `state = all`, `milestone: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content for one column plus `{filters, total, returned, truncated, error_count, issues}`. |
| `read_milestone` | `owner`, `repo`, `milestone_id`, `state = all`, `project: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content for a milestone plus pagination and filter metadata. |
| `read_project` | `owner`, `repo`, `project_id`, `state = all`, `milestone: int\|null = null`, `limit: int\|null = null`, `offset: int = 0` | **Expensive.** Full issue content grouped under every board column plus pagination and filter metadata. |

All full readers accept direct numeric IDs for their optional `milestone` or `project` filter. A filter naming something that does not exist is `[MILESTONE_NOT_FOUND]`/`[PROJECT_NOT_FOUND]` rather than an empty result: Forgejo's issues list treats an unknown filter ID as "matches nothing", which is indistinguishable from a board that genuinely holds no matching issues. The ID is confirmed with one list read, paid for only when a filter is actually passed. A reader's `limit` applies after the matching card/issue numbers have been collected, and `offset` is zero-based. Use `truncated=true` to know that another page remains.

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
| `list_milestones` | `owner`, `repo`, `state: open\|closed\|all = open` | `{count, milestones}` with `{id, title}` entries. Complete: Forgejo paginates this page at 20 and the client walks every page. `all` merges open and closed pages. |
| `create_milestone` | `owner`, `repo`, `title`, `description: str = ""`, `deadline: str = ""` | Creates a milestone. `deadline` is `YYYY-MM-DD`. Returns `{created, milestone}`. A blank title is refused as `[INVALID_INPUT]`, and a deadline Forgejo will not accept (for example `2026-02-30`) as `[REJECTED]` — it is no longer reported as created. |
| `edit_milestone` | `owner`, `repo`, `milestone_id`, `title: str\|null = null`, `description: str\|null = null`, `deadline: str\|null = null` | Updates milestone fields and returns `{updated, milestone_id}`. Fields left `null` are read back from the edit form and preserved; pass an empty string to clear one. Editing only the deadline now works — it previously reported success and changed nothing. |
| `close_milestone` | `owner`, `repo`, `milestone_id` | Closes a milestone and returns `{close: true, milestone_id}`. |
| `reopen_milestone` | `owner`, `repo`, `milestone_id` | Reopens a milestone and returns `{open: true, milestone_id}`. |
| `delete_milestone` | `owner`, `repo`, `milestone_id` | Deletes a milestone and returns `{deleted: true, milestone_id}`. A milestone that does not exist is `[MILESTONE_NOT_FOUND]` rather than a reported success. |

## Error handling

### MCP

Tool failures are raised as MCP `ToolError` results, so MCP clients receive an error result with `isError: true`. The text starts with a stable code:

```text
[NOT_FOUND] GET /team/platform/projects/999 -> HTTP 404
```

Common codes include:

| Code | Meaning |
|---|---|
| `MISSING_CONFIG` | The URL is missing, or login is required and the username/password is missing. |
| `AUTH_FAILED` | Login failed or did not establish a valid session. |
| `NETWORK_ERROR` | The Forgejo instance could not be reached. |
| `INVALID_STATE` | `state` was not `open`, `closed`, or `all`. |
| `INVALID_INPUT` | An argument was refused before any request was sent: a blank title, an unknown `card_type`, or an argument that does not match the tool's schema. Identifiers are matched strictly, so `"7"`, `7.0` and `true` are refused rather than coerced onto a real issue or project. |
| `REJECTED` | Forgejo refused the write and changed nothing. Its own message follows, for example an impossible deadline. |
| `CREATE_UNVERIFIED` | The write was accepted but the new resource could not be found afterwards. It may exist; re-read the list before retrying. |
| `ATTACH_UNVERIFIED` | `add_issues_to_project` was accepted but the issues are not cards on the board afterwards. They may be attached; re-read the board before retrying. |
| `CARD_NOT_FOUND` | An issue named for a card operation is not a card on that project. Attach it with `add_issues_to_project` first. |
| `ISSUE_NOT_FOUND` | A repository issue page did not expose its internal ID. |
| `COLUMN_NOT_FOUND` | A requested column is not on the project board. |
| `MILESTONE_NOT_FOUND` | A requested milestone, or one used as a reader's `milestone` filter, is not present. |
| `PROJECT_NOT_FOUND` | A project referenced by `create_issue`, or used as a reader's `project` filter, is not present. |
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

An argument that is malformed before dispatch — JSON that does not parse, or a JSON value of the wrong shape such as an object where an array belongs — is an argparse usage error on stderr with exit code `2`, not a JSON error payload.

## Cost and consistency notes

- `bulk_read_issues` is the preferred way to obtain many lightweight summaries.
- `read_card`, `read_column`, `read_milestone`, and `read_project` fetch full HTML issue pages and are network- and token-expensive.
- Project and milestone `state="all"` requests are implemented as separate open and closed page requests because Forgejo's page silently behaves like open when given all.
- `list_projects` and `list_milestones` are complete, but not free: Forgejo paginates both pages at 20 entries and publishes no total, so the client reads pages until one comes back empty. A repository with many projects or milestones costs one request per 20 entries, per state.
- Writes are not transactional. A multi-step operation can make progress before a later request fails.
- `add_issues_to_project` reads the board back to confirm the cards it made, which costs one request beyond the write. `move_card` and `bulk_move_cards` do not: their route reports success explicitly, so there is nothing to re-derive.
- A successful result means Forgejo accepted the write, except for `add_issues_to_project`, which confirms the cards exist before reporting success. These tools go through Forgejo's internal web forms, which answer a refusal with an ordinary page rather than an error status, so the client checks the response shape for the routes where that distinction exists and refuses input Forgejo would accept but silently mangle (see [the automation reference](forgejo-projects-automation-reference.md#how-a-write-reports-success-and-how-it-reports-refusal)). Reading back after a write is still the only way to confirm the *content* of what was stored.
- Partial edits preserve what they do not name: `update_project` and `edit_milestone` read the current values from Forgejo's edit form and merge. Pass an empty string to clear a field on purpose.
- The web routes are undocumented and can change with Forgejo upgrades. Re-test the tool after an upgrade.
