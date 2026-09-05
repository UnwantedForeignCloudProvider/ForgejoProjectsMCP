---
name: release-forgejo-projects-mcp
description: Prepare, publish, or verify a tag-driven forgejo-projects-mcp release or prerelease. Use when asked to choose a version, run release checks, update release notes, publish to PyPI and GitHub, deploy versioned documentation, or diagnose a failed release workflow.
---

# Release procedure

Notes:

- This runbook is maintainer-facing and intentionally excluded from public docs.

- If you're a regular user, you can safely ignore this skill.

A step-by-step runbook for cutting a release of **forgejo-projects-mcp**.
Written to be followed by an AI agent, but equally usable by a human.

Releases are **tag-driven**: pushing a git tag `vX.Y.Z` triggers
`.github/workflows/publish.yml`, which builds the package, publishes it to PyPI
(OIDC trusted publishing), and creates a draft GitHub Release, attaches the built
artifacts, then publishes it. The draft-first order is required when GitHub
release immutability is enabled because published releases cannot accept assets.
The same tag triggers `.github/workflows/docs.yml`, which stores the new mike
version on `gh-pages` and deploys the complete version tree through GitHub's
native Pages artifact workflow.
The package version is derived from the tag by `uv-dynamic-versioning` — there is
**no version number to edit** in `pyproject.toml`.

---

## 0. Rules for AI agents (read first)

- **Publishing is irreversible.** A version uploaded to PyPI can never be
  replaced or re-uploaded, only *yanked*. Pushing a tag is the point of no return.
- **Never push a release tag without explicit human approval** for that specific
  version. Do all the preparation and verification, then stop and ask.
- **Never reuse or move a tag.** Each release is a new, higher version. Do not
  delete a tag, force-push a tag, or re-point an existing one.
- **Never edit the version by hand.** It comes from the tag. If a version looks
  wrong, the cause is the tag or the git state, not `pyproject.toml`.
- **Do not put secrets in the repo.** Publishing uses OIDC; there is no token to
  add.
- Treat every ❌ / "STOP" below as a hard gate: if it fails, do not proceed —
  report the failure instead.

---

## 1. One-time prerequisites (verify once, per project)

These must already be true before the *first* release. If unsure, confirm with a
maintainer rather than assuming.

- [ ] GitHub Pages is configured in repository Settings with **Source** set to
      **GitHub Actions**. The `gh-pages` branch is managed by mike as persistent
      version storage; it is not the configured Pages source.
- [ ] The `github-pages` environment permits deployments from release tags
      matching `v*` if deployment branch/tag protection rules are enabled.
- [ ] The project name **`forgejo-projects-mcp`** is registered (or available) on
      PyPI. Check: <https://pypi.org/project/forgejo-projects-mcp/>.
- [ ] A **PyPI Trusted Publisher** is configured for this repo:
      owner `UnwantedForeignCloudProvider`, repo `ForgejoProjectsMCP`,
      workflow `publish.yml`, environment `pypi`.
- [ ] A GitHub **Environment named `pypi`** exists in repo Settings
      (optionally with required reviewers to gate real publishes).

GitHub release immutability may remain enabled. The `github-release` job is
compatible with it: `softprops/action-gh-release` must retain `draft: true`, and
the final `gh release edit --draft=false` step must run only after the wheel and
source distribution have been uploaded. If asset upload fails, the draft stays
mutable and the failed job can be rerun safely.

Missing Pages configuration fails the Docs workflow without affecting package
publishing. Missing PyPI configuration fails the `pypi-publish` job; the
`github-release` job can still succeed.

The first prerelease publishes its direct version URL, but intentionally does
not create `latest` or a root redirect. Those appear with the first stable tag.

---

## 2. Pre-release checklist (every release)

Run from a clean checkout of the branch you intend to release (normally the
default branch). Use `uv` for everything.

### 2.1 Repository state

- [ ] On the intended branch and up to date with the remote:
      `git fetch --tags && git status`
- [ ] **Working tree is clean** — no uncommitted or untracked changes:
      `git status --porcelain` prints nothing. ❌ STOP if it prints anything.
- [ ] Full history + tags are present locally (needed for versioning):
      `git fetch --tags --unshallow 2>/dev/null; git tag --list | tail`

### 2.2 Quality gates (all must pass)

```bash
uv sync --dev
uv run ruff check .        # lint  — must be clean
uv run ty check            # types — must be clean
uv run pytest -q           # tests — all must pass
uv build                   # must produce dist/*.whl and dist/*.tar.gz
```

❌ STOP if any command fails. Do not release a red build.

> Note: the tests here are offline unit/contract tests. They do **not** exercise
> a live Forgejo instance. A green suite means the code and packaging are sound,
> not that the tool was re-verified against a real server.

### 2.3 Decide the version (SemVer)

Pick `X.Y.Z` based on what changed since the last tag (`git describe --tags`):

- **PATCH** (`x.y.Z+1`): bug fixes only, no API/behaviour changes.
- **MINOR** (`x.Y+1.0`): new backwards-compatible tools/arguments/features.
- **MAJOR** (`X+1.0.0`): breaking changes to tool names, arguments, or output.
- Pre-1.0 caveat: while the version is `0.y.z`, a breaking change bumps the
  **MINOR**, not the major.
- Pre-releases use PEP 440 spellings on the tag: `v1.0.0rc1`, `v1.0.0b1`,
  `v1.0.0a1`. The workflow marks these as GitHub *pre-releases* automatically.

### 2.4 Check for documentation drift (and fix it)

Docs must match the code being released. Verify each of these and **update the
docs (and commit) before releasing** if anything is stale:

- [ ] **Tool surface matches the docs.** The set of tools and their
      arguments/filters must be reflected in `README.md` (the *Tools* section),
      `.agents/skills/forgejo-projects-cli/SKILL.md`, and the CLI help. Compare
      the live list against the docs:

      ```bash
      uv run forgejo-projects-cli --help            # every subcommand + options
      uv run python -c "import asyncio, forgejo_projects_mcp as f; \
        print(sorted(t.name for t in asyncio.run(f.mcp.list_tools())))"
      ```

      Every tool listed here must appear in the README Tools section, and any new
      arguments/filters/return fields (e.g. `state`, `limit`/`offset`,
      `total`/`returned`/`error_count`) must be documented.
- [ ] **Environment variables match.** Every var the code reads must be in
      `.env.example` and the README Configuration section:

      ```bash
      grep -rho "os.environ[.a-zA-Z]*(\?\?\s*\"[A-Z_]*\"" src | grep -o '"[A-Z_]*"' | sort -u
      # compare against the keys documented in .env.example
      grep -o '^[A-Z_]*' .env.example | sort -u
      ```
- [ ] **README/SKILL examples still run** — command names, flags, and default
      values shown in examples match current behaviour (spot-check a couple).
- [ ] **Behavioural docs are current** — error signaling (`isError`, error
      codes), pagination fields, cost warnings, and the auth/`.env` notes still
      describe what the code does.
- [ ] Nothing references removed/renamed tools, flags, or endpoints.
- [ ] **Versioned documentation matches the release.** Build from the exact
      release commit with `uv run mkdocs build --strict`. After the tag's Docs
      workflow completes, open the published site and verify that:
      - the version selector contains `X.Y.Z`;
      - the `/X.Y.Z/` pages contain the release's documentation;
      - `/latest/` points to the newest stable release;
      - a prerelease tag did **not** move `/latest/`; and
      - at least one previously published version URL still works.

If you changed docs, include them in the release commit (below).

### 2.5 Update the changelog

- [ ] In `CHANGELOG.md`, move the items under `## [Unreleased]` into a new
      section headed **exactly**:

      ## [X.Y.Z] - YYYY-MM-DD

  Use today's UTC date: `date -u +%Y-%m-%d`.
  The release workflow extracts the notes for tag `vX.Y.Z` from the matching
  `## [X.Y.Z]` heading, so the heading format must be exact.
- [ ] Leave a fresh, empty `## [Unreleased]` section above it.
- [ ] Commit the changelog **and any doc updates from 2.4**:
      `git add CHANGELOG.md README.md .env.example .agents docs && git commit -m "chore: release X.Y.Z"`
      (drop paths you didn't change).

### 2.6 Preview the version that will be built

The tag is not created yet, so this previews the *next-commit* version; the real
check happens in CI after tagging. Confirm the base looks right:

```bash
uvx uv-dynamic-versioning     # e.g. shows the current dev version
```

---

## 3. Cut the release (requires human approval)

STOP here and get explicit approval to release version `X.Y.Z`. Only after a
clear "yes":

```bash
git tag vX.Y.Z            # annotated is fine: git tag -a vX.Y.Z -m "vX.Y.Z"
git push origin vX.Y.Z
```

The tag **must** sit on the commit that contains the finalized changelog and the
code you validated in step 2. The CI guard in `publish.yml` fails the build if the
computed version does not equal the tag, so a mismatch is caught, not published.

Pushing the `v*` tag starts `publish.yml`. Do **not** also push tags with
`git push --tags` unless that is exactly what you intend (it would push *every*
local tag).

---

## 4. Post-release verification

- [ ] The **Publish** workflow run for the tag succeeds (all three jobs:
      `build`, `pypi-publish`, `github-release`).
- [ ] PyPI shows the new version: <https://pypi.org/project/forgejo-projects-mcp/>.
- [ ] The GitHub **Release** for `vX.Y.Z` exists, has the changelog notes, and has
      both `*.whl` and `*.tar.gz` attached. If repository release immutability is
      enabled, the release is immutable only after those files are present.
- [ ] A clean install works:

      uvx --from forgejo-projects-mcp==X.Y.Z forgejo-projects-mcp --help
      # or: uv tool install forgejo-projects-mcp==X.Y.Z

- [ ] `python -c "import importlib.metadata as m; print(m.version('forgejo-projects-mcp'))"`
      reports `X.Y.Z` in an environment where it is installed.

---

## 5. If something goes wrong

- **CI version-guard failed** ("Built version … does not match tag …"): the tag
  is not on the right commit or history/tags were shallow. Fix by deleting the
  bad tag **only if it never published anything**, then re-tag the correct commit:
  `git tag -d vX.Y.Z && git push origin :refs/tags/vX.Y.Z` — but confirm with a
  maintainer first, and never do this for a tag that already published to PyPI.
- **`pypi-publish` failed** (trusted publisher / environment missing): fix the
  one-time prerequisites in §1, then release a **new** patch version. You cannot
  re-run a publish for a version that partially uploaded.
- **A bad version reached PyPI**: it cannot be overwritten. **Yank** it on PyPI
  (hides it from new installs without breaking pins) and release a fixed
  **higher** version. Do this only when instructed.
- **GitHub Release asset upload failed because the release was already
  immutable**: a published immutable release cannot accept the missing files,
  even if repository immutability is later disabled. Do not delete the release,
  delete or move the tag, or reuse its version. Keep the published release as-is,
  preserve the workflow's draft-first sequence, and release a higher version.
- **TestPyPI dry run**: to rehearse without touching real PyPI, uncomment the
  `repository-url` line in `publish.yml`'s publish step and configure a matching
  trusted publisher on test.pypi.org.

---

## Quick reference

```bash
# validate
uv sync --dev && uv run ruff check . && uv run ty check && uv run pytest -q && uv build
# changelog: move Unreleased -> "## [X.Y.Z] - $(date -u +%Y-%m-%d)", commit
# release (after approval)
git tag vX.Y.Z && git push origin vX.Y.Z
```
