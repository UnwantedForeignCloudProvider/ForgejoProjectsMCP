# Architecture

## System shape

```text
MCP client ──stdio──> server.py ──> ForgejoClient ──Playwright HTTP──> Forgejo web routes
                           │              │
                           │              ├── session cookie cache
                           │              ├── compat.Profile (routes, patterns, CSRF)
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
| `forgejo_projects_mcp.compat` | Parses Forgejo versions and resolves the behavior profile for one: route templates, HTML pattern candidates, form value maps, and the CSRF strategy, plus the version-scoped quirks that override them. |
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

1. `ForgejoClient.ensure()` validates the Forgejo URL.
2. It starts Playwright if needed and opens an `APIRequestContext`.
3. If `storage_state.json` exists, the cookies are loaded.
4. `/user/settings` checks whether the session is still valid, and the same response body yields the instance version (and, on releases that need one, the session CSRF token).
5. If not authenticated, the client validates the username/password and posts them to `/user/login`.
6. A redirect response followed by a successful `/user/settings` check proves the session.
7. Playwright storage state is written to the config directory.

The probe is deliberately a rendered page rather than a lightweight endpoint:
every Forgejo page embeds its own version, so the authentication check that
precedes each request doubles as version detection and no separate version
request is ever made.

An optional credential-provider hook can recover an `AuthError` and retry this
flow. Only an interactive `forgejo-projects-cli` invocation installs the hook;
the stdio MCP server leaves it unset. Providers run outside the authentication
lock, and a replacement URL discards the context created for the old origin.

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

The exact methods, paths, and the behavior observed on each supported release are recorded in the [automation reference](forgejo-projects-automation-reference.md).

## Version adaptation

The web routes this client drives are undocumented and unversioned, so their
behavior differs between Forgejo releases. Rather than branching on version
inside each operation, `compat.py` resolves one `Profile` per instance and the
client reads everything version-dependent out of it:

- **routes** — every internal path is a named template, rendered by
  `profile.route(name, **params)`;
- **patterns** — every scraped element is an *ordered tuple of candidate regular
  expressions*, and the first that matches wins;
- **form values** — such as the card-type map; and
- **CSRF strategy** — `origin` or `token`.

A profile is the base (newest verified) behavior with every matching `Quirk`
applied in order, so supporting a release usually means adding one documented
quirk rather than editing the client. Three are registered today, and they
compose — Forgejo 1.20 matches all three:

| Quirk | Applies to | Effect |
|---|---|---|
| `legacy-board-vocabulary` | Forgejo below 1.21 | Project columns were called *boards* in the markup (`board-column`, `board-label`, `board-card-cnt`), so the column patterns are replaced wholesale. |
| `board-title-missing-from-page-title` | Forgejo below 10.0 | The board page `<title>` is only `owner/repo`, so just the project heading is trusted for the board title. |
| `csrf-token-required` | Forgejo below 14.0 | Writes are rejected with HTTP 400 *Invalid CSRF token* unless they carry the session token, which those releases publish on every authenticated page. |

Detection never becomes a single point of failure:

- an unreadable or unrecognised version resolves to the newest verified
  behavior instead of erroring; and
- a write rejected for a missing CSRF token is retried once with a token, and
  the session adopts token mode from then on — so an instance whose behavior
  does not match its version still works.

`forgejo_status` and `authenticate` report the detected version and the
resolved profile, including whether the version is inside the range the
integration suite exercises.

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

### Offline suite

The default test suite avoids a live Forgejo dependency. Fake Playwright contexts capture method, path, form, JSON payload, parameters, and response data. Tests cover:

- login success, bad credentials, session bounce, and cache writes;
- exact HTTP method/path contracts for projects, columns, cards, issues, and milestones;
- HTML parsing and ID recovery;
- state validation, pagination, filtering, partial bulk failures, throttling, and cleanup;
- MCP tool registration, schema validation, error signaling, and lifecycle cleanup; and
- CLI schema generation, JSON argument parsing, output, and exit codes.

Version handling has its own offline coverage: version parsing and profile
resolution in `tests/test_compat.py`, and the client behavior that follows from
them — probe-time detection, CSRF header injection, rejection recovery and
profile-driven parsing — in `tests/test_versioning.py`.

### Integration suite

`tests/integration/` runs the same operations against real Forgejo instances and
is opt-in. It is self-contained: naming a version starts a throwaway container
for it, waits for its health check, creates an admin, and seeds a repository
with issues and a milestone before any test runs.

```text
--forgejo-version N ──> docker compose up (tests/composes/) ──> healthcheck
                                    │
                                    ├── admin user via `forgejo admin user create`
                                    ├── repo + issues + milestone via the REST API
                                    └── boards + columns + cards via ForgejoClient
```

Repositories, issues and milestones are seeded through Forgejo's documented REST
API, which is stable across releases, so a seeding failure is never confused
with a scraping failure. Projects have no API at all, so board fixtures go
through the client under test.

Every test runs once per requested version, which is how version-specific
behavior is verified rather than assumed. See
[Installation](installation.md#integration-testing).

### The two suites are deliberately paired

Every offline test that can be run against a real instance has a live
counterpart, and the files mirror each other:

| Offline | Live | What only the live one can prove |
|---|---|---|
| `test_client.py` (auth half) | `test_live_auth.py` | A real login is accepted, persisted and replayed; a real rejection recovers |
| `test_client.py` (operations) | `test_live_client.py` | Recovered ids exist, filters narrow real results, failures carry real statuses |
| `test_parsing.py` | `test_live_parsing.py` | The markup the fixtures imitate is the markup Forgejo still emits |
| `test_tools.py` | `test_live_tools.py` | Each tool is wired to the client method it claims |
| `test_cli.py` | `test_live_cli.py` | The installed console script works as a process, from environment to exit code |
| `test_logging.py` | `test_live_logging.py` | Real passwords and real issue content stay out of the logs |
| `test_compat.py`, `test_versioning.py` | `test_live_compat.py`, `test_live_session.py` | Each release enforces the CSRF rule and renders the markup its profile predicts |

The division of labour is the point. The offline suite pins down logic that is
hard to provoke on demand — a rate-limited response, a transport error mid-parse
— and runs in seconds with no dependencies. The live suite proves the fixtures
still describe reality, which is the failure mode a mocked suite cannot detect:
a fixture and a parser can agree with each other long after both have stopped
agreeing with Forgejo.

Two things are covered offline only, because a stock instance will not produce
them: rate-limit (429) retry and back-off, and a version string the client
cannot read. Two are covered live only, because a fake transport cannot produce
them either: a genuine mid-flight session expiry, and a real CSRF rejection.
Timing is the one thing the live suite arranges — `expire_session_after_next_probe`
logs the session out immediately after the client checks it — because the client
verifies the session before every request, so an expiry set up beforehand would
be caught by that check instead of by the request it is meant to interrupt.

When changing a route or parser, update the corresponding contract tests, add a
`compat` quirk if the behavior is version-specific, and run the integration
suite against both the oldest and newest supported release
(`--forgejo-version 1.20 --forgejo-version 16`).

## Limitations and risk

Forgejo exposes no API for Projects/Kanban, so this tool resorts to browser-style
automation: it logs in with a username and password, keeps a session cookie, and
calls Forgejo's internal web routes, scraping HTML to recover ids and board
state. That is a fragile approach by nature, and this architecture is
intentionally a stop-gap:

- the routes and HTML selectors are **not a public contract** — they are
  undocumented and unversioned, so a Forgejo upgrade (even a minor one) can
  change markup or routes and silently break tools here;
- state is recovered by HTML scraping and regular expressions rather than a
  structured API, so parsing can drift;
- it authenticates as a **real user with a password**, not a scoped API token,
  which grants far more than the operations it performs;
- session state is stored locally as a sensitive cookie file;
- writes have no cross-request transaction or rollback, so a failed multi-step
  operation may leave partial state; and
- version adaptation covers the releases the integration suite exercises
  (1.20, 1.21 and majors 7 through 16). A newer release may need a new quirk,
  and an instance outside that window falls back to the newest known behavior
  without any guarantee that it fits.

Version detection and the quirk registry take some of the sting out of the first
two points — known differences are handled and regressions are caught early by
the live suite — but they do not make the approach robust.

Treat it as a best-effort convenience for personal or experimental use. Do not
put it on a critical path, run it against data you cannot afford to lose, or
depend on it for production workflows. Use the official Forgejo API for
operations it supports when building a broader integration, and if Forgejo ships
a stable Projects API, migrate to it rather than expanding the scraper.
