"""Transactional outbox relay.

The invariant this file exists to protect:

    BEGIN IMMEDIATE
      INSERT INTO decisions ...
      INSERT INTO actions (status='pending', idempotency_key=...)
    COMMIT
    -- transaction is closed BEFORE any network call happens

    relay: poll idx_actions_pending -> call executor -> mark status

No network call ever happens inside a transaction. Holding SQLite's single
write lock open across an HTTP round-trip to Razorpay would block every other
writer for the duration of that call, and a timeout would roll back a decision
that may already have been delivered.

Crash safety falls out of that ordering:
  - crash after COMMIT, before the call  -> row is still 'pending', relay retries
  - crash after the call, before marking -> relay retries, and the UNIQUE
    constraint on actions.idempotency_key means the provider is asked to do the
    same thing under the same key rather than a second, new thing

See ADR-003.
"""

from __future__ import annotations

import sqlite3

from rcp.execute.port import ExecResult, Executor
from rcp.schema import ActionStatus
from rcp.store import claim_pending, mark_action

MAX_ATTEMPTS = 3


def relay_once(
    conn: sqlite3.Connection,
    executor: Executor,
    *,
    now_ms: int,
    limit: int = 50,
) -> dict[str, int]:
    """Drain up to `limit` pending actions. Returns a tally for the audit line."""
    tally = {"sent": 0, "failed": 0, "retry": 0}

    # Snapshot the batch first: the transaction that read it is already closed
    # by the time we start making calls.
    for row in claim_pending(conn, now_ms=now_ms, limit=limit):
        action = dict(row)

        try:
            result = executor.execute(action)
        except Exception as exc:  # an executor must never take the relay down
            result = ExecResult(ok=False, error=f"network: {type(exc).__name__}: {exc}")

        if result.ok:
            mark_action(
                conn,
                action["id"],
                status=ActionStatus.SENT,
                sent_at=now_ms,
                provider_ref=result.provider_ref,
            )
            tally["sent"] += 1
            continue

        exhausted = action["attempts"] + 1 >= MAX_ATTEMPTS
        if result.retryable and not exhausted:
            # Leave it pending; the next poll picks it up. attempts increments
            # so the backoff ceiling is real.
            mark_action(conn, action["id"], status=ActionStatus.PENDING)
            tally["retry"] += 1
        else:
            mark_action(conn, action["id"], status=ActionStatus.FAILED)
            tally["failed"] += 1

    return tally


def drain(
    conn: sqlite3.Connection,
    executor: Executor,
    *,
    now_ms: int,
    max_rounds: int = MAX_ATTEMPTS + 1,
) -> dict[str, int]:
    """Run the relay until nothing is left pending or the round cap is hit.

    Bounded on purpose: an unbounded loop over a permanently failing executor
    would spin. `max_rounds` is one more than MAX_ATTEMPTS so every action gets
    the retries it is entitled to and then settles.
    """
    totals = {"sent": 0, "failed": 0, "retry": 0, "rounds": 0}
    for _ in range(max_rounds):
        tally = relay_once(conn, executor, now_ms=now_ms)
        totals["rounds"] += 1
        for k in ("sent", "failed", "retry"):
            totals[k] += tally[k]
        if tally["sent"] + tally["failed"] + tally["retry"] == 0:
            break
    return totals
