# forgejo-projects-mcp

An MCP server that lets an AI agent manage **Forgejo Projects / Kanban boards**,
which Forgejo does **not** expose over its REST API.

It works by driving the same internal web routes the browser uses, authenticated
with a session cookie. HTTP is done through Playwright's `APIRequestContext`, so
**no browser binary is downloaded** — only the `playwright` Python package is
needed. See `forgejo-projects-automation-reference.md` for the reverse-engineered
endpoints this is built on.

## ⚠️ This is a janky backend — do not rely on it for production

Forgejo exposes **no API** for Projects/Kanban, so this tool resorts to
**browser-style automation**: it logs in with a username and password, keeps a
session cookie, and calls Forgejo's **undocumented, unversioned internal web
routes** — scraping HTML to recover ids and board state. That is a fragile
approach by nature:

- These routes are **not a public contract**. A Forgejo upgrade (even a minor one)
  can change markup or routes and silently break tools here.
- State is recovered by **HTML scraping and regex**, not a structured API, so
  parsing can drift.
- It authenticates as a **real user with a password**, not a scoped API token,
  and performs writes with no transactional guarantees.

To take some of the sting out of that, the client detects the instance version
(from the same response that checks the session, so it costs no extra request)
and adapts its routes, parsing and CSRF handling to it. **Every published Forgejo
release from 1.20 to 16** is exercised end to end by an automated integration
suite that boots a throwaway instance per version. That does not make the approach robust — it
just means known differences are handled and regressions are caught early.

Treat it as a **best-effort convenience / stop-gap for personal or experimental
use**. Do **not** put it on a critical path, run it against data you can't afford
to lose, or depend on it for production workflows. If/when Forgejo ships a real
Projects API, migrate to it. Use at your own risk; test against a throwaway repo
first.

## Prerequisites

- **[uv](https://docs.astral.sh/uv/)** — used to install and run the tool.
  Install it from the official guide:
  <https://docs.astral.sh/uv/getting-started/installation/>.

## Installation

All methods install a `forgejo-projects-mcp` executable onto your PATH (in uv's
tool bin directory). If uv warns that the directory isn't on your PATH, run
`uv tool update-shell` once and restart your shell. Verify with
`forgejo-projects-mcp --help` (or `uv tool list`).

No `playwright install` step is needed — the tool uses Playwright's HTTP layer,
not a real browser.

### Latest release (PyPI)

```bash
uv tool install forgejo-projects-mcp
uv tool upgrade forgejo-projects-mcp     # update later
```

### Beta testing (latest from source)

Installs the current `main` branch straight from GitHub — newer than the last
release, and not guaranteed stable:

```bash
uv tool install git+https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP
uv tool upgrade forgejo-projects-mcp     # re-pull the latest main
```

### Local build (from a clone)

For development, or to install a specific checkout:

```bash
git clone https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP
cd ForgejoProjectsMCP
uv tool install .
```

To pick up local code changes automatically, install with
`uv tool install --editable .`; to update after pulling changes,
`uv tool install . --force`. To remove any of the above:
`uv tool uninstall forgejo-projects-mcp`.

## Configuration

Credentials come from environment variables:

| Variable | When required | Example |
|---|---|---|
| `FORGEJO_URL` | Always, so the cached session can be checked | `https://forge.example.com` |
| `FORGEJO_USERNAME` | When a fresh login is needed | `your-username` |
| `FORGEJO_PASSWORD` | When a fresh login is needed | `your-password` |

A `.env` file in the working directory is **loaded automatically** (via
python-dotenv) — copy `.env.example` to `.env` and fill it in; no `source`/
`export` needed. Real environment variables already set (and an MCP client's own
`env` block) take precedence. See `.env.example` for the full list, including the
optional `FORGEJO_MCP_MAX_CONCURRENCY`, `FORGEJO_MCP_RPS`, and
`FORGEJO_MCP_LOG_LEVEL`.

The authenticated session is cached at
`<config>/forgejo_projects_mcp/storage_state.json` and refreshed automatically
when it expires. Alongside it, the non-secret connection settings (instance URL
and username) are persisted to `<config>/forgejo_projects_mcp/config.json` after
a successful login. `<config>` is `$XDG_CONFIG_HOME` if set, otherwise `~/.config`
— resolved in an OS-agnostic way (Linux, macOS, Windows) via `Path.home()`.

Because the URL and username are persisted, **after the first successful login no
environment variables are required at all** — the cached session plus
`config.json` are enough. The password is **never** written to disk: it is
requested again (via env var, CLI option, or interactive prompt) only when
Forgejo requires a fresh login. Environment variables always take precedence over
`config.json`.

## Run

```bash
export FORGEJO_URL=... FORGEJO_USERNAME=... FORGEJO_PASSWORD=...
uv run forgejo-projects-mcp            # stdio MCP server
```

### Register with an MCP client

Once installed, reference the command directly:

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

If your MCP client doesn't inherit your shell PATH, use the absolute path to the
executable instead (find it with `which forgejo-projects-mcp`, or
`where forgejo-projects-mcp` on Windows).

## Agent installation

After `uv tool install .`, the `forgejo-projects-mcp` stdio command is on your
PATH. Register it with your agent below (replace the credential values). If the
command isn't found, use its absolute path (`which forgejo-projects-mcp`).

<details>
<summary><b>Claude Code</b></summary>

```bash
claude mcp add forgejo-projects-mcp \
  -e FORGEJO_URL=https://forge.example.com \
  -e FORGEJO_USERNAME=your-username \
  -e FORGEJO_PASSWORD=your-password \
  -- forgejo-projects-mcp
```

</details>

<details>
<summary><b>Codex</b></summary>

```bash
codex mcp add forgejo-projects-mcp \
  --env FORGEJO_URL=https://forge.example.com \
  --env FORGEJO_USERNAME=your-username \
  --env FORGEJO_PASSWORD=your-password \
  -- forgejo-projects-mcp
```

</details>

<details>
<summary><b>Qwen Code</b></summary>

```bash
qwen mcp add forgejo-projects-mcp \
  -e FORGEJO_URL=https://forge.example.com \
  -e FORGEJO_USERNAME=your-username \
  -e FORGEJO_PASSWORD=your-password \
  forgejo-projects-mcp
```

</details>

<details>
<summary><b>OpenClaw</b></summary>

```bash
openclaw mcp add forgejo-projects-mcp \
  --command forgejo-projects-mcp \
  --env FORGEJO_URL=https://forge.example.com \
  --env FORGEJO_USERNAME=your-username \
  --env FORGEJO_PASSWORD=your-password
```

</details>

<details>
<summary><b>opencode</b></summary>

opencode's `opencode mcp add` is an interactive wizard (no inline env flags), so
add it to `opencode.json` instead:

```json
{
  "mcp": {
    "forgejo-projects-mcp": {
      "type": "local",
      "command": ["forgejo-projects-mcp"],
      "environment": {
        "FORGEJO_URL": "https://forge.example.com",
        "FORGEJO_USERNAME": "your-username",
        "FORGEJO_PASSWORD": "your-password"
      }
    }
  }
}
```

</details>

<details>
<summary><b>Hermes</b></summary>

Hermes is config-file based — add to `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  forgejo-projects-mcp:
    command: forgejo-projects-mcp
    env:
      FORGEJO_URL: https://forge.example.com
      FORGEJO_USERNAME: your-username
      FORGEJO_PASSWORD: your-password
```

</details>

## Tools

**Session & discovery**
- `forgejo_status` — check authentication
- `authenticate(force=False)` — log in / refresh session
- `list_repositories(query, limit, page)` — repos the user can access (pick one to work in)

**Projects**
- `list_projects(owner, repo, state)`
- `create_project(owner, repo, title, description, card_type)`
- `get_project(owner, repo, project_id)` — board with columns + cards
- `update_project(...)`, `close_project(...)`, `reopen_project(...)`, `delete_project(...)`

**Columns**
- `create_column`, `edit_column`, `delete_column`, `set_default_column`

**Cards / issues**
- `create_issue(... project_id=)` — create an issue, optionally straight onto a board
- `add_issues_to_project`, `remove_issues_from_project`
- `move_card(owner, repo, project_id, column_id, issue_numbers)`
- `bulk_move_cards(owner, repo, project_id, moves)` — move many cards, each to its
  own column, in one call (`moves` = list of `{issue_number, column_id}`)
- `delete_issue`

**Bulk reads** (run concurrently, rate-limited)
- `bulk_read_issues(owner, repo, issue_numbers, state="all")` — lightweight
  summaries (number, title, state, milestone)
- `read_card(owner, repo, number)` — one card's full content (body + comments) ⚠️
- `read_column(owner, repo, project_id, column_id, state="all", milestone=None)` ⚠️
- `read_milestone(owner, repo, milestone_id, state="all", project=None)` ⚠️
- `read_project(owner, repo, project_id, state="all", milestone=None)` ⚠️

Optional filters on the readers use direct values (no name lookup): `state`
(`open`/`closed`/`all`), and a `milestone`/`project` **id** (each tool omits the
filter that is already its own subject).

The full readers take `limit`/`offset` to cap and page results, and return
`total` / `returned` / `truncated` / `error_count` so cost and completeness are
explicit. `bulk_read_issues` returns `count` (successful only) plus a separate
`errors` list.

⚠️ = network- and token-expensive; use only when needed. Concurrency and request
rate are tunable via `FORGEJO_MCP_MAX_CONCURRENCY` (default 8) and
`FORGEJO_MCP_RPS` (default 5).

**Error signaling.** Tool failures are returned as MCP errors (`isError: true`)
with a `[CODE] message` (e.g. `[NOT_FOUND]`, `[INVALID_STATE]`,
`[MILESTONE_NOT_FOUND]`, `[NETWORK_ERROR]`) — agents can detect failure without
parsing content. Invalid `state` values and missing projects/columns/milestones/
issues are hard errors, not silent empty results. Individual issues that fail to
read inside a bulk call are reported inline instead (partial success).

**Milestones**
- `list_milestones`, `create_milestone`, `edit_milestone`,
  `close_milestone`, `reopen_milestone`, `delete_milestone`

Issue arguments use the **repo issue number** (what you see as `#N`); the server
resolves the internal id automatically.

## CLI (no MCP client needed)

For harnesses that can't speak MCP, `forgejo-projects-cli` exposes **every tool
as a subcommand**, generated from the same tool definitions and dispatched
in-process — so it stays in sync automatically. It reads the same
`FORGEJO_URL` / `FORGEJO_USERNAME` / `FORGEJO_PASSWORD` env vars, prints the JSON
result to stdout, logs to stderr, and exits non-zero on an error result.

Credentials can also be passed as options, accepted **either before or after the
tool name**:

| Option | Notes |
|---|---|
| `--forgejo-url URL` | Overrides `FORGEJO_URL` and saved config |
| `--forgejo-username NAME` | Overrides `FORGEJO_USERNAME` and saved config |
| `--forgejo-password PASSWORD` | **Insecure** — visible in process lists / shell history |
| `--forgejo-password-stdin` | Reads the password from the first line of stdin (preferred) |

Precedence is **CLI option > env var > persisted `config.json`**. `--forgejo-password`
and `--forgejo-password-stdin` are mutually exclusive.

When stdin and stderr are attached to a terminal, missing or rejected
credentials are requested interactively. Password input is hidden, and login is
retried up to three times. Prompted credentials stay in memory; only the normal
session state and non-secret `config.json` are saved. Piped/automated CLI
invocations and the `forgejo-projects-mcp` stdio server never prompt.

```bash
forgejo-projects-cli --help                      # lists every tool
forgejo-projects-cli <tool> --help               # options for one tool

forgejo-projects-cli list_repositories --query kanban
forgejo-projects-cli create_project --owner o --repo r --title "Q3"
forgejo-projects-cli read_project --owner o --repo r --project_id 3 --state open
forgejo-projects-cli bulk_move_cards --owner o --repo r --project_id 3 \
    --moves '[{"issue_number": 5, "column_id": 12}]'

# One-shot with explicit credentials, password piped in (not in argv):
printf '%s\n' "$FORGEJO_PW" | forgejo-projects-cli \
    --forgejo-url https://forge.example.com --forgejo-username me \
    --forgejo-password-stdin list_repositories
```

Options mirror each tool's parameters (`--owner`, `--repo`, …); list/object
parameters (`--issue_numbers`, `--moves`) take a JSON string.

## Notes

- Verified against Forgejo **1.20, 1.21 and majors 7 through 16** — every
  published release — each exercised end to end by the integration suite
  (`uv run pytest -m integration --forgejo-version N`, which starts the instance
  for you). The web routes are internal and unversioned, so a future release may
  still need a new entry in `compat.py`.
- `forgejo_status` reports the detected version, the behavior in force for it,
  and whether that version is one the suite covers.
- The Forgejo session cookie does **not** authorize `/api/v1`, so everything runs
  through the web routes.
