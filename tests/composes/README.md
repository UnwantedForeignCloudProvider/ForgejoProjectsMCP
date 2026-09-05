# Integration test instances

`docker-compose.yaml` here is a single parameterized throwaway Forgejo stack.
`FORGEJO_VERSION` picks the image and `FORGEJO_PORT` the host port, and both are
baked into the compose project name, so several versions can run side by side.

**You normally do not run it yourself.** The integration suite starts and stops
it for you:

```bash
uv run pytest -m integration --forgejo-version 16
uv run pytest -m integration --forgejo-version 1.20 --forgejo-version 16
FORGEJO_TEST_VERSIONS=1.20,10,13,16 uv run pytest -m integration
```

Each requested version gets its own container on its own port — `3300 + major`
for 7 and up, and `3300 + minor` for the 1.x line, so 16 lands on `3316` and
1.20 on `3320` — plus an admin user and a freshly seeded repository with issues
and a milestone. Containers the suite started are removed when the session ends;
pass `--forgejo-keep` to leave them running, and a container that was *already*
running is adopted and never torn down.

Nothing runs by default: with no version requested, every integration test
skips, so a plain `uv run pytest` stays offline.

## What the suite covers

The live tests mirror the offline ones file by file, so each area is checked
both against a fake transport and against a real instance:

| File | Live counterpart of |
|---|---|
| `test_live_auth.py` | the auth half of `tests/test_client.py`, plus `tests/test_env.py` |
| `test_live_client.py` | the operation half of `tests/test_client.py` |
| `test_live_projects.py` | the board lifecycles |
| `test_live_parsing.py` | `tests/test_parsing.py` |
| `test_live_tools.py` | `tests/test_tools.py` and `tests/test_smoke.py` |
| `test_live_cli.py` | `tests/test_cli.py`, plus real subprocess runs |
| `test_live_logging.py` | `tests/test_logging.py` |
| `test_live_session.py`, `test_live_compat.py` | `tests/test_versioning.py` and `tests/test_compat.py` |

`helpers.py` provides what a live test needs and a fake transport gives away for
free: `watch_requests` records the requests a client really issues (method, path
and headers, so CSRF behavior can be asserted), and the REST helpers create the
extra fixtures a few tests need — a comment, a closed issue — through Forgejo's
documented API rather than through the client under test.

## Driving the stack by hand

```bash
# Start and wait for the health check:
FORGEJO_VERSION=16 FORGEJO_PORT=3316 \
  docker compose -f tests/composes/docker-compose.yaml up -d --wait

# Create the admin the suite expects:
docker compose -f tests/composes/docker-compose.yaml exec -u git server \
  forgejo admin user create --admin --username testadmin \
  --password testadmin-password-123 --email testadmin@example.com \
  --must-change-password=false

# Point the suite at it (no container is touched in this mode):
FORGEJO_TEST_URL=http://localhost:3316 \
  FORGEJO_TEST_ALLOW_WRITES=1 uv run pytest -m integration

# Tear down:
FORGEJO_VERSION=16 FORGEJO_PORT=3316 \
  docker compose -f tests/composes/docker-compose.yaml down
```

There is deliberately no data volume: the instance is disposable, so its state
lives in the container's writable layer and disappears with `down`.

## Credentials

The suite creates and uses these. Override them together, or not at all.

| Variable | Default |
|---|---|
| `FORGEJO_TEST_USERNAME` | `testadmin` |
| `FORGEJO_TEST_PASSWORD` | `testadmin-password-123` |

These are throwaway credentials for disposable local instances only. Do not
reuse them anywhere real.

## Other settings

| Variable | Default | Meaning |
|---|---|---|
| `FORGEJO_TEST_VERSIONS` | — | Comma-separated versions, as an alternative to `--forgejo-version`. |
| `FORGEJO_TEST_URL` | — | Use an instance you manage instead of Docker. |
| `FORGEJO_TEST_ALLOW_WRITES` | — | Set to `1` to let the suite create and delete data on an instance it does not own. |
| `FORGEJO_TEST_PORT_BASE` | `3300` | Base for the per-version host port. |
| `FORGEJO_TEST_BOOT_TIMEOUT` | `180` | Seconds to wait for an instance to become healthy. |

## Safety

Every test that creates or deletes anything requires a **disposable** target: a
container the suite started, or an external instance explicitly opened up with
`FORGEJO_TEST_ALLOW_WRITES=1`. Without that, those tests skip rather than touch
a forge that might be real. Keep it that way when adding tests — take the
`writable` fixture (directly, or through `live_project`) in anything that writes.

## Plain HTTP on a non-default port

This is exercised on purpose: `FORGEJO_URL` and the CLI's `--forgejo-url` accept
any scheme and port, and `ROOT_URL` in the compose file is pinned to the mapped
host port so redirect-based flows resolve correctly.
