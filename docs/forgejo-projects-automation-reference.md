# Forgejo pages and internal web routes — automation reference

This is the source of truth for every Forgejo page, internal web route, form,
query parameter, response shape, DOM anchor, and page-specific behavior on
which `forgejo_projects_mcp` depends.

The contracts below were audited against
`src/forgejo_projects_mcp/client.py` on **2026-09-05** and live-verified against
**every published Forgejo release from 1.20 through 16** by the integration
suite (1.20, 1.21, then majors 7 to 16 — Forgejo's numbering skips 2 to 6). The original
route discovery was performed against **Forgejo v15.0.7**. These routes are
internal, unversioned Forgejo web interfaces and may change between Forgejo
releases.

Where behavior differs by version, the difference is recorded here *and*
encoded as a quirk in `src/forgejo_projects_mcp/compat.py`, so the client adapts
instead of failing. See [Architecture](architecture.md#version-adaptation).

When code starts depending on another Forgejo page detail, update this file in
the same change. Do not leave the only record of a route, field name, selector,
response quirk, or Forgejo-specific workaround in source comments or tests.

## 1. Scope and evidence

The implemented client supports repository-level resources under
`/{owner}/{repo}`. Organization- and user-level project prefixes such as
`/{org}/-/projects` and `/{user}/-/projects` are not implemented or verified.

Evidence in this document has three levels:

- **Implemented and offline-tested** means the current client uses the contract
  and the test suite checks it with a fake Forgejo request context.
- **Live-verified** means the integration suite exercises it against a real
  instance of every supported release (1.20, 1.21, and majors 7 through 16).
- **Historically live-verified** means the original investigation exercised it
  against Forgejo v15.0.7.
- **Observed or inferred only** means the current client does not use it; do not
  rely on it without live verification.

The current implementation uses Playwright's HTTP-only `APIRequestContext`.
It does not launch a browser or use a persistent browser profile.

At the time of the v15.0.7 investigation, Projects/Kanban did not have the
needed `/api/v1` coverage. Issues, milestones, labels, comments, and assignees
did have official API coverage. The client nevertheless uses the internal web
routes documented here for all of its operations.

## 2. Identifier semantics

Forgejo exposes two different identifiers for an issue. They must not be
interchanged.

| Name | Meaning | Source |
|---|---|---|
| `project_id` | Repository-local project identifier | `/projects/{project_id}` links |
| `column_id` | Project-column identifier | `.project-column[data-id]` |
| `issue_number` | Repository-local issue number used in `/issues/{issue_number}` | Card link and `<span class="index">` |
| `issue_id` | Global Forgejo database ID used by board mutations | Card `data-issue` or issue-page `data-issue-id` |
| `milestone_id` | Repository milestone identifier | `/milestone/{milestone_id}` links |

In particular, `.issue-card[data-issue]` contains the global `issue_id`, not the
repository-local issue number. The latter is parsed from the card's
`/issues/{number}` link.

The accepted logical states are `open`, `closed`, and `all`. Project and
milestone list pages do not provide a working `all` view: Forgejo v15.0.7
silently showed open entries for `state=all`. The client therefore requests
`open` and `closed` separately and merges them by ID. The issues list does
receive `state=all` directly.

## 3. Authentication, session, and generic request behavior

### Authentication pages

| Purpose | Method | Path | Form or success condition |
|---|---|---|---|
| Check session | GET | `/user/settings` | HTTP 200 means authenticated; redirects are disabled |
| Log in | POST | `/user/login` | `user_name`, `password`, `remember=on` |

The session check reads its response *body* as well as its status, because
every rendered Forgejo page carries the instance version and, on releases that
need one, the session CSRF token. One request therefore establishes all three.
The markers, in the order the client tries them:

| Value | HTML contract | Verified on |
|---|---|---|
| Version | `assetVersionEncoded: encodeURIComponent('<version>')` in `window.config` | 1.20-16 |
| Version (fallback) | `/assets/js/index.js?v=<version>` on the page's own script tag | 1.20-16 |
| Version (fallback) | `Version:` followed by a link in the footer | admins, when the footer version is shown |
| CSRF token | `csrfToken: '<token>'` in `window.config` | 1.20-13 |
| CSRF token (fallback) | `<input name="_csrf" value="<token>">` in any form | 1.20-13 |

The version string appears as `16.0.3~gitea-1.22.0` in HTML and
`16.0.3+gitea-1.22.0` over the REST API; both parse to the same version.

Login redirects are disabled. Status 301, 302, 303, 307, or 308 is treated as
the initial success signal; a re-rendered HTTP 200 is treated as invalid
credentials. The client then checks `/user/settings` again before accepting the
session.

The session is stored as Playwright storage state at
`$XDG_CONFIG_HOME/forgejo_projects_mcp/storage_state.json`, or under
`Path.home()/.config/forgejo_projects_mcp/` when `XDG_CONFIG_HOME` is unset.
The session cookie observed on v15.0.7 was `i_like_gitea` with `HttpOnly`.

Every request context sends `Origin: {FORGEJO_URL}`. **CSRF handling is
version-specific**, and this is the largest behavioral difference found across
supported releases:

| Versions | Behavior | Client |
|---|---|---|
| 14.0 and newer | A matching `Origin` header is accepted; no token is needed | Sends no token (`csrf_mode: origin`) |
| 9.0 to 13.x | A write carrying only `Origin` is rejected with HTTP 400 and the body `Invalid CSRF token.` | Sends `X-Csrf-Token` on every non-GET (`csrf_mode: token`) |
| Below 9.0 | A write carrying only `Origin` is **silently discarded**: the response is HTTP 303 to `/` with an empty body, and nothing is written | Sends `X-Csrf-Token` on every non-GET (`csrf_mode: token`) |

Both `X-Csrf-Token` and an `_csrf` form field are accepted by the versions that
require a token; the client uses the header so JSON bodies (the card-move
route) are covered by the same mechanism.

Detection is not a single point of failure: a write rejected with HTTP 400 and
an `Invalid CSRF token` body is retried once with a token, and the session
switches to token mode from then on. That covers an instance whose version
cannot be read or whose behavior does not match its version.

**That recovery cannot work below Forgejo 9.0.** Those releases answer a
token-less write with the same `303` a successful write returns, so a dropped
write is indistinguishable from a completed one and there is nothing to detect.
The normal path is unaffected — the profile selects token mode from the version
before any write is attempted — but an instance below 9.0 whose version the
client cannot read at all will accept writes that do nothing. Anyone running one
should confirm `forgejo_status` reports a version rather than `null`.

Personal access tokens were not accepted by these internal web routes in that
investigation.

If a non-login request redirects to, or ends at, `/user/login`, the client logs
in again and retries the request once. Other redirects are normally followed;
write operations explicitly documented with redirect handling below disable
redirect following.

HTTP 400 and above is an error. For JSON errors, the client reads the
`message` member. For HTML errors, it exposes only the first concise `<p>`
value, not the raw page.

### Rate limiting

All requests share these client-side limits:

- Concurrent requests: `FORGEJO_MCP_MAX_CONCURRENCY`, default `8`, minimum `1`.
- Steady request rate: `FORGEJO_MCP_RPS`, default `5`, minimum `0.1`.
- HTTP 429 and 503 are retried up to two times. A numeric `Retry-After` value is
  honored; otherwise the delay is two seconds.

## 4. Repository search endpoint

Repository selection uses a JSON web endpoint rather than an HTML page.

| Method | Path | Query |
|---|---|---|
| GET | `/repo/search` | `q={query}`, `limit={limit}`, `page={page}` |

The response is expected to contain a `data` array. Each item may either be the
repository object itself or wrap it in `repository`. The client reads
`full_name`, `description`, `private`, `archived`, `empty`, and `fork`; owner
and repository name are derived by splitting `full_name`.

## 5. Project pages

### Routes and forms

| Action | Method | Path | Query or form |
|---|---|---|---|
| List one state | GET | `/{owner}/{repo}/projects` | Query `state=open` or `state=closed` |
| Create | POST | `/{owner}/{repo}/projects/new` | `redirect`, `title`, `content`, `template_type`, `card_type` |
| View board | GET | `/{owner}/{repo}/projects/{project_id}` | — |
| Read edit form | GET | `/{owner}/{repo}/projects/{project_id}/edit` | — |
| Update | POST | `/{owner}/{repo}/projects/{project_id}/edit` | `redirect`, `title`, `content`, `card_type` |
| Close | POST | `/{owner}/{repo}/projects/{project_id}/close` | Empty form |
| Reopen | POST | `/{owner}/{repo}/projects/{project_id}/open` | Empty form |
| Delete | POST | `/{owner}/{repo}/projects/{project_id}/delete` | Empty form |

The list parser finds double-quoted anchors ending in `/projects/{id}` and uses
their direct text as the title. Forgejo may render several links for one
project; the client keeps the longest non-empty title found for each ID and
returns projects sorted by ID.

For a logical `state=all`, the client performs two list requests (`open`, then
`closed`) and merges the results by project ID. Invalid states are rejected
before a request is sent.

### Project creation

The create form sends:

```text
redirect=
title={title}
content={description}
template_type=
card_type=1|2
```

`card_type=1` means text; `card_type=2` means images and text. Public tool values
`text` and `images_and_text` are mapped to those numeric strings. The client
always requests an empty template type.

Redirect following is disabled. The response does not supply an ID that the
client consumes, so it lists open projects and selects the last project, in ID
order, whose title exactly matches. If none matches, it falls back to the last
open project; if the list is empty, the returned project is `null`. Duplicate
titles or concurrent creation can therefore make ID recovery ambiguous.

### Project update

Before updating, the client reads the edit page and extracts:

- Current title from `name="title" ... value="..."`.
- Current card type from `name="card_type" ... value="..."`.

An omitted title and card type are preserved. The implementation does not read
the existing description: when `description` is omitted it still sends
`content=` and clears the description. This is current client behavior and an
important destructive edge case.

Project close, reopen, and delete disable redirect following. Project deletion
must use POST; DELETE returned HTTP 500 during the v15.0.7 live investigation.
The reopen route is implemented and offline-tested but was not live-verified in
the original investigation.

## 6. Project board HTML contract

`GET /{owner}/{repo}/projects/{project_id}` supplies the board data.

The current parser depends on these anchors:

| Value | HTML contract | Versions |
|---|---|---|
| Board title | Text of the project heading `<h2>`: class `tw-mb-0 tw-flex-1 tw-break-anywhere` (10-16), `gt-mb-0` (1.21-9), or `project-title` (1.20) | 1.20-16 |
| Real column | `<div class="...">` carrying `project-column` as a whole class token (optionally after other classes, and followed by a space or the closing quote) | 1.21-16 |
| Real column | The same shape with the class token `board-column` | 1.20 |
| Column ID | `data-id="{column_id}"` on that opening tag | 1.20-16 |
| Column title | Direct text under an element whose class contains `project-column-title-label` | 7-16 |
| Column title | Text inside `project-column-title` (1.21) or `board-label` (1.20), after the nested issue-count badge | 1.20-1.21 |
| Card/global issue ID | `data-issue="{issue_id}"` | 1.20-16 |
| Card issue number | `/issues/{issue_number}` link within the card block | 1.20-16 |
| Card title | Direct text of that issue link | 1.20-16 |

Matching `project-column` as a whole class token prevents elements such as
`project-column-header`, `project-column-title`, and
`new-project-column-modal` from becoming fake columns. The token may be
preceded by other classes: Forgejo 1.21 renders `class="ui segment
project-column"`, while 7 and newer render `class="project-column"`.

**Forgejo 1.20 predates the rename of project "boards" to "columns"** and has no
`project-column` markup at all: a column is `<div class="ui segment
board-column" data-id="N">`, its header is `board-column-header`, and its title
sits in a `board-label` element after a `board-card-cnt` badge. The
`legacy-board-vocabulary` quirk swaps in those patterns below 1.21. The hidden
new-column form is named for the same vocabulary there (`new-board-modal`
rather than `new-project-column-modal`).

On the 1.x line the default destination is rendered as an ordinary column
titled *Uncategorized* with `data-id="0"`, so a parsed board on those releases
contains a column whose id is `0`. Column ids are therefore non-negative rather
than positive, and `0` is a value the move routes accept.

The board title is read from the project heading rather than the page
`<title>`, because the two disagree by version: Forgejo 10 and newer render
`<title>Board name - owner/repo - ...</title>`, while **1.20 through 9 render
only `<title>owner/repo - ...</title>`** and never name the board there. The
heading is always present; only its class changed, tracking Forgejo's move from
the `gt-` utility prefix to Tailwind's `tw-`. The `<title>` forms remain as ordered
fallbacks for 10 and newer; the `board-title-missing-from-page-title` quirk
removes them below 10, where they would silently return the repository name.

The parser is regex-based and assumes the relevant double-quoted attributes
and their current ordering. Nested or substantially changed markup can break
it. Empty/missing values generally become an empty title, a missing issue
number, or an omitted column rather than a dedicated parse error.

The historically observed `.ui.cards` column container is not used by the
current parser. Do not depend on the previously documented
`.ui.cards[data-url=...]` selector without re-verifying it.

## 7. Column routes

| Action | Method | Path | Form or JSON |
|---|---|---|---|
| Create | POST | `/{owner}/{repo}/projects/{project_id}` | Form `title`, `color` |
| Edit | PUT | `/{owner}/{repo}/projects/{project_id}/{column_id}` | Form containing supplied `title` and/or `color` |
| Delete | DELETE | `/{owner}/{repo}/projects/{project_id}/{column_id}` | — |
| Set default | POST | `/{owner}/{repo}/projects/{project_id}/{column_id}/default` | Empty form |
| Reorder columns | POST | `/{owner}/{repo}/projects/{project_id}/move` | Inferred JSON `{columns:[{columnID, sorting}]}`; not implemented |

Colors are optional hex strings such as `#e01e5a`. Column edit must use PUT;
POST returned HTTP 405 during live verification.

After creation, the client reads the board and returns the last column whose
title exactly matches. If no title matches, the returned column is `null`.

The default column cannot be deleted. The client adds guidance to select
another default column when Forgejo rejects such a deletion. Historically, a
new project exposed an implicit “Uncategorized” destination as its default;
that observation is not a selector contract used by the current parser.

Column reordering and its payload were inferred from the UI but are not used or
covered by the client tests. Card movement is implemented separately.

## 8. Cards and issues

### Resolving issue numbers to global IDs

Board mutation endpoints require global `issue_id` values. Public tools accept
repository-local issue numbers, so the client first requests:

```http
GET /{owner}/{repo}/issues/{issue_number}
```

It extracts `data-issue-id="{issue_id}"`. If absent, resolution fails with
`ISSUE_NOT_FOUND`/HTTP 404. Normal add, remove, and move operations resolve
numbers sequentially; bulk move resolves them concurrently.

### Attach, detach, and move

| Action | Method | Path | Form or JSON |
|---|---|---|---|
| Attach issues | POST | `/{owner}/{repo}/issues/projects` | Form `id={project_id}`, `issue_ids={comma-separated global IDs}` |
| Detach issues | POST | `/{owner}/{repo}/issues/projects` | Form `id=0`, `issue_ids={comma-separated global IDs}` |
| Move/reorder cards | POST | `/{owner}/{repo}/projects/{project_id}/{column_id}/move` | JSON `{"issues":[{"issueID":ID,"sorting":0}]}` |

`sorting` is zero-based and follows the order supplied to the operation. The
JSON request explicitly uses `Content-Type: application/json`. The move route
historically returned `{"ok":true}`; the client returns parsed JSON when
possible and otherwise returns the HTTP status.

Attaching an issue places it in the project's default column. Detaching is the
equivalent of deleting a card and does not delete the issue itself.

Bulk movement groups requested cards by destination column and sends one move
request per column. Ordering is relative to each destination group.

### Create and delete issues

| Action | Method | Path | Form |
|---|---|---|---|
| Create | POST | `/{owner}/{repo}/issues/new` | `title`, `content`, plus optional `project_id`, `milestone_id`, `label_ids`, `assignee_ids` |
| Permanently delete | POST | `/{owner}/{repo}/issues/{issue_number}/delete` | Empty form |

`label_ids` and `assignee_ids` are comma-separated numeric IDs and are omitted
when their lists are empty. Project and milestone fields are omitted when they
are `null`.

The new issue's number arrives in one of **two shapes, depending on the
release**:

| Versions | Response |
|---|---|
| 1.21 and newer | HTTP 200 with JSON `{"redirect":"/.../issues/{issue_number}"}` |
| 1.20 | HTTP 303 with `Location: /.../issues/{issue_number}` and an empty body |

The client tries the JSON redirect first and falls back to the `Location`
header, so both are handled without needing to know the version. If neither
carries the path, creation is still reported as successful but the returned
issue number is `null`.

Permanent issue deletion is distinct from detaching a project card.

## 9. Issue list and detail pages

### Filtered issues list

The client obtains issue numbers from:

```http
GET /{owner}/{repo}/issues?state={open|closed|all}&type=all
```

It optionally adds direct-value query parameters:

```text
project={project_id}
milestone={milestone_id}
```

All `/issues/{number}` occurrences in the returned HTML are collected,
deduplicated, numerically sorted, and then read individually. This parser does
not distinguish issue links in the main result list from unrelated matching
links elsewhere on the page.

### Issue detail DOM contract

The parser for `GET /{owner}/{repo}/issues/{issue_number}` depends on:

| Value | HTML contract |
|---|---|
| Number | `<span class="index">#{number}</span>` |
| Title | `<meta property="og:title" content="{title}">` |
| Closed state | `issue-state-label` containing an SVG class with `octicon-issue-closed` |
| Raw issue body | `<div id="issue-{issue_id}-raw" ...>...</div>` — keyed by the **global** issue id, not the number |
| Milestone | Link ending in `/milestone/{milestone_id}` with direct text |
| Real comment block | `<div class="timeline-item comment" id="issuecomment-{id}">` |
| Comment author | Anchor whose class starts with `author` |
| Raw comment body | `<div id="issuecomment-{id}-raw" ...>...</div>` |

Timeline event blocks are intentionally excluded from comments. Parsed HTML
entities are unescaped. If the closed-state selector is absent, the parser
defaults to `open`; it does not independently prove that the issue is open.
Raw body extraction stops at the first closing `</div>`, so nested markup would
not be handled correctly. The page is expected to expose raw Markdown in these
elements.

The issue body's element id is the trap here. Issue *numbers* restart at 1 in
every repository, while issue *ids* keep counting across the instance, so the
two are equal only in the first repository an instance ever creates. The client
therefore reads `data-issue-id` from the page and looks the body up by that,
falling back to the number. A fixture — or a test instance with a single
repository — cannot tell the two apart, which is why the integration suite seeds
a second repository specifically to force them to diverge.

## 10. Composed read workflows

These are not additional Forgejo endpoints; they explain how the pages above
are combined.

- **Read card:** fetch and parse one issue detail page.
- **Bulk read issues:** fetch issue pages concurrently. Individual failures are
  returned inline instead of aborting the batch. `open`/`closed` filtering is
  applied after parsing; failures remain visible regardless of that filter.
- **Read column:** fetch the board, select the requested column, optionally use
  the filtered issues list for state/milestone filtering, then fetch each
  selected issue page.
- **Read project:** fetch the board, flatten cards in board/column order,
  optionally filter through the issues list, paginate, fetch issue pages, and
  reconstruct the column structure.
- **Read milestone:** merge open and closed milestone lists to verify the ID,
  query the issues list with the milestone and optional project filter, then
  fetch the selected issue pages.

`limit` and `offset` are local pagination applied after IDs have been scraped;
they are not sent to Forgejo. For an unfiltered project or column read
(`state=all` and no milestone), the issues list request is skipped. A missing
column or milestone produces a local HTTP-404-classified error.

## 11. Milestone pages

### Routes and forms

| Action | Method | Path | Query or form |
|---|---|---|---|
| List one state | GET | `/{owner}/{repo}/milestones` | Query `state=open` or `state=closed` |
| Create | POST | `/{owner}/{repo}/milestones/new` | `title`, `content`, `deadline` |
| Edit | POST | `/{owner}/{repo}/milestones/{milestone_id}/edit` | `title`, `content`, `deadline` |
| Close | POST | `/{owner}/{repo}/milestones/{milestone_id}/close` | `id={milestone_id}` |
| Reopen | POST | `/{owner}/{repo}/milestones/{milestone_id}/open` | `id={milestone_id}` |
| Delete | POST | `/{owner}/{repo}/milestones/delete` | `id={milestone_id}` |

The list page is plural (`/milestones`) but milestone links are singular
(`/milestone/{milestone_id}`). The parser reads double-quoted singular links,
keeps the longest non-empty direct text found for each ID, and sorts by ID.

For logical `state=all`, open and closed pages are fetched separately and
merged. The page silently showed open milestones for `state=all` during the
v15.0.7 investigation.

After creation, the client lists open milestones and returns the last exact
title match, or `null` if there is no match. Duplicate titles or concurrent
creation can make recovery ambiguous. `deadline` is sent as an optional
`YYYY-MM-DD` string; an empty string means no deadline.

Milestone edit always sends all three fields. Every omitted argument becomes
an empty string, so a nominally partial edit clears every unspecified title,
description, or deadline. This is current client behavior and a destructive
edge case.

The collection delete route is essential:

```http
POST /{owner}/{repo}/milestones/delete
id={milestone_id}
```

Do not use `POST /milestones/{milestone_id}/delete`; it was observed returning
HTTP 200 without deleting the milestone. Close and reopen use the item routes
but still include `id` in the form. Reopen is implemented and offline-tested,
but was not live-verified in the original investigation.

## 12. Observed routes outside current client coverage

These routes appeared in the original page investigation but are not called by
the current source. They are retained so that Forgejo page knowledge stays in
one place. Their exact forms and current-version behavior require verification
before implementation.

| Capability | Method | Path | Status |
|---|---|---|---|
| Update issue title | POST | `/{owner}/{repo}/issues/{issue_number}/title` | Observed, not implemented |
| Update issue content | POST | `/{owner}/{repo}/issues/{issue_number}/content` | Observed, not implemented |
| Update issue labels | POST | `/{owner}/{repo}/issues/labels` | Observed, not implemented |
| Update issue assignee | POST | `/{owner}/{repo}/issues/assignee` | Observed, not implemented |
| Reorder columns | POST | `/{owner}/{repo}/projects/{project_id}/move` | Payload inferred, not implemented |

The `/issues/projects` route and issue deletion route from the original helper
list are implemented and documented in section 8.

## 13. Verification record

### Historical live verification on Forgejo v15.0.7

- Created projects and exercised raw same-origin writes without `_csrf`.
- Created and edited columns with PUT, selected a default, and deleted a
  temporary non-default column.
- Created project issues, moved cards, and detached/re-attached issues through
  `/issues/projects`.
- Created, edited, closed, and deleted a milestone.
- Closed and deleted projects and deleted the test issues.
- Confirmed column edit is PUT, column delete is DELETE, and project delete is
  POST.

Project and milestone reopen, column reordering, organization/user-level
projects, and the unimplemented issue-sidebar routes were not live-verified in
that investigation.

### Live verification across Forgejo 1.20 through 16

The integration suite (`uv run pytest -m integration --forgejo-version N`) boots
a throwaway instance per version, seeds an admin, a repository, issues and a
milestone, then exercises the full surface: project create/list/read/rename/
close/reopen/delete, column create/edit/default/delete, card attach/move/bulk
move/detach, issue create-onto-board/read/delete, milestone create/edit/close/
reopen/delete, the composed project/column/milestone readers with paging, and
per-issue failure reporting in bulk reads.

Every route, form and DOM anchor in this document behaves identically on majors
7 through 16. The differences are all older, and all recorded above:

| Difference | Versions | Handled by |
|---|---|---|
| Writes need a CSRF token | below 14.0 | `csrf-token-required` quirk |
| Board title absent from the page `<title>` | below 10.0 | `board-title-missing-from-page-title` quirk |
| Columns are called "boards" in the markup | below 1.21 | `legacy-board-vocabulary` quirk |
| `issues/new` answers 303 rather than JSON | 1.20 | `Location` header fallback in `create_issue` |

Each is asserted by the suite, which runs every test once per requested version.

### Current offline verification

The tests assert the implemented request methods, paths, form/JSON payloads,
HTML parsing behavior, list-state merging, composed reads, and the corrected
milestone delete route. On the 2026-09-04 audit:

```text
uv run pytest -q
...................................................................      [100%]

uv run mkdocs build --strict
Documentation built successfully
```

In a sandbox where the normal user cache is read-only, `uv` can fail before
running tests with `Could not acquire lock`. Pointing its cache at a writable
temporary directory is an idempotent workaround:

```bash
UV_CACHE_DIR=/tmp/forgejo-projects-mcp-uv-cache uv run pytest -q
```

These are fake-context tests and do not replace an integration run against the
deployed Forgejo version. After a Forgejo release, run the integration suite
against it:

```bash
uv run pytest -m integration --forgejo-version <new> --forgejo-version 16
```

A failure there names the contract that moved. Record the difference in this
document, add a `compat.Quirk` scoped to the affected versions, and extend
`NEWEST_VERIFIED` once the new release passes.
