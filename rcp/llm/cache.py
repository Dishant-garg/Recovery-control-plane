"""Disk cache for LLM calls, committed to the repository.

Two reasons this is committed rather than gitignored:

1. **Cost.** The corpora here are small and bounded -- ~200 decline strings,
   ~40 message segments. Each unique input costs one call for the life of the
   project, not one per run.
2. **Reproducibility.** A reviewer with no API key runs `make eval` and gets the
   same numbers, because every call the pipeline needs is already answered on
   disk. That is the claim ADR-002 rests on.

The key includes the model and the tool schema, so changing either produces a
miss rather than silently reusing an answer produced under different conditions.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from rcp.store import REPO_ROOT, canonical_json

CACHE_DIR = REPO_ROOT / "data" / "llm_cache"


def cache_key(**parts: Any) -> str:
    return hashlib.sha256(canonical_json(parts).encode()).hexdigest()


class DiskCache:
    """One JSON file per entry. Sharded two levels so the directory stays
    browsable and git does not choke on a single flat folder."""

    def __init__(self, root: Path | None = None, *, enabled: bool = True) -> None:
        self.root = Path(root or CACHE_DIR)
        self.enabled = enabled
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _path(self, key: str) -> Path:
        return self.root / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # A corrupt entry is a miss, not a crash. Better to spend one call
            # than to fail a run over a half-written file.
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, key: str, value: dict[str, Any]) -> None:
        if not self.enabled:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a crash mid-write leaves the old entry, not a
        # truncated one that would poison every later run.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
        tmp.replace(path)
        self.writes += 1

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict[str, Any]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes,
                "hit_rate": round(self.hit_rate, 4)}
