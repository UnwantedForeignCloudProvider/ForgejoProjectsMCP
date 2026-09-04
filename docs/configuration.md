# Configuration

Configuration is environment-based. The package also loads a `.env` file automatically before other package modules read the environment.

## Authentication variables

| Variable | Requirement | Description | Example |
|---|---|---|---|
| `FORGEJO_URL` | Always | Forgejo instance base URL. Trailing `/` characters are removed. | `https://forge.example.com` |
| `FORGEJO_USERNAME` | Fresh login only | Forgejo username used for web login. | `your-username` |
| `FORGEJO_PASSWORD` | Fresh login only | Forgejo account password used for web login. | `your-password` |

The URL is needed before the client can load and check a cached session.
Username and password are validated only when Forgejo requires a fresh login,
including `authenticate(force=true)`. A valid cached session can therefore be
used with only `FORGEJO_URL`. A value missing when it is required produces an
`AuthError` with code `MISSING_CONFIG`.

In an interactive terminal, `forgejo-projects-cli` prompts for missing values or
replacement credentials after a rejected login. It tries at most three prompted
credential sets. Prompts are written to stderr, the password is not echoed, and
only the resulting session cookie state is persisted. The MCP stdio server and
noninteractive CLI invocations never prompt, so configure their environment or
`.env` file before use.

The tool intentionally uses a session login rather than a personal access token. The internal web routes used for Projects are not covered by Forgejo's normal `/api/v1` token authentication.

## Optional variables

| Variable | Default | Description |
|---|---:|---|
| `FORGEJO_MCP_MAX_CONCURRENCY` | `8` | Maximum number of in-flight HTTP requests. The effective value is clamped to at least `1`. |
| `FORGEJO_MCP_RPS` | `5` | Global steady-state request rate for this process. The effective value is clamped to at least `0.1` requests/second. |
| `FORGEJO_MCP_LOG_LEVEL` | `INFO` | Python logging level for server logs, written to stderr. Common values are `DEBUG`, `INFO`, `WARNING`, `ERROR`, and `CRITICAL`. |
| `XDG_CONFIG_HOME` | `~/.config` | Base directory for the session cache. This follows the XDG convention when set; `Path.home()` is used otherwise. |

The concurrency and rate settings apply to every client request, including requests made by bulk and full-board readers. They are process-wide defaults read when `client.py` is imported. Invalid non-numeric values for the first two settings can prevent the module from starting.

## `.env` loading and precedence

Copy the example file:

```bash
cp .env.example .env
```

Then edit the values. `.env` is searched from the process's current working directory upward, not relative to the installed package directory. It is loaded automatically; `source .env` and `export` are not required.

The precedence is:

1. variables already present in the process environment;
2. an MCP client's explicit `env` block, which is also process configuration; and
3. values loaded from `.env`.

The dotenv loader does not override an existing variable. If you run the installed executable from another directory, either place `.env` in that directory or configure the variables in the shell/MCP client.

Quote values containing shell or dotenv special characters. For example:

```dotenv
FORGEJO_PASSWORD='p@ss$word!'
```

Never commit `.env`. The repository's `.gitignore` excludes it, but check `git status` before sharing a checkout.

## Session cache

After a successful login, the authenticated Playwright storage state is saved at:

```text
<config>/forgejo_projects_mcp/storage_state.json
```

where `<config>` is:

- `$XDG_CONFIG_HOME` when that variable is set; or
- `Path.home() / ".config"` otherwise.

Examples:

```text
Linux:   ~/.config/forgejo_projects_mcp/storage_state.json
macOS:   ~/.config/forgejo_projects_mcp/storage_state.json
Windows: <home>/.config/forgejo_projects_mcp/storage_state.json
```

The client reuses this state and checks `/user/settings`. If the session expires, it logs in again and replaces the cache. `authenticate(force=true)` ignores the current cached session and creates a fresh login.

The state does not store the configured URL, username, or password. A later
process still needs `FORGEJO_URL`; if the cached session has expired, it also
needs username and password or an interactive CLI prompt.

The state file contains authentication cookies and must be treated as a credential. Protect it with the operating system's file permissions, do not put it in source control, and remove it when decommissioning a machine or account. Changing `XDG_CONFIG_HOME` makes the process use a different cache; it does not migrate the old file.

## Logging and stdio

The stdio MCP transport owns stdout. The server therefore sends logs to stderr and reserves stdout for protocol messages. The CLI also prints JSON to stdout and sends logs to stderr. Do not redirect diagnostic output into an MCP protocol stream.

To troubleshoot request and parsing behavior:

```bash
FORGEJO_MCP_LOG_LEVEL=DEBUG forgejo-projects-cli forgejo_status
```

With an MCP client, put the same variable in its `env` block.

At `DEBUG`, the client reports authentication and session decisions, sanitized
request paths, response status and timing, throttle waits, retries, and parser
result counts. Query values, form and JSON values, credentials, cookies, and
parsed issue content are not logged.

## Tuning bulk operations

Bulk moves and reads use asynchronous fan-out. The client limits concurrency with a semaphore and spaces requests to the configured RPS. If Forgejo or a reverse proxy responds with `429` or `503`, the client honors an integer `Retry-After` header when present, otherwise waits two seconds, and retries up to two times.

Use lower values when sharing a small Forgejo instance or when a reverse proxy has a strict rate limit:

```dotenv
FORGEJO_MCP_MAX_CONCURRENCY=2
FORGEJO_MCP_RPS=1
```

These settings reduce pressure; they do not make the underlying undocumented routes stable.
