"""Shared fixtures.

Tests use temp-file databases rather than `:memory:` on purpose: WAL mode and
`BEGIN IMMEDIATE` semantics only exist on a real file, and those are precisely
what most of these tests are checking.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _restore_environment():
    """Undo any environment a test mutates, for every test in the suite.

    `rcp.env.load_dotenv` writes `os.environ` directly, and monkeypatch cannot
    undo that. It has bitten twice: first when the env tests leaked
    `RCP_LLM=groq` into every later test that built an adapter, and again when
    the dashboard's agent route started loading .env and three unrelated tests
    began reaching for a live provider.

    Autouse and wholesale, because the leak never comes from the test that
    breaks -- so protecting only the tests that knowingly touch the environment
    is protecting the wrong ones.
    """
    snapshot = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(snapshot)

from rcp.store import canonical_json, close, connect, content_id, write_txn
from rcp.migrations import migrate

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "rcp.db"


@pytest.fixture
def conn(db_path: Path):
    c = connect(db_path)
    migrate(c)
    yield c
    close(c)


def make_customer(conn: sqlite3.Connection, cid: str = "cust_1", **kw: Any) -> str:
    row = {
        "id": cid, "segment": "subscription", "payday_dom": 5,
        "language": "hinglish", "ltv_paise": 5_000_00, "opted_out": 0,
        "created_at": 1000,
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, opted_out, created_at) "
        "VALUES (:id, :segment, :payday_dom, :language, :ltv_paise, :opted_out, "
        ":created_at) ON CONFLICT DO NOTHING",
        row,
    )
    return cid


def make_event(conn: sqlite3.Connection, eid: str = "evt_1", **kw: Any) -> str:
    row = {
        "id": eid, "provider": "razorpay", "provider_event_id": f"rzp_{eid}",
        "customer_id": "cust_1", "segment": "subscription", "occurred_at": 1000,
        "amount_paise": 150_000, "currency": "INR",
        "root_cause": "insufficient_funds", "retry_index": 0,
        "days_from_payday": -2, "payload": canonical_json({"error_code": "BAD_REQUEST"}),
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO events (id, provider, provider_event_id, customer_id, segment, "
        "occurred_at, amount_paise, currency, root_cause, retry_index, "
        "days_from_payday, payload) VALUES (:id, :provider, :provider_event_id, "
        ":customer_id, :segment, :occurred_at, :amount_paise, :currency, "
        ":root_cause, :retry_index, :days_from_payday, :payload)",
        row,
    )
    return eid


def make_decision(
    conn: sqlite3.Connection, did: str = "dec_1", eid: str = "evt_1", **kw: Any
) -> str:
    pid = f"prop_{did}"
    conn.execute(
        "INSERT INTO proposals (id, window_id, event_id, customer_id, proposer_id, "
        "channel, scheduled_at, claimed_success_prob, claimed_value_paise, "
        "incentive_paise, rationale, payload, created_at) VALUES "
        "(?, 'w1', ?, 'cust_1', 'subscription', 'sms', 2000, 0.4, 120000, 0, "
        "'payday-aware retry', '{}', 1500)",
        (pid, eid),
    )
    row = {
        "id": did, "window_id": "w1", "event_id": eid, "customer_id": "cust_1",
        "winning_proposal_id": pid, "score": 0.42, "outcome": "selected",
        "reason": "highest platform-side value", "policy_version": "v1",
        "decided_at": 1800, "detail": "{}",
    }
    row.update(kw)
    conn.execute(
        "INSERT INTO decisions VALUES (:id, :window_id, :event_id, :customer_id, "
        ":winning_proposal_id, :score, :outcome, :reason, :policy_version, "
        ":decided_at, :detail)",
        row,
    )
    return did


def make_chain(conn: sqlite3.Connection) -> tuple[str, str]:
    """Customer -> event -> proposal -> decision. Returns (event_id, decision_id)."""
    with write_txn(conn):
        make_customer(conn)
        make_event(conn)
        make_decision(conn)
    return "evt_1", "dec_1"


def populate_all(conn: sqlite3.Connection) -> None:
    """One row in every append-only table.

    A BEFORE UPDATE/DELETE trigger only fires when there is a row to touch, so
    trigger tests are vacuous against empty tables.
    """
    make_chain(conn)
    with write_txn(conn):
        act = action_row("populate:k1")
        conn.execute(
            "INSERT INTO actions VALUES (:id, :decision_id, :customer_id, "
            ":idempotency_key, :channel, :status, :scheduled_at, :sent_at, "
            ":attempts, :provider_ref, :body, :created_at)",
            dict(act, status="sent", sent_at=3000),
        )
        conn.execute(
            "INSERT INTO outcomes VALUES ('out_1', ?, 'evt_1', 'cust_1', 1, 150000, 0, 4000)",
            (act["id"],),
        )
        conn.execute(
            "INSERT INTO audit_mirror VALUES (0, 'h0', ?, 'decision', 'dec_1', 1000, '{}')",
            ("0" * 64,),
        )
        conn.execute(
            "INSERT INTO promises VALUES ('pr_1', 'cust_1', 'evt_1', 'proposed', "
            "150000, 9000, 1000, 1000)"
        )


def action_row(
    key: str, *, decision_id: str = "dec_1", scheduled_at: int = 2000, **kw: Any
) -> dict[str, Any]:
    row = {
        "id": content_id("act", key),
        "decision_id": decision_id,
        "customer_id": "cust_1",
        "idempotency_key": key,
        "channel": "sms",
        "status": "pending",
        "scheduled_at": scheduled_at,
        "sent_at": None,
        "attempts": 0,
        "provider_ref": None,
        "body": canonical_json({"template": "retry_reminder"}),
        "created_at": 1900,
    }
    row.update(kw)
    return row
