# `forgejo_projects_mcp.client`

Source: [`src/forgejo_projects_mcp/client.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/client.py)

`client.py` is the integration layer. It speaks to Forgejo's browser-facing web routes using Playwright's asynchronous `APIRequestContext`, persists the authenticated cookie state, parses HTML, and exposes typed Python methods used by the MCP server.

## Configuration constants

At import time the module computes:

```python
CONFIG_DIR = <XDG_CONFIG_HOME or Path.home() / ".config"> / "forgejo_projects_mcp"
STATE_FILE = CONFIG_DIR / "storage_state.json"
```

It also reads `FORGEJO_MCP_MAX_CONCURRENCY` (default `8`) and `FORGEJO_MCP_RPS` (default `5`). See [Configuration](../configuration.md).

## Exceptions

### `AuthError`

```python
class AuthError(RuntimeError)
AuthError(message: str, *, code: str = "AUTH_FAILED")
```

Raised when required credentials are absent or the login response does not establish a session. `code` is stable and is exposed by the server error boundary; missing variables use `MISSING_CONFIG`.

### `ForgejoError`

```python
class ForgejoError(RuntimeError)
ForgejoError(message: str, *, status: int | None = None, code: str | None = None)
```

Represents a Forgejo HTTP error, a transport failure, or a parsing/lookup failure. The optional `status` stores the HTTP status (`None` for transport failures), and `code` stores a stable label such as `NETWORK_ERROR`, `ISSUE_NOT_FOUND`, or `HTTP_404`.

## `ForgejoClient`

```python
ForgejoClient()
```

The constructor reads:

- `FORGEJO_URL`, with trailing slashes removed;
- `FORGEJO_USERNAME`;
- `FORGEJO_PASSWORD`; and
- the module-level throttling defaults.

It does not start Playwright or make a network request until an async operation needs it.

### Authentication and lifecycle

#### `ensure`

```python
async ensure() -> None
```

Ensures an authenticated request context exists. It validates credentials, starts Playwright, loads `STATE_FILE` when present, checks `/user/settings`, and logs in if needed. Calls are serialized with an async lock.

The `/user/settings` check is a rendered Forgejo page, and the client reads the instance version out of that same response — plus the session CSRF token on releases that require one. Version detection therefore costs no extra request, and the resolved [compatibility profile](compat.md) is available from `client.version` and `client.profile` afterwards.

Only the Forgejo URL is required to load and verify cached state. Username and
password are validated immediately before a fresh login. If an optional
credential provider is installed with `set_credential_provider`, an
`AuthError` can supply replacement credentials and retry; the provider is unset
for normal MCP server operation.

#### `login`

```python
async login(force: bool = False) -> dict[str, Any]
```

Performs an explicit login and returns:

```json
{
  "authenticated": true,
  "instance": "https://forge.example.com",
  "username": "your-username",
  "version": "16.0.3~gitea-1.22.0",
  "compatibility": {
    "version": "16.0.3~gitea-1.22.0",
    "version_short": "16.0.3",
    "csrf_mode": "origin",
    "quirks": [],
    "verified": true
  },
  "state_file": ".../storage_state.json",
  "config_file": ".../config.json"
}
```

`version` is `null` when the page carried no version marker; the client then uses the newest verified behavior. `compatibility` is [`Profile.describe()`](compat.md#describe).

With `force=True`, the existing request context/cache is ignored and a fresh login requiring username and password is performed. With `force=False`, valid cached state can be reused without login credentials.

#### `status`

```python
async status() -> dict[str, Any]
```

Calls `ensure()` and returns authenticated session information including `state_cached`, the detected `version`, and the resolved `compatibility` profile. `AuthError` and `ForgejoError` are converted into `{authenticated: False, error, instance, version}` rather than raised.

#### `close`

```python
async close() -> None
```

Disposes the request context and stops Playwright. It is safe to call multiple times and suppresses teardown failures after logging them at debug level.

### Version adaptation

```python
client.version  # Version | None -- the detected instance version
client.profile  # Profile -- the behavior resolved for it
```

Both are established by the session probe. Every route the client requests is
rendered from `profile.route(...)`, and every HTML element it scrapes comes from
the profile's ordered pattern candidates, so version differences live in
[`compat.py`](compat.md) rather than in the operations below.

Writes carry an `X-Csrf-Token` header when the profile's `csrf_mode` is `token`
(Forgejo below 14.0). If a write is nevertheless rejected with HTTP 400 and an
*Invalid CSRF token* body, the client fetches a token, retries the request once,
and keeps sending one for the rest of the session — so an instance whose
behavior does not match its version still works.

### Discovery and projects

#### `list_repositories`

```python
async list_repositories(query: str = "", limit: int = 50, page: int = 1) -> list[dict[str, Any]]
```

Requests `/repo/search` and normalizes each result to:

```python
{
    "full_name": str | None,
    "owner": str,
    "name": str,
    "description": str,
    "private": bool | None,
    "archived": bool | None,
    "empty": bool | None,
    "fork": bool | None,
}
```

#### `list_projects`

```python
async list_projects(owner: str, repo: str, state: str = "open") -> list[dict[str, Any]]
```

Returns `{id, title}` entries parsed from the repository projects page. `state` is validated against `open`, `closed`, and `all`. `all` makes separate open and closed requests and merges IDs.

#### `create_project`

```python
async create_project(
    owner: str, repo: str, title: str,
    description: str = "", card_type: str = "text",
) -> dict[str, Any]
```

Posts a project form, maps `text` to Forgejo card type `1` and `images_and_text` to `2` (through the profile's `card_types`), then reads the open project list to recover the new ID. Returns `{created: True, project: {id, title} | None}`.

#### `get_project`

```python
async get_project(owner: str, repo: str, project_id: int) -> dict[str, Any]
```

Parses the board page and returns:

```python
{
    "id": project_id,
    "title": str,
    "columns": [
        {
            "id": int,
            "title": str,
            "cards": [
                {"issue_id": int, "number": int | None, "title": str}
            ],
        }
    ],
}
```

#### `update_project`

```python
async update_project(
    owner: str, repo: str, project_id: int,
    title: str | None = None, description: str | None = None,
    card_type: str | None = None,
) -> dict[str, Any]
```

Reads the edit form to preserve an omitted title and card type. It sends `description` as an empty string when that argument is `None`, so callers should pass the desired description explicitly when updating only another field. Returns `{updated: True, project_id}`.

#### `close_project`, `reopen_project`, `delete_project`

```python
async close_project(owner, repo, project_id) -> dict[str, Any]
async reopen_project(owner, repo, project_id) -> dict[str, Any]
async delete_project(owner, repo, project_id) -> dict[str, Any]
```

Use POST actions `close`, `open`, and `delete`. They return `{close: True, project_id}`, `{open: True, project_id}`, or `{deleted: True, project_id}` respectively. Project deletion does not delete issues.

### Columns

```python
async create_column(owner, repo, project_id, title, color: str = "") -> dict[str, Any]
async edit_column(owner, repo, project_id, column_id,
                  title: str | None = None, color: str | None = None) -> dict[str, Any]
async delete_column(owner, repo, project_id, column_id) -> dict[str, Any]
async set_default_column(owner, repo, project_id, column_id) -> dict[str, Any]
```

- `create_column` posts the title/color and re-reads the board to recover the created column object.
- `edit_column` uses **PUT** and sends only non-`None` fields.
- `delete_column` uses **DELETE**. If the upstream rejects deletion, the raised error adds a hint that the default column must be changed first.
- `set_default_column` posts the default action and returns `{default_column, project_id}`.

### Cards and issues

#### `resolve_issue_id`

```python
async resolve_issue_id(owner: str, repo: str, number: int) -> int
```

Fetches the issue page and extracts `data-issue-id`. A missing marker raises `ForgejoError(code="ISSUE_NOT_FOUND", status=404)`. This is the boundary between repository issue numbers and Forgejo internal IDs.

#### `add_issues_to_project` and `remove_issues_from_project`

```python
async add_issues_to_project(owner, repo, project_id, issue_numbers: list[int]) -> dict[str, Any]
async remove_issues_from_project(owner, repo, issue_numbers: list[int]) -> dict[str, Any]
```

Both resolve every repository issue number first, then post to `/issues/projects`. Attach uses the project ID; detach uses project ID `0`. Detaching leaves the issue intact.

#### `move_card`

```python
async move_card(owner, repo, project_id, column_id,
                issue_numbers: list[int]) -> dict[str, Any]
```

Resolves issue IDs and posts JSON shaped like:

```json
{"issues": [{"issueID": 1042, "sorting": 0}, {"issueID": 1043, "sorting": 1}]}
```

The return value includes the original issue numbers, target column ID, and the upstream JSON body when available.

#### `create_issue` and `delete_issue`

```python
async create_issue(
    owner, repo, title, body: str = "", project_id: int | None = None,
    milestone_id: int | None = None, label_ids: list[int] | None = None,
    assignee_ids: list[int] | None = None,
) -> dict[str, Any]
async delete_issue(owner, repo, number) -> dict[str, Any]
```

`create_issue` posts the issue form and extracts the repository number from Forgejo's JSON redirect when available. `delete_issue` permanently deletes the issue by repository number.

### Full and bulk reads

#### `_parse_issue` output

Issue HTML is normalized to:

```python
{
    "number": int | None,
    "title": str,
    "state": "open" | "closed",
    "body": str,
    "milestone": {"id": int, "title": str} | None,
    "comments": [{"author": str | None, "body": str}],
}
```

`read_issue(owner, repo, number)` fetches and parses one issue. If the page lacks a number marker, it uses the requested number as a fallback.

The `body` is read from the raw element Forgejo keys by the issue's **global id**, taken from `data-issue-id` on the same page. Issue numbers restart per repository while ids count across the instance, so looking the body up by number returns an empty string in every repository but the first one an instance created.

```python
async read_issue(owner, repo, number: int) -> dict[str, Any]
async bulk_read_issues(owner, repo, numbers: list[int], state: str = "all") -> list[dict[str, Any]]
```

`bulk_read_issues` runs issue reads concurrently with `asyncio.gather(..., return_exceptions=True)`, preserves input order, and represents an individual failure as `{"number": n, "error": "..."}`. `state` post-filters successful issue content.

#### Filtered readers

```python
async read_column_content(
    owner, repo, project_id, column_id, state: str = "all",
    milestone: int | None = None, limit: int | None = None, offset: int = 0,
) -> dict[str, Any]

async read_milestone_content(
    owner, repo, milestone_id, state: str = "all",
    project: int | None = None, limit: int | None = None, offset: int = 0,
) -> dict[str, Any]

async read_project_content(
    owner, repo, project_id, state: str = "all",
    milestone: int | None = None, limit: int | None = None, offset: int = 0,
) -> dict[str, Any]
```

All validate state. They collect matching repository issue numbers, apply zero-based offset/limit, read full issue content, and report `total`, `returned`, `truncated`, and `error_count`. Project content preserves all board columns and groups selected issues beneath them. Missing columns and milestones raise `COLUMN_NOT_FOUND` and `MILESTONE_NOT_FOUND`.

#### `bulk_move_cards`

```python
async bulk_move_cards(owner, repo, project_id,
                      moves: list[dict[str, int]]) -> dict[str, Any]
```

Each move has `issue_number` and `column_id`. Issue ID resolution runs concurrently. Moves are grouped by column; each column request preserves the order of its input group, and column requests run concurrently.

### Milestones

```python
async list_milestones(owner, repo, state: str = "open") -> list[dict[str, Any]]
async create_milestone(owner, repo, title, description: str = "",
                       deadline: str = "") -> dict[str, Any]
async edit_milestone(owner, repo, milestone_id, title: str | None = None,
                     description: str | None = None,
                     deadline: str | None = None) -> dict[str, Any]
async close_milestone(owner, repo, milestone_id) -> dict[str, Any]
async reopen_milestone(owner, repo, milestone_id) -> dict[str, Any]
async delete_milestone(owner, repo, milestone_id) -> dict[str, Any]
```

Milestone listing validates state and merges separate open/closed pages for `all`. Creation re-reads open milestones to recover the new ID. Editing currently sends empty strings for omitted fields. Deletion uses the collection route with `id=N`, matching the observed Forgejo behavior; it does not use `/milestones/{id}/delete`.

## Private helpers worth knowing

The following methods are implementation details but explain operational behavior:

- `_request` adds authentication, JSON/form encoding, CSRF headers, throttling, 429/503 retries, session-bounce re-authentication, one CSRF-rejection retry, and HTTP error conversion;
- `_absorb_page` learns the version and CSRF token from any rendered page, and swaps in the profile for a newly detected version;
- `_route` renders an internal route through the active profile;
- `_default_config_dir` computes the OS-independent cache root;
- `_parse_projects_list`, `_parse_board`, `_parse_issue`, and `_parse_milestones` scrape HTML using the active profile's pattern candidates; each also accepts an explicit profile and falls back to `DEFAULT_PROFILE`;
- `_issue_body` resolves an issue's body by its global id, falling back to its number;
- `_filtered_issue_numbers` uses the repository issues page for server-side state/project/milestone filtering; and
- `_page` performs the final in-memory slice and returns `(selected, total)`.

The route contract and HTML anchors are documented separately in the [automation reference](../forgejo-projects-automation-reference.md).
