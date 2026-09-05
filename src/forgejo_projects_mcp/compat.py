"""Forgejo version detection and per-version behavior profiles.

Forgejo Projects are driven through undocumented internal web routes, so the
exact paths, form fields, CSRF rules and HTML markup are free to change between
releases. This module turns that risk into data: a :class:`Profile` describes
everything version-dependent that the client needs, and :func:`profile_for`
builds the right profile for a detected :class:`Version`.

How a profile is built
----------------------

There is one *base* profile (:data:`_BASE`) describing the newest verified
Forgejo behavior, plus an ordered list of :class:`Quirk` entries. A quirk is a
small, documented exception that applies to a version range and overrides a few
profile fields. ``profile_for(version)`` starts from the base and applies every
matching quirk in order, so supporting a new release usually means adding one
quirk rather than branching inside the client.

An **unknown** version (detection failed, or a release newer than anything we
know about) resolves to the base profile: newest-known behavior, plus the
client's runtime recovery (for example, retrying a rejected write with a CSRF
token). Nothing hard-fails just because a version could not be read.

Regular expressions live in the profile too, as *ordered candidate tuples*. A
parser tries each candidate in turn and uses the first that matches, so a markup
change can be handled by prepending a new pattern for the affected versions
while older instances keep working through the later candidates.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Any

logger = logging.getLogger("forgejo_projects_mcp.compat")

_VERSION_NUMBER_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


@dataclass(frozen=True, order=True)
class Version:
    """A comparable Forgejo version.

    Only the numeric triple takes part in comparisons; ``raw`` keeps the
    original string (for example ``16.0.3+gitea-1.22.0``) for diagnostics.
    """

    major: int
    minor: int = 0
    patch: int = 0
    raw: str = field(default="", compare=False)

    @classmethod
    def parse(cls, text: str | None) -> Version | None:
        """Parse the leading ``X.Y[.Z]`` of a Forgejo version string.

        Accepts every shape Forgejo publishes: ``16.0.3``,
        ``16.0.3+gitea-1.22.0`` (REST API), ``16.0.3~gitea-1.22.0`` (HTML asset
        version) and legacy ``1.21.11-2``. Returns ``None`` when no version
        number is present.
        """
        if not text:
            return None
        m = _VERSION_NUMBER_RE.search(text)
        if not m:
            return None
        return cls(
            major=int(m.group(1)),
            minor=int(m.group(2)),
            patch=int(m.group(3) or 0),
            raw=text.strip(),
        )

    def __str__(self) -> str:
        return self.raw or f"{self.major}.{self.minor}.{self.patch}"

    @property
    def short(self) -> str:
        """The numeric triple alone, without any build metadata."""
        return f"{self.major}.{self.minor}.{self.patch}"


# The oldest and newest releases this project has actually been exercised
# against (see tests/integration/). Outside this window the client still runs,
# but `forgejo_status` reports the version as unverified. Forgejo's numbering
# jumps straight from the 1.x line to 7.0, so this window covers every published
# release from 1.20 onwards.
OLDEST_VERIFIED = Version(1, 20, 0)
NEWEST_VERIFIED = Version(16, 0, 0)


# --------------------------------------------------------------------- routes
# Every internal web route the client uses, as a format template. Templates are
# rendered with owner/repo/ids by Profile.route(); a quirk can replace any entry
# for the versions that need a different path.
_ROUTES: Mapping[str, str] = {
    # session
    "login": "/user/login",
    "auth_probe": "/user/settings",
    "repo_search": "/repo/search",
    # projects
    "projects": "/{owner}/{repo}/projects",
    "project_new": "/{owner}/{repo}/projects/new",
    "project": "/{owner}/{repo}/projects/{project_id}",
    "project_edit": "/{owner}/{repo}/projects/{project_id}/edit",
    "project_close": "/{owner}/{repo}/projects/{project_id}/close",
    "project_open": "/{owner}/{repo}/projects/{project_id}/open",
    "project_delete": "/{owner}/{repo}/projects/{project_id}/delete",
    # columns (created by POSTing to the board itself)
    "column_new": "/{owner}/{repo}/projects/{project_id}",
    "column": "/{owner}/{repo}/projects/{project_id}/{column_id}",
    "column_default": "/{owner}/{repo}/projects/{project_id}/{column_id}/default",
    "column_move": "/{owner}/{repo}/projects/{project_id}/{column_id}/move",
    # issues / cards
    "issues": "/{owner}/{repo}/issues",
    "issue": "/{owner}/{repo}/issues/{number}",
    "issue_new": "/{owner}/{repo}/issues/new",
    "issue_delete": "/{owner}/{repo}/issues/{number}/delete",
    "issue_projects": "/{owner}/{repo}/issues/projects",
    # milestones
    "milestones": "/{owner}/{repo}/milestones",
    "milestone_new": "/{owner}/{repo}/milestones/new",
    "milestone_edit": "/{owner}/{repo}/milestones/{milestone_id}/edit",
    "milestone_close": "/{owner}/{repo}/milestones/{milestone_id}/close",
    "milestone_open": "/{owner}/{repo}/milestones/{milestone_id}/open",
    # NOTE: deletion is /milestones/delete?id=N -- /milestones/{id}/delete
    # answers 200 but does nothing.
    "milestone_delete": "/{owner}/{repo}/milestones/delete",
}


# The board title lives in the project heading. Forgejo renamed its utility-class
# prefix from `gt-` to `tw-` when it moved to Tailwind, so both spellings are
# kept; the heading is the only place the title appears on every release.
_BOARD_TITLE_HEADINGS = (
    r'<h2 class="tw-mb-0 tw-flex-1 tw-break-anywhere">\s*([^<]+?)\s*</h2>',
    r'<h2 class="gt-mb-0">\s*([^<]+?)\s*</h2>',
    r'<h2 class="project-title">\s*([^<]+?)\s*</h2>',
)


# ------------------------------------------------------------------- patterns
# Ordered candidates per parsed element. The first candidate that matches wins,
# so a version-specific pattern is prepended by a quirk while the trailing
# entries keep older (or newer-but-unchanged) instances working.
_PATTERNS: Mapping[str, tuple[str, ...]] = {
    # Instance version, read from any authenticated HTML page.
    "version": (
        r"assetVersionEncoded:\s*encodeURIComponent\(\s*'([^']+)'\s*\)",
        r"/assets/js/index\.js\?v=([0-9][^\"'&]*)",
        r"Version:\s*(?:<[^>]*>\s*)*([0-9]+\.[0-9]+[^<\s]*)",
    ),
    # CSRF token, present in window.config on Forgejo < 14 and in every form.
    "csrf_token": (
        r"csrfToken:\s*'([^']+)'",
        r'name="_csrf"\s+value="([^"]+)"',
    ),
    # Projects list: id + title from each board link.
    "projects_link": (r'href="[^"]*/projects/(\d+)"[^>]*>\s*([^<]+?)\s*</a>',),
    # Board: the split marker for a real column container. The class token must
    # end at a quote or space so project-column-header / -title / the
    # new-project-column modal are not treated as columns.
    # The column container. `project-column` must be a whole class token --
    # optionally preceded by other classes, and followed by a space or the
    # closing quote -- so that project-column-header, project-column-title and
    # new-project-column-modal are not mistaken for columns.
    "board_column_open": (
        r'(<div class="(?:[^"]*\s)?project-column(?=[ "])[^"]*"[^>]*>)',
    ),
    "board_column_id": (r'data-id="(\d+)"',),
    "board_column_title": (
        r"project-column-title-label[^>]*>\s*([^<]+?)\s*<",
        # Forgejo 1.x has no title *label*: the text sits directly in the title
        # element, after a nested issue-count badge.
        r"project-column-title[^>]*>"
        r"(?:.*?project-column-issue-count[^>]*>.*?</div>)?\s*([^<]+?)\s*<",
    ),
    "board_card": (r'data-issue="(\d+)"(.*?)(?=data-issue="|\Z)',),
    "board_card_number": (r'/issues/(\d+)"',),
    "board_card_title": (r'/issues/\d+"[^>]*>\s*([^<]+?)\s*</a>',),
    # Board title. The project heading carries the title verbatim on every
    # version we have exercised; the <title> variants are fallbacks for a future
    # template that drops the heading. See the board-title-missing-from-page-
    # title quirk for why the first <title> fallback is not safe everywhere.
    "board_title": _BOARD_TITLE_HEADINGS
    + (
        r"<title>\s*([^<]+?)\s+-\s+[^<]*</title>",
        r"<title>\s*([^<]+?)\s*</title>",
    ),
    # Project edit form (used to preserve fields the caller left unset).
    "project_edit_title": (r'name="title"[^>]*value="([^"]*)"',),
    "project_edit_card_type": (r'name="card_type"[^>]*value="([^"]*)"',),
    # Issue page.
    "issue_id": (r'data-issue-id="(\d+)"',),
    "issue_number": (r'<span class="index">#(\d+)</span>',),
    "issue_title": (r'<meta property="og:title" content="([^"]*)"',),
    "issue_closed": (
        r'issue-state-label"[^>]*>\s*<svg[^>]*octicon-issue-closed',
    ),
    "issue_raw": (r'id="{id}-raw"[^>]*>(.*?)</div>',),
    "issue_milestone": (
        r"""href=["'][^"']*/milestone/(\d+)["'][^>]*>\s*([^<]+?)\s*</a>""",
        # Forgejo 11 and below wrap an icon inside the sidebar link, so the
        # title is not the first thing after the opening tag.
        r"""href=["'][^"']*/milestone/(\d+)["'][^>]*>(?:\s*<[^>]*>)*\s*([^<]+?)\s*</a>""",
    ),
    "issue_link_number": (r"/issues/(\d+)",),
    "comment_block": (
        r'<div class="timeline-item comment" id="issuecomment-(\d+)">'
        r'(.*?)(?=<div class="timeline-item|\Z)',
    ),
    "comment_author": (r'class="author[^"]*"[^>]*>\s*([^<]+?)\s*</a>',),
    # Milestones list.
    "milestone_link": (r'href="[^"]*/milestone/(\d+)"[^>]*>\s*([^<]+?)\s*</a>',),
    # New-issue response: Forgejo answers 200 with {"redirect": ".../issues/N"}.
    "created_issue_number": (r"/issues/(\d+)",),
}


# CSRF strategies.
CSRF_ORIGIN = "origin"  # a matching Origin header is accepted; no token needed
CSRF_TOKEN = "token"  # an X-Csrf-Token header (or _csrf field) is required


@dataclass(frozen=True)
class Profile:
    """Everything version-dependent about talking to one Forgejo instance."""

    csrf_mode: str = CSRF_ORIGIN
    routes: Mapping[str, str] = field(default_factory=lambda: _ROUTES)
    patterns: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: _PATTERNS)
    card_types: Mapping[str, str] = field(
        default_factory=lambda: {"text": "1", "images_and_text": "2"}
    )
    version: Version | None = None
    quirks: tuple[str, ...] = ()

    # ------------------------------------------------------------- routing
    def route(self, name: str, **params: Any) -> str:
        """Render the web-route template for ``name``.

        Raises ``KeyError`` for an unknown operation, which is a programming
        error rather than a Forgejo failure.
        """
        template = self.routes[name]
        return template.format(**params)

    # ------------------------------------------------------------- parsing
    def compiled(self, key: str) -> tuple[re.Pattern[str], ...]:
        """The ordered, compiled candidates for a parsed element."""
        return _compile(self.patterns[key])

    def search(self, key: str, text: str, **fmt: Any) -> re.Match[str] | None:
        """First match from the first candidate that matches, or ``None``.

        ``fmt`` fills placeholders in the pattern itself (used by the
        ``issue_raw`` pattern, which is parameterized by element id); the
        substituted values are escaped.
        """
        for pattern in self._prepare(key, fmt):
            m = pattern.search(text)
            if m:
                return m
        return None

    def finditer(self, key: str, text: str, **fmt: Any) -> Iterator[re.Match[str]]:
        """Iterate matches from the first candidate that matches anything.

        Candidates are alternatives, not supplements: mixing results from two
        patterns would double-count elements, so only one candidate is used.
        """
        for pattern in self._prepare(key, fmt):
            found = list(pattern.finditer(text))
            if found:
                return iter(found)
        return iter(())

    def split(self, key: str, text: str) -> list[str]:
        """Split ``text`` on the first candidate that actually divides it.

        The pattern is expected to capture its separator, so the result
        alternates ``[before, separator, chunk, separator, chunk, ...]``.
        """
        for pattern in self.compiled(key):
            parts = pattern.split(text)
            if len(parts) > 1:
                return parts
        return [text]

    def with_csrf_mode(self, mode: str) -> Profile:
        """A copy using a different CSRF strategy.

        Used for runtime adaptation when an instance demands a token that its
        version alone did not predict.
        """
        return replace(self, csrf_mode=mode)

    def _prepare(self, key: str, fmt: Mapping[str, Any]) -> tuple[re.Pattern[str], ...]:
        if not fmt:
            return self.compiled(key)
        escaped = {k: re.escape(str(v)) for k, v in fmt.items()}
        return tuple(
            _compile_one(raw.format(**escaped)) for raw in self.patterns[key]
        )

    # --------------------------------------------------------- diagnostics
    def describe(self) -> dict[str, Any]:
        """A small, log- and tool-safe summary of the resolved behavior."""
        version = self.version
        return {
            "version": str(version) if version else None,
            "version_short": version.short if version else None,
            "csrf_mode": self.csrf_mode,
            "quirks": list(self.quirks),
            "verified": bool(
                version is not None
                and OLDEST_VERIFIED <= version
                and version.major <= NEWEST_VERIFIED.major
            ),
        }


@lru_cache(maxsize=256)
def _compile_one(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.S)


def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(_compile_one(p) for p in patterns)


@dataclass(frozen=True)
class Quirk:
    """One documented, version-scoped deviation from the base profile.

    ``below`` and ``at_least`` bound the versions the quirk applies to
    (``at_least <= version < below``); either may be ``None`` for an open end.
    ``overrides`` names plain :class:`Profile` fields; ``routes`` and
    ``patterns`` are merged into the inherited mappings rather than replacing
    them, so a quirk only states what actually differs.
    """

    id: str
    description: str
    overrides: Mapping[str, Any]
    at_least: Version | None = None
    below: Version | None = None

    def matches(self, version: Version | None) -> bool:
        """Whether this quirk applies. An unknown version matches nothing."""
        if version is None:
            return False
        if self.at_least is not None and version < self.at_least:
            return False
        if self.below is not None and version >= self.below:
            return False
        return True

    def apply(self, profile: Profile) -> Profile:
        changes = dict(self.overrides)
        for merged in ("routes", "patterns", "card_types"):
            if merged in changes:
                changes[merged] = {**getattr(profile, merged), **changes[merged]}
        return replace(profile, **changes)


# The newest verified behavior. Everything else is expressed as a quirk below.
_BASE = Profile()


# Ordered oldest-first; every matching quirk is applied in this order.
QUIRKS: tuple[Quirk, ...] = (
    Quirk(
        id="legacy-board-vocabulary",
        description=(
            "Forgejo below 1.21 predates the rename of project 'boards' to "
            "'columns': a column container carries the class board-column, its "
            "title sits in a board-label element after a board-card-cnt badge, "
            "and there is no project-column markup at all. The column patterns "
            "are replaced wholesale for those releases."
        ),
        overrides={
            "patterns": {
                "board_column_open": (
                    r'(<div class="(?:[^"]*\s)?board-column(?=[ "])[^"]*"[^>]*>)',
                ),
                "board_column_title": (
                    r"board-label[^>]*>"
                    r"(?:.*?board-card-cnt[^>]*>.*?</div>)?\s*([^<]+?)\s*<",
                ),
            }
        },
        below=Version(1, 21, 0),
    ),
    Quirk(
        id="board-title-missing-from-page-title",
        description=(
            "Forgejo below 10.0 renders a project board with a page <title> of "
            "just 'owner/repo', so the usual <title> fallbacks would report the "
            "repository instead of the board. Only the project heading is "
            "trusted on those versions."
        ),
        overrides={"patterns": {"board_title": _BOARD_TITLE_HEADINGS}},
        below=Version(10, 0, 0),
    ),
    Quirk(
        id="csrf-token-required",
        description=(
            "Forgejo below 14.0 rejects a write that carries only a matching "
            "Origin header with HTTP 400 'Invalid CSRF token'. Writes must "
            "send the session's CSRF token, which those versions publish as "
            "window.config.csrfToken on every authenticated page."
        ),
        overrides={"csrf_mode": CSRF_TOKEN},
        below=Version(14, 0, 0),
    ),
)


@lru_cache(maxsize=64)
def profile_for(version: Version | None) -> Profile:
    """Resolve the behavior profile for a detected instance version.

    An unrecognised or undetectable version resolves to the newest verified
    behavior, on the assumption that a future release continues from where the
    current one left off. The client's runtime recovery covers the rest.
    """
    profile = _BASE
    applied: list[str] = []
    for quirk in QUIRKS:
        if quirk.matches(version):
            profile = quirk.apply(profile)
            applied.append(quirk.id)
    resolved = replace(profile, version=version, quirks=tuple(applied))
    logger.debug(
        "Resolved compatibility profile version=%s csrf_mode=%s quirks=%s",
        version.short if version else "unknown",
        resolved.csrf_mode,
        ",".join(applied) or "none",
    )
    return resolved


DEFAULT_PROFILE = profile_for(None)


def detect_version(html: str) -> Version | None:
    """Extract the instance version from an authenticated HTML page.

    Forgejo embeds its asset version in ``window.config`` on every rendered
    page, which lets the session probe establish authentication *and* version in
    a single request. Returns ``None`` for a body that carries no version
    marker (an empty body, a redirect, or a future template change).
    """
    m = DEFAULT_PROFILE.search("version", html)
    return Version.parse(m.group(1)) if m else None


def detect_csrf_token(html: str) -> str | None:
    """Extract the session CSRF token from an HTML page, if it publishes one."""
    m = DEFAULT_PROFILE.search("csrf_token", html)
    return m.group(1) if m else None
