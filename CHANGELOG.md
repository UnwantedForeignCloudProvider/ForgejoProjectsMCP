# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are derived automatically from git tags (`vX.Y.Z`) via
`uv-dynamic-versioning`.

## [Unreleased]

### Added
- Persist the non-secret connection settings (instance URL and username) to
  `<config>/forgejo_projects_mcp/config.json` after a successful login, so later
  runs need no environment variables. The password is never persisted.
- CLI credential options accepted before or after the tool name:
  `--forgejo-url`, `--forgejo-username`, `--forgejo-password`, and
  `--forgejo-password-stdin` (reads the password from stdin). Precedence is
  CLI option > env var > persisted config.
- `forgejo_status` and `authenticate` now report the Forgejo instance version
  and the compatibility profile resolved for it (`csrf_mode`, the
  version-specific `quirks` in force, and whether the version is inside the
  verified range). The version is read from the same response that proves the
  session, so it costs no extra request.
- Per-version behavior adaptation: every internal route, HTML pattern and form
  value now comes from a version profile, and documented `quirks` override it
  for the releases that differ. Three are registered — Forgejo below 14.0
  requires a CSRF token on writes, Forgejo below 10.0 omits the board title from
  the page `<title>`, and Forgejo below 1.21 calls project columns "boards" in
  its markup. An unknown or unreadable version falls back to the newest verified
  behavior rather than failing.
- Support for every published Forgejo release from 1.20 onwards. Writes on
  releases below 14.0 were previously rejected with HTTP 400 "Invalid CSRF
  token" because only an `Origin` header was sent; they now carry the session's
  CSRF token, and a rejected write is retried once with a token on any version.
  Forgejo 1.20 and 1.21 boards, whose markup predates the current column
  vocabulary, are now parsed correctly.
- The number of a newly created issue is now also recovered from the `Location`
  header, which is how Forgejo 1.20 answers `issues/new` instead of returning
  JSON.
- Fully automated integration test suite (`pytest -m integration
  --forgejo-version N`): it starts a throwaway Forgejo container for each
  requested version, waits for its health check, creates an admin, and seeds a
  repository with issues and a milestone, then runs every test once per version.
  It covers the full board lifecycle — projects, columns, cards, issues,
  milestones, composed reads and paging — and tears down what it started.
  Verified against Forgejo 1.20, 1.21 and every major from 7 to 16.
- Live counterparts for the offline suite, so every behavior that was pinned
  down against a fake transport is also checked against a real instance:
  authentication, session caching and credential recovery; the operation,
  filtering and error paths of the client; the HTML parsers, run over pages
  Forgejo actually rendered; the MCP tool layer, dispatched end to end; the CLI,
  including runs as a separate process through the installed console script; the
  debug logging, asserting real passwords and real issue content never reach it;
  and version detection, CSRF mode and quirk resolution per release.

### Changed
- `tests/composes/` now holds a single parameterized Docker Compose stack driven
  by `FORGEJO_VERSION` and `FORGEJO_PORT`, replacing the per-version
  directories. The suite drives it directly, so no manual `docker compose` step
  is needed.
- An instance addressed by `FORGEJO_TEST_URL` is treated as not disposable:
  tests that create or delete data skip unless `FORGEJO_TEST_ALLOW_WRITES=1`.

### Fixed
- The board title is now read from the project heading instead of the page
  `<title>`, which reports the repository rather than the board on Forgejo below
  10.0 and could contain a truncated title wherever a board name contains " - ".
- `read_card`, `read_column`, `read_project` and `read_milestone` returned an
  empty `body` for every issue. Forgejo keys an issue's body element by the
  issue's global id, but the client looked it up by the repository-local issue
  number; the two are equal only in the first repository an instance ever
  creates, so the body was lost everywhere else.
- The same readers reported `milestone: null` for every issue on Forgejo 11 and
  below, which place an icon inside the milestone link so the title is not the
  first thing after the opening tag.

## [0.1.0rc2] - 2026-09-05

### Added
- Prompt interactively for missing or rejected Forgejo credentials in terminal
  CLI sessions, without persisting passwords.

### Fixed
- Publish GitHub Releases through a draft-first flow so immutable releases
  receive the wheel and source distribution before publication.

## [0.1.0rc1] - 2026-09-04

### Added
- Initial prerelease of the MCP server for managing Forgejo repository projects,
  columns, cards/issues, and milestones through Forgejo's internal web routes.
- Bulk and composed readers with state and resource filters, pagination, request
  throttling, and explicit partial-error reporting.
- A CLI exposing every MCP tool as a subcommand, automatic `.env` loading,
  cached session authentication, configurable logging, and concurrency controls.
- User, contributor, architecture, automation-reference, and versioned MkDocs
  documentation, plus tag-driven PyPI and GitHub release workflows.

<!--
When cutting a release, move the Unreleased items into a new section titled
exactly `## [X.Y.Z] - YYYY-MM-DD` (the release workflow extracts the notes for a
tag `vX.Y.Z` from the matching `## [X.Y.Z]` heading), then tag and push:

    git tag vX.Y.Z && git push origin vX.Y.Z

Example:

## [0.1.0] - 2026-09-04

### Added
- Initial release: MCP server for managing Forgejo Projects/Kanban boards
  (projects, columns, cards, milestones) over Forgejo's internal web routes.
-->
