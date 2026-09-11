# Tools reference

Every capability is exposed both as an MCP tool and as a
[CLI subcommand](cli.md) of the same name. Issue arguments use the **repo issue
number** (`#N`); the tool resolves the internal id. Projects, columns and
milestones are addressed by numeric **id**.

## Session & discovery

- `forgejo_status` — check authentication, and report the instance version and
  the compatibility profile in force for it.
- `authenticate(force=False)` — log in / refresh the cached session.
- `list_repositories(query, limit, page)` — repos the user can access.

## Projects

- `list_projects(owner, repo, state)`
- `create_project(owner, repo, title, description, card_type)`
- `get_project(owner, repo, project_id)` — board with columns + cards
- `update_project(...)`, `close_project(...)`, `reopen_project(...)`,
  `delete_project(...)`

## Columns

- `create_column`, `edit_column`, `delete_column`, `set_default_column`

## Cards / issues

- `create_issue(... project_id=)` — create an issue, optionally straight onto a
  board
- `add_issues_to_project` — reads the board back and reports the column each
  card landed in
- `remove_issues_from_project(owner, repo, issue_numbers, project_id)` —
  `project_id` is an optional guard, not a scope: it refuses the call when an
  issue is not a card on that board
- `move_card(owner, repo, project_id, column_id, issue_numbers)`
- `bulk_move_cards(owner, repo, project_id, moves)` — move many cards, each to its
  own column, in one call (`moves` = list of `{issue_number, column_id}`)
- `delete_issue`

## Milestones

- `list_milestones`, `create_milestone`, `edit_milestone`,
  `close_milestone`, `reopen_milestone`, `delete_milestone`

## Bulk reads

Run concurrently and rate-limited.

- `bulk_read_issues(owner, repo, issue_numbers, state="all")` — lightweight
  summaries (number, title, state, milestone). Returns `count` (successful),
  `error_count`, `issues`, and `errors`.

!!! warning "Full readers are network- and token-expensive"
    Use them only when full content is needed. They fetch every issue's body and
    comments.

- `read_card(owner, repo, number)` — one card's full content
- `read_column(owner, repo, project_id, column_id, state, milestone, limit, offset)`
- `read_milestone(owner, repo, milestone_id, state, project, limit, offset)`
- `read_project(owner, repo, project_id, state, milestone, limit, offset)`

The full readers return `total` / `returned` / `truncated` / `error_count` so cost
and completeness are explicit; use `limit`/`offset` to cap and page.

## Filters

The readers accept direct-value filters (no name lookup):

- `state` — `open`, `closed`, or `all`. An invalid value is a hard error.
- `milestone` / `project` — a numeric **id** (each tool omits the filter that is
  already its own subject). An id that does not exist is a hard error, not an
  empty result.

## Error signaling

Tool failures are returned as MCP errors (`isError: true`) with a
`[CODE] message` — e.g. `[NOT_FOUND]`, `[INVALID_STATE]`, `[MILESTONE_NOT_FOUND]`,
`[NETWORK_ERROR]`. Missing projects/columns/milestones/issues, unusable filter
ids and invalid `state` values are hard errors, not silent empty results. An
argument that does not match a tool's schema is `[INVALID_INPUT]`; identifiers
are matched strictly, so `"7"` is refused rather than coerced to `7`. Individual issues that fail to
read inside a bulk call are reported inline instead (partial success).

## Tuning

Concurrency, request rate and the per-request timeout are tunable via
`FORGEJO_MCP_MAX_CONCURRENCY` (default 8), `FORGEJO_MCP_RPS` (default 5) and
`FORGEJO_MCP_TIMEOUT` (default 30 seconds).
