"""Payday arithmetic, and the one piece of logic that exists in two places.

`payday_phase` is defined both as a SQL generated column (migrations.py) and as
a Python function (timeutil.py). The duplication is unavoidable -- the column
buckets stored events, the function buckets a *future* scheduled time that has
no row yet -- so it is pinned down by a test instead of a comment.
"""

from __future__ import annotations

import pytest

from rcp.store import canonical_json, write_txn
from rcp.timeutil import (
    MS_PER_DAY,
    day_of_month,
    days_from_payday,
    last_payday_ms,
    next_payday_ms,
    payday_phase,
    to_ms,
)
from tests.conftest import make_customer, make_event

JAN_1 = 1_735_689_600_000  # 2025-01-01T00:00:00Z, the sim epoch


def test_python_and_sql_payday_phase_agree(conn):
    """Exhaustive over the range the bucketing actually distinguishes."""
    with write_txn(conn):
        make_customer(conn)
        for i, days in enumerate(range(-40, 41)):
            make_event(
                conn, f"evt_{i:03d}", provider_event_id=f"rzp_{i:03d}",
                days_from_payday=days, payload=canonical_json({}),
            )

    rows = conn.execute(
        "SELECT days_from_payday AS d, payday_phase AS p FROM events"
    ).fetchall()
    assert len(rows) == 81
    for row in rows:
        assert row["p"] == payday_phase(row["d"]), f"disagree at {row['d']}"


def test_null_days_is_unknown_in_both(conn):
    with write_txn(conn):
        make_customer(conn)
        make_event(conn, "evt_null", days_from_payday=None,
                   payload=canonical_json({}))
    assert conn.execute(
        "SELECT payday_phase FROM events WHERE id = 'evt_null'"
    ).fetchone()[0] == "unknown" == payday_phase(None)


@pytest.mark.parametrize("days,expected", [
    (-4, "mid_cycle"), (-3, "pre_payday"), (0, "pre_payday"),
    (1, "post_payday"), (5, "post_payday"), (6, "mid_cycle"),
])
def test_phase_boundaries(days, expected):
    assert payday_phase(days) == expected


def test_next_payday_crosses_the_month(): 
    jan_10 = JAN_1 + 9 * MS_PER_DAY
    assert day_of_month(next_payday_ms(jan_10, 5)) == 5
    assert next_payday_ms(jan_10, 5) > jan_10   # February's
    assert day_of_month(next_payday_ms(jan_10, 25)) == 25


def test_short_month_clamps_rather_than_overflowing():
    """A 31st-of-month payday has to land somewhere in February."""
    feb_1 = to_ms(__import__("datetime").date(2025, 2, 1))
    assert day_of_month(next_payday_ms(feb_1, 31)) == 28


def test_last_payday_precedes_next():
    for dom in (1, 5, 15, 30):
        for offset in range(0, 60, 7):
            ms = JAN_1 + offset * MS_PER_DAY
            assert last_payday_ms(ms, dom) <= ms <= next_payday_ms(ms, dom)


def test_days_from_payday_sign_convention():
    """Negative means payday is still coming; positive means it just landed."""
    jan_7 = JAN_1 + 6 * MS_PER_DAY      # payday on the 5th -> 2 days after
    assert days_from_payday(jan_7, 5) == 2
    jan_3 = JAN_1 + 2 * MS_PER_DAY      # payday on the 5th -> 2 days away
    assert days_from_payday(jan_3, 5) == -2
    assert days_from_payday(JAN_1, None) is None
