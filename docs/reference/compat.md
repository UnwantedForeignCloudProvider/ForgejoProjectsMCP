# `forgejo_projects_mcp.compat`

Source: [`src/forgejo_projects_mcp/compat.py`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/src/forgejo_projects_mcp/compat.py)

`compat.py` holds everything that depends on *which* Forgejo release is being driven. Because Projects are automated through undocumented internal web routes, the paths, form fields, CSRF rules and HTML markup are all free to change between releases. This module keeps those differences as data instead of scattering version checks through the client.

## The model

- A **`Version`** is a comparable `major.minor.patch`, parsed from any string Forgejo publishes.
- A **`Profile`** describes every version-dependent thing the client needs: route templates, HTML patterns, form value maps and the CSRF strategy.
- A **`Quirk`** is one documented exception that applies to a version range and overrides part of a profile.
- **`profile_for(version)`** starts from the base profile — the newest verified behavior — and applies every matching quirk in order.

Supporting a new release is therefore usually one new `Quirk`, not a branch inside `client.py`.

An unknown version (detection failed, or a release newer than anything listed) resolves to the base profile. Nothing hard-fails because a version could not be read; the client's runtime recovery covers the rest.

## `Version`

```python
@dataclass(frozen=True, order=True)
class Version:
    major: int
    minor: int = 0
    patch: int = 0
    raw: str = ""   # excluded from comparisons
```

### `Version.parse`

```python
Version.parse(text: str | None) -> Version | None
```

Reads the leading `X.Y[.Z]` from any shape Forgejo publishes:

| Source | Example |
|---|---|
| REST API `/api/v1/version` | `16.0.3+gitea-1.22.0` |
| HTML `window.config.assetVersionEncoded` | `16.0.3~gitea-1.22.0` |
| Footer / plain | `16.0.3` |
| Legacy numbering | `1.21.11-2` |

Returns `None` when no version number is present. `raw` keeps the original string; `short` returns the numeric triple alone.

## `Profile`

```python
@dataclass(frozen=True)
class Profile:
    csrf_mode: str                                # "origin" or "token"
    routes: Mapping[str, str]                     # operation -> path template
    patterns: Mapping[str, tuple[str, ...]]       # element -> ordered candidates
    card_types: Mapping[str, str]                 # "text" -> "1", ...
    version: Version | None
    quirks: tuple[str, ...]                       # ids of the applied quirks
```

### Routing

```python
profile.route("column_move", owner="o", repo="r", project_id=3, column_id=7)
# -> "/o/r/projects/3/7/move"
```

Every internal route the client uses is a named template. `route()` raises `KeyError` for an unknown operation, which is a programming error rather than a Forgejo failure.

### Parsing

Patterns are **ordered candidate tuples**, and the first candidate that matches wins:

```python
profile.search(key, text, **fmt)     # first match, or None
profile.finditer(key, text, **fmt)   # all matches from one candidate
profile.split(key, text)             # split on the first candidate that divides
```

`finditer` never mixes two candidates — they are alternatives, so combining them would double-count elements. `fmt` fills placeholders inside the pattern itself (used by `issue_raw`, which is parameterized by element id) and its values are escaped.

A markup change is therefore handled by prepending a pattern for the affected versions while older instances keep working through the later candidates.

### `describe`

```python
profile.describe() -> dict
```

```json
{
  "version": "16.0.3~gitea-1.22.0",
  "version_short": "16.0.3",
  "csrf_mode": "origin",
  "quirks": [],
  "verified": true
}
```

This is what `forgejo_status` and `authenticate` return under `compatibility`. `verified` says whether the version falls inside the range the integration suite actually exercises — `OLDEST_VERIFIED` (1.20) to `NEWEST_VERIFIED` (16), which covers every published Forgejo release, since its numbering jumps from the 1.x line straight to 7.0.

### `with_csrf_mode`

```python
profile.with_csrf_mode(mode: str) -> Profile
```

Returns a copy using a different CSRF strategy, leaving everything else intact. The client uses it to adapt at runtime when an instance demands a token its version did not predict.

## `Quirk`

```python
@dataclass(frozen=True)
class Quirk:
    id: str
    description: str
    overrides: Mapping[str, Any]
    at_least: Version | None = None   # applies when at_least <= version
    below: Version | None = None      # applies when version < below
```

`overrides` names plain `Profile` fields. `routes`, `patterns` and `card_types` are **merged** into the inherited mappings rather than replacing them, so a quirk states only what actually differs. A quirk never matches an unknown version.

### Registered quirks

| Id | Applies to | Effect |
|---|---|---|
| `legacy-board-vocabulary` | below 1.21 | Project columns were called *boards* in the markup: the container carries `board-column` and its title sits in a `board-label` element after a `board-card-cnt` badge, with no `project-column` markup at all. |
| `board-title-missing-from-page-title` | below 10.0 | The board page `<title>` is just `owner/repo` on these releases, so only the project heading is trusted for the board title. |
| `csrf-token-required` | below 14.0 | These releases reject a write carrying only a matching `Origin` header with HTTP 400 *Invalid CSRF token*, so writes must send the session's token. |

Quirks compose: Forgejo 1.20 matches all three.

## Module functions

```python
profile_for(version: Version | None) -> Profile
detect_version(html: str) -> Version | None
detect_csrf_token(html: str) -> str | None
```

`detect_version` and `detect_csrf_token` read a rendered Forgejo page. Every authenticated page carries both, which is what lets the client's session probe establish authentication *and* version in one request — see [`client.py`](client.md).

`DEFAULT_PROFILE` is `profile_for(None)`: the newest verified behavior, used before any probe has run and by the parse helpers when no profile is passed.
