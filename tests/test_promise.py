"""Promise-to-pay state machine.

The failure mode this guards against is subtle and expensive: an accepted
promise silences a customer, so a promise that can never leave the `accepted`
state silences them forever. `sweep_overdue` and the legal-transition checks are
what stop a protective rule from becoming a permanent gag.
"""

from __future__ import annotations

import pytest

from rcp.compliance.promise import (
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    active_promise,
    create,
    sweep_overdue,
    transition,
)
from rcp.config import load
from rcp.schema import PromiseState
from rcp.store import write_txn
from rcp.timeutil import MS_PER_DAY
from tests.conftest import make_chain

NOW = 1_735_689_600_000


def make_promise(conn, *, due_in_days=5, state=PromiseState.PROPOSED, now=NOW):
    with write_txn(conn):
        return create(conn, customer_id="cust_1", event_id="evt_1",
                      amount_paise=150_000, due_at=now + due_in_days * MS_PER_DAY,
                      now_ms=now, state=state)


def test_legal_transitions_succeed(conn):
    make_chain(conn)
    pid = make_promise(conn)
    transition(conn, pid, PromiseState.ACCEPTED, now_ms=NOW + 1000)
    transition(conn, pid, PromiseState.KEPT, now_ms=NOW + 2000)
    assert conn.execute(
        "SELECT state FROM promises WHERE id=?", (pid,)).fetchone()[0] == "kept"


@pytest.mark.parametrize("bad", ["kept", "broken"])
def test_cannot_skip_acceptance(conn, bad):
    make_chain(conn)
    pid = make_promise(conn)
    with pytest.raises(IllegalTransition, match="not a legal move"):
        transition(conn, pid, bad, now_ms=NOW + 1000)


@pytest.mark.parametrize("state", sorted(TERMINAL))
def test_terminal_states_are_final(conn, state):
    make_chain(conn)
    pid = make_promise(conn, state=state)
    with pytest.raises(IllegalTransition, match="terminal"):
        transition(conn, pid, PromiseState.ACCEPTED, now_ms=NOW + 1000)


def test_unknown_promise_raises(conn):
    with pytest.raises(IllegalTransition, match="no such promise"):
        transition(conn, "pr_nope", PromiseState.ACCEPTED, now_ms=NOW)


def test_state_graph_has_no_dead_ends_except_terminals():
    reachable = {s for moves in TRANSITIONS.values() for s in moves}
    for state, moves in TRANSITIONS.items():
        if state in TERMINAL:
            assert moves == frozenset(), f"{state} should be terminal"
        else:
            assert moves, f"{state} has no way out"
    assert TERMINAL <= reachable, "every terminal state must be reachable"


# ---- silencing behaviour -------------------------------------------------

def test_only_accepted_promises_silence(conn):
    make_chain(conn)
    make_promise(conn, state=PromiseState.PROPOSED)
    assert active_promise(conn, "cust_1", NOW) is None, "a proposal is not a promise"


def test_accepted_promise_silences_until_due_plus_grace(conn):
    make_chain(conn)
    grace = int(load("policy")["promise_to_pay"]["grace_days"])
    make_promise(conn, due_in_days=5, state=PromiseState.ACCEPTED)

    assert active_promise(conn, "cust_1", NOW) is not None
    assert active_promise(conn, "cust_1", NOW + 5 * MS_PER_DAY) is not None
    # Grace runs past the due date: a payment made on the day takes time to
    # settle, and chasing on day zero punishes someone who did as they said.
    assert active_promise(conn, "cust_1", NOW + (5 + grace) * MS_PER_DAY) is not None
    assert active_promise(conn, "cust_1", NOW + (6 + grace) * MS_PER_DAY) is None


def test_sweep_breaks_promises_past_grace(conn):
    """Without this an accepted promise silences its customer forever."""
    make_chain(conn)
    grace = int(load("policy")["promise_to_pay"]["grace_days"])
    pid = make_promise(conn, due_in_days=3, state=PromiseState.ACCEPTED)

    assert sweep_overdue(conn, now_ms=NOW + 2 * MS_PER_DAY) == 0
    assert sweep_overdue(conn, now_ms=NOW + (4 + grace) * MS_PER_DAY) == 1
    assert conn.execute(
        "SELECT state FROM promises WHERE id=?", (pid,)).fetchone()[0] == "broken"
    assert active_promise(conn, "cust_1", NOW + 10 * MS_PER_DAY) is None


def test_create_is_idempotent(conn):
    make_chain(conn)
    first = make_promise(conn)
    second = make_promise(conn)
    assert first == second
    assert conn.execute("SELECT count(*) FROM promises").fetchone()[0] == 1


def test_promise_id_is_content_derived(conn):
    make_chain(conn)
    pid = make_promise(conn, due_in_days=5)
    assert pid.startswith("pr_")
    assert make_promise(conn, due_in_days=9) != pid
