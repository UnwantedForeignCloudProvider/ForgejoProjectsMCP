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


# Probe every supported Forgejo version before implementing a fix

When a bug report or review claims that `forgejo-projects-mcp` mishandles some
Forgejo behavior, the maintainer wants the behavior measured on **every**
supported release *before* any code is written — not after, and not only on the
version where the problem was reported. Their stated reasoning is that this is
what lets the compat layer "apply the fixes where they were actually needed":
version scoping should follow from evidence, so a `Quirk` is added only where a
release genuinely differs, and behavior that turns out to be uniform goes into
the base `Profile` with no quirk at all.

The practical shape of this is a throwaway probe script rather than a test.
`tests/integration/harness.py` can be imported directly: `ForgejoInstance(v)`
plus `up()`, `wait_healthy()`, `ensure_admin()` and `seed_repository()` give a
working instance per version, and probes can then drive `ForgejoClient._request`
to record raw HTTP statuses and read results back independently. Several
versions can be probed in parallel, since each gets its own container and port,
and a container left running from a previous probe is adopted rather than
rebuilt. Point `CONFIG_DIR`/`STATE_FILE`/`CONFIG_FILE` at a per-version
directory so parallel probes do not share session state.

The same expectation of thoroughness extends to the documentation that follows
the fix: the maintainer asks for exhaustivity in
`docs/forgejo-projects-automation-reference.md`, meaning the measured contract
itself — which routes signal success how, which inputs the server silently
accepts, what each release does differently — belongs in that file, including
the differences that were found and turned out **not** to matter. Recording that
a behavior was probed across all releases and proved uniform is worth as much as
recording a difference, because it tells the next reader the question was
already asked and answered.


# Releases must be verified against real Forgejo instances

When cutting a release of `forgejo-projects-mcp`, running the offline gates
(`ruff`, `ty`, `pytest -q`, `uv build`, `mkdocs build --strict`) is not enough.
The maintainer requires the dockerized integration suite to be run as part of
release preparation as well. Noting the gap as a caveat and asking for approval
anyway is not acceptable — run the tests.

The command starts and tears down throwaway containers itself, and takes roughly
eleven minutes, so run it in the background rather than under a short timeout:

```bash
uv run pytest -m integration \
  --forgejo-version 1.20 --forgejo-version 13 --forgejo-version 14 \
  --forgejo-version 15 --forgejo-version 16
```

All five versions are required, for two distinct reasons. **1.20 and 13** cover
the version-specific behavior in `src/forgejo_projects_mcp/compat.py`: 1.20
matches all three quirks and 13 only the CSRF one. **14, 15 and 16** are the
releases still in support, so they are what people actually run, and the
maintainer expects a release to verify them directly rather than inferring they
are fine because CI's matrix covers them — CI runs a branch, not the tagged
commit. Do not narrow the set to just the quirk-covering versions; that omission
was corrected explicitly.

Two traps to remember: a plain `uv run pytest -q` silently *skips* every
integration test when no `--forgejo-version` is passed, so a green offline run
says nothing about live behavior; and pointing `FORGEJO_TEST_URL` at an existing
instance is not a substitute, because such an instance is treated as
non-disposable and its write tests skip unless `FORGEJO_TEST_ALLOW_WRITES=1`.

Expect four intentional skips over the five versions, all of them the
CSRF-rejection-recovery test in `tests/integration/test_live_compat.py`, which
can only run in the `[9.0, 14.0)` window: it skips on 1.20 (below 9.0, where a
token-less write is dropped silently with a 303 rather than rejected) and on 14,
15 and 16 (an `Origin` header is accepted, so nothing rejects). Only 13 runs it.
A green sweep is `583 passed, 4 skipped` and takes about ten and a half minutes.

This requirement is written into
`.agents/skills/release-forgejo-projects-mcp/SKILL.md` as section 2.3, but the
expectation stands regardless of what any particular version of that file says.