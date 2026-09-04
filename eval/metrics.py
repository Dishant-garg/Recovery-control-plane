"""Outcome metrics, including false suppression.

Recovery rate alone is a bad scorecard: a policy that contacts everyone five
times will beat a careful one on recovery and quietly destroy more value than it
creates. So net value here is recovered minus what it cost to send minus what
the opt-outs were worth.

**False suppression is the metric that keeps the control plane honest.** Every
`suppressed` decision is a claim that acting was not worth it. This module goes
back to the outcome model and asks what would have happened if it had acted, and
counts the times the answer was "you would have been paid". A control plane that
suppresses aggressively can win on cost while silently abandoning recoverable
money; this is the number that catches it.

`net_value_ex_churn_paise` is reported alongside net value on purpose. Churn
cost charges a full customer lifetime value per opt-out, so with only a handful
of opt-outs the headline number is dominated by *which* customers happened to
churn rather than by policy quality. Reporting both makes that dependence
visible instead of burying it -- and it is why eval/run.py averages over
several seeds.

Computing the counterfactual requires ground truth, so this module -- like
everything in eval/ -- may read it. Nothing under rcp/ may.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rcp.config import load
from sim.outcomes import _draw, success_probability


def _seg(segments: tuple[str, ...] | None, column: str = "e.segment") -> tuple[str, list]:
    """Render an optional segment restriction as a SQL fragment + params."""
    if not segments:
        return "", []
    return f" AND {column} IN ({','.join('?' * len(segments))})", list(segments)


def spend(conn: sqlite3.Connection, segments: tuple[str, ...] | None) -> int:
    """What the sends actually cost."""
    costs = {k: int(v) for k, v in load("scoring")["channel_cost_paise"].items()}
    clause, params = _seg(segments)
    rows = conn.execute(
        "SELECT a.channel AS channel, count(*) AS n FROM actions a "
        "JOIN decisions d ON d.id = a.decision_id "
        "JOIN events    e ON e.id = d.event_id "
        f"WHERE a.status = 'sent'{clause} GROUP BY a.channel",
        params,
    ).fetchall()
    return sum(costs.get(r["channel"], 0) * r["n"] for r in rows)


def false_suppression(
    conn: sqlite3.Connection,
    cfg_outcomes: dict[str, Any],
    latents: dict[str, dict[str, Any]],
    segments: tuple[str, ...] | None,
) -> tuple[int, int]:
    """Suppressed decisions that would in fact have been paid.

    Uses the top-ranked proposal the arbiter considered and rejected -- recorded
    in `decisions.detail` -- and asks the outcome model what it would have done.
    The draw is keyed on the decision id, so this counterfactual is stable
    across runs and independent of evaluation order.

    Returns (count, paise left on the table).
    """
    count = value = 0
    clause, params = _seg(segments)
    rows = conn.execute(
        "SELECT d.id AS id, d.detail AS detail, e.root_cause AS root_cause, "
        "       e.amount_paise AS amount_paise, e.retry_index AS retry_index, "
        "       c.payday_dom AS payday_dom, c.id AS customer_id "
        "FROM decisions d "
        "JOIN events    e ON e.id = d.event_id "
        "JOIN customers c ON c.id = d.customer_id "
        f"WHERE d.outcome = 'suppressed'{clause} ORDER BY d.id ASC",
        params,
    ).fetchall()

    for row in rows:
        considered = (json.loads(row["detail"]) or {}).get("considered") or []
        if not considered:
            continue
        best = considered[0]  # already in the arbiter's ranked order
        latent = latents.get(row["customer_id"])
        if latent is None:
            continue

        p = success_probability(
            cfg_outcomes,
            root_cause=row["root_cause"],
            channel=best["channel"],
            scheduled_at=int(best["scheduled_at"]),
            payday_dom=row["payday_dom"],
            retry_index=row["retry_index"],
            propensity=latent["propensity"],
            incentive_paise=int(best.get("incentive_paise", 0)),
            amount_paise=int(row["amount_paise"]),
        )
        if _draw(f"cf_{row['id']}", "success") < p:
            count += 1
            value += int(row["amount_paise"])

    return count, value


def compliance_cost(
    conn: sqlite3.Connection,
    cfg_outcomes: dict[str, Any],
    latents: dict[str, dict[str, Any]],
    segments: tuple[str, ...] | None,
) -> tuple[int, int]:
    """What the compliance rules cost, in rupees.

    Only counts decisions the rules suppressed OUTRIGHT. A proposal denied for
    lack of whatsapp consent costs nothing if an SMS from another proposer ran
    in its place -- the rule redirected the action rather than preventing it.
    Counting every denial would inflate this number and make compliance look
    far more expensive than it is.

    Returns (count, paise that would have been recovered).
    """
    count = value = 0
    clause, params = _seg(segments)
    rows = conn.execute(
        "SELECT d.id AS id, d.detail AS detail, e.root_cause AS root_cause, "
        "       e.amount_paise AS amount_paise, e.retry_index AS retry_index, "
        "       c.payday_dom AS payday_dom, c.id AS customer_id "
        "FROM decisions d "
        "JOIN events    e ON e.id = d.event_id "
        "JOIN customers c ON c.id = d.customer_id "
        f"WHERE d.outcome = 'suppressed' AND d.reason LIKE 'compliance:%'{clause} "
        "ORDER BY d.id ASC",
        params,
    ).fetchall()

    for row in rows:
        denied = ((json.loads(row["detail"]) or {}).get("compliance") or {}).get("denied") or []
        latent = latents.get(row["customer_id"])
        if not denied or latent is None:
            continue
        best = denied[0]
        p = success_probability(
            cfg_outcomes,
            root_cause=row["root_cause"],
            channel=best["channel"],
            scheduled_at=int(best["scheduled_at"]),
            payday_dom=row["payday_dom"],
            retry_index=row["retry_index"],
            propensity=latent["propensity"],
            amount_paise=int(row["amount_paise"]),
        )
        if _draw(f"cc_{row['id']}", "success") < p:
            count += 1
            value += int(row["amount_paise"])

    return count, value


def case_metrics(
    conn: sqlite3.Connection, segments: tuple[str, ...] | None
) -> dict[str, Any]:
    """How the recovery *workflow* behaved, as opposed to how it scored.

    Every number above this function is event-level: it can tell you a policy
    sent 595 messages and recovered Rs 955,080, and cannot tell you whether it
    got there by working 500 cases properly or by thrashing 100 of them.

    The row that matters most here is `refusals costing a rung`. ADR-009
    describes a defect where every compliance refusal consumed a ladder rung,
    and 95 cases climbed the entire ladder having sent nothing at all. That was
    caught three days late, by hand, because no reported number moved when it
    was introduced -- recovery and spend both looked plausible. This is the
    number that would have moved on the same run.

    `cases_never_acted` is an invariant rather than a measurement: a case that
    exhausted the ladder without ever sending is a bug, and the correct value
    is zero.
    """
    clause, params = _seg(segments, "c.segment")

    def one(sql: str, extra: list | None = None) -> int:
        return conn.execute(
            sql, (extra if extra is not None else params)
        ).fetchone()[0]

    opened = one(f"SELECT count(*) FROM cases c WHERE 1 = 1{clause}")
    by_state = {
        r["state"]: r["n"]
        for r in conn.execute(
            f"SELECT state, count(*) AS n FROM cases c WHERE 1 = 1{clause} "
            "GROUP BY state",
            params,
        )
    }

    # `rung` means "the rung to try next" (ADR-008), so on a settled case it is
    # also the count of rungs consumed.
    rungs = conn.execute(
        f"SELECT COALESCE(AVG(c.rung), 0) AS rungs, "
        f"       COALESCE(AVG(c.attempts), 0) AS attempts "
        f"FROM cases c WHERE 1 = 1{clause}",
        params,
    ).fetchone()

    # A `held` timeline row records a review that produced no action. Whether
    # it cost a rung is the ADR-009 distinction, written by caseloop._refusal.
    held = {
        r["spent"]: r["n"]
        for r in conn.execute(
            "SELECT json_extract(ce.detail, '$.rung_spent') AS spent, "
            "       count(*) AS n FROM case_events ce "
            "JOIN cases c ON c.id = ce.case_id "
            f"WHERE ce.kind = 'held'{clause} GROUP BY spent",
            params,
        )
    }

    closed = sum(
        by_state.get(s, 0)
        for s in ("recovered", "written_off", "opted_out")
    )
    return {
        "cases_opened": opened,
        "cases_recovered": by_state.get("recovered", 0),
        "cases_written_off": by_state.get("written_off", 0),
        "cases_opted_out": by_state.get("opted_out", 0),
        "cases_still_open": opened - closed,
        "mean_rungs_climbed": round(float(rungs["rungs"]), 4),
        "attempts_per_case": round(float(rungs["attempts"]), 4),
        "refusals_costing_rung": held.get(1, 0),
        "refusals_deferred": held.get(0, 0),
        # Must be zero. See ADR-009.
        "cases_never_acted": one(
            "SELECT count(*) FROM cases c WHERE c.attempts = 0 "
            f"AND c.close_reason LIKE 'ladder_exhausted%'{clause}"
        ),
    }


def compute(
    conn: sqlite3.Connection,
    cfg_outcomes: dict[str, Any],
    latents: dict[str, dict[str, Any]],
    segments: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    clause, params = _seg(segments)
    ltv_fraction = float(load("scoring")["churn"].get("ltv_fraction", 1.0))

    def one(sql: str, extra: list | None = None) -> int:
        return conn.execute(sql, (extra if extra is not None else params)).fetchone()[0]

    events_total = conn.execute(
        "SELECT count(*) FROM events e WHERE 1 = 1" + clause, params
    ).fetchone()[0]

    joined = (
        "FROM outcomes o "
        "JOIN actions   a ON a.id = o.action_id "
        "JOIN decisions d ON d.id = a.decision_id "
        "JOIN events    e ON e.id = d.event_id "
        f"WHERE 1 = 1{clause}"
    )

    actions_sent = one(
        "SELECT count(*) FROM actions a "
        "JOIN decisions d ON d.id = a.decision_id "
        "JOIN events    e ON e.id = d.event_id "
        f"WHERE a.status = 'sent'{clause}"
    )
    recovered_count = one(f"SELECT count(*) {joined} AND o.succeeded = 1")
    recovered_paise = one(f"SELECT COALESCE(SUM(o.recovered_paise), 0) {joined}")
    opt_outs = one(f"SELECT COALESCE(SUM(o.opted_out), 0) {joined}")
    churn_cost = one(
        f"SELECT CAST(COALESCE(SUM(c.ltv_paise), 0) * {ltv_fraction} AS INT) "
        "FROM outcomes o "
        "JOIN actions   a ON a.id = o.action_id "
        "JOIN decisions d ON d.id = a.decision_id "
        "JOIN events    e ON e.id = d.event_id "
        "JOIN customers c ON c.id = o.customer_id "
        f"WHERE o.opted_out = 1{clause}"
    )

    send_spend = spend(conn, segments)
    fs_count, fs_paise = false_suppression(conn, cfg_outcomes, latents, segments)
    cc_count, cc_paise = compliance_cost(conn, cfg_outcomes, latents, segments)

    promises_secured = one(
        "SELECT count(*) FROM promises p JOIN events e ON e.id = p.event_id "
        f"WHERE 1 = 1{clause}"
    )
    promises_kept = one(
        "SELECT count(*) FROM promises p JOIN events e ON e.id = p.event_id "
        f"WHERE p.state = 'kept'{clause}"
    )
    promised_recovered = one(
        "SELECT COALESCE(SUM(p.amount_paise), 0) FROM promises p "
        f"JOIN events e ON e.id = p.event_id WHERE p.state = 'kept'{clause}"
    )
    customers_touched = one(
        "SELECT count(DISTINCT a.customer_id) FROM actions a "
        "JOIN decisions d ON d.id = a.decision_id "
        "JOIN events    e ON e.id = d.event_id "
        f"WHERE a.status = 'sent'{clause}"
    )

    return {
        "events_total": events_total,
        "events_decided": one(
            "SELECT count(*) FROM decisions d "
            f"JOIN events e ON e.id = d.event_id WHERE 1 = 1{clause}"
        ),
        "actions_sent": actions_sent,
        "recovered_count": recovered_count,
        "recovery_rate": (
            round(recovered_count / events_total, 6) if events_total else 0.0
        ),
        "recovered_paise": recovered_paise,
        "spend_paise": send_spend,
        "opt_outs": opt_outs,
        "churn_cost_paise": churn_cost,
        "promises_secured": promises_secured,
        "promises_kept": promises_kept,
        "promised_recovered_paise": promised_recovered,
        "compliance_denied_count": one(
            "SELECT count(*) FROM decisions d JOIN events e ON e.id = d.event_id "
            f"WHERE d.outcome = 'suppressed' AND d.reason LIKE 'compliance:%'{clause}"
        ),
        "compliance_cost_count": cc_count,
        "compliance_cost_paise": cc_paise,
        # A promise honoured is money in the bank, so it belongs in net value.
        "net_value_paise": (
            recovered_paise + promised_recovered - send_spend - churn_cost
        ),
        "net_value_ex_churn_paise": (
            recovered_paise + promised_recovered - send_spend
        ),
        # What each contact was worth, churn aside. This is the targeting
        # metric: a restraint strategy can win it, whereas net-value-ex-churn
        # it definitionally cannot -- with churn removed every send has a
        # margin of ~Rs 1,200 against a ~Rs 5 send cost, so "send everything"
        # wins that comparison by arithmetic, not by being better.
        "margin_per_send_paise": (
            round((recovered_paise + promised_recovered - send_spend) / actions_sent)
            if actions_sent else 0
        ),
        "recovery_per_send": (
            round(recovered_count / actions_sent, 4) if actions_sent else 0.0
        ),
        "suppressed_value": one(
            "SELECT count(*) FROM decisions d "
            "JOIN events e ON e.id = d.event_id "
            "WHERE d.outcome = 'suppressed' "
            f"AND d.reason LIKE 'negative platform value%'{clause}"
        ),
        "suppressed_cap": one(
            "SELECT count(*) FROM decisions d "
            "JOIN events e ON e.id = d.event_id "
            "WHERE d.outcome = 'suppressed' "
            f"AND d.reason LIKE 'contact cap%'{clause}"
        ),
        "false_suppression_count": fs_count,
        "false_suppression_paise": fs_paise,
        "contacts_per_customer": round(
            actions_sent / max(1, customers_touched), 4
        ),
        **case_metrics(conn, segments),
    }
