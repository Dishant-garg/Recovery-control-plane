"""YAML config loading.

Configs are cached by path so repeated reads inside one run are free and, more
importantly, cannot observe a file changing mid-run -- a decision pass must see
one consistent policy.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@lru_cache(maxsize=None)
def load(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml. Cached for the life of the process."""
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"missing config: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def policy_version(name: str = "policy") -> str:
    """Stamped onto every decision so a replay can be tied to the rules that
    produced it."""
    cfg = load(name)
    return str(cfg.get("version", "unversioned"))
