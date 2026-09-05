"""Forgejo Projects/Kanban client.

Forgejo does not expose Projects/Kanban over its REST API, so this client drives
the same internal web routes the browser uses, authenticated with a session
cookie. It uses Playwright's APIRequestContext (HTTP only -- no browser binary
required).

Credentials come from the environment:
    FORGEJO_URL       e.g. https://forge.example.com
    FORGEJO_USERNAME
    FORGEJO_PASSWORD

The interactive CLI may provide missing or replacement values in memory, and the
CLI also accepts them as arguments.

The authenticated session (cookies) is persisted to
    <config>/forgejo_projects_mcp/storage_state.json
and the non-secret connection settings (instance URL and username) to
    <config>/forgejo_projects_mcp/config.json
(where <config> is $XDG_CONFIG_HOME or ~/.config, resolved per-OS). Both are
reused across runs, so after the first successful login no env vars are required;
the session is refreshed automatically when it expires. The password is never
persisted -- the cached session replaces it until it expires.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    APIRequestContext,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)

from .compat import (
    CSRF_TOKEN,
    DEFAULT_PROFILE,
    Profile,
    Version,
    detect_csrf_token,
    detect_version,
    profile_for,
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
CONFIG_FILE = CONFIG_DIR / "config.json"


def _load_saved_config() -> dict[str, str]:
    """Read persisted non-secret settings, tolerating a missing/corrupt file."""
    try:
        data = json.loads(CONFIG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, str)}


_URL_ENV = "FORGEJO_URL"
_USERNAME_ENV = "FORGEJO_USERNAME"
_PASSWORD_ENV = "FORGEJO_PASSWORD"
_REDIRECTS = (301, 302, 303, 307, 308)

# Politeness limits for the (undocumented) web routes. Bulk operations fan out
# with asyncio.gather, so cap in-flight requests and space them out to avoid
# hammering the instance / tripping a reverse-proxy rate limit. Tunable via env.
_MAX_CONCURRENCY = max(1, int(os.environ.get("FORGEJO_MCP_MAX_CONCURRENCY", "8")))
_REQUESTS_PER_SECOND = max(0.1, float(os.environ.get("FORGEJO_MCP_RPS", "5")))
_MAX_RATE_RETRIES = 2

_VALID_STATES = ("open", "closed", "all")


def _page(numbers: list[int], limit: int | None, offset: int) -> tuple[list[int], int]:
    """Slice ``numbers[offset:offset+limit]`` and return (slice, total)."""
    total = len(numbers)
    end = (offset + limit) if limit is not None else None
    return numbers[offset:end], total


def _log_path(path: str) -> str:
    """Return a log-safe path without query parameters, fragments, or newlines."""
    try:
        path_only = urlsplit(path).path or "/"
    except ValueError:
        return "<invalid-path>"
    return path_only.replace("\r", "\\r").replace("\n", "\\n")

# Body marker Forgejo returns when a write is rejected for a missing or stale
# CSRF token. Recovering from it at runtime keeps an instance working when its
# version could not be read, or when a proxy enforces CSRF on its behalf.
_CSRF_REJECTED = "invalid csrf token"


class AuthError(RuntimeError):
    """Raised when credentials are missing or login fails."""

    def __init__(self, message: str, *, code: str = "AUTH_FAILED") -> None:
        super().__init__(message)
        self.code = code


CredentialProvider = Callable[[AuthError], tuple[str, str, str] | None]


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
        # Precedence: environment overrides the persisted config file; the
        # password is a secret and is read from the environment only.
        saved = _load_saved_config()
        self.base_url = (
            os.environ.get(_URL_ENV) or saved.get("base_url", "")
        ).rstrip("/")
        self.username = os.environ.get(_USERNAME_ENV) or saved.get("username", "")
        self.password = os.environ.get(_PASSWORD_ENV, "")
        self._credential_provider: CredentialProvider | None = None
        self._pw: Playwright | None = None
        self._ctx: APIRequestContext | None = None
        self._lock = asyncio.Lock()
        # Instance version and the behavior profile derived from it. Both are
        # established by the session probe (see _is_authenticated) and stay on
        # the newest verified behavior until an instance says otherwise.
        self._version: Version | None = None
        self._profile: Profile = DEFAULT_PROFILE
        self._csrf_token: str | None = None
        # Concurrency cap + steady-rate throttle, shared by all requests so that
        # bulk fan-out stays polite. (asyncio primitives bind to the running loop
        # on first use, so constructing them here is fine.)
        self._request_slots = asyncio.Semaphore(_MAX_CONCURRENCY)
        self._rate_lock = asyncio.Lock()
        self._next_request = 0.0
        logger.debug(
            "Client initialized max_concurrency=%d requests_per_second=%.1f",
            _MAX_CONCURRENCY,
            _REQUESTS_PER_SECOND,
        )

    async def _throttle(self) -> None:
        """Block just long enough to keep the global request rate <= RPS."""
        async with self._rate_lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            wait = max(0.0, self._next_request - now)
            self._next_request = max(now, self._next_request) + 1.0 / _REQUESTS_PER_SECOND
        if wait:
            logger.debug(
                "Request throttle waiting wait_ms=%.1f requests_per_second=%.1f",
                wait * 1000,
                _REQUESTS_PER_SECOND,
            )
            await asyncio.sleep(wait)

    # ------------------------------------------------------------------ auth
    def set_credential_provider(self, provider: CredentialProvider | None) -> None:
        """Set an optional in-process credential recovery callback.

        The stdio MCP server leaves this unset. The dedicated CLI installs a
        provider only for interactive terminal invocations. The callback
        receives the triggering AuthError and returns (URL, username, password)
        or None to preserve the error.
        """
        self._credential_provider = provider

    def _persist_config(self) -> None:
        """Persist non-secret connection settings (URL, username) for reuse.

        Written alongside the session state so later runs need no env vars. The
        password is never persisted -- the cached session cookie replaces it, and
        it is re-supplied via env/CLI/prompt when the session expires.
        """
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(
                json.dumps({"base_url": self.base_url, "username": self.username})
            )
            logger.debug("Persisted non-secret config to %s", CONFIG_FILE)
        except OSError:  # persisting config is best-effort, never fatal
            logger.debug("Could not persist config file", exc_info=True)

    @staticmethod
    def _raise_for_missing(values: tuple[tuple[str, str], ...]) -> None:
        missing = [name for name, value in values if not value]
        if missing:
            logger.debug("Configuration validation failed missing=%s", ",".join(missing))
            raise AuthError(
                "Missing environment variable(s): " + ", ".join(missing),
                code="MISSING_CONFIG",
            )

    def _check_base_url(self) -> None:
        self._raise_for_missing(((_URL_ENV, self.base_url),))

    def _check_login_credentials(self) -> None:
        self._raise_for_missing(
            (
                (_USERNAME_ENV, self.username),
                (_PASSWORD_ENV, self.password),
            )
        )

    async def _recover_credentials(self, error: AuthError) -> bool:
        """Ask the configured provider for replacements, if one is installed."""
        provider = self._credential_provider
        if provider is None:
            return False
        logger.debug("Requesting replacement credentials code=%s", error.code)
        replacement = provider(error)
        if replacement is None:
            logger.debug("Credential provider declined recovery code=%s", error.code)
            return False
        base_url, username, password = replacement
        normalized_url = base_url.rstrip("/")
        url_changed = normalized_url != self.base_url
        if url_changed and self._ctx is not None:
            ctx, self._ctx = self._ctx, None
            try:
                await ctx.dispose()
            except Exception:  # replacing a stale context must remain best-effort
                logger.debug("Error disposing context after URL change", exc_info=True)
        self.base_url = normalized_url
        self.username = username
        self.password = password
        logger.debug(
            "Replacement credentials applied url_changed=%s username_present=%s "
            "password_present=%s",
            url_changed,
            bool(username),
            bool(password),
        )
        return True

    async def _with_credential_recovery(
        self, operation: Callable[[], Awaitable[Any]]
    ) -> Any:
        while True:
            try:
                return await operation()
            except AuthError as error:
                if not await self._recover_credentials(error):
                    raise

    def _unreachable(self, exc: Exception) -> ForgejoError:
        """Wrap a transport failure in a clean, non-leaking ForgejoError."""
        first_line = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        return ForgejoError(
            f"Could not reach Forgejo at {self.base_url}: {first_line}",
            code="NETWORK_ERROR",
        )

    async def _new_context(self, use_state: bool) -> None:
        cached_state = use_state and STATE_FILE.exists()
        logger.debug(
            "Creating request context use_cached_state=%s replacing_context=%s",
            cached_state,
            self._ctx is not None,
        )
        if self._ctx is not None:
            await self._ctx.dispose()
            self._ctx = None
        kwargs: dict[str, Any] = {
            "base_url": self.base_url,
            "extra_http_headers": {"Origin": self.base_url},
        }
        if cached_state:
            kwargs["storage_state"] = str(STATE_FILE)
        assert self._pw is not None
        self._ctx = await self._pw.request.new_context(**kwargs)
        logger.debug("Request context created use_cached_state=%s", cached_state)

    async def _ensure_once(self) -> None:
        self._check_base_url()
        logger.debug(
            "Ensuring authenticated session driver_started=%s context_present=%s",
            self._pw is not None,
            self._ctx is not None,
        )
        async with self._lock:
            try:
                if self._pw is None:
                    logger.debug("Starting Playwright request driver")
                    self._pw = await async_playwright().start()
                if self._ctx is None:
                    await self._new_context(use_state=True)
                if not await self._is_authenticated():
                    logger.debug("Session is not authenticated; starting login")
                    await self._login_locked()
                else:
                    logger.debug("Existing session is authenticated")
            except PlaywrightError as e:
                logger.debug(
                    "Authentication setup failed error_type=%s", type(e).__name__
                )
                raise self._unreachable(e) from e

    async def ensure(self) -> None:
        """Guarantee an authenticated context exists, logging in if needed."""
        await self._with_credential_recovery(self._ensure_once)

    async def _is_authenticated(self) -> bool:
        """Probe the session, learning the instance version from the same reply.

        The probe target is an ordinary rendered Forgejo page, so its body
        carries the instance version -- and, on versions that need one, the
        session's CSRF token. Reading them here makes the authentication check
        that precedes every request double as version detection, instead of
        costing a second round trip.
        """
        if self._ctx is None:
            logger.debug("Authentication probe skipped context_present=false")
            return False
        loop = asyncio.get_running_loop()
        started = loop.time()
        r = await self._ctx.get(self._route("auth_probe"), max_redirects=0)
        authenticated = r.status == 200
        if authenticated:
            self._absorb_page(await self._body(r))
        logger.debug(
            "Authentication probe status=%d authenticated=%s version=%s "
            "elapsed_ms=%.1f",
            r.status,
            authenticated,
            self._version.short if self._version else "unknown",
            (loop.time() - started) * 1000,
        )
        return authenticated

    @staticmethod
    async def _body(r) -> str:
        """Response body as text, tolerating a body that cannot be decoded."""
        try:
            return await r.text()
        except Exception:  # reading a body opportunistically must never fail
            logger.debug("Could not decode response body", exc_info=True)
            return ""

    def _absorb_page(self, html: str) -> None:
        """Learn the version (and CSRF token) from a rendered Forgejo page."""
        token = detect_csrf_token(html)
        if token:
            self._csrf_token = token
        version = detect_version(html)
        if version is None or version == self._version:
            return
        self._version = version
        self._profile = profile_for(version)
        logger.info(
            "Forgejo version detected: %s (csrf_mode=%s, quirks=%s)",
            version.short,
            self._profile.csrf_mode,
            ",".join(self._profile.quirks) or "none",
        )

    def _route(self, name: str, **params: Any) -> str:
        """Render an internal web route as the detected version expects it."""
        return self._profile.route(name, **params)

    @property
    def version(self) -> Version | None:
        """The detected instance version, or ``None`` before the first probe."""
        return self._version

    @property
    def profile(self) -> Profile:
        """The behavior profile in force for the detected instance version."""
        return self._profile

    # ------------------------------------------------------------------ csrf
    async def _csrf_token_for_write(self) -> str | None:
        """The CSRF token to send with a write, if this version needs one."""
        if self._profile.csrf_mode != CSRF_TOKEN:
            return None
        if self._csrf_token is None:
            await self._refresh_csrf()
        return self._csrf_token

    async def _refresh_csrf(self, *, adopt_token_mode: bool = False) -> None:
        """Re-read the session CSRF token from a rendered Forgejo page.

        ``adopt_token_mode`` switches this session to token mode after a write
        was rejected for a missing token, which is how an instance whose
        version we could not read (or misjudged) is recovered.
        """
        if self._ctx is not None:
            r = await self._ctx.get(self._route("auth_probe"), max_redirects=0)
            self._absorb_page(await self._body(r))
        if adopt_token_mode and self._profile.csrf_mode != CSRF_TOKEN:
            logger.info(
                "Forgejo rejected a write for a missing CSRF token; using token "
                "mode for the rest of this session (version=%s)",
                self._version.short if self._version else "unknown",
            )
            self._profile = self._profile.with_csrf_mode(CSRF_TOKEN)

    async def _login_locked(self) -> None:
        self._check_base_url()
        self._check_login_credentials()
        logger.debug("Login attempt started")
        await self._new_context(use_state=False)
        assert self._ctx is not None
        loop = asyncio.get_running_loop()
        started = loop.time()
        r = await self._ctx.post(
            self._route("login"),
            form={
                "user_name": self.username,
                "password": self.password,
                "remember": "on",
            },
            max_redirects=0,
        )
        logger.debug(
            "Login response status=%d elapsed_ms=%.1f",
            r.status,
            (loop.time() - started) * 1000,
        )
        # Success redirects (303 -> /). A re-rendered 200 means bad credentials.
        if r.status not in _REDIRECTS:
            logger.debug("Login rejected status=%d", r.status)
            raise AuthError(
                "Login failed -- check FORGEJO_USERNAME / FORGEJO_PASSWORD "
                f"(status {r.status})."
            )
        if not await self._is_authenticated():
            raise AuthError("Login succeeded but no valid session was established.")
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        await self._ctx.storage_state(path=str(STATE_FILE))
        self._persist_config()
        logger.debug("Login completed session_state_persisted=true")

    async def _login_once(self, force: bool) -> None:
        self._check_base_url()
        if force:
            self._check_login_credentials()
        logger.debug(
            "Explicit authentication requested force=%s context_present=%s",
            force,
            self._ctx is not None,
        )
        async with self._lock:
            try:
                if self._pw is None:
                    logger.debug("Starting Playwright request driver")
                    self._pw = await async_playwright().start()
                if force or self._ctx is None:
                    await self._new_context(use_state=not force)
                if force or not await self._is_authenticated():
                    await self._login_locked()
                else:
                    logger.debug("Explicit authentication reused existing session")
            except PlaywrightError as e:
                logger.debug(
                    "Explicit authentication failed error_type=%s", type(e).__name__
                )
                raise self._unreachable(e) from e

    async def login(self, force: bool = False) -> dict[str, Any]:
        """Explicit login. Returns basic session info."""
        await self._with_credential_recovery(lambda: self._login_once(force))
        return {
            "authenticated": True,
            "instance": self.base_url,
            "username": self.username,
            "version": str(self._version) if self._version else None,
            "compatibility": self._profile.describe(),
            "state_file": str(STATE_FILE),
            "config_file": str(CONFIG_FILE),
        }

    async def status(self) -> dict[str, Any]:
        try:
            await self.ensure()
            logger.debug(
                "Authentication status check succeeded version=%s",
                self._version.short if self._version else "unknown",
            )
            return {
                "authenticated": True,
                "instance": self.base_url,
                "username": self.username,
                # The session probe reads the version out of the same response
                # that proves the session, so this costs no extra request.
                "version": str(self._version) if self._version else None,
                "compatibility": self._profile.describe(),
                "state_file": str(STATE_FILE),
                "state_cached": STATE_FILE.exists(),
                "config_file": str(CONFIG_FILE),
                "config_cached": CONFIG_FILE.exists(),
            }
        except (AuthError, ForgejoError) as e:
            logger.debug(
                "Authentication status check failed error_type=%s code=%s",
                type(e).__name__,
                getattr(e, "code", None),
            )
            return {
                "authenticated": False,
                "error": str(e),
                "instance": self.base_url,
                "version": str(self._version) if self._version else None,
            }

    async def close(self) -> None:
        """Best-effort teardown of the request context and Playwright driver.

        Safe to call multiple times and when nothing was ever started. Errors
        during teardown are logged, never raised, so shutdown always completes.
        """
        logger.debug(
            "Closing client context_present=%s driver_started=%s",
            self._ctx is not None,
            self._pw is not None,
        )
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
        logger.debug("Client closed")

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
        _rate_retry: int = _MAX_RATE_RETRIES,
        _csrf_retry: bool = True,
    ):
        await self.ensure()
        assert self._ctx is not None
        safe_path = _log_path(path)
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
        # Versions predating Forgejo's Origin-based CSRF check reject a write
        # unless it carries the session's token (see compat.QUIRKS).
        if method.upper() not in ("GET", "HEAD"):
            csrf = await self._csrf_token_for_write()
            if csrf:
                kwargs["headers"] = {
                    **kwargs.get("headers", {}),
                    "X-Csrf-Token": csrf,
                }
        logger.debug(
            "HTTP request method=%s path=%s follow_redirects=%s param_keys=%s "
            "form_fields=%s json_payload=%s auth_retry_available=%s "
            "rate_retries_remaining=%d",
            method,
            safe_path,
            follow,
            sorted(params) if params else [],
            sorted(form) if form else [],
            json is not None,
            _retry,
            _rate_retry,
        )
        # Concurrency cap + steady-rate spacing around every outbound request.
        loop = asyncio.get_running_loop()
        slot_started = loop.time()
        async with self._request_slots:
            logger.debug(
                "Request slot acquired method=%s path=%s wait_ms=%.1f",
                method,
                safe_path,
                (loop.time() - slot_started) * 1000,
            )
            await self._throttle()
            started = loop.time()
            try:
                r = await self._ctx.fetch(path, **kwargs)
            except PlaywrightError as e:
                logger.debug(
                    "HTTP request failed method=%s path=%s elapsed_ms=%.1f error_type=%s",
                    method,
                    safe_path,
                    (loop.time() - started) * 1000,
                    type(e).__name__,
                )
                raise self._unreachable(e) from e

        logger.debug(
            "HTTP response method=%s path=%s status=%d elapsed_ms=%.1f",
            method,
            safe_path,
            r.status,
            (loop.time() - started) * 1000,
        )

        # Respect upstream rate limiting (Forgejo or a fronting proxy).
        if r.status in (429, 503) and _rate_retry > 0:
            delay = self._retry_after_seconds(r)
            logger.info(
                "Rate limited (%s) on %s %s; backing off %.1fs",
                r.status,
                method,
                safe_path,
                delay,
            )
            logger.debug(
                "Rate-limit retry scheduled method=%s path=%s delay_seconds=%.1f "
                "retries_remaining=%d",
                method,
                safe_path,
                delay,
                _rate_retry - 1,
            )
            await asyncio.sleep(delay)
            return await self._request(
                method, path, form=form, json=json, params=params,
                follow=follow, _retry=_retry, _rate_retry=_rate_retry - 1,
                _csrf_retry=_csrf_retry,
            )
        if r.status in (429, 503):
            logger.debug(
                "Rate-limit retry budget exhausted method=%s path=%s status=%d",
                method,
                safe_path,
                r.status,
            )

        # Detect a bounced-to-login response (expired session) and retry once.
        location = r.headers.get("location", "")
        bounced = ("/user/login" in location) or (
            r.url and "/user/login" in r.url and "/user/login" not in path
        )
        if bounced and _retry:
            logger.info("Session expired for %s %s; re-authenticating", method, safe_path)
            await self.login(force=True)
            logger.debug(
                "Authentication retry scheduled method=%s path=%s", method, safe_path
            )
            return await self._request(
                method, path, form=form, json=json, params=params,
                follow=follow, _retry=False, _csrf_retry=_csrf_retry,
            )
        if bounced:
            logger.debug(
                "Authentication retry already used method=%s path=%s", method, safe_path
            )

        # A write rejected for CSRF: pick up a token and retry once. This is
        # the safety net for an instance whose version could not be read, or
        # whose behavior does not match what its version implies.
        if (
            r.status == 400
            and _csrf_retry
            and method.upper() not in ("GET", "HEAD")
            and _CSRF_REJECTED in (await self._body(r)).lower()
        ):
            logger.info(
                "CSRF rejection on %s %s; retrying with a session token",
                method,
                safe_path,
            )
            await self._refresh_csrf(adopt_token_mode=True)
            return await self._request(
                method, path, form=form, json=json, params=params,
                follow=follow, _retry=_retry, _rate_retry=_rate_retry,
                _csrf_retry=False,
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
        response_format = "json" if "json" in ctype else "html"
        try:
            if response_format == "json":
                msg = (await r.json()).get("message", "")
                logger.debug(
                    "Parsed error response format=json message_present=%s", bool(msg)
                )
                return f": {msg}" if msg else ""
            body = await r.text()
            m = re.search(r"<p[^>]*>\s*([^<]{3,200}?)\s*</p>", body)
            logger.debug(
                "Parsed error response format=html message_present=%s input_chars=%d",
                m is not None,
                len(body),
            )
            if m:
                return f": {m.group(1).strip()}"
        except Exception as exc:
            logger.debug(
                "Error response parsing failed format=%s error_type=%s",
                response_format,
                type(exc).__name__,
            )
        return ""

    @staticmethod
    def _retry_after_seconds(r, default: float = 2.0) -> float:
        """Parse a Retry-After header (integer seconds); fall back to default."""
        raw = r.headers.get("retry-after", "")
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            logger.debug(
                "Retry-After parsing failed; using default delay_seconds=%.1f", default
            )
            return default

    async def _get_text(self, path: str, params: dict | None = None) -> str:
        r = await self._request("GET", path, params=params, follow=True)
        body = await r.text()
        logger.debug(
            "Decoded text response path=%s input_chars=%d", _log_path(path), len(body)
        )
        return body

    # ---------------------------------------------------------- repositories
    async def list_repositories(
        self, query: str = "", limit: int = 50, page: int = 1
    ) -> list[dict[str, Any]]:
        r = await self._request(
            "GET",
            self._route("repo_search"),
            params={"q": query, "limit": str(limit), "page": str(page)},
            follow=True,
        )
        try:
            data = await r.json()
        except Exception as exc:
            logger.debug(
                "Repository search response parsing failed error_type=%s",
                type(exc).__name__,
            )
            raise
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
        logger.debug("Parsed repository search response repositories=%d", len(out))
        return out

    # --------------------------------------------------------------- parsing
    @staticmethod
    def _parse_projects_list(
        html: str, profile: Profile = DEFAULT_PROFILE
    ) -> list[dict[str, Any]]:
        projects: dict[int, str] = {}
        for m in profile.finditer("projects_link", html):
            pid = int(m.group(1))
            title = unescape(m.group(2).strip())
            if title and (pid not in projects or len(title) > len(projects[pid])):
                projects[pid] = title
        result = [{"id": pid, "title": t} for pid, t in sorted(projects.items())]
        logger.debug(
            "Parsed projects list input_chars=%d projects=%d", len(html), len(result)
        )
        return result

    @staticmethod
    def _parse_board(
        html: str, profile: Profile = DEFAULT_PROFILE
    ) -> dict[str, Any]:
        # The profile's candidates capture the board title directly, so no
        # suffix stripping is needed here (see compat._PATTERNS["board_title"]).
        title_m = profile.search("board_title", html)
        board_title = unescape(title_m.group(1).strip()) if title_m else ""

        columns: list[dict[str, Any]] = []
        # Split at each *real* column container. The class token must be exactly
        # "project-column" (followed by a quote or space) so we don't also match
        # project-column-header / project-column-title / new-project-column-modal.
        parts = profile.split("board_column_open", html)
        for i in range(1, len(parts), 2):
            opening = parts[i]
            chunk = parts[i + 1] if i + 1 < len(parts) else ""
            id_m = profile.search("board_column_id", opening)
            if not id_m:
                continue
            col_id = int(id_m.group(1))
            tt = profile.search("board_column_title", chunk)
            col_title = unescape(tt.group(1).strip()) if tt else ""
            cards = []
            for cm in profile.finditer("board_card", chunk):
                issue_id = int(cm.group(1))
                block = cm.group(2)
                num_m = profile.search("board_card_number", block)
                title_m2 = profile.search("board_card_title", block)
                cards.append(
                    {
                        "issue_id": issue_id,
                        "number": int(num_m.group(1)) if num_m else None,
                        "title": unescape(title_m2.group(1).strip()) if title_m2 else "",
                    }
                )
            columns.append({"id": col_id, "title": col_title, "cards": cards})
        card_count = sum(len(column["cards"]) for column in columns)
        logger.debug(
            "Parsed project board input_chars=%d title_present=%s columns=%d cards=%d",
            len(html),
            bool(board_title),
            len(columns),
            card_count,
        )
        return {"title": board_title, "columns": columns}

    async def resolve_issue_id(self, owner: str, repo: str, number: int) -> int:
        """Map a repo-local issue number to its global issue id."""
        html = await self._get_text(
            self._route("issue", owner=owner, repo=repo, number=number)
        )
        m = self._profile.search("issue_id", html)
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
            self._route("projects", owner=owner, repo=repo), params={"state": state}
        )
        return self._parse_projects_list(html, self._profile)

    async def list_projects(
        self, owner: str, repo: str, state: str = "open"
    ) -> list[dict[str, Any]]:
        self._check_state(state)
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
        ct = self._profile.card_types.get(card_type, str(card_type))
        await self._request(
            "POST",
            self._route("project_new", owner=owner, repo=repo),
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
            self._route("project", owner=owner, repo=repo, project_id=project_id)
        )
        board = self._parse_board(html, self._profile)
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
            self._route("project_edit", owner=owner, repo=repo, project_id=project_id)
        )
        cur_title = self._profile.search("project_edit_title", html)
        cur_ct = self._profile.search("project_edit_card_type", html)
        ct_map = self._profile.card_types
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
            self._route("project_edit", owner=owner, repo=repo, project_id=project_id),
            form=form,
            follow=False,
        )
        return {"updated": True, "project_id": project_id}

    async def _project_action(
        self, owner: str, repo: str, project_id: int, action: str
    ) -> dict[str, Any]:
        """POST a project state change; ``action`` names both route and result."""
        await self._request(
            "POST",
            self._route(
                f"project_{action}", owner=owner, repo=repo, project_id=project_id
            ),
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
            self._route(
                "project_delete", owner=owner, repo=repo, project_id=project_id
            ),
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
            self._route("column_new", owner=owner, repo=repo, project_id=project_id),
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
            self._route(
                "column",
                owner=owner,
                repo=repo,
                project_id=project_id,
                column_id=column_id,
            ),
            form=form,
            follow=False,
        )
        return {"updated": True, "column_id": column_id}

    async def delete_column(self, owner, repo, project_id, column_id) -> dict[str, Any]:
        try:
            await self._request(
                "DELETE",
                self._route(
                    "column",
                    owner=owner,
                    repo=repo,
                    project_id=project_id,
                    column_id=column_id,
                ),
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
            self._route(
                "column_default",
                owner=owner,
                repo=repo,
                project_id=project_id,
                column_id=column_id,
            ),
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
            self._route("issue_projects", owner=owner, repo=repo),
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
            self._route("issue_projects", owner=owner, repo=repo),
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
            self._route(
                "column_move",
                owner=owner,
                repo=repo,
                project_id=project_id,
                column_id=column_id,
            ),
            json=payload,
            follow=False,
        )
        try:
            body = await r.json()
            logger.debug("Parsed move-card response format=json")
        except Exception as exc:
            logger.debug(
                "Move-card response parsing failed error_type=%s; using status fallback",
                type(exc).__name__,
            )
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
            self._route("issue_new", owner=owner, repo=repo),
            form=form,
            follow=False,
        )
        number = None
        for source, raw in await self._created_issue_paths(r):
            num_m = self._profile.search("created_issue_number", raw)
            if num_m:
                number = int(num_m.group(1))
                logger.debug("Parsed create-issue response format=%s", source)
                break
        if number is None:
            logger.debug("Create-issue response carried no issue number")
        return {"created": True, "number": number, "title": title}

    @staticmethod
    async def _created_issue_paths(r) -> list[tuple[str, str]]:
        """Strings from a new-issue response that may hold the issue path.

        The number arrives in one of two shapes depending on the Forgejo
        release: newer versions answer 200 with JSON ``{"redirect":
        ".../issues/N"}``, while Forgejo below 1.21 answers 303 with that path
        in the ``Location`` header. Both are offered, in that order.
        """
        candidates: list[tuple[str, str]] = []
        try:
            body = await r.json()
            if isinstance(body, dict):
                candidates.append(("json", str(body.get("redirect", ""))))
        except Exception:  # a non-JSON body just means the redirect shape
            logger.debug("Create-issue response is not JSON", exc_info=True)
        candidates.append(("location", r.headers.get("location", "")))
        return candidates

    async def delete_issue(self, owner, repo, number) -> dict[str, Any]:
        await self._request(
            "POST",
            self._route("issue_delete", owner=owner, repo=repo, number=number),
            form={},
            follow=False,
        )
        return {"deleted": True, "number": number}

    # --------------------------------------------------- reading issue content
    @staticmethod
    def _extract_raw(
        html: str, base_id: str, profile: Profile = DEFAULT_PROFILE
    ) -> str:
        """Return the raw (markdown) text from a Forgejo ``#<id>-raw`` element."""
        m = profile.search("issue_raw", html, id=base_id)
        return unescape(m.group(1).strip()) if m else ""

    @staticmethod
    def _issue_body(
        html: str, number: int | None, profile: Profile = DEFAULT_PROFILE
    ) -> str:
        """The issue's own markdown body, from whichever id keys its element.

        Forgejo keys the body's raw element by the issue's *global* id, which
        equals the repo-local number only in the first repository an instance
        ever creates -- everywhere else the two diverge, and keying by the
        number silently yields an empty body. The number is still tried as a
        fallback so a release that keys it differently keeps working.
        """
        candidates = []
        id_m = profile.search("issue_id", html)
        if id_m:
            candidates.append(f"issue-{id_m.group(1)}")
        if number is not None:
            candidates.append(f"issue-{number}")
        for base_id in candidates:
            body = ForgejoClient._extract_raw(html, base_id, profile)
            if body:
                return body
        return ""

    @staticmethod
    def _parse_issue(
        html: str, profile: Profile = DEFAULT_PROFILE
    ) -> dict[str, Any]:
        num_m = profile.search("issue_number", html)
        number = int(num_m.group(1)) if num_m else None
        title_m = profile.search("issue_title", html)
        title = unescape(title_m.group(1)) if title_m else ""
        state = "closed" if profile.search("issue_closed", html) else "open"
        body = ForgejoClient._issue_body(html, number, profile)
        milestone = None
        ms_m = profile.search("issue_milestone", html)
        if ms_m:
            milestone = {"id": int(ms_m.group(1)), "title": unescape(ms_m.group(2).strip())}
        comments = []
        for cm in profile.finditer("comment_block", html):
            cid, block = cm.group(1), cm.group(2)
            author_m = profile.search("comment_author", block)
            comments.append(
                {
                    "author": author_m.group(1).strip() if author_m else None,
                    "body": ForgejoClient._extract_raw(
                        html, f"issuecomment-{cid}", profile
                    ),
                }
            )
        result = {
            "number": number,
            "title": title,
            "state": state,
            "body": body,
            "milestone": milestone,
            "comments": comments,
        }
        logger.debug(
            "Parsed issue input_chars=%d number_present=%s title_present=%s "
            "milestone_present=%s comments=%d",
            len(html),
            number is not None,
            bool(title),
            milestone is not None,
            len(comments),
        )
        return result

    async def read_issue(self, owner, repo, number: int) -> dict[str, Any]:
        """Full content of one issue/card: title, state, body, milestone, comments."""
        html = await self._get_text(
            self._route("issue", owner=owner, repo=repo, number=number)
        )
        data = self._parse_issue(html, self._profile)
        if data["number"] is None:
            data["number"] = number
        return data

    @staticmethod
    def _check_state(state: str) -> None:
        if state not in _VALID_STATES:
            raise ForgejoError(
                f"Invalid state {state!r}; expected one of {', '.join(_VALID_STATES)}.",
                status=400,
                code="INVALID_STATE",
            )

    @staticmethod
    def _split(issues: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
        """Partition read results into (successful, failed)."""
        ok = [i for i in issues if "error" not in i]
        errors = [i for i in issues if "error" in i]
        return ok, errors

    async def bulk_read_issues(
        self, owner, repo, numbers: list[int], state: str = "all"
    ) -> list[dict[str, Any]]:
        """Read many issues concurrently (rate-limited). Per-issue errors are
        returned inline as ``{"number": n, "error": ...}`` rather than aborting.
        ``state`` ('open'/'closed'/'all') post-filters the results."""
        self._check_state(state)
        results = await asyncio.gather(
            *[self.read_issue(owner, repo, n) for n in numbers],
            return_exceptions=True,
        )
        out: list[dict[str, Any]] = []
        for n, res in zip(numbers, results, strict=True):
            if isinstance(res, BaseException):
                out.append({"number": n, "error": str(res)})
            else:
                out.append(res)
        if state in ("open", "closed"):
            out = [i for i in out if "error" in i or i.get("state") == state]
        return out

    async def _filtered_issue_numbers(
        self,
        owner,
        repo,
        *,
        state: str = "all",
        project: int | None = None,
        milestone: int | None = None,
    ) -> list[int]:
        """Issue numbers from the issues list, filtered server-side by the
        (direct-value) state / project / milestone parameters."""
        params: dict[str, str] = {"state": state, "type": "all"}
        if project is not None:
            params["project"] = str(project)
        if milestone is not None:
            params["milestone"] = str(milestone)
        html = await self._get_text(
            self._route("issues", owner=owner, repo=repo), params=params
        )
        numbers = sorted(
            {int(m.group(1)) for m in self._profile.finditer("issue_link_number", html)}
        )
        logger.debug(
            "Parsed filtered issue list input_chars=%d issues=%d", len(html), len(numbers)
        )
        return numbers

    async def read_milestone_content(
        self, owner, repo, milestone_id, state: str = "all",
        project: int | None = None, limit: int | None = None, offset: int = 0,
    ) -> dict[str, Any]:
        self._check_state(state)
        milestones = await self.list_milestones(owner, repo, "all")
        match = next((m for m in milestones if m["id"] == milestone_id), None)
        if match is None:
            raise ForgejoError(
                f"Milestone {milestone_id} not found in {owner}/{repo}.",
                status=404,
                code="MILESTONE_NOT_FOUND",
            )
        numbers = await self._filtered_issue_numbers(
            owner, repo, state=state, milestone=milestone_id, project=project
        )
        selected, total = _page(numbers, limit, offset)
        issues = await self.bulk_read_issues(owner, repo, selected)
        ok, errors = self._split(issues)
        return {
            "milestone": {"id": milestone_id, "title": match["title"]},
            "filters": {"state": state, "project": project},
            "total": total,
            "returned": len(issues),
            "truncated": offset + len(selected) < total,
            "error_count": len(errors),
            "issues": ok + errors,
        }

    async def _board_allowed_numbers(
        self, owner, repo, project_id, state: str, milestone: int | None
    ) -> set[int] | None:
        """The set of issue numbers matching state/milestone within a project, or
        None when no server-side filter is active (meaning: allow everything)."""
        if state == "all" and milestone is None:
            return None
        return set(
            await self._filtered_issue_numbers(
                owner, repo, state=state, project=project_id, milestone=milestone
            )
        )

    async def read_column_content(
        self, owner, repo, project_id, column_id,
        state: str = "all", milestone: int | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> dict[str, Any]:
        self._check_state(state)
        board = await self.get_project(owner, repo, project_id)
        col = next((c for c in board["columns"] if c["id"] == column_id), None)
        if col is None:
            raise ForgejoError(
                f"Column {column_id} not found in project {project_id}.",
                status=404,
                code="COLUMN_NOT_FOUND",
            )
        allowed = await self._board_allowed_numbers(
            owner, repo, project_id, state, milestone
        )
        numbers = [
            c["number"]
            for c in col["cards"]
            if c.get("number") is not None and (allowed is None or c["number"] in allowed)
        ]
        selected, total = _page(numbers, limit, offset)
        issues = await self.bulk_read_issues(owner, repo, selected)
        _ok, errors = self._split(issues)
        return {
            "column": {"id": col["id"], "title": col["title"]},
            "filters": {"state": state, "milestone": milestone},
            "total": total,
            "returned": len(issues),
            "truncated": offset + len(selected) < total,
            "error_count": len(errors),
            "issues": issues,
        }

    async def read_project_content(
        self, owner, repo, project_id,
        state: str = "all", milestone: int | None = None,
        limit: int | None = None, offset: int = 0,
    ) -> dict[str, Any]:
        self._check_state(state)
        board = await self.get_project(owner, repo, project_id)
        allowed = await self._board_allowed_numbers(
            owner, repo, project_id, state, milestone
        )
        numbers = [
            c["number"]
            for col in board["columns"]
            for c in col["cards"]
            if c.get("number") is not None and (allowed is None or c["number"] in allowed)
        ]
        selected, total = _page(numbers, limit, offset)
        sel_set = set(selected)
        read = await self.bulk_read_issues(owner, repo, selected)
        by_num = {r.get("number"): r for r in read}
        _ok, errors = self._split(read)
        columns = [
            {
                "id": col["id"],
                "title": col["title"],
                "cards": [
                    by_num[c["number"]]
                    for c in col["cards"]
                    if c.get("number") in sel_set
                ],
            }
            for col in board["columns"]
        ]
        return {
            "project_id": project_id,
            "title": board.get("title"),
            "filters": {"state": state, "milestone": milestone},
            "total": total,
            "returned": len(selected),
            "truncated": offset + len(selected) < total,
            "error_count": len(errors),
            "column_count": len(columns),
            "columns": columns,
        }

    async def bulk_move_cards(
        self, owner, repo, project_id, moves: list[dict[str, int]]
    ) -> dict[str, Any]:
        """Move many cards at once. ``moves`` is a list of
        ``{"issue_number": N, "column_id": C}``; cards headed to the same column
        are placed in the order given. Issue-id lookups and per-column moves run
        concurrently (rate-limited)."""
        numbers = [m["issue_number"] for m in moves]
        ids = await asyncio.gather(
            *[self.resolve_issue_id(owner, repo, n) for n in numbers]
        )
        id_by_num = dict(zip(numbers, ids, strict=True))
        groups: dict[int, list[int]] = {}
        for m in moves:
            groups.setdefault(m["column_id"], []).append(m["issue_number"])

        async def _move(col: int, col_nums: list[int]) -> dict[str, Any]:
            payload = {
                "issues": [
                    {"issueID": id_by_num[n], "sorting": i}
                    for i, n in enumerate(col_nums)
                ]
            }
            await self._request(
                "POST",
                self._route(
                    "column_move",
                    owner=owner,
                    repo=repo,
                    project_id=project_id,
                    column_id=col,
                ),
                json=payload,
                follow=False,
            )
            return {"column_id": col, "moved": col_nums}

        columns = await asyncio.gather(
            *[_move(c, ns) for c, ns in groups.items()]
        )
        return {"moved_count": len(moves), "columns": list(columns)}

    # ------------------------------------------------------------ milestones
    @staticmethod
    def _parse_milestones(
        html: str, profile: Profile = DEFAULT_PROFILE
    ) -> list[dict[str, Any]]:
        out: dict[int, str] = {}
        for m in profile.finditer("milestone_link", html):
            mid = int(m.group(1))
            title = unescape(m.group(2).strip())
            if title and (mid not in out or len(title) > len(out[mid])):
                out[mid] = title
        result = [{"id": mid, "title": t} for mid, t in sorted(out.items())]
        logger.debug(
            "Parsed milestones list input_chars=%d milestones=%d", len(html), len(result)
        )
        return result

    async def _list_milestones_state(self, owner, repo, state):
        html = await self._get_text(
            self._route("milestones", owner=owner, repo=repo), params={"state": state}
        )
        return self._parse_milestones(html, self._profile)

    async def list_milestones(self, owner, repo, state: str = "open"):
        self._check_state(state)
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
            self._route("milestone_new", owner=owner, repo=repo),
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
            self._route(
                "milestone_edit", owner=owner, repo=repo, milestone_id=milestone_id
            ),
            form=form,
            follow=False,
        )
        return {"updated": True, "milestone_id": milestone_id}

    async def _milestone_action(self, owner, repo, milestone_id, action):
        """POST a milestone state change; ``action`` names route and result."""
        await self._request(
            "POST",
            self._route(
                f"milestone_{action}",
                owner=owner,
                repo=repo,
                milestone_id=milestone_id,
            ),
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
            self._route("milestone_delete", owner=owner, repo=repo),
            form={"id": str(milestone_id)},
            follow=False,
        )
        return {"deleted": True, "milestone_id": milestone_id}
