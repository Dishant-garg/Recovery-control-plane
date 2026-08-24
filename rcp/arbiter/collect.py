"""Window aggregation: gather the events in a window and run the proposers.

Batching into windows is what makes arbitration possible at all. Deciding one
event at a time means every decision looks locally reasonable while the customer
quietly receives five messages in a day. The window is the unit over which
budgets and caps mean something.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from rcp.proposers.base import ProposalContext, Proposer, insert_proposals
from rcp.store import write_txn
from rcp.timeutil import MS_PER_DAY, day_start_ms


def window_id_for(ms: int, *, epoch_ms: int) -> str:
    """Daily windows, numbered from the sim epoch."""
    return f"w_{(day_start_ms(ms) - day_start_ms(epoch_ms)) // MS_PER_DAY:04d}"


def events_in_window(
    conn: sqlite3.Connection,
    *,
    start_ms: int,
    end_ms: int,
    segments: tuple[str, ...] | None = None,
) -> list[sqlite3.Row]:
    """Total order on (occurred_at, id) -- ties must not depend on storage order."""
    sql = "SELECT * FROM events WHERE occurred_at >= ? AND occurred_at < ?"
    params: list[Any] = [start_ms, end_ms]
    if segments:
        sql += f" AND segment IN ({','.join('?' * len(segments))})"
        params += list(segments)
    return conn.execute(sql + " ORDER BY occurred_at ASC, id ASC", params).fetchall()


def load_customers(
    conn: sqlite3.Connection, ids: Iterable[str]
) -> dict[str, dict[str, Any]]:
    ids = sorted(set(ids))
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        f"SELECT * FROM customers WHERE id IN ({placeholders})", ids
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def collect(
    conn: sqlite3.Connection,
    proposers: list[Proposer],
    *,
    window_id: str,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    segments: tuple[str, ...] | None = None,
) -> int:
    """Run every proposer over every event in the window. Returns rows inserted.

    Proposers see only the event and the customer -- no executor, no outcome
    model, no clock. `now_ms` is passed in.

    `segments` restricts which events are considered at all. eval/ uses it to
    hold both policies to the same event set, so a segment the control plane
    has no proposer for yet does not silently count as a policy failure.
    """
    events = events_in_window(
        conn, start_ms=start_ms, end_ms=end_ms, segments=segments
    )
    if not events:
        return 0

    customers = load_customers(conn, (e["customer_id"] for e in events))

    proposals = []
    for event in events:
        customer = customers.get(event["customer_id"])
        if customer is None:
            continue
        ctx = ProposalContext(
            event=dict(event),
            customer=customer,
            window_id=window_id,
            now_ms=now_ms,
        )
        for proposer in proposers:
            proposal = proposer.propose(ctx)
            if proposal is not None:
                proposals.append(proposal)

    if not proposals:
        return 0
    with write_txn(conn):
        return insert_proposals(conn, proposals)
