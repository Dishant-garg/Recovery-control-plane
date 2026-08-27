"""Winner selection, with a written reason for everything that did not happen.

A suppression is a decision. `decisions` gets a row whether an action goes out
or not, carrying the reason and the numbers behind it, because "we deliberately
did not contact this customer, and here is what that cost" is exactly the claim
this project has to be able to defend.

Note on transaction shape: the cap check, the decision row, and the action row
all commit together, but they cannot be delegated wholesale to
`store.reserve_contact_in_txn` -- `actions.decision_id` is a foreign key, so the
decision has to be written *between* the cap read and the action insert. The two
halves are composed here directly, inside one IMMEDIATE transaction.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rcp.arbiter.score import Scored, rank, score_proposal
from rcp.audit import AuditLog
from rcp.compliance.engine import evaluate, policy_version as policy_version_from_config
from rcp.config import load
from rcp.schema import ActionStatus, DecisionOutcome
from rcp.store import (
    canonical_json,
    contact_count,
    content_id,
    insert_action_once,
    write_txn,
)
from rcp.timeutil import MS_PER_DAY


def _existing_decisions(conn: sqlite3.Connection, window_id: str) -> set[str]:
    return {
        r["event_id"]
        for r in conn.execute(
            "SELECT event_id FROM decisions WHERE window_id = ?", (window_id,)
        )
    }


def _proposals_by_event(
    conn: sqlite3.Connection, window_id: str
) -> dict[str, list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT * FROM proposals WHERE window_id = ? "
        "ORDER BY event_id ASC, proposer_id ASC, id ASC",
        (window_id,),
    ).fetchall()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["event_id"], []).append(dict(row))
    return grouped


def _write_decision(
    conn: sqlite3.Connection,
    *,
    decision_id: str,
    window_id: str,
    event: dict[str, Any],
    winner: Scored | None,
    outcome: DecisionOutcome,
    reason: str,
    policy_version: str,
    now_ms: int,
    detail: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO decisions VALUES (:id, :window_id, :event_id, :customer_id, "
        ":winning_proposal_id, :score, :outcome, :reason, :policy_version, "
        ":decided_at, :detail)",
        {
            "id": decision_id,
            "window_id": window_id,
            "event_id": event["id"],
            "customer_id": event["customer_id"],
            "winning_proposal_id": winner.proposal_id if winner else None,
            "score": float(winner.score_paise) if winner else None,
            "outcome": outcome.value,
            "reason": reason,
            "policy_version": policy_version,
            "decided_at": now_ms,
            "detail": canonical_json(detail),
        },
    )


def select_window(
    conn: sqlite3.Connection,
    *,
    window_id: str,
    now_ms: int,
    log: AuditLog | None = None,
    cap: int | None = None,
    min_score_paise: int | None = None,
    policy_version: str | None = None,
) -> dict[str, int]:
    """Arbitrate every event that has proposals in this window.

    `cap` / `min_score_paise` default to config/scoring.yaml. They are
    overridable so eval/ can run the naive baseline through this exact same
    machinery with the guards switched off -- which is what makes the
    comparison a policy difference rather than a code difference.
    """
    cfg = load("scoring")
    cap = int(cfg["contact_cap"]["max_contacts"]) if cap is None else cap
    cap_window_ms = int(cfg["contact_cap"]["window_days"]) * MS_PER_DAY
    min_score = (
        int(cfg["min_score_paise"]) if min_score_paise is None else min_score_paise
    )
    # Stamped from policy.yaml, not scoring.yaml -- the version that matters for
    # replay is which RULES were in force, not which weights.
    policy_version = policy_version or policy_version_from_config()

    grouped = _proposals_by_event(conn, window_id)
    already = _existing_decisions(conn, window_id)

    stats = {"selected": 0, "suppressed_value": 0, "suppressed_cap": 0,
             "suppressed_compliance": 0, "compliance_modified": 0, "skipped": 0}

    for event_id in sorted(grouped):
        if event_id in already:
            stats["skipped"] += 1  # re-running a window is a no-op
            continue

        event = dict(conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone())
        customer = dict(conn.execute(
            "SELECT * FROM customers WHERE id = ?", (event["customer_id"],)
        ).fetchone())

        window_start = now_ms - cap_window_ms
        contacts_so_far = contact_count(conn, customer["id"], window_start)

        # Compliance runs BEFORE scoring. A quiet-hours shift can move an action
        # into a different payday phase, which changes what it is worth --
        # scoring first would value a plan that is not going to be executed.
        permitted, refused = [], []
        for proposal in grouped[event_id]:
            verdict = evaluate(conn, proposal, event, customer, now_ms=now_ms)
            if verdict.allowed:
                permitted.append((verdict.proposal, verdict))
            else:
                refused.append({
                    "proposal_id": proposal["id"],
                    "proposer_id": proposal["proposer_id"],
                    "channel": proposal["channel"],
                    "scheduled_at": proposal["scheduled_at"],
                    "denied_by": verdict.denied_by,
                    "reason": verdict.reason,
                    "trail": verdict.trail,
                })

        by_id = {p["id"]: v for p, v in permitted}
        ranked = rank([
            score_proposal(conn, p, event, customer, contacts_so_far=contacts_so_far)
            for p, _ in permitted
        ])
        best = ranked[0] if ranked else None
        detail = {
            "contacts_so_far": contacts_so_far,
            "cap": cap,
            "min_score_paise": min_score,
            "compliance": {
                "denied": refused,
                "applied": [
                    {"proposal_id": pid, "modifications": v.modifications}
                    for pid, v in by_id.items() if v.modified
                ],
            },
            "considered": [s.to_audit() for s in ranked],
        }
        decision_id = content_id("dec", window_id, event_id)

        taken = "suppressed"

        with write_txn(conn):
            # Authoritative cap read, inside the same transaction as the write.
            observed = contact_count(conn, customer["id"], window_start)

            if best is None and refused:
                # Every proposal was refused. This is a compliance outcome, not
                # a valuation one, and conflating the two would hide the rules
                # behind an economic-sounding reason.
                _write_decision(
                    conn, decision_id=decision_id, window_id=window_id, event=event,
                    winner=None, outcome=DecisionOutcome.SUPPRESSED,
                    reason=f"compliance: denied by {refused[0]['denied_by']} "
                           f"({refused[0]['reason']})",
                    policy_version=policy_version, now_ms=now_ms, detail=detail,
                )
                stats["suppressed_compliance"] += 1

            elif best is None or best.score_paise <= min_score:
                reason = (
                    "no proposals" if best is None else
                    f"negative platform value: best score {best.score_paise} paise "
                    f"<= floor {min_score}"
                )
                _write_decision(
                    conn, decision_id=decision_id, window_id=window_id, event=event,
                    winner=None, outcome=DecisionOutcome.SUPPRESSED, reason=reason,
                    policy_version=policy_version, now_ms=now_ms, detail=detail,
                )
                stats["suppressed_value"] += 1

            elif observed >= cap:
                _write_decision(
                    conn, decision_id=decision_id, window_id=window_id, event=event,
                    winner=None, outcome=DecisionOutcome.SUPPRESSED,
                    reason=f"contact cap reached: {observed} contacts in "
                           f"{cfg['contact_cap']['window_days']}d, cap {cap}",
                    policy_version=policy_version, now_ms=now_ms,
                    detail={**detail, "would_have_scored": best.score_paise,
                            "would_have_channel": best.proposal["channel"]},
                )
                stats["suppressed_cap"] += 1

            else:
                _write_decision(
                    conn, decision_id=decision_id, window_id=window_id, event=event,
                    winner=best, outcome=DecisionOutcome.SELECTED,
                    reason=f"highest platform-side value: {best.score_paise} paise "
                           f"via {best.proposal['channel']}",
                    policy_version=policy_version, now_ms=now_ms, detail=detail,
                )
                key = f"{customer['id']}:{event_id}:{best.proposal['channel']}:{window_id}"
                insert_action_once(conn, {
                    "id": content_id("act", key),
                    "decision_id": decision_id,
                    "customer_id": customer["id"],
                    "idempotency_key": key,
                    "channel": best.proposal["channel"],
                    "status": ActionStatus.PENDING.value,
                    "scheduled_at": int(best.proposal["scheduled_at"]),
                    "sent_at": None,
                    "attempts": 0,
                    "provider_ref": None,
                    # The proposer's payload rides along: it is what tells the
                    # executor and the outcome model what this action actually
                    # asks for (a promise-to-pay, an incentive, a retry).
                    "body": canonical_json({
                        **json.loads(best.proposal["payload"] or "{}"),
                        "channel": best.proposal["channel"],
                        "root_cause": event["root_cause"],
                        "amount_paise": event["amount_paise"],
                        "language": customer["language"],
                        "incentive_paise": best.proposal["incentive_paise"],
                        "rationale": best.proposal["rationale"],
                    }),
                    "created_at": now_ms,
                })
                stats["selected"] += 1
                if by_id[best.proposal_id].modified:
                    stats["compliance_modified"] += 1
                taken = "selected"

        if log is not None:
            log.append(
                "decision",
                {"decision_id": decision_id, "event_id": event_id,
                 "outcome": taken,
                 "score_paise": best.score_paise if best else None},
                ts=now_ms, ref_id=decision_id,
            )

    return stats
