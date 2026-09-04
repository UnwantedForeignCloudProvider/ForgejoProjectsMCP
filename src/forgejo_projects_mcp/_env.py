"""Load a local ``.env`` before any other module reads the environment.

Imported first from ``__init__`` for its side effect. Real environment variables
already set (and an MCP client's own ``env`` block) take precedence — dotenv does
not override them.
"""

from dotenv import find_dotenv, load_dotenv


def load_env() -> None:
    """Load a .env from the current working directory (or a parent), if present."""
    # usecwd=True searches from the process's CWD upward (not this module's dir).
    load_dotenv(find_dotenv(usecwd=True))


load_env()
