"""The case lifecycle.

The property that matters most here is that a closed case stays closed. A
recovery system that can reopen a case someone opted out of is worse than one
that never had cases at all.
"""

from __future__ import annotations

import pytest

from rcp.cases import (
    CLOSED_STATES,
    IllegalTransition,
    advance,
    by_event,
    close,
    due_for_review,
    mark_promised,
    open_case,
    record,
    reopen_after_broken_promise,
    timeline,
)
from rcp.schema import CaseEventKind, CaseState, DecidedBy
from rcp.store import canonical_json, write_txn
from rcp.timeutil import MS_PER_DAY
from tests.conftest import make_customer, make_event

NOW = 1_735_689_600_000


def seed(conn, **event_kw):
    with write_txn(conn):
        make_customer(conn)
        make_event(conn, payload=canonical_json({}), **event_kw)
        event = dict(conn.execute("SELECT * FROM events WHERE id='evt_1'").fetchone())
        return open_case(conn, event, now_ms=NOW), event


def test_opening_records_the_first_timeline_entry(conn):
    case_id, _ = seed(conn)
    entries = timeline(conn, case_id)

    assert len(entries) == 1
    assert entries[0]["kind"] == CaseEventKind.OPENED.value
    assert entries[0]["seq"] == 0
    assert entries[0]["decided_by"] == DecidedBy.POLICY.value


def test_opening_twice_is_a_noop(conn):
    """Re-running a day must not duplicate work -- same guarantee as ADR-002."""
    case_id, event = seed(conn)
    with write_txn(conn):
        again = open_case(conn, event, now_ms=NOW + 1000)

    assert again == case_id
    assert len(timeline(conn, case_id)) == 1
    assert conn.execute("SELECT count(*) FROM cases").fetchone()[0] == 1


def test_due_for_review_is_what_makes_it_a_sequence(conn):
    """The old loop only saw events from the current day; this is the query
    that lets a case opened on day 3 be worked on day 10."""
    case_id, _ = seed(conn)
    assert [r["id"] for r in due_for_review(conn, now_ms=NOW)] == [case_id]

    advance(conn, case_id, attempted_rung=1, next_review_at=NOW + 3 * MS_PER_DAY,
            now_ms=NOW, decided_by=DecidedBy.POLICY, reason="escalated", acted=True)

    assert due_for_review(conn, now_ms=NOW + MS_PER_DAY) == [], "still in cooldown"
    assert len(due_for_review(conn, now_ms=NOW + 4 * MS_PER_DAY)) == 1


def test_advance_counts_attempts_only_when_it_acted(conn):
    """A held case still consumed a review and still owes the timeline a
    reason, but it did not spend an attempt."""
    case_id, _ = seed(conn)
    advance(conn, case_id, attempted_rung=0, next_review_at=NOW + MS_PER_DAY,
            now_ms=NOW, decided_by=DecidedBy.AGENT, reason="waiting for payday",
            acted=False, consumed=False)

    row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["attempts"] == 0
    assert row["rung"] == 0, "a deliberate wait does not spend the rung"
    assert timeline(conn, case_id)[-1]["kind"] == CaseEventKind.HELD.value

    advance(conn, case_id, attempted_rung=1, next_review_at=NOW + 4 * MS_PER_DAY,
            now_ms=NOW + MS_PER_DAY, decided_by=DecidedBy.POLICY,
            reason="escalated to sms", acted=True)
    assert dict(conn.execute("SELECT * FROM cases").fetchone())["attempts"] == 1
    assert timeline(conn, case_id)[-1]["kind"] == CaseEventKind.ACTED.value


@pytest.mark.parametrize("state", sorted(CLOSED_STATES))
def test_a_closed_case_is_never_worked_again(conn, state):
    case_id, _ = seed(conn)
    assert close(conn, case_id, state=state, reason="done", now_ms=NOW)

    assert due_for_review(conn, now_ms=NOW + 90 * MS_PER_DAY) == []
    with pytest.raises(IllegalTransition, match="closed case is never reopened"):
        advance(conn, case_id, attempted_rung=1, next_review_at=NOW, now_ms=NOW,
                decided_by=DecidedBy.POLICY, reason="try again", acted=True)


def test_closing_twice_is_quiet_not_an_error(conn):
    """Several signals can land on one day -- the money arriving and the
    write-off timer expiring. First one wins, quietly."""
    case_id, _ = seed(conn)
    assert close(conn, case_id, state=CaseState.RECOVERED, reason="paid",
                 now_ms=NOW) is True
    assert close(conn, case_id, state=CaseState.WRITTEN_OFF, reason="stale",
                 now_ms=NOW) is False

    row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["state"] == CaseState.RECOVERED.value
    assert row["close_reason"] == "paid"


def test_closing_into_a_non_terminal_state_is_rejected(conn):
    case_id, _ = seed(conn)
    with pytest.raises(IllegalTransition, match="not terminal"):
        close(conn, case_id, state=CaseState.WAITING, reason="hm", now_ms=NOW)


def test_every_closed_case_carries_a_reason(conn):
    case_id, _ = seed(conn)
    close(conn, case_id, state=CaseState.WRITTEN_OFF, reason="stale", now_ms=NOW)

    row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["closed_at"] is not None and row["close_reason"] == "stale"
    assert timeline(conn, case_id)[-1]["kind"] == CaseEventKind.CLOSED.value


def test_a_promise_pauses_rather_than_closes(conn):
    """The money is not in yet. If the promise breaks the case must be
    workable again, so `promised` is deliberately not terminal."""
    case_id, _ = seed(conn)
    due = NOW + 10 * MS_PER_DAY
    mark_promised(conn, case_id, due_at=due, now_ms=NOW, promise_id="pr_1")

    row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["state"] == CaseState.PROMISED.value
    assert row["closed_at"] is None
    assert due_for_review(conn, now_ms=due + 1) == [], "promised is not reviewable"

    reopen_after_broken_promise(conn, case_id, next_review_at=NOW, now_ms=due)
    assert len(due_for_review(conn, now_ms=NOW)) == 1


def test_timeline_is_append_only(conn):
    case_id, _ = seed(conn)
    with pytest.raises(Exception, match="append-only"):
        conn.execute("UPDATE case_events SET reason = 'rewritten'")
    with pytest.raises(Exception, match="append-only"):
        conn.execute("DELETE FROM case_events")


def test_timeline_sequence_is_dense_and_ordered(conn):
    case_id, _ = seed(conn)
    for i in range(3):
        with write_txn(conn):
            record(conn, case_id, kind=CaseEventKind.HELD,
                   decided_by=DecidedBy.POLICY, reason=f"hold {i}", now_ms=NOW + i)

    assert [e["seq"] for e in timeline(conn, case_id)] == [0, 1, 2, 3]


def test_every_timeline_entry_names_who_decided(conn):
    """Without this column the trail says a customer was contacted four times
    but not whether a policy, an agent, or a rule chose it."""
    case_id, _ = seed(conn)
    advance(conn, case_id, attempted_rung=1, next_review_at=NOW, now_ms=NOW,
            decided_by=DecidedBy.AGENT, reason="escalated", acted=True)
    close(conn, case_id, state=CaseState.WRITTEN_OFF, reason="stale", now_ms=NOW)

    deciders = [e["decided_by"] for e in timeline(conn, case_id)]
    assert deciders == ["policy", "agent", "stopping_rule"]


def test_by_event_finds_the_case(conn):
    case_id, _ = seed(conn)
    assert by_event(conn, "evt_1")["id"] == case_id
    assert by_event(conn, "evt_missing") is None


def test_unknown_case_raises(conn):
    with pytest.raises(IllegalTransition, match="no such case"):
        close(conn, "case_nope", state=CaseState.RECOVERED, reason="x", now_ms=NOW)


def test_a_refused_rung_is_still_consumed(conn):
    """The bug this guards against ran the whole eval into the ground: a case
    compliance refused never climbed, so it was re-reviewed and refused again
    every cooldown until the 45-day staleness rule closed it -- 7,092 held
    reviews against 513 real actions."""
    case_id, _ = seed(conn)
    advance(conn, case_id, attempted_rung=0, next_review_at=NOW + MS_PER_DAY,
            now_ms=NOW, decided_by=DecidedBy.COMPLIANCE,
            reason="denied by consent", acted=False, consumed=True)

    row = dict(conn.execute("SELECT * FROM cases").fetchone())
    assert row["rung"] == 1, "a rung we tried and were refused is spent"
    assert row["attempts"] == 0, "but nothing was sent, so no attempt was used"
