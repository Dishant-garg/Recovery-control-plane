"""Feature-keyed precedent: backoff behaviour and explainability.

These are the properties that justify not using a vector index -- the estimate
degrades predictably when evidence is thin, and every answer names the exact
feature tuple it came from.
"""

from __future__ import annotations

from rcp.precedent import MIN_TRIALS, lookup
from rcp.store import canonical_json, content_id, write_txn
from tests.conftest import action_row, make_customer, make_decision, make_event


def seed_history(conn, *, n: int, successes: int, root_cause="insufficient_funds",
                 amount_paise=150_000, days_from_payday=-2, channel="sms"):
    """Insert n completed actions, `successes` of which recovered the money."""
    with write_txn(conn):
        make_customer(conn)
        for i in range(n):
            eid = content_id("evt", root_cause, amount_paise, days_from_payday, channel, i)
            make_event(
                conn, eid, provider_event_id=f"rzp_{eid}", root_cause=root_cause,
                amount_paise=amount_paise, days_from_payday=days_from_payday,
                payload=canonical_json({"error_code": "X"}),
            )
            did = make_decision(conn, content_id("dec", eid), eid)
            act = action_row(f"{eid}:key", decision_id=did, channel=channel)
            conn.execute(
                "INSERT INTO actions VALUES (:id, :decision_id, :customer_id, "
                ":idempotency_key, :channel, :status, :scheduled_at, :sent_at, "
                ":attempts, :provider_ref, :body, :created_at)",
                dict(act, status="sent", sent_at=3000),
            )
            conn.execute(
                "INSERT INTO outcomes VALUES (?, ?, ?, 'cust_1', ?, ?, 0, 4000)",
                (content_id("out", eid), act["id"], eid,
                 1 if i < successes else 0,
                 amount_paise if i < successes else 0),
            )


QUERY = dict(
    root_cause="insufficient_funds",
    amount_bucket="500_2000",
    payday_phase="pre_payday",
    channel="sms",
)


def test_exact_tier_when_evidence_is_sufficient(conn):
    seed_history(conn, n=40, successes=12)
    p = lookup(conn, **QUERY)

    assert p.level == "exact"
    assert (p.successes, p.trials) == (12, 40)
    assert abs(p.posterior - (12 + 1) / (40 + 2)) < 1e-9
    assert "12 of 40" in p.explanation
    assert "root_cause=insufficient_funds" in p.explanation


def test_backs_off_when_the_narrow_tier_is_thin(conn):
    """Few pre-payday sms attempts, plenty at other payday phases -- the query
    should fall back to the tier that ignores payday_phase."""
    seed_history(conn, n=3, successes=1, days_from_payday=-2)     # pre_payday
    seed_history(conn, n=40, successes=20, days_from_payday=10)   # mid_cycle

    p = lookup(conn, **QUERY)
    assert p.level == "no_payday"
    assert p.trials == 43
    assert "payday_phase" not in p.explanation


def test_thin_global_evidence_is_flagged_not_hidden(conn):
    seed_history(conn, n=2, successes=2)
    p = lookup(conn, **QUERY)

    assert p.level == "global"
    assert p.trials == 2 < MIN_TRIALS
    assert "thin evidence" in p.explanation
    assert p.posterior < 1.0, "the Beta prior must keep 2/2 away from certainty"


def test_no_history_returns_the_uniform_prior(conn):
    p = lookup(conn, **QUERY)
    assert (p.successes, p.trials) == (0, 0)
    assert p.posterior == 0.5
    assert p.level == "global"


def test_lookup_is_deterministic(conn):
    seed_history(conn, n=30, successes=9)
    a = lookup(conn, **QUERY)
    b = lookup(conn, **QUERY)
    assert a.model_dump() == b.model_dump()
