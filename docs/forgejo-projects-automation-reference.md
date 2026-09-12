# Forgejo pages and internal web routes — automation reference

This is the source of truth for every Forgejo page, internal web route, form,
query parameter, response shape, DOM anchor, and page-specific behavior on
which `forgejo_projects_mcp` depends.

The contracts below were audited against
`src/forgejo_projects_mcp/client.py` on **2026-09-06** and live-verified against
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

### How a write reports success, and how it reports refusal

**A status below 400 does not mean a write happened.** Forgejo's classic form
routes answer an accepted submission with a redirect, and a *refused* one by
re-rendering the form as HTTP 200 with a flash error. Both are below 400, so a
status check alone reads a refusal as a success.

The routes where a redirect is the success signal are listed in
`Profile.redirect_writes`, and on those — and only those — the client treats a
non-redirect as the rejection it is (`[REJECTED]`, HTTP 422), carrying the flash
message where one is rendered:

| Route | Success | Refusal |
|---|---|---|
| `POST /projects/new` | 303 to `/projects` | 200, re-rendered form |
| `POST /projects/{project_id}/edit` | 303 to `/projects` | 200, re-rendered form |
| `POST /milestones/new` | 303 to `/milestones` | 200, re-rendered form |
| `POST /milestones/{milestone_id}/edit` | 303 to `/milestones` | 200, re-rendered form |

Every other write route answers a *success* with 200, so the same rule must not
be applied to them:

| Route | Success | Notes |
|---|---|---|
| `POST /projects/{project_id}` (column create) | 200 `{"ok":true}` | JSON, not a redirect |
| `PUT`/`DELETE /projects/{project_id}/{column_id}` | 200 | |
| `POST /projects/{project_id}/{column_id}/default` | 200 | |
| `POST /projects/{project_id}/{column_id}/move` | 200 `{"ok":true}` | |
| `POST /issues/projects` | 200 | |
| `POST /issues/new` | 200 JSON (1.20: 303) | see section 8 |
| `POST /milestones/delete` | 200 `{"redirect": ...}` | **also 200 when nothing was deleted** |
| project/milestone `close` and `open` | 200 (1.20: 303) | |
| `POST /projects/{project_id}/delete` | 200 | 404 when already gone |

The redirect `Location` on an accepted create names the *collection*
(`/projects`, `/milestones`), never the new resource, so it cannot be used to
recover the new id. Recovery is by title; see the sections below.

The flash element carrying the refusal message is:

```html
<div class="ui negative message flash-message flash-error"><p>{message}</p></div>
```

Forgejo 13 added `hx-swap-oob="true"` to that element, and 16 renders an `id`
alongside the class list (which release introduced the `id` was not measured).
The `form_error` pattern therefore keys on the class token alone and tolerates
other attributes on either side of it. The message text
sits in a nested `<p>`; matching the container's own text would capture the
whitespace between the tags and report a refusal with a blank reason. Some
messages are untranslated i18n keys (`form.Title cannot be empty.`), and the
quoting of the date message differs (`&#39;` on 1.20, `&#34;` from 7 on).

Live-verified on every release from 1.20 to 16: the table above is identical on
all of them, so this is a base-profile contract rather than a quirk.

### Rate limiting and timeouts

All requests share these client-side limits:

- Concurrent requests: `FORGEJO_MCP_MAX_CONCURRENCY`, default `8`, minimum `1`.
- Steady request rate: `FORGEJO_MCP_RPS`, default `5`, minimum `0.1`.
- Per-request timeout: `FORGEJO_MCP_TIMEOUT`, in seconds, default `30`,
  minimum `1`. It is applied to the request context, so it bounds every call.
- HTTP 429 and 503 are retried up to two times. A numeric `Retry-After` value is
  honored; otherwise the delay is two seconds.

The timeout is set explicitly rather than left to Playwright's own 30-second
default, because an instance that accepts connections but never answers — a
stalled proxy, or a `FORGEJO_URL` pointing somewhere unrelated — is otherwise
indistinguishable from a hang for half a minute, with no way to shorten it. A
caller that would rather fail fast can lower it. Note that a *missing*
configuration never reaches this path at all: `FORGEJO_URL` is validated before
any connection is attempted, so `forgejo_status` answers immediately with
`authenticated: false` and `MISSING_CONFIG`.

## 4. Repository search endpoint

Repository selection uses a JSON web endpoint rather than an HTML page.

| Method | Path | Query |
|---|---|---|
| GET | `/repo/search` | `q={query}`, `limit={limit}`, `page={page}` |

The response is expected to contain a `data` array. Each item may either be the
repository object itself or wrap it in `repository`. The client reads
`full_name`, `private` and `fork`; owner and repository name are derived by
splitting `full_name`.

**The route answers with a trimmed repository object.** `description` is always
`""`, and `archived` and `empty` are always `false`, whatever the repository
really is (verified on 1.20, 14, 15 and 16 with a described, an archived and an
empty repository). `private` and `fork` are real. The client reports the three
trimmed fields as `null` rather than pass on a wrong answer, and a live test
asserts that the route still trims them, so a release that starts sending real
values is noticed.

The documented `GET /api/v1/repos/search` does return all of them, but it does
not honour the web session: called with the client's cookie it answers as an
anonymous user and omits the user's private repositories (verified on 1.20, 14,
15 and 16). A cached session carries no password to authenticate it another
way, so the client cannot use it.

**Pagination boundaries are Forgejo's, and the client does not clamp them**
(verified on 14, 15 and 16):

- a `limit` of `0` or less yields Forgejo's own default page size, neither an
  empty list nor an unbounded one;
- `page=0` behaves as `page=1`; there is no page zero;
- the endpoint publishes no total, so a short page is the only end-of-list
  signal. This is unlike the projects and milestones list pages, which the
  client walks to completion itself (see [§5a](#5a-list-pagination)).

The values are passed through deliberately: clamping them would make the client
assert a contract it does not own, and would mask an upstream change.

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

**These list pages are paginated — see [section 5a](#5a-list-pagination).** A
single request returns at most 20 entries and says nothing about the rest.

### Project creation

The create form sends:

```text
redirect=
title={title}
content={description}
template_type=
card_type=0|1
```

**`card_type=0` means text only; `card_type=1` means images and text.** The
values are those of the edit form's dropdown options, which render as:

```html
<input type="hidden" name="card_type" value="{stored}">
<div class="menu">
  <div class="item" data-id="0" data-value="0">Text only</div>
  <div class="item" data-id="1" data-value="1">Images and text</div>
</div>
```

Public tool values `text` and `images_and_text` map to `0` and `1`. Forgejo
**accepts any value here and stores anything it does not recognise as `0`**,
without an error — `2` and `bogus` both produce a text-only board — so the enum
is enforced client-side (`[INVALID_INPUT]`) instead of being left to the server.
Confirmed on 1.20 and 16 by creating a project per value and reading the stored
value back off the edit form. The client always requests an empty template type.

**Forgejo also accepts an empty project title**, answering 303 as it does for a
valid one, and creates a board that the list page then omits: `_parse_projects_list`
keeps only non-empty titles, and Forgejo itself renders the board's heading as
`owner/repo`. The board stays reachable by id but cannot be found again through
any list. The client refuses an empty or whitespace-only title before the
request (`[INVALID_INPUT]`) rather than creating something unfindable.

Redirect following is disabled. The redirect names `/projects`, not the new
board, so the client lists open projects and selects the last project, in ID
order, whose title exactly matches (compared against the stripped title, since
the list page renders titles stripped). If none matches it raises
`[CREATE_UNVERIFIED]`, saying the resource may exist and should be re-read.

It must not fall back to another project: an earlier implementation returned the
last open project when nothing matched, which meant a refused or hidden create
handed back a **live, unrelated board id** that a follow-up mutation would then
target. Duplicate titles or concurrent creation still make ID recovery ambiguous
in the sense that the newest match wins.

### Project update

The edit route is a **full replacement**: any field missing from the form is
cleared, not kept. Before updating, the client therefore reads the edit page and
extracts every field it is about to send:

| Field | HTML contract |
|---|---|
| Title | `<input name="title" ... value="{title}">` |
| Card type | `<input type="hidden" name="card_type" value="{0\|1}">` |
| Description | `<textarea name="content" ...>{description}</textarea>` |

An argument left unset is carried over from that read; passing an empty string
clears the field deliberately. An update that would leave the title blank is
refused for the same reason a blank create is (`[INVALID_INPUT]`) — Forgejo
accepts it and the board then disappears from the list pages.

Values recovered this way are **HTML-escaped**, because they come out of an
attribute or a textarea, and must be unescaped before being posted back. Posting
them as parsed would escape them once more on every partial edit, so a title
containing `&` would decay into `&amp;`, `&amp;amp;`, and so on.

The description textarea is matched by its `name` rather than by attribute
order, because Forgejo 10 replaced the plain textarea with the Markdown editor
and `name="content"` moved behind `class`/`aria-label` attributes.

Project close, reopen, and delete disable redirect following. Project deletion
must use POST; DELETE returned HTTP 500 during the v15.0.7 live investigation.
The reopen route is implemented and offline-tested but was not live-verified in
the original investigation.

## 5a. List pagination

`/{owner}/{repo}/projects` and `/{owner}/{repo}/milestones` both render **20
entries per page** and publish **no total, no page count, and no "next" marker
the client parses**. A request without a `page` parameter returns the first page
only, so reading one page silently returns a *prefix* of the list.

| Property | Behavior |
|---|---|
| Page size | 20, on both pages |
| Parameter | `?page=N`, combined with `state` |
| Page past the end | HTTP 200 with **zero** entries — not a clamp to the last page |
| Ordering, projects | Newest first, so the *oldest* fall off page 1 |
| Ordering, milestones | Oldest first, so the *newest* fall off page 1 |

The client therefore walks pages until one comes back empty or stops adding new
ids, and merges by id. Both stop conditions are load-bearing: the empty page is
the normal terminator, and the no-new-ids check is what would stop a server that
answered an out-of-range page by clamping to the last one instead.

The opposite orderings matter more than they look. Because milestones put the
newest on the last page, a milestone created once 20 already existed was not on
page 1 — and since creates verify themselves by title against this list
(sections 5 and 11), a successful creation came back as `[CREATE_UNVERIFIED]`.
Projects order the other way, so the same bug never showed there. Anything that
resolves a resource through these lists inherits this behavior.

Live-verified on **1.20, 1.21, 9, 13, 15 and 16**: identical page size, `page`
parameter, out-of-range response and orderings on all six, so this is base-
profile behavior with no quirk. The page walk is capped at
`_MAX_LIST_PAGES` (50, i.e. 1000 entries) purely as a runaway guard; reaching it
is logged as a warning rather than passed off as a complete list.

Forgejo's REST API (`/api/v1/repos/{owner}/{repo}/milestones`) is a usable
independent check for milestones and is what the integration suite compares
against; projects have no such API, which is why this client exists.

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
POST returned HTTP 405 during live verification. Both routes answer a success
with HTTP 200, so the redirect rule in section 3 does not apply to them.

**Column edit is a genuine partial update**, unlike the project and milestone
edit forms: a PUT carrying only `color` leaves the title untouched. Verified on
1.20 and 16. The client therefore sends only the fields it was given, and needs
no read-modify-write here.

**Forgejo accepts an empty or whitespace-only column title** on create and on
edit, producing a column that nothing in the board markup distinguishes from any
other unnamed one. The client refuses a blank title before the request
(`[INVALID_INPUT]`).

**A color Forgejo cannot parse is answered with a bare HTTP 500**, on create and
on edit alike. The accepted shape is **exactly six hex digits** after a `#`:
the CSS three-digit shorthand `#fff` reads like a valid color but produces that
same 500, verified live. The client checks the value against the profile's
`column_color` pattern before sending, so a typo is an `[INVALID_INPUT]` naming
the field rather than an upstream server error. Colors are not rendered back by
any reader the client parses, so a stored color cannot be verified through this
client — which is why `column_color` is asserted against a real instance rather
than against a fixture.

After creation, the client reads the board and returns the last column whose
title exactly matches. If no title matches it raises `[CREATE_UNVERIFIED]`
rather than returning `column: null` next to `created: true`, which read as a
success and was not one.

**Every column route that takes a column id answers an unknown one with a bare
HTTP 500**, except the move route. The default column also cannot be deleted,
and that failure is the *same* HTTP 500, so on delete the two were
indistinguishable:

| Route | Unknown column id | Column from another project |
|---|---|---|
| `PUT /projects/{project_id}/{column_id}` | 500 | 422 `ProjectColumn[N] is not in Project[M] as expected` |
| `DELETE /projects/{project_id}/{column_id}` | 500 | 422, same message |
| `POST /projects/{project_id}/{column_id}/default` | 500 | 422, same message |
| `POST /projects/{project_id}/{column_id}/move` | **404** | — |

The client therefore reads the board before edit, delete and set-default: an id
that is not on it is `[COLUMN_NOT_FOUND]` (404), and only a column that really
exists reaches Forgejo — so the "set another column as default first" guidance
is now attached solely to a genuine default-column refusal. The cross-project
case is left to Forgejo, which already explains it precisely; note that its
message names the internal `ProjectBoard` on Forgejo 1.20 and `ProjectColumn`
from 1.21 on, tracking the same rename as the board markup. Verified on 1.20 and
16.

Historically, a new project exposed an implicit “Uncategorized” destination as
its default; that observation is not a selector contract used by the current
parser.

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

**The attach route reports nothing a caller can check.** It answers a write that
changed nothing exactly as it answers one that worked: an ordinary redirect, no
body, no indication of which column the card was placed in (verified on 14, 15
and 16). This is the one board mutation with no success signal of its own — the
move route returns `{"ok":true}` — so the client reads the board back after
attaching and confirms each issue is a card, reporting `[ATTACH_UNVERIFIED]`
when it is not. Detach is not read back: `id=0` is idempotent and clearing an
assignment that was already clear is not a failure.

**Detaching takes no project argument, and there is no scoped alternative.** An
issue holds exactly one project assignment, so "remove from A while staying on
B" is not a state Forgejo can represent. The client's optional `project_id` is
therefore a client-side guard, not a scope: it refuses the call when an issue is
not a card on the named board. The route itself is unchanged.

**A misaddressed move names nothing.** Moving an issue that is not a card on the
project answers a bare HTTP 500, and moving to a column that is not on the
board answers a bare HTTP 404 (both verified on 1.20, 14, 15 and 16). The
client reads the board once before writing and reports `[CARD_NOT_FOUND]` or
`[COLUMN_NOT_FOUND]` (404) naming what is not on it, so "not here" is
distinguishable from a server fault. On 1.20 the default column is `0` and is
rendered even when empty, so it passes the check; on 14, 15 and 16 `0` is not
a column, and the route answers it with 404 like any other unknown id.

**`bulk_move_cards` is not atomic.** The route moves one column per request,
with no transaction and no undo. Checking every card and destination first
removes the deterministic cause of a half-applied batch, but a request can
still fail after others have landed (a column deleted meanwhile, a server
fault). The client then waits for every request to settle and reports
`[BULK_MOVE_PARTIAL]`, naming the columns that moved and those that did not. It
does not roll back: the board exposes card order but not the stored `sorting`
values a faithful rollback would need, and a rollback that failed would leave a
third state.

**Re-attaching an issue that is already on the project does not move its card.**
The card keeps its column and position; only a *different* project's id
reparents it (above). Verified on 15 and 16 across four sequences — default
column set before and after attaching, an Uncategorized column present, and the
issue batched with a second one — because a route that reset placement on retry
would make every attach unsafe to repeat, and a QA report claimed it did.

Attaching an issue places it in the project's default column. Detaching is the
equivalent of deleting a card and does not delete the issue itself.

**Detaching reports the issues it was asked about, not the ones it changed.**
`id=0` clears the assignment unconditionally, and Forgejo answers the same way
whether or not the issue was on a board, so an issue that was already detached
comes back in the `detached` list too. The list is therefore the *resulting
state* -- every number in it is now on no board -- rather than a record of work
performed. Telling the two apart would mean reading each issue's page before
writing, and the route offers nothing cheaper; callers that need that
distinction must read first.

**A card created by `issues/new` with a `project_id` materialises the default
column.** On Forgejo 7 and newer a fresh board has no columns at all, and the
first card added this way causes an `Uncategorized` column to appear and holds
the card. Verified on 15 and 16. The create response carries only the issue
number and title, so the resulting column is one board read away, not part of
the answer.

**An issue holds one project assignment, so attaching is really moving.**
Posting a second project's `id` for an issue that is already on a board removes
it from the first board; the route offers no way to keep an issue on two boards
at once. Verified on 15 and 16 by attaching one issue to project A, then to
project B, and reading both boards: A loses the card, B gains it. The client
cannot make this additive — it is how the route stores the relationship — so
`add_issues_to_project` documents the move, and the same single assignment is
what makes detaching global (below).

Detaching with `id=0` clears the issue's project assignment **globally**: an
issue attached to two boards is removed from both by one call. The route takes
no project argument, so there is no project-scoped removal to expose. This is
worth stating plainly wherever the operation is described, because the tool
name (`remove_issues_from_project`) reads as though it were scoped to one.

Bulk movement groups requested cards by destination column and sends one move
request per column. Ordering is relative to each destination group.

Because those per-column requests run concurrently, a batch naming the same
issue twice with different destinations used to send both — the card landed
wherever the later write completed, while the reply listed it under both
columns. Nothing in the route can express that intent, so the client refuses a
batch containing a repeated issue number (`[INVALID_INPUT]`) instead of
reporting a state that never existed. `move_card` applies the same rule to its
own `issue_numbers` list.

### Create and delete issues

| Action | Method | Path | Form |
|---|---|---|---|
| Create | POST | `/{owner}/{repo}/issues/new` | `title`, `content`, plus optional `project_id`, `milestone_id`, `label_ids`, `assignee_ids` |
| Permanently delete | POST | `/{owner}/{repo}/issues/{issue_number}/delete` | Empty form |

`label_ids` and `assignee_ids` are comma-separated numeric IDs and are omitted
when their lists are empty. Project and milestone fields are omitted when they
are `null`.

**Forgejo validates these sidebar references only as it applies them, and
reports a failure without naming the field** — and not even with a consistent
status. Measured on 1.20 and 16:

| Reference | 1.20 | 16 | Issue created? |
|---|---|---|---|
| Unknown `milestone_id` | 500 | 500 | No |
| `milestone_id` from another repository | 500 | 500 | No |
| Unknown `project_id` | **500** | **404** | No |
| Unknown `assignee_ids` | 500 | 500 | No |
| Unknown `label_ids` | 200, **silently ignored** | 200, **silently ignored** | Yes |

The client turns the ambiguous cases into a stable answer: on any error from
this route it looks up whichever of `project_id`/`milestone_id` was supplied and,
if one is genuinely absent, reports `[PROJECT_NOT_FOUND]` or
`[MILESTONE_NOT_FOUND]` (404) — so the same input produces the same code on
every release, despite 1.20 and 16 disagreeing on the status. The lookup is what
authorises the relabelling, so a 404 from an unknown *repository* keeps its own
meaning. When every supplied reference checks out, the upstream error is kept
and a note names the fields worth re-reading (an unusable assignee cannot be
looked up through any route the client uses). None of this costs anything on a
successful create: the diagnosis runs only after a failure.

An unknown label id is the one case with no error at all — the issue is created
and the label is silently dropped. The client cannot detect this, because it has
no labels route; a caller that needs labels applied must read the issue back.

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

**An unknown `project` or `milestone` id matches nothing rather than erroring.**
Forgejo applies both as direct values and renders an ordinary empty issue list
for an id that does not exist, which is indistinguishable from a filter that
genuinely has no matching issues (verified on 14, 15 and 16). The client
therefore confirms a caller-supplied filter id against the project or milestone
list before querying, and reports `[PROJECT_NOT_FOUND]`/`[MILESTONE_NOT_FOUND]`.
The confirmation is skipped when no filter is passed, and for the project or
milestone that is the reader's own subject, which has already been resolved.

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

The board reader and the composed readers return deliberately different card
shapes, and neither is a subset of the other: `get_project` gives card identity
(`issue_id`, `number`, `title`) straight from the board markup, while
`read_project` gives issue *content* (`number`, `title`, `state`, `body`,
`milestone`, `comments`) fetched from each issue page. Only `number` is a
mutation input, and both carry it; `issue_id` is internal and must not be passed
to a card operation (section 2).

- **Read card:** fetch and parse one issue detail page.
- **Bulk read issues:** collapse repeated issue numbers to their first
  occurrence, then fetch issue pages concurrently. Individual failures are
  returned inline instead of aborting the batch. `open`/`closed` filtering is
  applied after parsing; failures remain visible regardless of that filter.
- **Read column:** fetch the board, select the requested column, confirm a
  caller-supplied milestone filter against the milestone list, optionally use
  the filtered issues list for state/milestone filtering, then fetch each
  selected issue page.
- **Read project:** fetch the board, flatten cards in board/column order,
  confirm a caller-supplied milestone filter against the milestone list,
  optionally filter through the issues list, paginate, fetch issue pages, and
  reconstruct the column structure.
- **Read milestone:** merge open and closed milestone lists to verify the ID,
  confirm a caller-supplied project filter against the project list, query the
  issues list with the milestone and optional project filter, then fetch the
  selected issue pages.
- **Attach issues:** resolve each issue number to its global ID, post the attach
  form, then read the board back and confirm each issue is a card, reporting the
  column it landed in.

`limit` and `offset` are local pagination applied after IDs have been scraped;
they are not sent to Forgejo. For an unfiltered project or column read
(`state=all` and no milestone), the issues list request is skipped, and so is
the filter confirmation. A missing column or milestone, and a filter naming a
project or milestone that does not exist, produce a local HTTP-404-classified
error. A reader never re-confirms the project or milestone that is its own
subject: reading the board or resolving the milestone has already proved it.

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
v15.0.7 investigation. **This page is paginated at 20 entries with the newest
last — see [section 5a](#5a-list-pagination); it is the list whose truncation
broke milestone creation.**

After creation, the client lists open milestones and returns the last exact
title match, or raises `[CREATE_UNVERIFIED]` if there is no match. Duplicate
titles or concurrent creation can make recovery ambiguous. `deadline` is sent as
an optional `YYYY-MM-DD` string; an empty string means no deadline.

Unlike projects and columns, **Forgejo refuses a blank milestone title itself**
— and refuses an impossible date such as `2026-02-30` — by re-rendering the form
as HTTP 200 (section 3). The client rejects a blank title before the request all
the same, so the three resources behave alike, and reads the refusal for
everything else.

### Milestone edit

The edit route is a **full replacement**, exactly like the project one, and
Forgejo refuses the whole submission when the title arrives empty. Sending only
the field being changed therefore did the worst possible thing: a
description-only or deadline-only edit submitted a blank title and was refused
outright, while a title-only edit succeeded and silently cleared the description
and the deadline.

The client now reads the edit form first and merges, so an argument left unset
is preserved and an empty string clears the field deliberately:

| Field | HTML contract |
|---|---|
| Title | `<input name="title" ... value="{title}">` |
| Deadline | `<input type="date" id="deadline" name="deadline" value="{YYYY-MM-DD}">` |
| Description | `<textarea ... name="content" ...>{description}</textarea>` |

As on the project edit page, the textarea is matched by `name` rather than
attribute order: Forgejo 10 moved the milestone description into the Markdown
editor, which renders `class` and `aria-label` before `name`.

There is no Forgejo defect behind the deadline: with a title in the form, the
new deadline persists on every release from 1.20 to 16.

The collection delete route is essential:

```http
POST /{owner}/{repo}/milestones/delete
id={milestone_id}
```

Do not use `POST /milestones/{milestone_id}/delete`; it was observed returning
HTTP 200 without deleting the milestone. Close and reopen use the item routes
but still include `id` in the form. Reopen is implemented and offline-tested,
but was not live-verified in the original investigation.

**The collection delete route answers HTTP 200 with `{"redirect": ...}` whether
or not the milestone existed**, so a delete of something already gone is
indistinguishable from a real one and cannot be detected after the fact. The
client resolves the milestone against the merged open/closed list first and
raises `[MILESTONE_NOT_FOUND]` (404) when it is absent, so `deleted: true` means
a milestone was actually removed. This is the one delete in the client that
cannot be made idempotent-and-honest by reading the response.

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

### Write-integrity behavior: uniform, so not a quirk

The success/refusal signalling in section 3, the acceptance of blank project and
column titles, the `card_type` values and their silent normalisation, and the
unconditional HTTP 200 from `milestones/delete` were probed directly on **every
supported release — 1.20, 1.21, and majors 7 through 16**. All twelve behave
identically, so none of it is version-scoped: the contracts live in the base
profile (`Profile.redirect_writes`, `Profile.card_types`, the `form_error` and
edit-form patterns) with no `Quirk` attached.

The probe did surface three version differences, none of which affects the
client, and all of which are the reason `redirect_writes` names four routes
rather than "every write":

| Difference | Versions | Why it does not matter |
|---|---|---|
| project/milestone `close` and `open` answer 303, not 200 | 1.20 only | Not in `redirect_writes`; the client checks neither |
| `issues/new` refuses a blank title with 200, not 400 | 1.20 only | The client does not create issues with blank titles |
| Attaching to an unknown project answers 404, not 500 | 16 (older: 500) | Both are errors and surface as such |

`test_the_redirect_writes_really_do_redirect_on_this_version` and
`test_a_refused_form_is_recognised_on_this_version` in
`tests/integration/test_live_compat.py` assert both halves of the section 3
contract per version, so a release that switches one of those four routes to
200-on-success fails the suite instead of silently turning every write on it
into a reported rejection.

### Column error shapes: uniform on the releases probed

The per-route error table in section 7 was probed on **1.20 and 16** — the two
ends of the supported range — by addressing each column route with an id that
exists nowhere and with one belonging to a different project. Both releases
answer identically, apart from the internal type name inside Forgejo's
cross-project message (`ProjectBoard` on 1.20, `ProjectColumn` from 1.21),
which the client does not parse. No quirk is involved:
`test_every_column_operation_says_so_for_an_unknown_id` runs against every
requested version, so a release that starts distinguishing these cases itself
will show up as a failure rather than as a silently redundant board read.

### What is *not* a Forgejo contract

These behaviors look like instance behavior but are the client's own, and are
recorded here so they are not re-diagnosed as Forgejo defects:

- **A refused filter is not an empty board.** Forgejo answers an unknown
  `project=`/`milestone=` filter with an ordinary empty issue list; the
  `[PROJECT_NOT_FOUND]`/`[MILESTONE_NOT_FOUND]` raised for one is the client
  confirming the id first (section 9).
- **A guarded detach is not a scoped route.** `remove_issues_from_project`'s
  optional `project_id` is checked client-side; the route it posts is the same
  unscoped one either way (section 8).
- **A strictly rejected identifier is not a Forgejo validation error.**
  `[INVALID_INPUT]` for `"7"` where `7` was meant is argument validation in this
  client, before any request is sent. Forgejo never sees the call.

- **A stalled request is not a hang.** Every request is bounded by
  `FORGEJO_MCP_TIMEOUT` (section 3). Before that bound was set explicitly the
  limit was Playwright's own 30-second default, which made an unreachable-but-
  connectable instance look like an indefinite hang.
- **A missing configuration is not a connection failure.** `FORGEJO_URL` is
  validated before any connection is attempted, so `forgejo_status` answers in
  well under a second with `MISSING_CONFIG`. A `forgejo_status` that *does* take
  30 seconds is reaching a configured instance that does not answer — note that
  the package loads a `.env` searched from the working directory upward, so a
  command run inside a checkout inherits that file's credentials even when the
  surrounding shell defines none. See
  [Configuration](configuration.md#env-loading-and-precedence).

### Current offline verification

The tests assert the implemented request methods, paths, form/JSON payloads,
HTML parsing behavior, list-state merging, composed reads, the corrected
milestone delete route, and the write-integrity rules: that a refused form is
reported as `[REJECTED]` rather than a success, that blank titles and unknown
card types are refused before a request is sent, that a partial project or
milestone edit preserves the fields it was not given, and that a create which
cannot be matched back raises instead of returning an unrelated resource, and
that every column operation rejects an unknown id without issuing the request,
that an attachment which produced no card is `[ATTACH_UNVERIFIED]` rather than a
reported success, that an unusable reader filter is named instead of returning
an empty result, that a repeated issue number is read once, and that a
stringified or fractional identifier is refused rather than coerced.
On the 2026-09-06 audit:

```text
uv run pytest
184 passed, 134 skipped

uv run mkdocs build --strict
Documentation built successfully
```

The skips are the integration tests, which stay inert until a
`--forgejo-version` is requested; see below.

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
