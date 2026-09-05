# forgejo-projects-mcp

`forgejo-projects-mcp` gives an MCP-capable agent access to Forgejo Projects / Kanban boards, including operations that Forgejo does not currently expose through its REST API.

It can:

- authenticate with a Forgejo username and password;
- discover repositories the authenticated user can access;
- create, inspect, update, close, reopen, and delete projects;
- create and manage project columns;
- create, attach, detach, move, read, and delete issue cards;
- create and manage milestones; and
- expose the same operations through a generated command-line interface.

> **Important:** this is a best-effort automation layer over undocumented Forgejo web routes, not a versioned Projects API. It is verified against every published release from **1.20 through 16**, but those routes can change in any future one. Test it against a disposable repository before using it for important data. Read [Architecture](architecture.md#limitations-and-risk) and the [security policy](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/SECURITY.md) before deploying it.

## Choose a path

| I want to… | Start here |
|---|---|
| Install and try the tool for the first time | [Getting started](getting-started.md) |
| Choose an installation method or develop from source | [Installation](installation.md) |
| Set credentials, logging, throttling, or session storage | [Configuration](configuration.md) |
| Use MCP tools or the command-line interface | [Usage](usage.md) |
| Understand the modules and request lifecycle | [Architecture](architecture.md) |
| Look up a Python module | [Python API reference](reference/index.md) |
| Inspect the Forgejo web routes behind the client | [Automation reference](forgejo-projects-automation-reference.md) |

## Requirements

- Python **3.14 or newer**;
- [uv](https://docs.astral.sh/uv/) for installation and execution; and
- a Forgejo account with permission to manage the target repository.

The client uses Playwright's HTTP-only `APIRequestContext`; it does **not** launch a browser and does not require a browser binary download.

## Two interfaces, one tool surface

The normal entry point is the stdio MCP server:

```bash
forgejo-projects-mcp
```

For scripts, test harnesses, or environments without an MCP transport, use:

```bash
forgejo-projects-cli --help
```

The CLI discovers the registered MCP tools at startup, generates one subcommand per tool, and dispatches through the same server layer. The two interfaces therefore share authentication, validation, error handling, and behavior.

## Documentation conventions

- `owner` and `repo` identify a repository such as `team/platform`.
- `project_id`, `column_id`, and `milestone_id` are Forgejo internal numeric IDs.
- `number` and `issue_numbers` are repository issue numbers, the values shown as `#123` in Forgejo. The client resolves them to internal issue IDs when a web route needs those IDs.
- `state` accepts only `open`, `closed`, or `all` where documented.
- Operations marked **expensive** fetch full issue pages and can consume substantially more network traffic and context than summary operations.
