"""The contact cap must never break -- across 1000 seeds, and under concurrency.

The second test here is the interesting one: it also demonstrates that
`BEGIN IMMEDIATE` is load-bearing rather than decorative, by showing that the
same logic under `BEGIN DEFERRED` overshoots the cap.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
from pathlib import Path

from rcp.migrations import migrate
from rcp.schema import Suppressed
from rcp.store import connect, contact_count, insert_action_once, reserve_contact
from tests.conftest import action_row, make_chain

WINDOW_START = 0


def test_cap_holds_across_1000_seeds(conn):
    """Sequential, but with adversarial cap/attempt combinations."""
    make_chain(conn)
    rng_outer = random.Random(42)

    for seed in range(1000):
        rng = random.Random(seed)
        cap = rng.randint(1, 5)
        attempts = rng.randint(1, 12)
        customer = f"cust_seed_{seed}"

        conn.execute(
            f"INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, opted_out, created_at) "
            "VALUES (?, 'cart', 5, 'en', 100000, 0, 0)",
            (customer,),
        )

        granted = 0
        for i in range(attempts):
            row = action_row(
                f"{customer}:attempt_{i}",
                customer_id=customer,
                scheduled_at=rng_outer.randint(1, 5000),
            )
            result = reserve_contact(
                conn, row, window_start_ms=WINDOW_START, cap=cap
            )
            if isinstance(result, Suppressed):
                assert result.reason == "contact_cap_reached"
                assert result.cap == cap
            else:
                granted += 1

        observed = contact_count(conn, customer, WINDOW_START)
        assert observed == granted <= cap, (
            f"seed={seed} cap={cap} attempts={attempts} observed={observed}"
        )


def _racing_reserve(
    db_path: Path,
    customer: str,
    key: str,
    cap: int,
    mode: str,
    barrier: threading.Barrier,
    aborted: list[str],
) -> None:
    """Check-then-insert on its own connection, deliberately WITHOUT the
    in-process WRITE_LOCK, so only SQLite's own locking is under test."""
    conn = connect(db_path)
    try:
        barrier.wait(timeout=10)
        conn.execute(f"BEGIN {mode}")
        try:
            observed = contact_count(conn, customer, WINDOW_START)
            time.sleep(0.02)  # widen the read -> write window
            if observed < cap:
                insert_action_once(conn, action_row(key, customer_id=customer))
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            aborted.append(str(exc))
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
    finally:
        conn.close()


def _race(
    db_path: Path, customer: str, cap: int, mode: str, threads: int = 8
) -> tuple[int, list[str]]:
    """Returns (contacts that landed, abort messages)."""
    aborted: list[str] = []
    barrier = threading.Barrier(threads)
    workers = [
        threading.Thread(
            target=_racing_reserve,
            args=(db_path, customer, f"{customer}:t{i}", cap, mode, barrier, aborted),
        )
        for i in range(threads)
    ]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=30)

    conn = connect(db_path, read_only=True)
    try:
        return contact_count(conn, customer, WINDOW_START), aborted
    finally:
        conn.close()


def _fresh(db_path: Path) -> None:
    conn = connect(db_path)
    migrate(conn)
    make_chain(conn)
    conn.execute("INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, opted_out, created_at) "
                 "VALUES ('cust_race', 'cart', 5, 'en', 1, 0, 0)")
    conn.close()


def test_immediate_fills_the_cap_exactly_with_no_lost_work(db_path):
    _fresh(db_path)
    landed, aborted = _race(db_path, "cust_race", cap=3, mode="IMMEDIATE")

    assert landed == 3, "the cap should be filled, not undershot"
    assert aborted == [], f"IMMEDIATE must not abort anyone: {aborted}"


def test_deferred_silently_loses_writes(db_path):
    """Not a test of our code -- a test of why the keyword is there.

    The failure mode is the opposite of the intuitive one. Under WAL, DEFERRED
    does NOT overshoot the cap: snapshot isolation stops that. Instead a
    transaction that read a snapshot and then tries to write gets
    SQLITE_BUSY_SNAPSHOT, and `busy_timeout` cannot rescue it, because waiting
    would not make a stale snapshot fresh. The write is dropped.

    Measured here: 8 threads, cap 3 -> only 1 contact lands, 7 abort. Those 6
    missing contacts are recoverable payments that were within budget and never
    got reached, which eval/metrics.py counts as false suppression.

    If this ever starts passing with no aborts, SQLite's locking changed and the
    IMMEDIATE requirement should be re-derived rather than assumed.
    """
    _fresh(db_path)
    landed, aborted = _race(db_path, "cust_race", cap=3, mode="DEFERRED")

    assert aborted, "expected DEFERRED to abort transactions under contention"
    assert all("locked" in msg for msg in aborted), aborted
    assert landed < 3, (
        f"DEFERRED undershoots the cap by dropping writes; landed={landed}"
    )
