# Architecture

## System shape

```text
MCP client ──stdio──> server.py ──> ForgejoClient ──Playwright HTTP──> Forgejo web routes
                           │              │
                           │              ├── session cookie cache
                           │              ├── HTML parsing / ID recovery
                           │              └── throttling and retries
                           │
CLI ──generated argparse───┘
```

Both interfaces use the same registered tool functions and the same module-level `ForgejoClient`. This keeps arguments, validation, error signaling, and behavior aligned.

## Modules

| Module | Responsibility |
|---|---|
| `forgejo_projects_mcp.__init__` | Package entry point; imports environment loading first, re-exports `main` and `mcp`, and exposes the installed package version. |
| `forgejo_projects_mcp._env` | Loads `.env` from the process working directory or a parent directory before configuration is read. |
| `forgejo_projects_mcp.client` | Owns authentication, the Playwright request context, session persistence, internal Forgejo web requests, HTML parsing, ID resolution, throttling, retries, and resource cleanup. |
| `forgejo_projects_mcp.server` | Defines the MCP server, lifecycle hook, tool functions, response shaping, and the exception-to-`ToolError` boundary. |
| `forgejo_projects_mcp.cli` | Inspects MCP tool schemas, builds an argparse subcommand for each tool, parses JSON collection arguments, invokes tools in-process, and maps failures to exit code `1`. |

The detailed module pages are in the [Python API reference](reference/index.md).

## Startup and environment order

Import order is intentional:

1. importing the package imports `_env`;
2. `_env` loads `.env` without overriding existing environment variables;
3. the package imports `server`, which constructs the module-level `ForgejoClient`;
4. `client.py` reads the URL, credentials, concurrency, and rate settings; and
5. the MCP server or CLI starts using those values.

This means settings such as `XDG_CONFIG_HOME`, `FORGEJO_MCP_MAX_CONCURRENCY`, and `FORGEJO_MCP_RPS` should be present before the process starts. A `.env` in the current directory works because it is loaded before `client.py` reads them.

## MCP server lifecycle

`server.py` creates:

- an `MCPServer("forgejo-projects-mcp")` instance;
- one `ForgejoClient`; and
- an async lifespan context.

The lifespan logs startup, yields control to the MCP runtime, then calls `client.close()` in `finally`. Cleanup disposes the Playwright request context and stops Playwright. It is deliberately idempotent and best-effort so shutdown errors do not prevent the process from exiting.

Each tool is a thin async adapter. Simple list operations add a count wrapper; full reads and mutations pass through the client result. `_safe()` catches expected client failures and unexpected exceptions, logs the event, and raises `ToolError` so the MCP protocol marks the result as an error instead of returning a successful value containing an error string.

`forgejo_status` is intentionally different: it catches authentication and Forgejo errors inside `client.status()` and returns `authenticated: false`, which makes it useful as a health check.

## Client request lifecycle

The first authenticated operation follows this path:

1. `ForgejoClient.ensure()` validates the three required credentials.
2. It starts Playwright if needed and opens an `APIRequestContext`.
3. If `storage_state.json` exists, the cookies are loaded.
4. `/user/settings` checks whether the session is still valid.
5. If not authenticated, the client posts the username and password to `/user/login`.
6. A redirect response followed by a successful `/user/settings` check proves the session.
7. Playwright storage state is written to the config directory.

Every request passes through `_request()`:

- requests are constrained by a shared concurrency semaphore;
- a monotonic schedule spaces requests at the configured RPS;
- HTTP `429` and `503` responses are retried up to two times with `Retry-After` or a two-second fallback;
- a response bounced to `/user/login` triggers one re-authentication and one request retry;
- HTTP errors become `ForgejoError` with status and a stable or generic code; and
- transport errors become `ForgejoError(code="NETWORK_ERROR")` without leaking raw client details beyond a concise first line.

## Forgejo integration strategy

Forgejo Projects / Kanban are accessed through the browser-facing web routes because the normal REST API does not expose them. The client uses Playwright's HTTP API rather than a browser UI, then scrapes HTML and regular expressions to recover project, column, issue, and milestone data.

Projects and columns use repository paths such as:

```text
/{owner}/{repo}/projects
/{owner}/{repo}/projects/{project_id}
```

Issues and milestones also use web pages. For a route that needs a global issue ID, the client first fetches `/{owner}/{repo}/issues/{number}` and extracts `data-issue-id`. Project board HTML supplies card issue IDs and repository numbers through `data-issue` and issue links.

The exact methods, paths, and observed Forgejo v15.0.7 behavior are recorded in the [automation reference](forgejo-projects-automation-reference.md).

## Data and identity model

The public tool API deliberately uses human-facing repository issue numbers. Forgejo's project move and attach routes require internal issue IDs, so the client translates at the boundary:

```text
repository issue number (#42)
        │ GET issue page
        ▼
internal issue ID (for example 1042)
        │ project web route
        ▼
board card
```

Project, column, and milestone IDs are already exposed as numeric IDs in parsed HTML and are passed directly. Confusing these ID domains is a common source of `NOT_FOUND` or incorrect mutations.

## Reads, filtering, and pagination

Summary reads fetch and parse issue pages concurrently, preserving input order and inlining individual failures. Full readers first identify matching issue numbers, apply `state`, `project`, and/or `milestone` filters, then slice with `offset` and `limit`, and finally fetch full issue content. Their metadata makes completeness explicit:

- `total`: matching issue/card count before pagination;
- `returned`: items selected for this call;
- `truncated`: whether another page exists; and
- `error_count`: selected issue pages that failed to parse or load.

Project reads regroup the selected issue content under the original board columns. A filter can therefore leave an empty column in the result while preserving board structure.

## Testing architecture

The test suite avoids a live Forgejo dependency. Fake Playwright contexts capture method, path, form, JSON payload, parameters, and response data. Tests cover:

- login success, bad credentials, session bounce, and cache writes;
- exact HTTP method/path contracts for projects, columns, cards, issues, and milestones;
- HTML parsing and ID recovery;
- state validation, pagination, filtering, partial bulk failures, throttling, and cleanup;
- MCP tool registration, schema validation, error signaling, and lifecycle cleanup; and
- CLI schema generation, JSON argument parsing, output, and exit codes.

When changing a route or parser, update the corresponding contract tests and perform a live throwaway-repository verification against the Forgejo version being supported.

## Limitations and risk

This architecture is intentionally a stop-gap:

- the routes and HTML selectors are undocumented and unversioned;
- parsing depends on markup and regular expressions;
- username/password authentication is broader than a scoped token;
- session state is stored locally as a sensitive cookie file;
- writes do not have a cross-request transaction or rollback;
- a failed multi-step operation may leave partial state; and
- the code was verified against one Forgejo instance/version.

Use the official Forgejo API for operations it supports when building a broader integration. If Forgejo ships a stable Projects API, the client should migrate to it rather than expanding the scraper.
