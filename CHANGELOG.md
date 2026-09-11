# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Versions are derived automatically from git tags (`vX.Y.Z`) via
`uv-dynamic-versioning`.

## [Unreleased]

### Added

- `remove_issues_from_project` accepts an optional `project_id`. It is a guard,
  not a scope: when given, every issue must already be a card on that board or
  the call is refused with `CARD_NOT_FOUND` and nothing is detached. Forgejo
  clears an issue's project assignment outright and an issue only ever holds
  one, so a project-scoped variant is not a thing that can exist — but naming
  the board you believe the issues are on now stops the call from silently
  clearing an assignment you did not mean to touch.

### Changed

- `add_issues_to_project` reads the board back and reports where each card
  landed. The attach route answers a write that changed nothing exactly like one
  that worked, so `attached` previously meant only that Forgejo had not refused
  the form. The result now carries `cards`, naming the column each issue landed
  in, and an attachment that produced no card is `ATTACH_UNVERIFIED` rather than
  a reported success. This costs one board read per call. `move_card` and
  `bulk_move_cards` deliberately do not do this: their route reports success
  explicitly, so there is nothing to re-derive.
- Numeric arguments are matched strictly. Pydantic's default coercion turned
  `"7"`, `7.0` and `true` into integers, so a malformed call became a
  well-formed call against the wrong resource — `true` read issue 1 — and the
  caller was told it had succeeded. These are now `INVALID_INPUT`. The published
  tool schemas are unchanged, but an MCP client that sends a stringified number
  where an id belongs will start being refused.
- `bulk_read_issues` reads a repeated issue number once. A duplicate used to be
  fetched once per occurrence and counted once per occurrence, so a list
  assembled from two overlapping sources paid for the overlap and then
  over-reported it. `count` is now the number of distinct issues read. Unlike a
  batch of moves, a repeated read contradicts nothing, so it is collapsed rather
  than refused.
- The CLI converts optional numeric and array options before dispatch. An
  optional tool argument is published as `anyOf: [integer, null]` with no type
  of its own, so the generated parser attached no converter and handed the tool
  a string; that was invisible while arguments were coerced and would have
  broken every optional numeric flag under strict matching. A JSON argument of
  the wrong shape, such as an object where an array belongs, is now a usage
  error (exit code 2) instead of being passed on.
- Documented Forgejo's `list_repositories` pagination boundaries: a `limit` of
  `0` or less yields Forgejo's own default page size, `page=0` behaves as page
  `1`, and the endpoint publishes no total. Behavior is unchanged — the client
  passes both values through deliberately rather than asserting a contract it
  does not own.

- `add_issues_to_project` is documented as the move it is. Forgejo stores one
  project assignment per issue, so attaching an issue that is already on another
  board removes it from that board; the route cannot express membership of two
  boards. Behavior is unchanged — the tool description, usage table and
  automation reference now say so plainly.

### Fixed

- The full readers name an unusable filter instead of returning an empty result.
  Forgejo applies `project=` and `milestone=` as direct values and renders an
  ordinary empty issue list for an id it does not know, so
  `read_project --milestone 999999` reported a successful read of zero issues —
  indistinguishable from a board that genuinely had none, and a way for an agent
  to conclude that work had vanished. The id is confirmed first and reported as
  `MILESTONE_NOT_FOUND`/`PROJECT_NOT_FOUND`. The lookup is only paid for when a
  filter is actually passed, and never for the project or milestone that is the
  reader's own subject.
- A rejected argument carries the `INVALID_INPUT` code instead of raw
  multi-line Pydantic text ending in a documentation URL. Argument validation
  runs before the tool body, so it was the one failure in this surface with no
  `[CODE]` for a caller to branch on. This applies to the MCP transport and the
  CLI alike.
- `list_projects` and `list_milestones` now return every entry. Forgejo
  paginates both list pages at 20 and publishes no total, so the client was
  reading only the first page: an existing project or milestone beyond it looked
  simply absent. The client now walks the pages.
- `create_milestone` no longer reports `CREATE_UNVERIFIED` for a milestone it
  successfully created. Creates verify themselves by reading the list back, and
  the milestones page puts the newest entries on the *last* page, so every
  milestone created once 20 existed failed its own verification — and retrying
  produced duplicates. This followed from the truncation above and is fixed with
  it. (Projects order the other way, so they were never affected.)
- `create_issue` names an unusable `project_id` or `milestone_id` instead of
  passing on an unnamed upstream error. Forgejo validates these only as it
  applies them and reports the failure inconsistently — an unknown project is
  HTTP 404 on Forgejo 16 but part of the same bare HTTP 500 on 1.20 — so the
  client confirms which reference is missing and reports
  `PROJECT_NOT_FOUND`/`MILESTONE_NOT_FOUND` on every release. No issue is
  created in either case. The check runs only after a failure, so a valid
  create costs nothing extra.
- `move_card` and `bulk_move_cards` report an issue that is not a card on the
  project as `CARD_NOT_FOUND` instead of passing on the bare HTTP 500 Forgejo
  answers with, which was indistinguishable from a server fault.
- `bulk_move_cards` refuses a batch naming the same issue twice. The per-column
  requests run concurrently, so such a batch applied both moves, left the card
  wherever the later write landed, and reported it in both columns at once.
  `move_card` applies the same rule to its own list.
- Column colors are validated before the request. Forgejo answers a color it
  cannot parse with a bare HTTP 500; anything that is not six hex digits after a
  `#` is now `INVALID_INPUT`. Note that Forgejo does not accept the `#fff`
  shorthand, so neither does this client.
- `create_project` no longer returns an unrelated project when the board it
  created cannot be identified. It previously fell back to the last project in
  the list, so a follow-up edit or delete could be aimed at someone else's
  board. It now raises `CREATE_UNVERIFIED`, and `create_column` and
  `create_milestone` do the same instead of returning `created: true` with a
  null resource.
- A write Forgejo refuses is now reported as `REJECTED` instead of a success.
  Forgejo answers a refused form by re-rendering it with HTTP 200, which is
  below the error threshold, so an impossible milestone deadline such as
  `2026-02-30` used to come back as `created: true` with nothing created.
- `edit_milestone` now preserves the fields it was not given. The edit route
  replaces the whole milestone, so editing only the description or only the
  deadline submitted a blank title, which Forgejo refuses outright — the call
  reported success and changed nothing — while editing only the title silently
  cleared the description and the deadline. Arguments left unset are read back
  from Forgejo's edit form; pass an empty string to clear a field on purpose.
- `update_project` likewise preserves an omitted description rather than
  clearing it, and decodes the values it carries over, so a title or description
  containing `&`, quotes or angle brackets no longer gains a layer of HTML
  escaping on every partial edit.
- `create_project(card_type=...)` now stores the card type that was asked for.
  The two names were mapped to the wrong Forgejo values: `text` produced an
  images-and-text board, and `images_and_text` was out of range and produced a
  text board. An unknown `card_type` is now `INVALID_INPUT` rather than being
  silently stored as text.
- `delete_milestone` no longer reports success for a milestone that does not
  exist. Forgejo answers that route with HTTP 200 either way, so the milestone
  is resolved first and a missing one is `MILESTONE_NOT_FOUND`.
- `delete_column`, `edit_column` and `set_default_column` all report an unknown
  column id as `COLUMN_NOT_FOUND` instead of an upstream HTTP 500. On delete
  that 500 was also blamed on the default column, which sent callers off to
  change a default unrelated to the failure.
- Every request now carries an explicit timeout, tunable with
  `FORGEJO_MCP_TIMEOUT` (default 30 seconds). The bound was previously
  Playwright's own implicit 30-second default, so an instance that accepted
  connections without answering looked like a hang and could not be made to
  fail sooner.
- Blank and whitespace-only titles are refused as `INVALID_INPUT` for projects,
  columns and milestones. Forgejo accepts an empty project or column title and
  creates a resource its own list pages then omit, leaving no way to find it
  again.

## [0.1.0rc3] - 2026-09-05

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
