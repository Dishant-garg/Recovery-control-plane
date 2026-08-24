"""End-to-end behaviour of the collect -> score -> select slice.

The reproducibility test at the bottom is the one that matters most: it is the
claim the README leads with, and the only way to keep it true is to check it.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from rcp.arbiter.collect import collect, window_id_for
from rcp.arbiter.score import Scored, rank
from rcp.arbiter.select import select_window
from rcp.proposers.base import ProposalContext
from rcp.proposers.subscription import SubscriptionProposer
from rcp.store import canonical_json, write_txn
from rcp.timeutil import MS_PER_DAY
from tests.conftest import make_customer, make_event

REPO_ROOT = Path(__file__).resolve().parent.parent
JAN_1 = 1_735_689_600_000


def ctx_for(conn, **event_kw) -> ProposalContext:
    with write_txn(conn):
        make_customer(conn, payday_dom=event_kw.pop("payday_dom", 5))
        make_event(conn, payload=canonical_json({}), **event_kw)
    event = dict(conn.execute("SELECT * FROM events WHERE id = 'evt_1'").fetchone())
    customer = dict(conn.execute(
        "SELECT * FROM customers WHERE id = 'cust_1'").fetchone())
    return ProposalContext(event=event, customer=customer, window_id="w_0001",
                           now_ms=event["occurred_at"])


# ---- proposers -----------------------------------------------------------

def test_proposers_never_reach_an_executor():
    """ADR-001, checked structurally rather than trusted."""
    for path in (REPO_ROOT / "rcp" / "proposers").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert "execute" not in name, f"{path.name} imports {name}"


def test_proposal_is_deterministic(conn):
    ctx = ctx_for(conn)
    a = SubscriptionProposer().propose(ctx)
    b = SubscriptionProposer().propose(ctx)
    assert a.model_dump() == b.model_dump()


def test_insufficient_funds_defers_to_after_payday(conn):
    """The central bet: money is not there yet, so wait for it."""
    ctx = ctx_for(conn, occurred_at=JAN_1 + 1 * MS_PER_DAY,  # Jan 2, payday 5th
                  days_from_payday=-3, root_cause="insufficient_funds")
    proposal = SubscriptionProposer().propose(ctx)

    assert proposal.channel == "retry"
    assert proposal.scheduled_at > ctx.now_ms + 2 * MS_PER_DAY
    assert "payday" in proposal.rationale


def test_dead_mandate_gets_a_message_not_another_retry(conn):
    """Retrying a revoked mandate fails identically forever."""
    ctx = ctx_for(conn, root_cause="mandate_expired")
    proposal = SubscriptionProposer().propose(ctx)

    assert proposal.channel != "retry"
    assert json.loads(proposal.payload)["needs_customer_action"] is True


def test_opted_out_customers_get_nothing(conn):
    ctx = ctx_for(conn)
    ctx = ProposalContext(event=ctx.event, customer={**ctx.customer, "opted_out": 1},
                          window_id=ctx.window_id, now_ms=ctx.now_ms)
    assert SubscriptionProposer().propose(ctx) is None


def test_other_segments_are_left_alone(conn):
    ctx = ctx_for(conn)
    ctx = ProposalContext(event=ctx.event, customer={**ctx.customer, "segment": "cart"},
                          window_id=ctx.window_id, now_ms=ctx.now_ms)
    assert SubscriptionProposer().propose(ctx) is None


# ---- arbiter -------------------------------------------------------------

def _scored(score, proposer_id, proposal_id) -> Scored:
    return Scored(
        proposal={"id": proposal_id, "proposer_id": proposer_id, "channel": "sms",
                  "scheduled_at": 0},
        calibration=None, gross_paise=0, channel_cost_paise=0,
        incentive_paise=0, churn_cost_paise=0, score_paise=score,
    )


def test_ties_break_deterministically():
    """SQLite guarantees nothing on ties and neither does a plain sort. An
    unstable winner here silently breaks byte-reproducibility."""
    items = [
        _scored(100, "zeta", "prop_9"),
        _scored(100, "alpha", "prop_2"),
        _scored(100, "alpha", "prop_1"),
        _scored(200, "zeta", "prop_0"),
    ]
    order = [(s.proposer_id, s.proposal_id) for s in rank(items)]
    assert order == [("zeta", "prop_0"), ("alpha", "prop_1"),
                     ("alpha", "prop_2"), ("zeta", "prop_9")]
    assert rank(items) == rank(list(reversed(items)))


def test_suppression_is_recorded_as_a_decision(conn):
    """"We deliberately did not act" has to be auditable, not an absence."""
    ctx = ctx_for(conn, amount_paise=2000, root_cause="invalid_account")
    with write_txn(conn):
        from rcp.proposers.base import insert_proposals
        insert_proposals(conn, [SubscriptionProposer().propose(ctx)])

    stats = select_window(conn, window_id="w_0001", now_ms=ctx.now_ms + 1000)
    assert stats["suppressed_value"] == 1

    row = conn.execute("SELECT * FROM decisions").fetchone()
    assert row["outcome"] == "suppressed"
    assert row["winning_proposal_id"] is None
    assert "negative platform value" in row["reason"]
    assert json.loads(row["detail"])["considered"], "must record what it rejected"
    assert conn.execute("SELECT count(*) FROM actions").fetchone()[0] == 0


def test_rerunning_a_window_is_a_noop(conn):
    ctx = ctx_for(conn)
    collect(conn, [SubscriptionProposer()], window_id="w_0001",
            start_ms=ctx.now_ms - 1, end_ms=ctx.now_ms + 1, now_ms=ctx.now_ms)

    first = select_window(conn, window_id="w_0001", now_ms=ctx.now_ms + 1000)
    second = select_window(conn, window_id="w_0001", now_ms=ctx.now_ms + 2000)

    assert first["selected"] + first["suppressed_value"] == 1
    assert second["skipped"] == 1
    assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_window_ids_are_daily_and_ordered():
    assert window_id_for(JAN_1, epoch_ms=JAN_1) == "w_0000"
    assert window_id_for(JAN_1 + 9 * MS_PER_DAY, epoch_ms=JAN_1) == "w_0009"
    assert window_id_for(JAN_1 + MS_PER_DAY - 1, epoch_ms=JAN_1) == "w_0000"


# ---- the headline claim --------------------------------------------------

@pytest.mark.slow
def test_eval_is_byte_reproducible(tmp_path):
    """`make eval` twice must produce identical bytes.

    This is what lets the README quote a number a reviewer can reproduce
    offline with no API key. Everything else in the project is downstream of
    it staying true.
    """
    def run() -> str:
        subprocess.run(
            [sys.executable, "-m", "eval.run", "--seed", "42"],
            cwd=REPO_ROOT, check=True, capture_output=True,
        )
        return (REPO_ROOT / "data" / "seed_42" / "results.json").read_text()

    assert run() == run()
