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

The tests are offline unit and contract tests. They check parsing, route methods, MCP schemas, error boundaries, and CLI dispatch; they do not contact a live Forgejo instance.

## After installation

Configure the required environment variables and run the smoke test described in [Getting started](getting-started.md):

```bash
forgejo-projects-cli forgejo_status
```

Then register `forgejo-projects-mcp` as a local stdio MCP server. See [Usage](usage.md#registering-the-mcp-server).

