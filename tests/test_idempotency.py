"""Double execute -> exactly one action.

This exercises a database constraint rather than a code path. The application
could be rewritten around it and exactly-once would still hold, which is the
point of pushing the guarantee into the schema.
"""

from __future__ import annotations

from rcp.execute.outbox import drain
from rcp.execute.simulated import SimulatedExecutor
from rcp.store import insert_action_once, write_txn
from tests.conftest import action_row, make_chain


def test_duplicate_key_inserts_once(conn):
    make_chain(conn)
    row = action_row("cust_1:evt_1:sms:attempt_0")

    with write_txn(conn):
        first = insert_action_once(conn, row)
    with write_txn(conn):
        second = insert_action_once(conn, dict(row, id="act_different_id"))

    assert first is not None
    assert second is None, "replay must not create a second action"
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 1


def test_relay_does_not_resend_a_sent_action(conn):
    make_chain(conn)
    with write_txn(conn):
        insert_action_once(conn, action_row("cust_1:evt_1:sms:attempt_0"))

    executor = SimulatedExecutor(failure_rate=0.0)
    first = drain(conn, executor, now_ms=3000)
    second = drain(conn, executor, now_ms=4000)

    assert first["sent"] == 1
    assert second["sent"] == 0, "a settled action must not be picked up again"
    assert conn.execute(
        "SELECT attempts FROM actions"
    ).fetchone()[0] == 1


def test_immutable_columns_are_rejected(conn):
    """status/sent_at/attempts/provider_ref are mutable; nothing else is."""
    make_chain(conn)
    with write_txn(conn):
        insert_action_once(conn, action_row("k1"))

    conn.execute("UPDATE actions SET status = 'sent', sent_at = 9999")  # allowed

    for column, value in [("channel", "'voice'"), ("body", "'{}'"),
                          ("idempotency_key", "'k2'"), ("customer_id", "'cust_9'")]:
        try:
            conn.execute(f"UPDATE actions SET {column} = {value}")
        except Exception as exc:
            assert "only status" in str(exc)
        else:
            raise AssertionError(f"{column} should not be mutable")


def test_actions_cannot_be_deleted(conn):
    make_chain(conn)
    with write_txn(conn):
        insert_action_once(conn, action_row("k1"))
    try:
        conn.execute("DELETE FROM actions")
    except Exception as exc:
        assert "append-only" in str(exc)
    else:
        raise AssertionError("actions must not be deletable")
