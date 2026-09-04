---
name: forgejo-projects-cli
description: >-
  Manage Forgejo Projects / Kanban boards (which Forgejo does not expose over its
  REST API) from the command line via `forgejo-projects-cli`. Use when the user
  wants to list, create, edit, move, or delete Forgejo projects, board columns,
  cards/issues, or milestones, or read a board/column/milestone's contents —
  especially in a harness without MCP support. Triggers: "forgejo project",
  "kanban board", "forgejo column/card/milestone", "move card", "read the board".
---

# forgejo-projects-cli

A command-line front end over the forgejo-projects-mcp tools. Every tool is a
subcommand; each call prints a JSON result to **stdout**, logs to **stderr**, and
exits **0 on success / non-zero on error**. Use this instead of the MCP server
when the environment can't speak MCP.

## Prerequisites

- The `forgejo-projects-cli` command must be installed (`uv tool install .` from
  the repo, or `uv tool install forgejo-projects-mcp`).
- These environment variables must be set (the CLI authenticates and caches the
  session automatically):
  - `FORGEJO_URL` — e.g. `https://forge.example.com`
  - `FORGEJO_USERNAME`
  - `FORGEJO_PASSWORD`

If they are missing, calls fail with `[MISSING_CONFIG]`. Do not put credentials
on the command line.

## Discovering commands (do this instead of guessing)

```bash
forgejo-projects-cli --help          # list every subcommand (tool)
forgejo-projects-cli <tool> --help   # exact options for one tool
```

Options mirror each tool's parameters as `--flag` (e.g. `--owner`, `--repo`,
`--project_id`). List/object parameters take a **JSON string**:
`--issue_numbers '[5,6]'`, `--moves '[{"issue_number":5,"column_id":12}]'`.

## Conventions

- **Ids vs numbers:** `--project_id`, `--column_id`, `--milestone_id` are numeric
  ids (get them from `list_projects` / `get_project` / `list_milestones`).
  Cards/issues are addressed by their **repo issue number** (the `#N` you see);
  the tool resolves the internal id itself.
- **`--state`** accepts `open`, `closed`, or `all`. An invalid value is a hard
  error (`[INVALID_STATE]`), not a silent default.
- **Filters** on readers use ids: `--milestone <id>`, `--project <id>`.
- **Output is JSON on stdout**; parse it. Logs go to stderr — ignore for parsing.
- **Errors:** a failed call prints `{"error": "[CODE] message"}` and exits
  non-zero. Missing project/column/milestone/issue → hard error (`[NOT_FOUND]` /
  `[MILESTONE_NOT_FOUND]` / `[COLUMN_NOT_FOUND]`), never an empty success.

## Typical workflow

1. Pick the repo: `forgejo-projects-cli list_repositories --query <name>` →
   note `owner` and `name`.
2. Inspect: `list_projects`, then `get_project` to see columns + card numbers.
3. Act (create/move/edit/delete).

## Recipes

```bash
# Discover
forgejo-projects-cli list_repositories --query kanban
forgejo-projects-cli list_projects --owner o --repo r --state all
forgejo-projects-cli get_project --owner o --repo r --project_id 3   # columns + cards

# Create structure
forgejo-projects-cli create_project --owner o --repo r --title "Q3 Roadmap"
forgejo-projects-cli create_column  --owner o --repo r --project_id 3 --title "To Do"
forgejo-projects-cli set_default_column --owner o --repo r --project_id 3 --column_id 10

# Cards
forgejo-projects-cli create_issue --owner o --repo r --title "Fix login" --project_id 3
forgejo-projects-cli move_card    --owner o --repo r --project_id 3 --column_id 11 \
    --issue_numbers '[5]'
forgejo-projects-cli bulk_move_cards --owner o --repo r --project_id 3 \
    --moves '[{"issue_number":5,"column_id":11},{"issue_number":6,"column_id":12}]'

# Milestones
forgejo-projects-cli create_milestone --owner o --repo r --title "v1.0" --deadline 2026-12-31
forgejo-projects-cli list_milestones  --owner o --repo r --state all
```

## Reading content — mind the cost

`read_card`, `read_column`, `read_milestone`, `read_project` fetch and return the
**full** content (body + all comments) of potentially many issues. They are
**network- and token-expensive** — avoid them unless the user asks for full
content or it is genuinely necessary. Prefer cheaper options first:

- `list_projects` / `get_project` / `list_milestones` for structure and ids.
- `bulk_read_issues --issue_numbers '[…]'` for lightweight summaries (number,
  title, state, milestone) of specific issues.

When you do use the full readers, cap cost with `--limit` and page with
`--offset`; they return `total` / `returned` / `truncated` / `error_count` so you
know how much you got. Narrow with `--state` and `--milestone`/`--project`.

```bash
# summaries (cheap)
forgejo-projects-cli bulk_read_issues --owner o --repo r --issue_numbers '[5,6,7]'
# full board, first 20 open cards only (expensive — use deliberately)
forgejo-projects-cli read_project --owner o --repo r --project_id 3 \
    --state open --limit 20 --offset 0
```

## Notes

- Destructive subcommands (`delete_project`, `delete_column`, `delete_issue`,
  `delete_milestone`) are permanent — confirm intent before running them.
- This drives Forgejo's internal web UI routes (no official API), so treat it as
  best-effort automation, not a production-critical interface.
