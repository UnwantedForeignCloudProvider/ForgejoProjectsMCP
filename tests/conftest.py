"""Shared fakes for offline tests.

The client talks to Playwright's APIRequestContext. These fakes stand in for it
so the whole test suite runs with no network, no browser and no credentials.
"""

from __future__ import annotations

import json as _json
from collections.abc import Callable

import pytest

from forgejo_projects_mcp.client import ForgejoClient


class FakeResponse:
    def __init__(self, status=200, headers=None, text="", json_data=None, url=""):
        self.status = status
        self.headers = headers or {}
        self._text = text
        self._json = json_data
        self.url = url
        self.ok = status < 400

    async def text(self):
        return self._text

    async def json(self):
        if self._json is not None:
            return self._json
        return _json.loads(self._text)


class FakeContext:
    """Records every request and delegates to a handler(method, path, kw)."""

    def __init__(self, handler: Callable):
        self.handler = handler
        self.calls: list[dict] = []
        self.storage_saved: list[str] = []
        self.disposed = False

    async def fetch(self, path, method="GET", **kw):
        self.calls.append({"method": method, "path": path, **kw})
        res = self.handler(method, path, kw)
        return res

    async def get(self, path, **kw):
        return await self.fetch(path, method="GET", **kw)

    async def post(self, path, **kw):
        return await self.fetch(path, method="POST", **kw)

    async def put(self, path, **kw):
        return await self.fetch(path, method="PUT", **kw)

    async def delete(self, path, **kw):
        return await self.fetch(path, method="DELETE", **kw)

    async def storage_state(self, path=None):
        self.storage_saved.append(path)

    async def dispose(self):
        self.disposed = True


class FakePlaywright:
    """Fake `Playwright` whose request.new_context() yields a FakeContext."""

    def __init__(self, handler: Callable):
        self._handler = handler
        self.contexts: list[FakeContext] = []
        self.stopped = False
        outer = self

        class _Req:
            async def new_context(self, **kw):
                ctx = FakeContext(outer._handler)
                ctx.new_context_kwargs = kw
                outer.contexts.append(ctx)
                return ctx

        self.request = _Req()

    async def stop(self):
        self.stopped = True


def make_client(handler: Callable, authed: bool = True) -> ForgejoClient:
    """A ForgejoClient wired to fakes. If authed, skip the login handshake."""
    c = ForgejoClient()
    c.base_url = "https://forge.test"
    c.username = "u"
    c.password = "p"
    c._pw = FakePlaywright(handler)
    c._ctx = FakeContext(handler)
    if authed:
        async def _always_authed():
            return True

        c._is_authenticated = _always_authed  # type: ignore[assignment]
    return c


@pytest.fixture
def tmp_state(monkeypatch, tmp_path):
    """Redirect the session-state file into a temp dir for login tests."""
    import forgejo_projects_mcp.client as client_mod

    monkeypatch.setattr(client_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(client_mod, "STATE_FILE", tmp_path / "storage_state.json")
    return tmp_path
