"""Load `.env` into the process environment.

The repo shipped `.env.example` while nothing actually read a `.env` file, so
copying it and filling in a key did nothing and the failure looked like a bad
key. This closes that gap.

Deliberately hand-rolled rather than pulling in python-dotenv: it is twenty
lines, and the project's only hard dependencies are pydantic, pyyaml and pytest.

**A real environment variable always wins over the file.** That ordering is what
makes `RCP_LLM=groq make analyze` behave the way anyone would expect even when
`.env` says something else -- the explicit thing on the command line should not
be silently overridden by a file you edited last week.
"""

from __future__ import annotations

import os
from pathlib import Path

from rcp.store import REPO_ROOT

ENV_PATH = REPO_ROOT / ".env"


def load_dotenv(path: Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Read KEY=VALUE lines into os.environ. Returns what it set.

    Missing file is fine -- everything in this project runs without one.
    """
    path = Path(path or ENV_PATH)
    if not path.exists():
        return {}

    applied: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not key or (not value and key not in os.environ):
            # An empty value in .env.example means "not configured", not
            # "configure this to empty string".
            continue
        if key in os.environ and not override:
            continue
        os.environ[key] = value
        applied[key] = value
    return applied
