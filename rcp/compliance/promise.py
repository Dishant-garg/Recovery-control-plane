"""Promise-to-pay state machine.

    proposed --> accepted --> kept
             |            \\-> broken
             \\-> expired

A promise is the most valuable thing a recovery flow can obtain: the customer
has told you when they will pay. It is also the easiest thing to destroy, by
continuing to chase them afterwards. `rules.ActivePromise` reads this state and
silences contact until the due date passes.

Transitions are validated here rather than trusted. The `promises` table's
trigger already refuses to mutate anything except state/due_at/updated_at, so
the storage layer guarantees history is not rewritten; this module guarantees
the state graph itself is respected.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from rcp.config import load
from rcp.schema import PromiseState
from rcp.store import content_id, write_txn
from rcp.timeutil import MS_PER_DAY

# The only moves that exist. Anything else is a bug, not a business case.
TRANSITIONS: dict[str, frozenset[str]] = {
    PromiseState.PROPOSED.value: frozenset(
        {PromiseState.ACCEPTED.value, PromiseState.EXPIRED.value}
    ),
    PromiseState.ACCEPTED.value: frozenset(
        {PromiseState.KEPT.value, PromiseState.BROKEN.value}
    ),
    PromiseState.KEPT.value: frozenset(),
    PromiseState.BROKEN.value: frozenset(),
    PromiseState.EXPIRED.value: frozenset(),
}

TERMINAL = frozenset(
    {PromiseState.KEPT.value, PromiseState.BROKEN.value, PromiseState.EXPIRED.value}
)


class IllegalTransition(Exception):
    """Raised on a move the state graph does not contain."""


def create(
    conn: sqlite3.Connection,
    *,
    customer_id: str,
    event_id: str,
    amount_paise: int,
    due_at: int,
    now_ms: int,
    state: PromiseState | str = PromiseState.PROPOSED,
) -> str:
    """Record a new promise. Must be called inside an open transaction."""
    state = state.value if isinstance(state, PromiseState) else state
    promise_id = content_id("pr", customer_id, event_id, due_at)
    conn.execute(
        "INSERT INTO promises VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO NOTHING",
        (promise_id, customer_id, event_id, state, amount_paise, due_at,
         now_ms, now_ms),
    )
    return promise_id


def transition(
    conn: sqlite3.Connection, promise_id: str, to_state: PromiseState | str,
    *, now_ms: int,
) -> None:
    """Move a promise, or raise. Opens its own transaction."""
    to_state = to_state.value if isinstance(to_state, PromiseState) else to_state
    row = conn.execute(
        "SELECT state FROM promises WHERE id = ?", (promise_id,)
    ).fetchone()
    if row is None:
        raise IllegalTransition(f"no such promise: {promise_id}")

    allowed = TRANSITIONS[row["state"]]
    if to_state not in allowed:
        raise IllegalTransition(
            f"{row['state']} -> {to_state} is not a legal move"
            + (f"; allowed: {sorted(allowed)}" if allowed else " (terminal state)")
        )

    with write_txn(conn):
        conn.execute(
            "UPDATE promises SET state = ?, updated_at = ? WHERE id = ?",
            (to_state, now_ms, promise_id),
        )


def active_promise(
    conn: sqlite3.Connection, customer_id: str, now_ms: int
) -> dict[str, Any] | None:
    """The accepted, not-yet-overdue promise silencing this customer, if any.

    Grace runs past the due date on purpose: a payment made on the due date
    takes time to settle, and chasing on day zero punishes someone who did
    exactly what they said they would.
    """
    grace = int(load("policy")["promise_to_pay"]["grace_days"]) * MS_PER_DAY
    row = conn.execute(
        "SELECT * FROM promises WHERE customer_id = ? AND state = 'accepted' "
        "AND due_at + ? >= ? ORDER BY due_at ASC, id ASC LIMIT 1",
        (customer_id, grace, now_ms),
    ).fetchone()
    return dict(row) if row else None


def sweep_overdue(conn: sqlite3.Connection, *, now_ms: int) -> int:
    """Mark promises whose grace window has closed as broken.

    Without this, an accepted promise silences a customer forever -- the most
    expensive possible failure mode for a rule whose job is to protect them.
    """
    grace = int(load("policy")["promise_to_pay"]["grace_days"]) * MS_PER_DAY
    overdue = conn.execute(
        "SELECT id FROM promises WHERE state = 'accepted' AND due_at + ? < ? "
        "ORDER BY id ASC",
        (grace, now_ms),
    ).fetchall()
    if not overdue:
        return 0
    with write_txn(conn):
        conn.executemany(
            "UPDATE promises SET state = 'broken', updated_at = ? WHERE id = ?",
            [(now_ms, r["id"]) for r in overdue],
        )
    return len(overdue)
