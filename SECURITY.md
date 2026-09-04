# Security Policy

Thanks for helping keep **forgejo-projects-mcp** and its users safe.

Please read this before reporting. A focused, well-scoped report is triaged far
faster than a vague one.

## Supported versions

Only the **latest released version** (and current `main`) is supported. Before
reporting, update and — if installed as a tool — reinstall from current source
(`uv tool install . --reinstall`); some issues are stale builds running old code.

## Reporting a vulnerability

**Open a public issue.**
**Yes, as weird as it sounds, report that way.**

## Scope — please keep this project's nature in mind

This tool automates Forgejo's **undocumented internal web UI routes** using a
**username/password login** and a cached session cookie (see the README
warning). Some properties are known and by design, not vulnerabilities:

- It authenticates as a **real user** (no scoped API token — Forgejo has no
  Projects API), so it inherits that user's permissions.
- It performs writes with **no transactional guarantees**.
- It stores an authenticated session at
  `<config>/forgejo_projects_mcp/storage_state.json`.

**In scope** (please report):

- Credentials or session data being **logged, printed, or leaked** (e.g. into
  stdout, error messages, tool output, or the session file with unsafe perms).
- Sending credentials/requests to a host **other than** the configured
  `FORGEJO_URL`.
- Injection or unsafe handling of server-returned content (parsed HTML) that
  leads to code execution or data exposure on the client.
- A dependency with a known, exploitable vulnerability as used here.
- Anything that lets a **malicious Forgejo instance/response** compromise the
  machine running the tool.

**Out of scope** (not vulnerabilities here):

- The tool acting with the logged-in user's own permissions.
- Needing a password instead of a token (no Projects API exists).
- Rate-limiting/DoS of *your own* Forgejo instance from heavy bulk reads
  (that's what `FORGEJO_MCP_MAX_CONCURRENCY` / `FORGEJO_MCP_RPS` are for).
- Findings that require an already-compromised machine or shell.

## What makes a high-quality report

Include all of these — reports missing the essentials may be sent back for them:

- **Summary** — one sentence: the vulnerability and its impact.
- **Severity / impact** — what an attacker gains (data leak, code execution,
  privilege escalation, …) and the **preconditions** required.
- **Affected component** — which tool, function, or file, and the version or git
  commit you tested.
- **Environment** — how it's run (MCP server vs `forgejo-projects-cli`), OS,
  Python version, and the **Forgejo version** of the instance if relevant (this
  tool targets the internal web UI, which changes across releases).
- **Reproduction** — minimal, ordered steps to trigger it, ideally against a
  **throwaway repo/instance**. State whether it's reliable or intermittent.
- **Evidence** — the relevant output or logs (`FORGEJO_MCP_LOG_LEVEL=DEBUG`) that
  demonstrate the issue — with all secrets **redacted** (see below).
- **Suggested fix** — optional, but welcome.

### Never include real secrets

When demonstrating a leak, **redact** the actual values. Do **not** paste real
passwords, tokens, cookies, `storage_state.json` contents, or private instance
hostnames. A short, sanitized proof (e.g. "field `X` appears in stdout as
`FORGEJO_PASS…`") is enough — describe *where* a secret appears, not the secret.

## Conduct

Be precise and act in good faith. Do not test against instances or data you
don't own or have permission to use. No extortion or "beg bounties" — there is
no bug-bounty program.
