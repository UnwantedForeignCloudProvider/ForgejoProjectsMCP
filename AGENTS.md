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
