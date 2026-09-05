# Project Guidelines

## Scope and tooling

- This is a Python 3.14+ project with a `src/` layout. Use `uv` for dependency
  management and for running project commands.
- Keep changes focused on the requested behavior. Preserve unrelated work in the
  working tree and avoid unnecessary refactors.
- Follow the existing style and public interfaces. Prefer small, typed functions
  and add or update tests alongside behavior changes.

## Forgejo integration

- Forgejo Projects are accessed through undocumented internal web routes. Treat
  route methods, paths, payloads, parsed HTML, and response handling as contracts.
- When changing a route or parser, update its offline contract tests. Clearly
  document behavior that requires live verification against a throwaway Forgejo
  repository; never run destructive live tests without explicit authorization.
- Routes, HTML patterns, form values and the CSRF strategy live in
  `src/forgejo_projects_mcp/compat.py`, not inline in `client.py`. When Forgejo
  changes one of them in a specific release, add a documented `Quirk` scoped to
  the affected versions rather than branching on the version in the client, and
  record the difference in `docs/forgejo-projects-automation-reference.md`.
- The integration suite is self-contained and safe to run locally: it starts
  throwaway containers itself. After touching a route or parser, run it against
  the oldest and newest majors the client claims to handle:

  ```bash
  uv run pytest -m integration --forgejo-version 1.20 --forgejo-version 16
  ```
- CI runs the suite against the releases still in support (14, 15, 16) only, and
  none of them has a quirk in force. Anything touching `compat.py` therefore has
  to be verified locally against the affected releases — a push will not do it
  for you.
- The offline and integration suites are paired: an offline test that *can* run
  against a real instance has a live counterpart in the matching
  `tests/integration/test_live_*.py`. When you add or change an offline test,
  add or update its live counterpart too, unless the behavior genuinely cannot
  be provoked on a real instance (a 429 response, an unreadable version string)
  — say so in the test module's docstring when it cannot.
- Prefer a live assertion that the fixture still matches reality over a richer
  fixture. A hand-written fixture and the parser that reads it can agree with
  each other long after both have stopped agreeing with Forgejo.
- Never commit credentials, session state, `.env` contents, or sensitive values in
  logs, fixtures, examples, or documentation.

## Required follow-up for code changes

- Update the relevant user, CLI, configuration, architecture, tool, or automation
  documentation whenever code behavior changes.
- Add a concise user-facing entry under `## [Unreleased]` in `CHANGELOG.md`, using
  the appropriate Keep a Changelog subsection. Do not create a release section
  unless explicitly preparing a release.
- Run all required checks after code edits and resolve failures before finishing:

  ```bash
  uv run pytest -q
  uv run ruff check .
  uv run ty check
  uv run mkdocs build --strict
  ```

- If a required check cannot run, report exactly which command was skipped or
  failed and why.

## Documentation and release metadata

- Keep examples and command output consistent with the current CLI and MCP tool
  schemas.
- Keep release instructions aligned with `.github/workflows/` and the tag-derived
  versioning configured in `pyproject.toml`.
- Preserve the changelog format because release automation extracts notes from
  version headings.
