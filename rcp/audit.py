"""Append-only audit log: JSONL canonical, SQLite mirror derived.

The hash chain deliberately lives outside the database. If it lived in the same
file every other write touches, "append-only" would be an assertion; as a
separate JSONL file it is git-diffable, survives DB corruption, and can be
re-verified from scratch by anyone with the repo. `audit_mirror` in SQLite is a
derived index so viewer/export.py can query instead of re-parsing.

`make verify-audit` rebuilds the chain and exits non-zero on tamper.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from rcp.store import canonical_json, write_txn

GENESIS = "0" * 64


def _digest(prev_hash: str, seq: int, kind: str, ref_id: str | None,
            ts: int, body: dict[str, Any]) -> str:
    payload = canonical_json(
        {"seq": seq, "kind": kind, "ref_id": ref_id, "ts": ts, "body": body}
    )
    return hashlib.sha256(f"{prev_hash}{payload}".encode()).hexdigest()


class AuditLog:
    """Append-only writer over a JSONL file.

    Not thread-safe on its own; callers append from the decision path, which is
    already serialized behind store.WRITE_LOCK.
    """

    def __init__(self, path: Path | str, *, reset: bool = False) -> None:
        """`reset` truncates the log before writing.

        Only legitimate when the database the log describes is *also* being
        replaced -- which is exactly what `eval/run.py` does when it forks a
        fresh per-arm database. Without it the chain accumulates records from
        every previous run: still internally valid, and describing databases
        that no longer exist. Measured before this existed: 136,644 audit
        records against 1,049 rows in `decisions`, and a 51 MB file for a run
        that produced 577 actions.

        Never pass this in production. There, a log outliving its database is
        the point.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if reset and self.path.exists():
            self.path.unlink()
        self._seq, self._prev = self._resume()

    def _resume(self) -> tuple[int, str]:
        """Pick up where a previous run left off, without loading the file."""
        if not self.path.exists():
            return 0, GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        if last is None:
            return 0, GENESIS
        rec = json.loads(last)
        return rec["seq"] + 1, rec["hash"]

    def append(
        self,
        kind: str,
        body: dict[str, Any],
        *,
        ts: int,
        ref_id: str | None = None,
    ) -> str:
        """Write one record, return its hash."""
        seq = self._seq
        h = _digest(self._prev, seq, kind, ref_id, ts, body)
        rec = {
            "seq": seq,
            "hash": h,
            "prev_hash": self._prev,
            "kind": kind,
            "ref_id": ref_id,
            "ts": ts,
            "body": body,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(canonical_json(rec) + "\n")
        self._seq, self._prev = seq + 1, h
        return h


def read_all(path: Path | str) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def verify(path: Path | str) -> tuple[bool, str]:
    """Recompute the chain from scratch.

    Returns (ok, message). A tampered body, a reordered line, or a deleted
    record all break the chain at the point of damage and are named in the
    message.
    """
    records = read_all(path)
    prev = GENESIS
    for i, rec in enumerate(records):
        if rec["seq"] != i:
            return False, f"seq mismatch at line {i}: expected {i}, found {rec['seq']}"
        if rec["prev_hash"] != prev:
            return False, f"chain break at seq {rec['seq']}: prev_hash does not match"
        expected = _digest(prev, rec["seq"], rec["kind"], rec["ref_id"],
                           rec["ts"], rec["body"])
        if expected != rec["hash"]:
            return False, f"tampered record at seq {rec['seq']}: hash does not match body"
        prev = rec["hash"]
    return True, f"chain intact: {len(records)} records"


def sync_mirror(conn: sqlite3.Connection, path: Path | str) -> int:
    """Refresh the SQLite mirror from the canonical JSONL.

    audit_mirror is append-only too, so this inserts only records the mirror has
    not seen. If the JSONL was rewritten rather than appended to, the conflicting
    insert fails loudly instead of quietly diverging.
    """
    records = read_all(path)
    if not records:
        return 0
    have = conn.execute("SELECT COALESCE(MAX(seq), -1) FROM audit_mirror").fetchone()[0]
    fresh = [r for r in records if r["seq"] > have]
    if not fresh:
        return 0
    with write_txn(conn):
        conn.executemany(
            "INSERT INTO audit_mirror (seq, hash, prev_hash, kind, ref_id, ts, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (r["seq"], r["hash"], r["prev_hash"], r["kind"],
                 r["ref_id"], r["ts"], canonical_json(r["body"]))
                for r in fresh
            ],
        )
    return len(fresh)


if __name__ == "__main__":  # `make verify-audit`
    import sys

    from rcp.store import audit_path

    seed = int(sys.argv[1]) if len(sys.argv) > 1 else 42
    target = audit_path(seed)
    if not target.exists():
        print(f"no audit log at {target} -- run `make data` first")
        raise SystemExit(1)
    ok, message = verify(target)
    print(f"{'OK  ' if ok else 'FAIL'} {target}: {message}")
    raise SystemExit(0 if ok else 1)
