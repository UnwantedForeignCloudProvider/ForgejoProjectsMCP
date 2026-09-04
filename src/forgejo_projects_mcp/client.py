"""Forgejo Projects/Kanban client.

Forgejo does not expose Projects/Kanban over its REST API, so this client drives
the same internal web routes the browser uses, authenticated with a session
cookie. It uses Playwright's APIRequestContext (HTTP only -- no browser binary
required).

Credentials come from the environment:
    FORGEJO_URL       e.g. https://forge.example.com
    FORGEJO_USERNAME
    FORGEJO_PASSWORD

The authenticated session (cookies) is persisted to
    <config>/forgejo_projects_mcp/storage_state.json
(where <config> is $XDG_CONFIG_HOME or ~/.config, resolved per-OS) and reused
across runs; it is refreshed automatically when it expires.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from html import unescape
from pathlib import Path
from typing import Any

from playwright.async_api import (
    APIRequestContext,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

logger = logging.getLogger("forgejo_projects_mcp.client")


def _default_config_dir() -> Path:
    """Return the state directory, resolved OS-agnostically.

    Uses ``$XDG_CONFIG_HOME`` when set (common on Linux), otherwise
    ``<home>/.config`` — where ``<home>`` is ``Path.home()``, which resolves
    correctly on Linux, macOS and Windows.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "forgejo_projects_mcp"


CONFIG_DIR = _default_config_dir()
STATE_FILE = CONFIG_DIR / "storage_state.json"

_REDIRECTS = (301, 302, 303, 307, 308)


class AuthError(RuntimeError):
    """Raised when credentials are missing or login fails."""

    def __init__(self, message: str, *, code: str = "AUTH_FAILED") -> None:
        super().__init__(message)
        self.code = code


class ForgejoError(RuntimeError):
    """Raised when a Forgejo web route errors or the instance is unreachable.

    ``status`` is the HTTP status (``None`` for transport/network failures) and
    ``code`` is a stable machine-readable label used to classify the error.
    """

    def __init__(
        self, message: str, *, status: int | None = None, code: str | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class ForgejoClient:
    def __init__(self) -> None:
        self.base_url = os.environ.get("FORGEJO_URL", "").rstrip("/")
        self.username = os.environ.get("FORGEJO_USERNAME", "")
        self.password = os.environ.get("FORGEJO_PASSWORD", "")
        self._pw: Playwright | None = None
        self._ctx: APIRequestContext | None = None
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ auth
    def _check_env(self) -> None:
        missing = [
            name
            for name, val in (
                ("FORGEJO_URL", self.base_url),
                ("FORGEJO_USERNAME", self.username),
                ("FORGEJO_PASSWORD", self.password),
            )
            if not val
        ]
        if missing:
            raise AuthError(
                "Missing environment variable(s): " + ", ".join(missing),
                code="MISSING_CONFIG",
            )

    def _unreachable(self, exc: Exception) -> ForgejoError:
        """Wrap a transport failure in a clean, non-leaking ForgejoError."""
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return ForgejoError(
            f"Could not reach Forgejo at {self.base_url}: {first_line}",
            code="NETWORK_ERROR",
        )

    async def _new_context(self, use_state: bool) -> None:
        if self._ctx is not None:
            await self._ctx.dispose()
            self._ctx = None
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "extra_http_headers": {"Origin": self.base_url},
        }
        if use_state and STATE_FILE.exists():
            kwargs["storage_state"] = str(STATE_FILE)
        assert self._pw is not None
        self._ctx = await self._pw.request.new_context(**kwargs)

    async def ensure(self) -> None:
        """Guarantee an authenticated context exists, logging in if needed."""
        self._check_env()
        async with self._lock:
            try:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                if self._ctx is None:
                    await self._new_context(use_state=True)
                if not await self._is_authenticated():
                    await self._login_locked()
            except PlaywrightError as e:
                raise self._unreachable(e) from e

    async def _is_authenticated(self) -> bool:
        if self._ctx is None:
            return False
        r = await self._ctx.get("/user/settings", max_redirects=0)
        return r.status == 200

    async def _login_locked(self) -> None:
        await self._new_context(use_state=False)
        assert self._ctx is not None
        r = await self._ctx.post(
            "/user/login",
            form={
                "user_name": self.username,
                "password": self.password,
                "remember": "on",
            },
            max_redirects=0,
        )
        # Success redirects (303 -> /). A re-rendered 200 means bad credentials.
        if r.status not in _REDIRECTS:
            raise AuthError(
                "Login failed -- check FORGEJO_USERNAME / FORGEJO_PASSWORD "
                f"(status {r.status})."
            )
        if not await self._is_authenticated():
            raise AuthError("Login succeeded but no valid session was established.")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        await self._ctx.storage_state(path=str(STATE_FILE))

    async def login(self, force: bool = False) -> dict[str, Any]:
        """Explicit login. Returns basic session info."""
        self._check_env()
        async with self._lock:
            try:
                if self._pw is None:
                    self._pw = await async_playwright().start()
                if force or self._ctx is None:
                    await self._new_context(use_state=not force)
                if force or not await self._is_authenticated():
                    await self._login_locked()
            except PlaywrightError as e:
                raise self._unreachable(e) from e
        return {
            "authenticated": True,
            "instance": self.base_url,
            "username": self.username,
            "state_file": str(STATE_FILE),
        }

    async def status(self) -> dict[str, Any]:
        try:
            await self.ensure()
            return {
                "authenticated": True,
                "instance": self.base_url,
                "username": self.username,
                "state_file": str(STATE_FILE),
                "state_cached": STATE_FILE.exists(),
            }
        except (AuthError, ForgejoError) as e:
            return {"authenticated": False, "error": str(e), "instance": self.base_url}

    async def close(self) -> None:
        """Best-effort teardown of the request context and Playwright driver.

        Safe to call multiple times and when nothing was ever started. Errors
        during teardown are logged, never raised, so shutdown always completes.
        """
        ctx, self._ctx = self._ctx, None
        if ctx is not None:
            try:
                await ctx.dispose()
            except Exception:  # teardown must not raise
                logger.debug("Error disposing request context", exc_info=True)
        pw, self._pw = self._pw, None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:  # teardown must not raise
                logger.debug("Error stopping Playwright", exc_info=True)

    # -------------------------------------------------------------- requests
    async def _request(
        self,
        method: str,
        path: str,
        *,
        form: dict | None = None,
        json: Any | None = None,
        params: dict | None = None,
        follow: bool = True,
        _retry: bool = True,
    ):
        await self.ensure()
        assert self._ctx is not None
        kwargs: dict[str, Any] = {"method": method}
        if form is not None:
            kwargs["form"] = form
        if json is not None:
            kwargs["data"] = json
            kwargs["headers"] = {"Content-Type": "application/json"}
        if params is not None:
            kwargs["params"] = params
        if not follow:
            kwargs["max_redirects"] = 0
        try:
            r = await self._ctx.fetch(path, **kwargs)
        except PlaywrightError as e:
            raise self._unreachable(e) from e

        # Detect a bounced-to-login response (expired session) and retry once.
        location = r.headers.get("location", "")
        bounced = ("/user/login" in location) or (
            r.url and "/user/login" in r.url and "/user/login" not in path
        )
        if bounced and _retry:
            logger.info("Session expired for %s %s; re-authenticating", method, path)
            async with self._lock:
                try:
                    await self._login_locked()
                except PlaywrightError as e:
                    raise self._unreachable(e) from e
            return await self._request(
                method, path, form=form, json=json, params=params,
                follow=follow, _retry=False,
            )

        if not follow and r.status in _REDIRECTS:
            return r
        if r.status >= 400:
            raise ForgejoError(
                f"{method} {path} -> HTTP {r.status}{await self._error_detail(r)}",
                status=r.status,
                code=f"HTTP_{r.status}",
            )
        return r

    @staticmethod
    async def _error_detail(r) -> str:
        """Extract a concise message from an error response (no raw HTML)."""
        ctype = r.headers.get("content-type", "")
        try:
            if "json" in ctype:
                msg = (await r.json()).get("message", "")
                return f": {msg}" if msg else ""
            body = await r.text()
            m = re.search(r"<p[^>]*>\s*([^<]{3,200}?)\s*</p>", body)
            if m:
                return f": {m.group(1).strip()}"
        except Exception:
            pass
        return ""

    async def _get_text(self, path: str, params: dict | None = None) -> str:
        r = await self._request("GET", path, params=params, follow=True)
        return await r.text()

    # ------------------------------------------------------------- utilities
    @staticmethod
    def _repo_base(owner: str, repo: str) -> str:
        return f"/{owner}/{repo}"

    # ---------------------------------------------------------- repositories
    async def list_repositories(
        self, query: str = "", limit: int = 50, page: int = 1
    ) -> list[dict[str, Any]]:
        r = await self._request(
            "GET",
            "/repo/search",
            params={"q": query, "limit": str(limit), "page": str(page)},
            follow=True,
        )
        data = await r.json()
        out = []
        for item in data.get("data", []):
            repo = item.get("repository", item)
            out.append(
                {
                    "full_name": repo.get("full_name"),
                    "owner": (repo.get("full_name") or "/").split("/")[0],
                    "name": (repo.get("full_name") or "/").split("/")[-1],
                    "description": repo.get("description") or "",
                    "private": repo.get("private"),
                    "archived": repo.get("archived"),
                    "empty": repo.get("empty"),
                    "fork": repo.get("fork"),
                }
            )
        return out

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _parse_projects_list(html: str) -> list[dict[str, Any]]:
        projects: dict[int, str] = {}
        for m in re.finditer(
            r'href="[^"]*/projects/(\d+)"[^>]*>\s*([^<]+?)\s*</a>', html
        ):
            pid = int(m.group(1))
            title = unescape(m.group(2).strip())
            if title and (pid not in projects or len(title) > len(projects[pid])):
                projects[pid] = title
        return [{"id": pid, "title": t} for pid, t in sorted(projects.items())]

    @staticmethod
    def _parse_board(html: str) -> dict[str, Any]:
        title_m = re.search(r"<title>([^<]+)</title>", html)
        board_title = unescape(title_m.group(1).split(" - ")[0].strip()) if title_m else ""

        columns: list[dict[str, Any]] = []
        # Split at each *real* column container. The class token must be exactly
        # "project-column" (followed by a quote or space) so we don't also match
        # project-column-header / project-column-title / new-project-column-modal.
        parts = re.split(r'(<div class="project-column[ "][^>]*>)', html)
        for i in range(1, len(parts), 2):
            opening = parts[i]
            chunk = parts[i + 1] if i + 1 < len(parts) else ""
            id_m = re.search(r'data-id="(\d+)"', opening)
            if not id_m:
                continue
            col_id = int(id_m.group(1))
            tt = re.search(
                r'project-column-title-label[^>]*>\s*([^<]+?)\s*<', chunk
            )
            col_title = unescape(tt.group(1).strip()) if tt else ""
            cards = []
            for cm in re.finditer(
                r'data-issue="(\d+)"(.*?)(?=data-issue="|\Z)', chunk, re.DOTALL
            ):
                issue_id = int(cm.group(1))
                block = cm.group(2)
                num_m = re.search(r'/issues/(\d+)"', block)
                title_m2 = re.search(r'/issues/\d+"[^>]*>\s*([^<]+?)\s*</a>', block)
                cards.append(
                    {
                        "issue_id": issue_id,
                        "number": int(num_m.group(1)) if num_m else None,
                        "title": unescape(title_m2.group(1).strip()) if title_m2 else "",
                    }
                )
            columns.append({"id": col_id, "title": col_title, "cards": cards})
        return {"title": board_title, "columns": columns}

    async def resolve_issue_id(self, owner: str, repo: str, number: int) -> int:
        """Map a repo-local issue number to its global issue id."""
        html = await self._get_text(f"{self._repo_base(owner, repo)}/issues/{number}")
        m = re.search(r'data-issue-id="(\d+)"', html)
        if not m:
            raise ForgejoError(
                f"Could not resolve issue #{number} in {owner}/{repo}.",
                status=404,
                code="ISSUE_NOT_FOUND",
            )
        return int(m.group(1))

    # -------------------------------------------------------------- projects
    async def _list_projects_state(
        self, owner: str, repo: str, state: str
    ) -> list[dict[str, Any]]:
        html = await self._get_text(
            f"{self._repo_base(owner, repo)}/projects", params={"state": state}
        )
        return self._parse_projects_list(html)

    async def list_projects(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        # Forgejo's projects page has no "all" view (it silently shows "open"),
        # so merge the two concrete states ourselves.
        if state == "all":
            merged: dict[int, dict[str, Any]] = {}
            for st in ("open", "closed"):
                for p in await self._list_projects_state(owner, repo, st):
                    merged[p["id"]] = p
            return [merged[k] for k in sorted(merged)]
        return await self._list_projects_state(owner, repo, state)

    async def create_project(
        self,
        owner: str,
        repo: str,
        title: str,
        description: str = "",
        card_type: str = "text",
    ) -> dict[str, Any]:
        ct = {"text": "1", "images_and_text": "2"}.get(card_type, str(card_type))
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/new",
            form={
                "redirect": "",
                "title": title,
                "content": description,
                "template_type": "",
                "card_type": ct,
            },
            follow=False,
        )
        # Recover the new id (highest id whose title matches).
        projects = await self.list_projects(owner, repo, state="open")
        match = [p for p in projects if p["title"] == title]
        new = match[-1] if match else (projects[-1] if projects else None)
        return {"created": True, "project": new}

    async def get_project(
        self, owner: str, repo: str, project_id: int
    ) -> dict[str, Any]:
        html = await self._get_text(
            f"{self._repo_base(owner, repo)}/projects/{project_id}"
        )
        board = self._parse_board(html)
        board["id"] = project_id
        return board

    async def update_project(
        self,
        owner: str,
        repo: str,
        project_id: int,
        title: str | None = None,
        description: str | None = None,
        card_type: str | None = None,
    ) -> dict[str, Any]:
        # Read current values from the edit form for any field left unset.
        html = await self._get_text(
            f"{self._repo_base(owner, repo)}/projects/{project_id}/edit"
        )
        cur_title = re.search(r'name="title"[^>]*value="([^"]*)"', html)
        cur_ct = re.search(r'name="card_type"[^>]*value="([^"]*)"', html)
        ct_map = {"text": "1", "images_and_text": "2"}
        new_title = title if title is not None else (cur_title.group(1) if cur_title else "")
        if card_type:
            new_ct = ct_map.get(card_type, card_type)
        else:
            new_ct = cur_ct.group(1) if cur_ct else "1"
        form = {
            "redirect": "",
            "title": new_title,
            "content": description if description is not None else "",
            "card_type": new_ct,
        }
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/edit",
            form=form,
            follow=False,
        )
        return {"updated": True, "project_id": project_id}

    async def _project_action(
        self, owner: str, repo: str, project_id: int, action: str
    ) -> dict[str, Any]:
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/{action}",
            form={},
            follow=False,
        )
        return {action: True, "project_id": project_id}

    async def close_project(self, owner, repo, project_id):
        return await self._project_action(owner, repo, project_id, "close")

    async def reopen_project(self, owner, repo, project_id):
        return await self._project_action(owner, repo, project_id, "open")

    async def delete_project(self, owner, repo, project_id):
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/delete",
            form={},
            follow=False,
        )
        return {"deleted": True, "project_id": project_id}

    # --------------------------------------------------------------- columns
    async def create_column(
        self, owner, repo, project_id, title, color: str = ""
    ) -> dict[str, Any]:
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}",
            form={"title": title, "color": color},
            follow=False,
        )
        board = await self.get_project(owner, repo, project_id)
        match = [c for c in board["columns"] if c["title"] == title]
        return {"created": True, "column": (match[-1] if match else None)}

    async def edit_column(
        self, owner, repo, project_id, column_id,
        title: str | None = None, color: str | None = None,
    ) -> dict[str, Any]:
        form: dict[str, str] = {}
        if title is not None:
            form["title"] = title
        if color is not None:
            form["color"] = color
        await self._request(
            "PUT",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/{column_id}",
            form=form,
            follow=False,
        )
        return {"updated": True, "column_id": column_id}

    async def delete_column(self, owner, repo, project_id, column_id) -> dict[str, Any]:
        try:
            await self._request(
                "DELETE",
                f"{self._repo_base(owner, repo)}/projects/{project_id}/{column_id}",
                follow=False,
            )
        except ForgejoError as e:
            raise ForgejoError(
                f"{e} (note: the default column cannot be deleted -- set another "
                f"column as default first with set_default_column).",
                status=e.status,
                code=e.code,
            ) from e
        return {"deleted": True, "column_id": column_id}

    async def set_default_column(
        self, owner, repo, project_id, column_id
    ) -> dict[str, Any]:
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/{column_id}/default",
            form={},
            follow=False,
        )
        return {"default_column": column_id, "project_id": project_id}

    # ----------------------------------------------------------------- cards
    async def _resolve_ids(self, owner, repo, numbers: list[int]) -> list[int]:
        ids = []
        for n in numbers:
            ids.append(await self.resolve_issue_id(owner, repo, n))
        return ids

    async def add_issues_to_project(
        self, owner, repo, project_id, issue_numbers: list[int]
    ) -> dict[str, Any]:
        ids = await self._resolve_ids(owner, repo, issue_numbers)
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/issues/projects",
            form={"id": str(project_id), "issue_ids": ",".join(map(str, ids))},
            follow=False,
        )
        return {"attached": issue_numbers, "project_id": project_id}

    async def remove_issues_from_project(
        self, owner, repo, issue_numbers: list[int]
    ) -> dict[str, Any]:
        ids = await self._resolve_ids(owner, repo, issue_numbers)
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/issues/projects",
            form={"id": "0", "issue_ids": ",".join(map(str, ids))},
            follow=False,
        )
        return {"detached": issue_numbers}

    async def move_card(
        self, owner, repo, project_id, column_id, issue_numbers: list[int]
    ) -> dict[str, Any]:
        ids = await self._resolve_ids(owner, repo, issue_numbers)
        payload = {
            "issues": [
                {"issueID": iid, "sorting": idx} for idx, iid in enumerate(ids)
            ]
        }
        r = await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/projects/{project_id}/{column_id}/move",
            json=payload,
            follow=False,
        )
        try:
            body = await r.json()
        except Exception:
            body = {"status": r.status}
        return {"moved": issue_numbers, "column_id": column_id, "result": body}

    # ---------------------------------------------------------------- issues
    async def create_issue(
        self,
        owner,
        repo,
        title,
        body: str = "",
        project_id: int | None = None,
        milestone_id: int | None = None,
        label_ids: list[int] | None = None,
        assignee_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        form: dict[str, str] = {"title": title, "content": body}
        if project_id is not None:
            form["project_id"] = str(project_id)
        if milestone_id is not None:
            form["milestone_id"] = str(milestone_id)
        if label_ids:
            form["label_ids"] = ",".join(map(str, label_ids))
        if assignee_ids:
            form["assignee_ids"] = ",".join(map(str, assignee_ids))
        r = await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/issues/new",
            form=form,
            follow=False,
        )
        # Forgejo replies 200 with JSON {"redirect": ".../issues/N"}.
        number = None
        try:
            data = await r.json()
            num_m = re.search(r"/issues/(\d+)", data.get("redirect", ""))
            if num_m:
                number = int(num_m.group(1))
        except Exception:
            pass
        return {"created": True, "number": number, "title": title}

    async def delete_issue(self, owner, repo, number) -> dict[str, Any]:
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/issues/{number}/delete",
            form={},
            follow=False,
        )
        return {"deleted": True, "number": number}

    # ------------------------------------------------------------ milestones
    @staticmethod
    def _parse_milestones(html: str) -> list[dict[str, Any]]:
        out: dict[int, str] = {}
        for m in re.finditer(
            r'href="[^"]*/milestone/(\d+)"[^>]*>\s*([^<]+?)\s*</a>', html
        ):
            mid = int(m.group(1))
            title = unescape(m.group(2).strip())
            if title and (mid not in out or len(title) > len(out[mid])):
                out[mid] = title
        return [{"id": mid, "title": t} for mid, t in sorted(out.items())]

    async def _list_milestones_state(self, owner, repo, state):
        html = await self._get_text(
            f"{self._repo_base(owner, repo)}/milestones", params={"state": state}
        )
        return self._parse_milestones(html)

    async def list_milestones(self, owner, repo, state: str = "open"):
        # Forgejo's milestones page has no "all" view (it silently shows "open"),
        # so merge the two concrete states ourselves.
        if state == "all":
            merged: dict[int, dict[str, Any]] = {}
            for st in ("open", "closed"):
                for m in await self._list_milestones_state(owner, repo, st):
                    merged[m["id"]] = m
            return [merged[k] for k in sorted(merged)]
        return await self._list_milestones_state(owner, repo, state)

    async def create_milestone(
        self, owner, repo, title, description: str = "", deadline: str = ""
    ) -> dict[str, Any]:
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/milestones/new",
            form={"title": title, "content": description, "deadline": deadline},
            follow=False,
        )
        ms = await self.list_milestones(owner, repo, state="open")
        match = [m for m in ms if m["title"] == title]
        return {"created": True, "milestone": (match[-1] if match else None)}

    async def edit_milestone(
        self, owner, repo, milestone_id,
        title: str | None = None, description: str | None = None,
        deadline: str | None = None,
    ) -> dict[str, Any]:
        form = {
            "title": title or "",
            "content": description or "",
            "deadline": deadline or "",
        }
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/milestones/{milestone_id}/edit",
            form=form,
            follow=False,
        )
        return {"updated": True, "milestone_id": milestone_id}

    async def _milestone_action(self, owner, repo, milestone_id, action):
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/milestones/{milestone_id}/{action}",
            form={"id": str(milestone_id)},
            follow=False,
        )
        return {action: True, "milestone_id": milestone_id}

    async def close_milestone(self, owner, repo, milestone_id):
        return await self._milestone_action(owner, repo, milestone_id, "close")

    async def reopen_milestone(self, owner, repo, milestone_id):
        return await self._milestone_action(owner, repo, milestone_id, "open")

    async def delete_milestone(self, owner, repo, milestone_id):
        # The real route is POST /{owner}/{repo}/milestones/delete?id=N
        # (NOT /milestones/{id}/delete, which 200s but does nothing).
        await self._request(
            "POST",
            f"{self._repo_base(owner, repo)}/milestones/delete",
            form={"id": str(milestone_id)},
            follow=False,
        )
        return {"deleted": True, "milestone_id": milestone_id}
