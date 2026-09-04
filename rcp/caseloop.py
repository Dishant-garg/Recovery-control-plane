"""The case loop: open cases, work the ones that are due, close the settled.

This is what turns one-shot decisions into a recovery *workflow*. The old daily
pass only ever collected events that occurred that day, so a failure on day 3
was never revisited -- there was no sequence, and therefore nothing to escalate
and nothing to stop. See ADR-008.

One day looks like:

    open_cases_for_day    every new failure becomes a case
    work_due_cases        stopping rules -> ladder rung -> proposer ->
                          compliance -> arbiter -> outbox
    close_settled_cases   money arrived, or the customer opted out

`work_due_cases` takes a `decide` hook. Default is the policy: climb to the next
rung. `rcp/agents/recovery.py` passes an agent instead, and either way the
choice is written to the case timeline with `decided_by` recording which it was.
The agent can only pick the next rung or stop -- it cannot skip to voice on the
first attempt, and compliance runs after it regardless (ADR-005).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

from rcp import cases
import json

from rcp.arbiter.select import select_window
from rcp.audit import AuditLog
from rcp.compliance.rules import CHANNEL_UNUSABLE, RETRY_LATER, STOP
from rcp.escalation import (
    channel_at,
    cooldown_ms,
    expected_value,
    next_rung,
    should_stop,
)
from rcp.precedent import lookup
from rcp.proposers.base import ProposalContext, insert_proposals
from rcp.config import load
from rcp.schema import CaseState, DecidedBy
from rcp.store import write_txn
from rcp.timeutil import MS_PER_DAY


@dataclass(frozen=True)
class Move:
    """What to do with a case this review. Returned by the `decide` hook."""

    action: str                      # "escalate" | "hold" | "stop"
    reason: str
    decided_by: str = DecidedBy.POLICY.value
    hold_days: int = 1
    detail: dict[str, Any] | None = None


def policy_decide(case: dict[str, Any], context: dict[str, Any]) -> Move:
    """The default: if the ladder has a rung left, climb it.

    Deliberately simple. Every guard that could stop this has already run --
    stopping rules before, compliance and the contact cap after -- so the
    policy's job is only to keep the case moving.
    """
    return Move(
        action="escalate",
        reason=f"rung {context['rung']} ({context['channel']}), "
               f"attempt {int(case['attempts']) + 1}",
        decided_by=DecidedBy.POLICY.value,
    )


def open_cases_for_day(
    conn: sqlite3.Connection,
    *,
    start_ms: int,
    end_ms: int,
    now_ms: int,
    segments: tuple[str, ...] | None = None,
) -> int:
    """Every failure that arrived today becomes a case."""
    sql = "SELECT * FROM events WHERE occurred_at >= ? AND occurred_at < ?"
    params: list[Any] = [start_ms, end_ms]
    if segments:
        sql += f" AND segment IN ({','.join('?' * len(segments))})"
        params += list(segments)

    rows = conn.execute(sql + " ORDER BY occurred_at ASC, id ASC", params).fetchall()
    if not rows:
        return 0
    with write_txn(conn):
        return sum(1 for row in rows
                   if cases.open_case(conn, dict(row), now_ms=now_ms))


def work_due_cases(
    conn: sqlite3.Connection,
    proposers: list[Any],
    *,
    window_id: str,
    now_ms: int,
    log: AuditLog | None = None,
    decide: Callable[[dict, dict], Move] = policy_decide,
    **select_kwargs: Any,
) -> dict[str, int]:
    """One review pass over every case that is due."""
    by_segment = {seg: p for p in proposers for seg in p.segments}
    stats = {"reviewed": 0, "escalated": 0, "held": 0, "stopped": 0,
             "no_proposal": 0}
    pending: list[tuple[dict, int, Move]] = []
    proposals = []

    for row in cases.due_for_review(conn, now_ms=now_ms):
        case = dict(row)
        stats["reviewed"] += 1

        event = dict(conn.execute(
            "SELECT * FROM events WHERE id = ?", (case["event_id"],)).fetchone())
        customer = dict(conn.execute(
            "SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone())

        rung = next_rung(case, event["root_cause"])
        channel = channel_at(case["segment"], rung) if rung is not None else None

        # Stopping rules run first and are priced with the odds of the rung we
        # would actually try next -- asking "is this worth chasing" against a
        # channel we are not going to use would answer the wrong question.
        posterior = 0.0
        if channel is not None:
            posterior = lookup(
                conn, root_cause=event["root_cause"],
                amount_bucket=event["amount_bucket"],
                payday_phase=event["payday_phase"], channel=channel,
            ).posterior

        stop = should_stop(
            case, now_ms=now_ms, opted_out=bool(customer["opted_out"]),
            expected_value_paise=expected_value(case, posterior),
            root_cause=event["root_cause"],
        )
        if stop is not None:
            cases.close(conn, case["id"], state=stop.close_state,
                        reason=f"{stop.rule}: {stop.reason}", now_ms=now_ms,
                        decided_by=DecidedBy.STOPPING_RULE,
                        detail={"rule": stop.rule, **(stop.observed or {})})
            stats["stopped"] += 1
            if log is not None:
                log.append("case_closed",
                           {"case_id": case["id"], "rule": stop.rule},
                           ts=now_ms, ref_id=case["id"])
            continue

        move = decide(case, {"rung": rung, "channel": channel,
                             "posterior": posterior, "event": event,
                             "customer": customer, "now_ms": now_ms})

        if move.action == "stop":
            cases.close(conn, case["id"], state=CaseState.WRITTEN_OFF,
                        reason=move.reason, now_ms=now_ms,
                        decided_by=move.decided_by, detail=move.detail)
            stats["stopped"] += 1
            continue

        if move.action == "hold":
            # A deliberate wait. The rung is still owed to us, so it is not
            # consumed -- the case comes back to the same rung.
            cases.advance(conn, case["id"], attempted_rung=int(case["rung"]),
                          next_review_at=now_ms + move.hold_days * 86_400_000,
                          now_ms=now_ms, decided_by=move.decided_by,
                          reason=move.reason, acted=False, consumed=False,
                          detail=move.detail)
            stats["held"] += 1
            continue

        proposer = by_segment.get(case["segment"])
        proposal = None
        if proposer is not None:
            proposal = proposer.propose(ProposalContext(
                event=event, customer=customer, window_id=window_id,
                now_ms=now_ms, rung=rung, channel=channel,
                case_id=case["id"], attempts=int(case["attempts"]),
            ))

        if proposal is None:
            # No proposer for this segment, or it declined. Hold rather than
            # burn the rung -- the case did nothing wrong.
            # Nothing will ever propose for this segment, so consume the rung
            # rather than re-asking the same question every cooldown.
            cases.advance(conn, case["id"], attempted_rung=rung,
                          next_review_at=now_ms + cooldown_ms(case["segment"], rung),
                          now_ms=now_ms, decided_by=DecidedBy.POLICY,
                          reason="no proposal for this segment", acted=False)
            stats["no_proposal"] += 1
            continue

        proposals.append(proposal)
        pending.append((case, rung, move))

    if proposals:
        with write_txn(conn):
            insert_proposals(conn, proposals)

    select_window(conn, window_id=window_id, now_ms=now_ms, log=log,
                  **select_kwargs)

    # Read back what the arbiter and compliance actually did, so the case
    # timeline records the real outcome rather than the intent.
    for case, rung, move in pending:
        decision = conn.execute(
            "SELECT outcome, reason, detail FROM decisions WHERE window_id = ? "
            "AND event_id = ?", (window_id, case["event_id"]),
        ).fetchone()

        if decision is not None and decision["outcome"] == "selected":
            cases.advance(
                conn, case["id"], attempted_rung=rung,
                next_review_at=now_ms + cooldown_ms(case["segment"], rung),
                now_ms=now_ms, decided_by=move.decided_by, reason=move.reason,
                acted=True, consumed=True,
                detail={"window_id": window_id,
                        "channel": channel_at(case["segment"], rung)},
            )
            stats["escalated"] += 1
            continue

        outcome = _refusal(conn, case, decision, rung=rung, now_ms=now_ms)
        stats[outcome] += 1

    return stats


def _refusal(
    conn: sqlite3.Connection,
    case: dict[str, Any],
    decision: Any,
    *,
    rung: int,
    now_ms: int,
) -> str:
    """Apply a refusal to the case, honouring what the refusal actually meant.

    Treating every refusal the same was the defect this function exists to fix:
    a rung burned by "no WhatsApp consent" and a rung burned by "contact cap
    reached this week" are completely different, and collapsing them had cases
    exhausting the entire ladder having sent nothing at all.
    """
    reason = decision["reason"] if decision else "no decision recorded"
    detail = json.loads(decision["detail"]) if decision else {}
    denied = (detail.get("compliance") or {}).get("denied") or []

    disposition = CHANNEL_UNUSABLE
    retry_after = None
    rule = None

    if denied:
        rule = denied[0].get("denied_by")
        disposition = denied[0].get("disposition") or CHANNEL_UNUSABLE
        retry_after = denied[0].get("retry_after_ms")
    elif reason.startswith("contact cap"):
        # The global cap lives in the arbiter's transaction, not the rule
        # engine, so it never appears in the compliance trail. It is still a
        # timing block: the window rolls and the answer changes.
        rule, disposition = "contact_cap", RETRY_LATER
        retry_after = now_ms + int(
            load("scoring")["contact_cap"]["window_days"]) * MS_PER_DAY
    elif reason.startswith("negative platform value"):
        # Climbing after this would be backwards. The ladder only gets more
        # expensive, so an action already judged not worth Rs 2 cannot become
        # worth Rs 120 one rung up.
        cases.close(conn, case["id"], state=CaseState.WRITTEN_OFF,
                    reason=f"not_worth_chasing: {reason}", now_ms=now_ms,
                    decided_by=DecidedBy.STOPPING_RULE,
                    detail={"rule": "not_worth_chasing", "rung": rung})
        return "stopped"

    if disposition == STOP:
        cases.close(conn, case["id"], state=CaseState.OPTED_OUT,
                    reason=f"{rule}: {reason}", now_ms=now_ms,
                    decided_by=DecidedBy.COMPLIANCE,
                    detail={"rule": rule, "disposition": disposition})
        return "stopped"

    consumed = disposition != RETRY_LATER
    next_review = (
        max(retry_after or 0, now_ms + MS_PER_DAY) if not consumed
        else now_ms + cooldown_ms(case["segment"], rung)
    )
    cases.advance(
        conn, case["id"], attempted_rung=rung, next_review_at=next_review,
        now_ms=now_ms, decided_by=DecidedBy.COMPLIANCE, reason=reason,
        acted=False, consumed=consumed,
        detail={"rule": rule, "disposition": disposition,
                "rung_spent": consumed,
                "channel": channel_at(case["segment"], rung)},
    )
    return "held"


def close_settled_cases(conn: sqlite3.Connection, *, now_ms: int) -> dict[str, int]:
    """Close cases the world has already answered.

    Recovery is checked before opt-out: a customer who paid and then unsubscribed
    is a recovered case, not an abandoned one, and recording it the other way
    would understate what the system earned.
    """
    tally = {"recovered": 0, "opted_out": 0}

    recovered = conn.execute(
        "SELECT c.id, SUM(o.recovered_paise) AS paise FROM cases c "
        "JOIN outcomes o ON o.event_id = c.event_id "
        "WHERE c.state NOT IN ('recovered', 'written_off', 'opted_out') "
        "AND o.succeeded = 1 GROUP BY c.id ORDER BY c.id"
    ).fetchall()
    for row in recovered:
        if cases.close(conn, row["id"], state=CaseState.RECOVERED,
                       reason=f"recovered {row['paise']} paise", now_ms=now_ms,
                       decided_by=DecidedBy.POLICY):
            tally["recovered"] += 1

    abandoned = conn.execute(
        "SELECT c.id FROM cases c JOIN customers cu ON cu.id = c.customer_id "
        "WHERE cu.opted_out = 1 "
        "AND c.state NOT IN ('recovered', 'written_off', 'opted_out') "
        "ORDER BY c.id"
    ).fetchall()
    for row in abandoned:
        if cases.close(conn, row["id"], state=CaseState.OPTED_OUT,
                       reason="customer opted out of contact", now_ms=now_ms,
                       decided_by=DecidedBy.STOPPING_RULE):
            tally["opted_out"] += 1

    return tally
