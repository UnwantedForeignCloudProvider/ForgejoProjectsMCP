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
- It was verified against **one instance (v15.0.7)** only.

Treat it as a **best-effort convenience / stop-gap for personal or experimental
use**. Do **not** put it on a critical path, run it against data you can't afford
to lose, or depend on it for production workflows. If/when Forgejo ships a real
Projects API, migrate to it. Use at your own risk; test against a throwaway repo
first.

## Install (local build)

Clone the repo and install it as a system-wide command with uv:

```bash
git clone https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP
cd ForgejoProjectsMCP
uv tool install .
```

This installs a `forgejo-projects-mcp` executable onto your PATH (in uv's tool
bin directory). If uv warns that the directory isn't on your PATH, run
`uv tool update-shell` once and restart your shell. Verify with:

```bash
forgejo-projects-mcp --help   # or: uv tool list
```

No `playwright install` step is needed — the tool uses Playwright's HTTP layer,
not a real browser. To pick up local code changes automatically, install with
`uv tool install --editable .`; to update after pulling changes, `uv tool
install . --force`; to remove, `uv tool uninstall forgejo-projects-mcp`.

## Configuration

Credentials come from environment variables:

| Variable | Example |
|---|---|
| `FORGEJO_URL` | `https://forge.example.com` |
| `FORGEJO_USERNAME` | `your-username` |
| `FORGEJO_PASSWORD` | `your-password` |

The authenticated session is cached at
`<config>/forgejo_projects_mcp/storage_state.json` and refreshed automatically
when it expires. `<config>` is `$XDG_CONFIG_HOME` if set, otherwise `~/.config`
— resolved in an OS-agnostic way (Linux, macOS, Windows) via `Path.home()`.

## Run

```bash
export FORGEJO_URL=... FORGEJO_USERNAME=... FORGEJO_PASSWORD=...
uv run forgejo-projects-mcp            # stdio MCP server
```

### Register with an MCP client

Once installed with `uv tool install .`, reference the command directly:

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
- `delete_issue`

**Milestones**
- `list_milestones`, `create_milestone`, `edit_milestone`,
  `close_milestone`, `reopen_milestone`, `delete_milestone`

Issue arguments use the **repo issue number** (what you see as `#N`); the server
resolves the internal id automatically.

## Notes

- Tested against Forgejo **v15.0.7**. The web routes are internal and unversioned,
  so a major Forgejo upgrade may require adjusting `client.py`.
- The Forgejo session cookie does **not** authorize `/api/v1`, so everything runs
  through the web routes.
