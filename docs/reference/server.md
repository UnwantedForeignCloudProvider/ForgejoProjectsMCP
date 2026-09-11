# `forgejo_projects_mcp.server`

Source: [`src/forgejo_projects_mcp/server.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/server.py)

This module defines the MCP server and the complete registered tool set. It is the public protocol boundary over `ForgejoClient`.

## Server objects

```python
mcp = MCPServer("forgejo-projects-mcp", lifespan=_lifespan)
client = ForgejoClient()
```

### `_lifespan`

The async context manager logs startup and shutdown and always calls `client.close()` after the server yields. This releases Playwright resources during normal MCP shutdown.

### `_classify`

```python
_classify(exc: Exception) -> dict[str, Any]
```

Maps errors to non-leaking categories:

- `AuthError` → `client_error` with its stable code;
- `NETWORK_ERROR` → `external_error` with retry guidance;
- HTTP 5xx → `server_error` / `UPSTREAM_ERROR`;
- HTTP 401/403/404 and other 4xx → `client_error`; and
- unclassified failures → `server_error` / `INTERNAL_ERROR`.

The returned mapping is used to construct the stable `[CODE] message` prefix. Unexpected exception details are logged, not sent to the caller.

### `_safe`

```python
async _safe(coro: Awaitable) -> Any
```

Runs a client coroutine. Expected `AuthError` and `ForgejoError` failures, plus unexpected exceptions, become `ToolError`. `KeyboardInterrupt` and cancellation are not caught, so shutdown and cancellation can propagate correctly.

### `_ToolServer` and `Id`

`_ToolServer` is a thin `MCPServer` subclass overriding `call_tool`. Argument
validation runs inside the SDK, before `_safe` can see it, and a rejection used
to surface as raw multi-line Pydantic text — the one failure in this surface
with no `[CODE]` to branch on. The override converts a `ToolError` whose
`__cause__` is a Pydantic `ValidationError` into `[INVALID_INPUT] <tool>: <field>:
<reason>`, and re-raises anything else untouched so a code from `_safe` survives.
Both transports reach a tool through this method — the MCP wire handler calls it,
and the CLI dispatches through it — so normalising here covers both.

`Id` is `Annotated[int, Field(strict=True)]`, used for every numeric tool
argument. Pydantic's default lax mode coerces `"7"`, `7.0` and `true` into
integers, which turned a malformed call into a well-formed call against the wrong
resource. The published JSON schema is unchanged (`{"type": "integer"}`), so MCP
clients and the generated CLI see no new contract.

## Registered tools

The server registers exactly these 31 tools:

```text
forgejo_status, authenticate, list_repositories,
list_projects, create_project, get_project, update_project,
close_project, reopen_project, delete_project,
create_column, edit_column, delete_column, set_default_column,
create_issue, add_issues_to_project, remove_issues_from_project,
move_card, delete_issue,
bulk_move_cards, bulk_read_issues, read_card, read_column,
read_milestone, read_project,
list_milestones, create_milestone, edit_milestone,
close_milestone, reopen_milestone, delete_milestone
```

The exact signatures are generated from the functions below and exposed in the MCP input schemas. The [usage guide](../usage.md#tool-reference) gives the consumer-facing argument and result reference.

### Session and repository tools

```python
async forgejo_status() -> dict
async authenticate(force: bool = False) -> dict
async list_repositories(query: str = "", limit: int = 50,
                        page: int = 1) -> dict
```

`list_repositories` wraps the client list in `{count, repositories}`. `forgejo_status` returns a value even when credentials or the instance are unavailable; `authenticate` raises a flagged MCP error on failure.

### Project tools

```python
async list_projects(owner: str, repo: str, state: str = "open") -> dict
async create_project(owner: str, repo: str, title: str,
                     description: str = "",
                     card_type: str = "text") -> dict
async get_project(owner: str, repo: str, project_id: int) -> dict
async update_project(owner: str, repo: str, project_id: int,
                     title: str | None = None,
                     description: str | None = None,
                     card_type: str | None = None) -> dict
async close_project(owner: str, repo: str, project_id: int) -> dict
async reopen_project(owner: str, repo: str, project_id: int) -> dict
async delete_project(owner: str, repo: str, project_id: int) -> dict
```

List operations add a count wrapper; mutations and board reads return the client dictionaries directly.

### Column tools

```python
async create_column(owner: str, repo: str, project_id: int,
                    title: str, color: str = "") -> dict
async edit_column(owner: str, repo: str, project_id: int, column_id: int,
                  title: str | None = None,
                  color: str | None = None) -> dict
async delete_column(owner: str, repo: str, project_id: int,
                    column_id: int) -> dict
async set_default_column(owner: str, repo: str, project_id: int,
                         column_id: int) -> dict
```

### Issue and card tools

```python
async create_issue(owner: str, repo: str, title: str, body: str = "",
                   project_id: int | None = None,
                   milestone_id: int | None = None,
                   label_ids: list[int] | None = None,
                   assignee_ids: list[int] | None = None) -> dict
async add_issues_to_project(owner: str, repo: str, project_id: int,
                            issue_numbers: list[int]) -> dict
async remove_issues_from_project(owner: str, repo: str,
                                 issue_numbers: list[int]) -> dict
async move_card(owner: str, repo: str, project_id: int, column_id: int,
                issue_numbers: list[int]) -> dict
async delete_issue(owner: str, repo: str, number: int) -> dict
```

All issue arguments are repository issue numbers, not internal issue IDs. `delete_issue` is permanent; removing an issue from a project is the non-destructive card operation.

### Bulk and content readers

```python
async bulk_move_cards(owner: str, repo: str, project_id: int,
                      moves: list[dict[str, int]]) -> dict
async bulk_read_issues(owner: str, repo: str,
                       issue_numbers: list[int],
                       state: str = "all") -> dict
async read_card(owner: str, repo: str, number: int) -> dict
async read_column(owner: str, repo: str, project_id: int, column_id: int,
                  state: str = "all", milestone: int | None = None,
                  limit: int | None = None, offset: int = 0) -> dict
async read_milestone(owner: str, repo: str, milestone_id: int,
                     state: str = "all", project: int | None = None,
                     limit: int | None = None, offset: int = 0) -> dict
async read_project(owner: str, repo: str, project_id: int,
                   state: str = "all", milestone: int | None = None,
                   limit: int | None = None, offset: int = 0) -> dict
```

`bulk_read_issues` strips body and comments from successful results and separates per-issue failures into `errors`. A repeated issue number is read once, so `count` is the number of distinct issues read. The four full readers are marked expensive in their tool descriptions so MCP clients and agents can choose them deliberately.

### Milestone tools

```python
async list_milestones(owner: str, repo: str,
                      state: str = "open") -> dict
async create_milestone(owner: str, repo: str, title: str,
                       description: str = "",
                       deadline: str = "") -> dict
async edit_milestone(owner: str, repo: str, milestone_id: int,
                     title: str | None = None,
                     description: str | None = None,
                     deadline: str | None = None) -> dict
async close_milestone(owner: str, repo: str, milestone_id: int) -> dict
async reopen_milestone(owner: str, repo: str, milestone_id: int) -> dict
async delete_milestone(owner: str, repo: str, milestone_id: int) -> dict
```

`list_milestones` wraps results in `{count, milestones}`. Milestone full reads accept a direct project ID filter; they do not perform a project-name lookup.

## Entry point

```python
def main() -> None
```

Calls `mcp.run()`, which starts the stdio MCP transport. The module's script guard invokes `main()` when executed directly. The package console script points to this function through `forgejo_projects_mcp:main`.

