"""SQLite connection layer.

Everything that makes SQLite behave well for this project lives here. None of
the pragmas below are defaults, and two of them are load-bearing:

  - `isolation_level=None` disables Python's implicit BEGIN/COMMIT magic, so
    transaction boundaries are written explicitly and are legible.
  - `BEGIN IMMEDIATE` (see `write_txn`) takes the write lock up front.

Measured, 8 threads racing a cap of 3 on the same file (see
tests/test_contact_cap_invariant.py):

    DEFERRED   final=1  committed=1  aborted=7   ("database is locked")
    IMMEDIATE  final=3  committed=8  aborted=0

Note what DEFERRED does and does not do here. Under WAL it does not overshoot
the cap -- snapshot isolation prevents that. It fails the other way: a
transaction that read a snapshot and then tries to write gets SQLITE_BUSY_SNAPSHOT,
and `busy_timeout` cannot help, because waiting would not make a stale snapshot
fresh. The write is simply lost. In this domain that means recoverable payments
that were within budget never get contacted -- a false suppression, which is
one of the numbers eval/metrics.py reports.

IMMEDIATE takes the write lock before reading, so `busy_timeout` CAN wait on it,
and every caller gets a correct answer instead of a dropped one.

`foreign_keys` and `busy_timeout` are per-connection and are NOT stored in the
file -- every connection must run the pragma block. Only WAL persists.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from rcp.migrations import migrate
from rcp.schema import ActionStatus, Suppressed

T = TypeVar("T", bound=BaseModel)

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"

# FastAPI's webhook handler and the outbox relay can both write. SQLite allows
# exactly one writer; this lock keeps them from thrashing on busy_timeout.
WRITE_LOCK = threading.Lock()

_PRAGMAS = """
PRAGMA journal_mode = WAL;         -- persists in the file; set once
PRAGMA synchronous  = NORMAL;      -- safe under WAL, much faster writes
PRAGMA foreign_keys = ON;          -- OFF by default, and PER-CONNECTION
PRAGMA busy_timeout = 5000;        -- removes 'database is locked' outright
PRAGMA temp_store   = MEMORY;
PRAGMA cache_size   = -65536;      -- 64 MB (negative value means KiB)
PRAGMA mmap_size    = 268435456;   -- 256 MB
"""


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def rcp_db_path(seed: int = 42) -> Path:
    """The only database rcp/ is allowed to open.

    Ground truth (outcome model parameters, counterfactuals, latent per-customer
    propensities) lives in a sibling truth.db reachable only through
    sim/truth_store.py. Two filesystem paths do more for that guarantee than
    any amount of discipline -- see tests/test_ground_truth_isolation.py.
    """
    return DATA_DIR / f"seed_{seed}" / "rcp.db"


def audit_path(seed: int = 42) -> Path:
    return DATA_DIR / f"seed_{seed}" / "audit.jsonl"


# --------------------------------------------------------------------------
# connections
# --------------------------------------------------------------------------

def connect(path: Path | str, *, read_only: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    uri = f"file:{path}?mode={'ro' if read_only else 'rwc'}"
    conn = sqlite3.connect(
        uri,
        uri=True,
        isolation_level=None,     # we write our own BEGIN
        check_same_thread=False,  # guarded by WRITE_LOCK for writes
    )
    conn.row_factory = sqlite3.Row
    conn.executescript(_PRAGMAS)
    if read_only:
        conn.execute("PRAGMA query_only = ON")  # hard guard for viewer/export
    return conn


def open_rcp(seed: int = 42, *, read_only: bool = False) -> sqlite3.Connection:
    conn = connect(rcp_db_path(seed), read_only=read_only)
    if not read_only:
        migrate(conn)
    return conn


def close(conn: sqlite3.Connection) -> None:
    """Always close through here -- PRAGMA optimize keeps the query planner's
    statistics fresh, which matters once the 100k stress run has skewed them."""
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.Error:
        pass
    conn.close()


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialized write transaction. See the module docstring on why IMMEDIATE."""
    with WRITE_LOCK:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")


# --------------------------------------------------------------------------
# deterministic identity
# --------------------------------------------------------------------------

def canonical_json(obj: Any) -> str:
    """Byte-stable JSON. Used for both id derivation and the audit hash chain,
    so the two can never disagree about what a record 'is'."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_id(prefix: str, *parts: Any) -> str:
    """A content-addressed id.

    Deliberately not a random ULID: a random id would make two runs of the same
    seed produce different bytes, which breaks the reproducibility claim that
    the whole eval rests on.
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return f"{prefix}_{digest[:16]}"


# --------------------------------------------------------------------------
# row mapping -- the one place SQL meets schema.py
# --------------------------------------------------------------------------

def row_to(model: type[T], row: sqlite3.Row | None) -> T | None:
    return None if row is None else model.model_validate(dict(row))


def rows_to(model: type[T], rows: Iterable[sqlite3.Row]) -> list[T]:
    return [model.model_validate(dict(r)) for r in rows]


# --------------------------------------------------------------------------
# invariants that belong to the storage layer
# --------------------------------------------------------------------------

def insert_action_once(conn: sqlite3.Connection, action: dict[str, Any]) -> str | None:
    """Insert an outbox row, or return None if this idempotency_key already ran.

    Exactly-once is a UNIQUE constraint here, not a code path -- which is why
    tests/test_idempotency.py exercises the database rather than the caller.

    Must be called inside an open transaction.
    """
    row = conn.execute(
        """
        INSERT INTO actions (id, decision_id, customer_id, idempotency_key,
                             channel, status, scheduled_at, sent_at, attempts,
                             provider_ref, body, created_at)
        VALUES (:id, :decision_id, :customer_id, :idempotency_key,
                :channel, :status, :scheduled_at, :sent_at, :attempts,
                :provider_ref, :body, :created_at)
        ON CONFLICT(idempotency_key) DO NOTHING
        RETURNING id
        """,
        action,
    ).fetchone()
    return None if row is None else row["id"]


def contact_count(
    conn: sqlite3.Connection, customer_id: str, window_start_ms: int
) -> int:
    """Contacts that count against the cap: anything sent or still queued."""
    return conn.execute(
        """
        SELECT count(*) FROM actions
        WHERE customer_id = ?
          AND scheduled_at >= ?
          AND status IN ('pending', 'sent')
        """,
        (customer_id, window_start_ms),
    ).fetchone()[0]


def reserve_contact(
    conn: sqlite3.Connection,
    action: dict[str, Any],
    *,
    window_start_ms: int,
    cap: int,
) -> str | Suppressed:
    """Check the contact cap and claim a slot atomically.

    The read and the write have to sit inside one IMMEDIATE transaction or the
    cap is advisory. Returns the action id, or a Suppressed carrying the
    observed numbers so the audit line explains itself.
    """
    with write_txn(conn):
        observed = contact_count(conn, action["customer_id"], window_start_ms)
        if observed >= cap:
            return Suppressed(
                reason="contact_cap_reached", observed=observed, cap=cap
            )
        action_id = insert_action_once(conn, action)
        if action_id is None:
            return Suppressed(
                reason="duplicate_idempotency_key",
                detail=action["idempotency_key"],
            )
        return action_id


def claim_pending(
    conn: sqlite3.Connection, *, now_ms: int, limit: int = 50
) -> list[sqlite3.Row]:
    """Outbox poll. Total order on (scheduled_at, id) -- SQLite gives no
    guarantee on ties, and an unstable order here would make replay
    non-deterministic."""
    return conn.execute(
        """
        SELECT * FROM actions
        WHERE status = 'pending' AND scheduled_at <= ?
        ORDER BY scheduled_at ASC, id ASC
        LIMIT ?
        """,
        (now_ms, limit),
    ).fetchall()


def mark_action(
    conn: sqlite3.Connection,
    action_id: str,
    *,
    status: ActionStatus | str,
    sent_at: int | None = None,
    provider_ref: str | None = None,
) -> None:
    """The only mutation allowed on actions. A trigger enforces that the other
    columns are immutable, so a careless UPDATE elsewhere fails loudly."""
    status = status.value if isinstance(status, ActionStatus) else status
    with write_txn(conn):
        conn.execute(
            """
            UPDATE actions
               SET status       = ?,
                   sent_at      = COALESCE(?, sent_at),
                   provider_ref = COALESCE(?, provider_ref),
                   attempts     = attempts + 1
             WHERE id = ?
            """,
            (status, sent_at, provider_ref, action_id),
        )
