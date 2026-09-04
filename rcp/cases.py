"""The case: one unit of recovery work, carried across days.

Before this existed, an event got exactly one decision and was never looked at
again -- the daily loop only ever collected events that occurred *that day*, so
a failure on day 3 was invisible on day 10. Nothing persisted, so there was
nothing to escalate and nothing to stop, which is why two of the four things a
recovery system is judged on were missing. See ADR-008.

Every state change writes a `case_events` row recording **who decided**: policy,
agent, compliance, or a stopping rule. That column is the whole point. "This
customer heard from us four times" is not an audit trail; "the agent escalated
to WhatsApp on day 6 because two SMS attempts went unanswered and precedent put
recovery at 0.31" is.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from rcp.schema import Case, CaseEventKind, CaseState, DecidedBy
from rcp.store import canonical_json, content_id, write_txn

# Terminal states. A case here is finished and must never be contacted again.
CLOSED_STATES = frozenset({
    CaseState.RECOVERED.value,
    CaseState.WRITTEN_OFF.value,
    CaseState.OPTED_OUT.value,
})

ACTIVE_STATES = frozenset({
    CaseState.OPEN.value,
    CaseState.WAITING.value,
    CaseState.PROMISED.value,
})


class IllegalTransition(Exception):
    """Raised on a move the case lifecycle does not contain."""


def open_case(
    conn: sqlite3.Connection, event: dict[str, Any], *, now_ms: int
) -> str:
    """Open a case for an event, or return the existing one.

    Idempotent by content-derived id plus `UNIQUE(event_id)`, so re-running a
    day is a no-op rather than a duplicate -- the same guarantee ADR-002 gives
    everywhere else. Must be called inside an open transaction.
    """
    case_id = content_id("case", event["id"])
    row = conn.execute(
        "INSERT INTO cases (id, event_id, customer_id, segment, amount_paise, "
        "state, rung, attempts, next_review_at, opened_at, closed_at, close_reason) "
        "VALUES (?, ?, ?, ?, ?, 'open', 0, 0, ?, ?, NULL, NULL) "
        "ON CONFLICT(event_id) DO NOTHING RETURNING id",
        (case_id, event["id"], event["customer_id"], event["segment"],
         event["amount_paise"], now_ms, now_ms),
    ).fetchone()

    if row is not None:
        record(conn, case_id, kind=CaseEventKind.OPENED,
               decided_by=DecidedBy.POLICY, rung=0,
               reason=f"case opened for {event['root_cause']}",
               detail={"event_id": event["id"], "segment": event["segment"],
                       "amount_paise": event["amount_paise"]},
               now_ms=now_ms)
    return case_id


def record(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    kind: CaseEventKind | str,
    decided_by: DecidedBy | str,
    reason: str,
    now_ms: int,
    rung: int | None = None,
    detail: dict[str, Any] | None = None,
) -> str:
    """Append to a case's timeline. Must be called inside an open transaction."""
    kind = kind.value if isinstance(kind, CaseEventKind) else kind
    decided_by = decided_by.value if isinstance(decided_by, DecidedBy) else decided_by

    seq = conn.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM case_events WHERE case_id = ?",
        (case_id,),
    ).fetchone()[0]

    event_id = content_id("cev", case_id, seq)
    conn.execute(
        "INSERT INTO case_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(case_id, seq) DO NOTHING",
        (event_id, case_id, seq, kind, rung, decided_by, reason,
         canonical_json(detail or {}), now_ms),
    )
    return event_id


def due_for_review(
    conn: sqlite3.Connection, *, now_ms: int, limit: int = 500
) -> list[sqlite3.Row]:
    """Cases the loop should look at today.

    This query is what turns one-shot decisions into a sequence. Total order on
    (next_review_at, id) because SQLite guarantees nothing on ties and an
    unstable order here would make the whole run non-reproducible (ADR-002).
    """
    return conn.execute(
        "SELECT * FROM cases WHERE state IN ('open', 'waiting') "
        "AND next_review_at IS NOT NULL AND next_review_at <= ? "
        "ORDER BY next_review_at ASC, id ASC LIMIT ?",
        (now_ms, limit),
    ).fetchall()


def advance(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    attempted_rung: int,
    next_review_at: int | None,
    now_ms: int,
    decided_by: DecidedBy | str,
    reason: str,
    acted: bool,
    consumed: bool = True,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record a review and schedule the next one.

    Three flags, and the distinction between the last two is what keeps a case
    from looping forever:

      `acted`    an action actually went out; counts toward max_attempts.
      `consumed` this rung has been used up. True when we acted AND when
                 compliance refused -- we tried, and trying is what a rung is
                 for. False only for a deliberate wait, where the rung is still
                 owed to us.
    """
    with write_txn(conn):
        current = _load(conn, case_id)
        if current["state"] in CLOSED_STATES:
            raise IllegalTransition(
                f"case {case_id} is {current['state']}; a closed case is never "
                f"reopened"
            )
        conn.execute(
            "UPDATE cases SET state = ?, rung = ?, attempts = attempts + ?, "
            "next_review_at = ? WHERE id = ?",
            (CaseState.WAITING.value, attempted_rung + (1 if consumed else 0),
             1 if acted else 0, next_review_at, case_id),
        )
        record(conn, case_id,
               kind=CaseEventKind.ACTED if acted else CaseEventKind.HELD,
               decided_by=decided_by, rung=attempted_rung, reason=reason,
               detail=detail, now_ms=now_ms)


def close(
    conn: sqlite3.Connection,
    case_id: str,
    *,
    state: CaseState | str,
    reason: str,
    now_ms: int,
    decided_by: DecidedBy | str = DecidedBy.STOPPING_RULE,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Close a case. Returns False if it was already closed.

    Closing is idempotent rather than an error: several signals can arrive for
    the same case in one day (the money landed *and* the write-off timer
    expired), and the first one to fire should win quietly.
    """
    state = state.value if isinstance(state, CaseState) else state
    if state not in CLOSED_STATES:
        raise IllegalTransition(
            f"{state} is not terminal; expected one of {sorted(CLOSED_STATES)}"
        )

    with write_txn(conn):
        current = _load(conn, case_id)
        if current["state"] in CLOSED_STATES:
            return False
        conn.execute(
            "UPDATE cases SET state = ?, closed_at = ?, close_reason = ?, "
            "next_review_at = NULL WHERE id = ?",
            (state, now_ms, reason, case_id),
        )
        record(conn, case_id, kind=CaseEventKind.CLOSED, decided_by=decided_by,
               rung=current["rung"], reason=reason, detail=detail, now_ms=now_ms)
        return True


def mark_promised(
    conn: sqlite3.Connection, case_id: str, *, due_at: int, now_ms: int,
    promise_id: str,
) -> None:
    """A promise pauses the case rather than closing it.

    The money is not in yet -- if the promise breaks, the case has to be
    workable again, which is why `promised` is not a terminal state.
    """
    with write_txn(conn):
        conn.execute(
            "UPDATE cases SET state = ?, next_review_at = ? WHERE id = ?",
            (CaseState.PROMISED.value, due_at, case_id),
        )
        record(conn, case_id, kind=CaseEventKind.OUTCOME,
               decided_by=DecidedBy.POLICY,
               reason=f"promise to pay accepted, due {due_at}",
               detail={"promise_id": promise_id, "due_at": due_at},
               now_ms=now_ms)


def reopen_after_broken_promise(
    conn: sqlite3.Connection, case_id: str, *, next_review_at: int, now_ms: int
) -> None:
    """A broken promise puts the case back in the queue."""
    with write_txn(conn):
        current = _load(conn, case_id)
        if current["state"] != CaseState.PROMISED.value:
            return
        conn.execute(
            "UPDATE cases SET state = 'waiting', next_review_at = ? WHERE id = ?",
            (next_review_at, case_id),
        )
        record(conn, case_id, kind=CaseEventKind.OUTCOME,
               decided_by=DecidedBy.POLICY,
               reason="promise broken, case returned to the queue",
               now_ms=now_ms)


def timeline(conn: sqlite3.Connection, case_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq ASC", (case_id,)
    ).fetchall()


def by_event(conn: sqlite3.Connection, event_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM cases WHERE event_id = ?", (event_id,)
    ).fetchone()
    return dict(row) if row else None


def load(conn: sqlite3.Connection, case_id: str) -> Case:
    return Case.model_validate(dict(_load(conn, case_id)))


def _load(conn: sqlite3.Connection, case_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
    if row is None:
        raise IllegalTransition(f"no such case: {case_id}")
    return row
