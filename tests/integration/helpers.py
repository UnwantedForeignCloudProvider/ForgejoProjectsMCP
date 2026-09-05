"""Small utilities shared by the live tests.

The offline suite can inspect every request because it owns the fake transport.
Against a real instance the equivalent visibility comes from :func:`watch_requests`,
which records what the client actually puts on the wire without changing it, so a
live test can assert the same things its mocked counterpart asserts: which route
was called, and which headers went with it.

The REST helpers here create the extra state a few tests need (a comment, a
closed issue). They deliberately use Forgejo's documented API rather than the
client under test, so a test that needs a closed issue fails on its own subject
rather than on the setup.
"""

from __future__ import annotations

import uuid
from typing import Any

from .harness import ForgejoInstance


class RequestLog:
    """Every request a watched client issued, in order."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def paths(self, method: str | None = None) -> list[str]:
        return [
            call["path"]
            for call in self.calls
            if method is None or call["method"] == method.upper()
        ]

    def find(self, method: str, path: str) -> dict[str, Any] | None:
        """The first recorded call matching ``method`` and ``path`` exactly."""
        for call in self.calls:
            if call["method"] == method.upper() and call["path"] == path:
                return call
        return None

    def require(self, method: str, path: str) -> dict[str, Any]:
        """The first matching call, failing with context when there is none."""
        call = self.find(method, path)
        assert call is not None, (
            f"no {method} {path} recorded; saw {[(c['method'], c['path']) for c in self.calls]}"
        )
        return call

    def headers_for(self, method: str, path: str) -> dict[str, str]:
        return self.require(method, path)["headers"]

    def clear(self) -> None:
        self.calls.clear()


def watch_requests(client, run_async) -> RequestLog:
    """Record every request ``client`` makes from now on.

    Playwright funnels ``get``/``post``/``fetch`` through one internal entry
    point, so wrapping that catches the session probe as well as the operations
    themselves -- which is what lets a live test prove that version detection
    costs no extra request. The wrapper is installed on the context factory too,
    so requests keep being recorded after a re-login swaps the context.
    """
    log = RequestLog()
    run_async(client.ensure())

    def instrument(context) -> None:
        impl = context._impl_obj
        if getattr(impl, "_watched", False):
            return
        impl._watched = True
        original = impl._inner_fetch

        async def _inner_fetch(request, url, method=None, headers=None, data=None,
                               params=None, form=None, *args, **kw):
            log.calls.append(
                {
                    "method": (method or "GET").upper(),
                    "path": url,
                    "headers": dict(headers or {}),
                    "params": dict(params or {}),
                    "form": dict(form or {}),
                    "data": data,
                }
            )
            return await original(
                request, url, method, headers, data, params, form, *args, **kw
            )

        impl._inner_fetch = _inner_fetch

    instrument(client._ctx)

    requests = client._pw.request
    new_context = requests.new_context

    async def watched_new_context(**kwargs):
        context = await new_context(**kwargs)
        instrument(context)
        return context

    requests.new_context = watched_new_context
    return log


def unique(prefix: str) -> str:
    """A name no other test or run will collide with."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def add_comment(instance: ForgejoInstance, owner: str, repo: str, number: int,
                body: str) -> None:
    """Comment on an issue through the REST API."""
    instance.api(
        "POST", f"/repos/{owner}/{repo}/issues/{number}/comments", {"body": body}
    )


def create_issue(instance: ForgejoInstance, owner: str, repo: str, title: str,
                 body: str = "", **fields: Any) -> int:
    """Create an issue through the REST API and return its number."""
    payload: dict[str, Any] = {"title": title, "body": body, **fields}
    created = instance.api("POST", f"/repos/{owner}/{repo}/issues", payload)
    return int(created["number"])


def set_issue_state(instance: ForgejoInstance, owner: str, repo: str, number: int,
                    state: str) -> None:
    """Open or close an issue through the REST API."""
    instance.api(
        "PATCH", f"/repos/{owner}/{repo}/issues/{number}", {"state": state}
    )


def expire_session_after_next_probe(client) -> None:
    """Really log the session out, immediately after the client's next probe.

    A mid-flight expiry is the one situation a live test cannot simply wait for:
    the client checks the session before every request, so any invalidation done
    up front is caught by that check instead of by the request. Hooking the
    probe reproduces the real ordering -- the session is valid when checked and
    genuinely gone a moment later, because Forgejo really is asked to end it.

    Only the timing is arranged: the logout, the bounce to the login page and
    the recovery that follows are all the instance's own behavior.
    """
    state = {"expired": False}
    probe = client._is_authenticated

    async def probe_then_expire() -> bool:
        authenticated = await probe()
        if authenticated and not state["expired"]:
            state["expired"] = True
            headers = {}
            if client._csrf_token:
                headers["X-Csrf-Token"] = client._csrf_token
            await client._ctx.post(
                "/user/logout", form={}, headers=headers, max_redirects=0
            )
        return authenticated

    client._is_authenticated = probe_then_expire
