"""Connection-layer settings and the append-only rule.

`foreign_keys` and `busy_timeout` are per-connection and are not stored in the
file, so a second connection that skips the pragma block silently loses them.
These tests assert every connection gets them.
"""

from __future__ import annotations

import pytest

from rcp.migrations import APPEND_ONLY, migrate
from rcp.store import close, connect, content_id, write_txn
from tests.conftest import action_row, make_chain, populate_all


def test_pragmas_are_applied(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL


def test_second_connection_also_gets_them(db_path):
    """WAL persists in the file; the rest do not. A fresh connection must not
    silently come up with foreign keys disabled."""
    first = connect(db_path)
    migrate(first)
    close(first)

    second = connect(db_path)
    try:
        assert second.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert second.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    finally:
        close(second)


def test_read_only_connection_refuses_writes(db_path):
    rw = connect(db_path)
    migrate(rw)
    close(rw)

    ro = connect(db_path, read_only=True)
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(Exception):
            ro.execute("INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, opted_out, created_at) "
                       "VALUES ('c','cart',1,'en',1,0,1)")
    finally:
        ro.close()


def test_strict_tables_reject_wrong_types(conn):
    with pytest.raises(Exception, match="cannot store"):
        conn.execute("INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, opted_out, created_at) "
                     "VALUES ('c','cart',1,'en','oops',0,1)")


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(Exception, match="FOREIGN KEY"):
        with write_txn(conn):
            conn.execute(
                "INSERT INTO events (id, provider, provider_event_id, customer_id, "
                "segment, occurred_at, amount_paise, payload) VALUES "
                "('e1','razorpay','r1','cust_missing','cart',1,1,'{}')"
            )


@pytest.mark.parametrize("table", APPEND_ONLY)
def test_append_only_tables_reject_mutation(conn, table):
    populate_all(conn)  # a BEFORE trigger needs a row to fire on
    assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > 0

    for stmt in (f"UPDATE {table} SET rowid = rowid", f"DELETE FROM {table}"):
        with pytest.raises(Exception, match="append-only"):
            conn.execute(stmt)


def test_rollback_leaves_no_partial_write(conn):
    make_chain(conn)
    with pytest.raises(RuntimeError):
        with write_txn(conn):
            conn.execute(
                "INSERT INTO actions VALUES (:id, :decision_id, :customer_id, "
                ":idempotency_key, :channel, :status, :scheduled_at, :sent_at, "
                ":attempts, :provider_ref, :body, :created_at)",
                action_row("k1"),
            )
            raise RuntimeError("boom")

    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 0
    assert conn.in_transaction is False


def test_content_ids_are_stable():
    """A random ULID here would make two runs of the same seed produce
    different bytes, which is the whole reproducibility claim."""
    a = content_id("evt", "razorpay", "pay_1", 1000)
    b = content_id("evt", "razorpay", "pay_1", 1000)
    assert a == b
    assert a != content_id("evt", "razorpay", "pay_2", 1000)
    assert a.startswith("evt_") and len(a) == 4 + 16
