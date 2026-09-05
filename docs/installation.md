# Installation

## Requirements

- Python **3.14 or newer**;
- [uv](https://docs.astral.sh/uv/getting-started/installation/); and
- access to a Forgejo instance and an account that can manage the target repository.

No browser installation is required. The dependency on Playwright is used for its asynchronous HTTP request context, not for launching Chromium, Firefox, or WebKit.

## Install the latest release from PyPI

```bash
uv tool install forgejo-projects-mcp
```

Verify the installation:

```bash
forgejo-projects-mcp --help
forgejo-projects-cli --help
uv tool list
```

Upgrade later with:

```bash
uv tool upgrade forgejo-projects-mcp
```

Remove it with:

```bash
uv tool uninstall forgejo-projects-mcp
```

uv installs the executables into its tool bin directory. If it reports that this directory is not on PATH, run `uv tool update-shell` and restart the shell.

## Install the latest source branch

To test code newer than the latest release:

```bash
uv tool install git+https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP
```

Update the source installation by pulling the current branch again:

```bash
uv tool upgrade forgejo-projects-mcp
```

This path tracks the repository's current main branch and is not guaranteed to be as stable as a PyPI release.

## Install from a local checkout

Clone and install a specific checkout:

```bash
git clone https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP
cd ForgejoProjectsMCP
uv tool install .
```

To make local source edits immediately visible to the installed command, use an editable installation:

```bash
uv tool install --editable .
```

After changing the checkout or switching commits, reinstall the non-editable tool with:

```bash
uv tool install . --force
```

## Development environment

From a checkout, install the locked development dependencies:

```bash
uv sync --dev
```

Run the local entry points through uv:

```bash
uv run forgejo-projects-mcp --help
uv run forgejo-projects-cli --help
```

Run the repository's quality checks:

```bash
uv run ruff check .
uv run ty check
uv run pytest -q
uv build
```

The default tests are offline unit and contract tests. They check parsing, route methods, MCP schemas, error boundaries, and CLI dispatch; they do not contact a live Forgejo instance.

## Integration testing

An opt-in integration suite exercises the real web routes against real Forgejo instances. It is fully automated: name a version and it starts a throwaway container for it, waits for the health check, creates an admin user, and seeds a repository with issues and a milestone before any test runs.

```bash
uv run pytest -m integration --forgejo-version 16
```

Repeat the option to check several releases; every test runs once per version, which is how version-specific behavior is verified instead of assumed:

```bash
uv run pytest -m integration --forgejo-version 1.20 --forgejo-version 16
FORGEJO_TEST_VERSIONS=1.20,10,13,16 uv run pytest -m integration
```

It needs Docker (with the Compose plugin) and pulls `codeberg.org/forgejo/forgejo:<version>`. Each version gets its own container and port, so several can run at once. Every published release from 1.20 to 16 is covered. Containers the suite started are removed at the end of the session; `--forgejo-keep` leaves them running for a faster edit-run loop, and an instance that is already running is reused rather than replaced.

Nothing runs by default: with no version requested, every integration test skips, so a plain `uv run pytest` stays offline.

The suite mirrors the offline one rather than sampling it: authentication and
session caching, every client operation and its error paths, the HTML parsers
run over pages Forgejo actually rendered, the MCP tool layer, the CLI (including
runs as a separate process through the installed console script), the debug
logging, and version detection with its per-release CSRF and markup differences.
The pairing is set out in [Architecture](architecture.md#testing-architecture).

To use an instance you manage yourself, set `FORGEJO_TEST_URL` and no container is touched. Such an instance is treated as **not** disposable — every test that creates or deletes anything skips unless `FORGEJO_TEST_ALLOW_WRITES=1` says otherwise — so the suite cannot damage a shared forge by accident.

Full details, including credentials and the remaining settings, are in [`tests/composes/README.md`](https://github.com/UnwantedForeignCloudProvider/ForgejoProjectsMCP/blob/main/tests/composes/README.md).

## After installation

Configure the required environment variables and run the smoke test described in [Getting started](getting-started.md):

```bash
forgejo-projects-cli forgejo_status
```

Then register `forgejo-projects-mcp` as a local stdio MCP server. See [Usage](usage.md#registering-the-mcp-server).

