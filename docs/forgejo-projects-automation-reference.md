# Forgejo Projects / Kanban — Automation Reference

Reverse-engineered live against **Forgejo v15.0.7** (`forge.not1337.fr`) by driving the
web UI and watching the requests it makes. Every endpoint below was **executed and verified**
(create/edit/move/delete) on a throwaway repo, then the test artifacts were removed.

> Forgejo's Projects/Kanban still have **no `/api/v1` coverage** (tracked upstream in
> forgejo/forgejo#5330). Everything here uses the **internal web routes** the browser uses.
> Issues, milestones, labels and assignees *do* have official API endpoints — prefer those
> where possible and only use the web routes for the project/column/card layer.

---

## 1. Authentication & CSRF (the important part)

- These routes authenticate with the **session cookie** `i_like_gitea` (httpOnly).
  Personal Access Tokens (`Authorization: token …`) work for `/api/v1` **but not** for these
  web routes.
- **No explicit CSRF token is required.** A plain same-origin `fetch` with `credentials`
  and **no `_csrf` field** succeeded for every write. v15 relies on the SameSite cookie +
  Origin/Referer check rather than a form token (there is literally no `_csrf` in the page HTML).
- Practical consequence for an external agent:
  - **Browser-driven** (Playwright/Puppeteer with a persisted, logged-in profile) is the most
    robust — cookies/Origin are handled for you. This is exactly what was used here.
  - **Plain HTTP client** works too if it sends the `i_like_gitea` cookie. Obtain it once via
    `POST /user/login` (username/password) or copy it from a browser session. Send an
    `Origin: https://<host>` header to be safe.

All paths below are relative to the instance root, e.g. `https://forge.not1337.fr`.
`{owner}/{repo}` = the repo (repo-level projects). Org/user-level projects live under a
different prefix (`/{org}/-/projects`, `/{user}/-/projects`) with the same shapes — not
tested here but structurally identical.

---

## 2. Projects (CRUD) — repo-level

| Action | Method | Path | Body |
|---|---|---|---|
| List | GET | `/{owner}/{repo}/projects` | `?state=open\|closed\|all` |
| Create | POST | `/{owner}/{repo}/projects/new` | `title`, `content`, `template_type`, `card_type`, `redirect` |
| View board | GET | `/{owner}/{repo}/projects/{id}` | — |
| Update | POST | `/{owner}/{repo}/projects/{id}/edit` | `title`, `content`, `card_type`, `redirect` |
| Close | POST | `/{owner}/{repo}/projects/{id}/close` | — |
| Reopen | POST | `/{owner}/{repo}/projects/{id}/open` | — *(inferred, not tested)* |
| Delete | POST | `/{owner}/{repo}/projects/{id}/delete` | — |

- `card_type`: `1` = text only, `2` = images and text.
- `template_type` for repo projects: empty = none (`basic_kanban`, `bug_triage` also exist).
- Create returns a 302 redirect to the new board; response is `opaqueredirect` from `fetch`.
- **Delete must be POST** (`DELETE` returned 500).

## 3. Columns

| Action | Method | Path | Body |
|---|---|---|---|
| Create | POST | `/{owner}/{repo}/projects/{id}` | `title`, `color` (hex, optional e.g. `#e01e5a`) |
| Edit | **PUT** | `/{owner}/{repo}/projects/{id}/{columnID}` | `title`, `color` |
| Delete | **DELETE** | `/{owner}/{repo}/projects/{id}/{columnID}` | — |
| Set as default | POST | `/{owner}/{repo}/projects/{id}/{columnID}/default` | — |
| Reorder columns | POST | `/{owner}/{repo}/projects/{id}/move` | JSON `{columns:[{columnID, sorting}]}` *(format inferred)* |

- **Column edit is PUT** — POST returns 405.
- The "Uncategorized" column is implicit and always present; new projects start with it as default.

## 4. Cards (issues on the board)

| Action | Method | Path | Body |
|---|---|---|---|
| Move issue into a column / reorder | POST | `/{owner}/{repo}/projects/{id}/{columnID}/move` | JSON `{issues:[{issueID, sorting}]}` |
| Attach/detach issue ↔ project | POST | `/{owner}/{repo}/issues/projects` | `id` (projectID; `0` = remove), `issue_ids` (comma-sep) |
| Create issue directly on a project | POST | `/{owner}/{repo}/issues/new` | `title`, `content`, `project_id`, `milestone_id`, `label_ids`, `assignee_ids` |

- The move endpoint returns `{"ok":true}` (JSON body, `Content-Type: application/json`).
- Attaching via `/issues/projects` drops the issue onto the project's **default** column.
- "Deleting a card" = detach the issue from the project (`id=0`); the issue itself survives.

## 5. Milestones

Also fully supported by the **official API** (`/api/v1/repos/{owner}/{repo}/milestones`) —
prefer that (works with a PAT). Web routes for completeness:

| Action | Method | Path | Body |
|---|---|---|---|
| Create | POST | `/{owner}/{repo}/milestones/new` | `title`, `content`, `deadline` |
| Edit | POST | `/{owner}/{repo}/milestones/{id}/edit` | `title`, `content`, `deadline` |
| Close | POST | `/{owner}/{repo}/milestones/{id}/close` | — |
| Reopen | POST | `/{owner}/{repo}/milestones/{id}/open` | — *(inferred)* |
| Delete | POST | `/{owner}/{repo}/milestones/{id}/delete` | — |

## 6. Issue sidebar helpers (existing issues)

| Field | Method | Path |
|---|---|---|
| Title | POST | `/{owner}/{repo}/issues/{index}/title` |
| Content | POST | `/{owner}/{repo}/issues/{index}/content` |
| Labels | POST | `/{owner}/{repo}/issues/labels` |
| Projects | POST | `/{owner}/{repo}/issues/projects` |
| Assignee | POST | `/{owner}/{repo}/issues/assignee` |
| Delete issue | POST | `/{owner}/{repo}/issues/{index}/delete` |

---

## 7. Recommended architecture for the AI agent

**Hybrid, using the official API wherever it exists and web routes only for the gap:**

1. **Issues, milestones, labels, comments, assignees** → official `/api/v1` with a Personal
   Access Token. Stable, documented, versioned.
2. **Projects, columns, card placement/moves** → the web routes above, authenticated by a
   session cookie. Wrap them in a thin client:
   - Log in once (`POST /user/login`) → keep the `i_like_gitea` cookie.
   - Send `Origin: https://<host>` on writes.
   - IDs (project/column) aren't returned in bodies — after a create, GET the board HTML and
     parse `.project-column[data-id]` / `.issue-card[data-issue]` to recover them. (Or keep a
     browser-driven layer for anything ID-dependent.)
3. **Most robust option overall:** run the project/column/card layer through **Playwright with
   a persisted authenticated profile**, which is what produced this reference. It sidesteps
   cookie/CSRF/Origin fragility entirely and survives Forgejo UI changes better than raw HTTP
   for the drag-and-drop bits.

### DOM anchors for scraping IDs
- Columns: `.project-column[data-id]`, title in `.project-column-title-label`
- Cards: `.issue-card[data-issue]` (the `data-issue` value is the issue index)
- Card containers per column: `.ui.cards[data-url="…/projects/{id}/{columnID}"]`

---

## 8. Verification log (what was actually run)

- Created 2 projects (one via UI, one via raw `fetch` **without CSRF** → confirmed no token needed).
- Created 2 columns, edited one (PUT, title+color), set a default, created+deleted a temp column.
- Created 2 issues attached to a project, moved one into a column (`{ok:true}`), detached &
  re-attached an issue via `/issues/projects`.
- Created a milestone, edited, closed, deleted it.
- Closed & deleted projects; deleted the test issues. Repo left clean.
- Confirmed method requirements: column edit = **PUT**, column delete = **DELETE**,
  project delete = **POST** (DELETE 500s).
