# Contributing

Thanks for your interest in **forgejo-projects-mcp**.

## Please do not open pull requests

This project **does not accept pull requests.** Any PR — however good — will be
closed unmerged. Please don't spend your time preparing one.

Why: this is a small, single-maintainer, best-effort tool built on Forgejo's
**undocumented internal web routes** (see the warning in the README). Changes
need to be validated by hand against a live instance, and the maintainer keeps
tight control over that surface. Code contributions are more work to review and
verify than they're worth here.

What *is* very welcome: **clear bug reports and well-argued suggestions** as
issues. A good report is often more valuable than a patch.

## How to help

Open an **issue**. Two kinds are useful:

1. **Bug reports** — something behaves incorrectly.
2. **Suggestions / feature ideas** — a change worth considering.

Before opening one:

- **Search existing issues** first to avoid duplicates; add to the existing
  thread instead of opening a new one.
- Confirm you're on the **latest version** and, if installed as a tool,
  reinstall from the current source (`uv tool install . --reinstall`) — several
  "bugs" are stale builds running old code.
- Keep each issue to **one problem or one idea.**

## What makes a high-quality bug report

Include all of the following. Reports missing the essentials may be closed asking
for them.

- **Summary** — one sentence: what's wrong.
- **Environment**
  - `forgejo-projects-mcp` version (or git commit) and how it's run (MCP server
    or `forgejo-projects-cli`), and the MCP client if relevant.
  - **Forgejo version** of your instance (this matters a lot — the tool targets
    the internal web UI, which changes between releases). Find it in Forgejo's
    footer or Site Administration.
  - OS and Python version (`python --version`), and `uv --version`.
- **Exact command or tool call** you ran, with arguments. Redact secrets, but
  keep the structure (owner/repo/ids).
- **What you expected** to happen.
- **What actually happened** — the full **JSON result** or error, and the
  relevant **stderr logs**. Re-run with `FORGEJO_MCP_LOG_LEVEL=DEBUG` and include
  the output.
- **Reproduction steps** — a minimal, ordered sequence that reliably triggers it,
  ideally against a **throwaway repo**. State whether it happens every time or
  intermittently.
- **Scope** — does it affect one tool or several? Does it depend on state
  (open/closed), filters, pagination, board layout, or size?

> Never paste credentials, tokens, cookies, private URLs, or the contents of
> `storage_state.json`. Redact instance hostnames if sensitive.

### Handy: capture context

```bash
# versions
forgejo-projects-cli --help >/dev/null && echo "cli present"
python --version; uv --version

# reproduce with debug logs (stderr) alongside the JSON result (stdout)
FORGEJO_MCP_LOG_LEVEL=DEBUG forgejo-projects-cli <tool> <args...>
```

## What makes a high-quality suggestion

- **Problem first** — describe the underlying need or pain, not just a proposed
  solution. "I want to do X because Y" beats "add flag --z".
- **Concrete example** — the exact call you'd make and the result you'd want.
- **Scope and impact** — which tools/areas it touches; is it additive or a
  breaking change to existing output/arguments?
- **Alternatives** — anything you tried or considered, and why it fell short.
- **Fit** — remember the project deliberately stays a thin, best-effort wrapper
  over Forgejo's internal routes. Suggestions that keep that surface small and
  predictable are the most likely to land. Anything requiring an official
  Forgejo API is out of scope (there isn't one for Projects — that's the whole
  reason this exists).

## Security issues

Do **not** open a public issue for a security problem. Contact the maintainer
privately instead.

## Conduct

Be concise, be kind, assume good faith. Low-effort, hostile, or AI-slop reports
may be closed without comment.
