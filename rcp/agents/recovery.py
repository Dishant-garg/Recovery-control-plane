"""The recovery agent: decides what to do with one case, this review.

Three choices only -- **escalate, hold, or stop** -- and the kernel bounds all
three. The agent cannot pick an arbitrary rung (the ladder owns channel order,
ADR-008), cannot exceed the contact cap (the arbiter holds that inside a
transaction, ADR-003), and cannot contact someone who opted out (compliance runs
after it regardless, ADR-005).

That bounding is the point. The brief asks for a *bounded* recovery workflow;
an agent that could do anything would not be one.

Two implementations behind one interface, as ADR-007 requires:

  **policy mode** (default, and what the batch eval uses) -- a deterministic
  rule over the case's own state. `make eval` stays byte-reproducible and free.

  **live mode** (`--live N`) -- a real tool loop, budgeted to N cases so a demo
  cannot spend an afternoon of tokens. Past the budget it falls back to policy,
  which is also what happens if the provider is unreachable.

The interesting tool is `compliance_preview`: the agent can ask what the engine
would say *before* committing, so it negotiates with its own bounds rather than
being silently overruled.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rcp.caseloop import Move, policy_decide
from rcp.compliance.engine import evaluate
from rcp.llm.client import AgentResult, LLMClient, Tool, _summarize
from rcp.precedent import lookup
from rcp.schema import DecidedBy
from rcp.timeutil import MS_PER_DAY

SYSTEM = """You decide what to do next with one payment-recovery case.

You have exactly three moves:

  escalate  try the next rung of the ladder now
  hold      wait; say how many days and why
  stop      give up on this case permanently

You cannot choose a channel -- the ladder owns that, cheapest and least
intrusive first. You cannot exceed the contact cap or reach someone who opted
out; those are enforced after you and will simply overrule you.

What you are actually judging is whether this case is worth another contact
*now*. Things that should move you:

  - the precedent posterior for this root cause and channel
  - how many times we have already contacted this person, across all their cases
  - whether the timing is wrong rather than the case being bad (a subscription
    retry before payday is a wasted rung; the same retry after payday is not)
  - whether compliance is about to refuse anyway

Bias: `hold` is underused. Waiting three days for payday costs almost nothing
and can double the odds. `stop` is for cases that will not pay, not for cases
that are inconvenient right now.

When you have decided, call `submit_decision` once. That call is your answer --
do not write it out in prose as well."""


def build_tools(
    conn: sqlite3.Connection,
    case: dict[str, Any],
    context: dict[str, Any],
    sink: dict[str, Any],
) -> list[Tool]:
    event, customer = context["event"], context["customer"]
    channel = context["channel"]

    def case_timeline() -> dict[str, Any]:
        rows = conn.execute(
            "SELECT seq, kind, rung, decided_by, reason, at FROM case_events "
            "WHERE case_id = ? ORDER BY seq", (case["id"],)
        ).fetchall()
        return {
            "case_id": case["id"], "segment": case["segment"],
            "root_cause": event["root_cause"],
            "amount_paise": case["amount_paise"],
            "attempts_so_far": case["attempts"],
            "next_rung": context["rung"], "next_channel": channel,
            "opened_days_ago": (context["now_ms"] - case["opened_at"]) // MS_PER_DAY,
            "timeline": [dict(r) for r in rows],
        }

    def precedent_for(channel_name: str) -> dict[str, Any]:
        p = lookup(conn, root_cause=event["root_cause"],
                   amount_bucket=event["amount_bucket"],
                   payday_phase=event["payday_phase"], channel=channel_name)
        return {"posterior": p.posterior, "successes": p.successes,
                "trials": p.trials, "tier": p.level,
                "explanation": p.explanation}

    def customer_history() -> dict[str, Any]:
        row = conn.execute(
            "SELECT count(*) AS sent, COALESCE(SUM(o.succeeded), 0) AS recovered "
            "FROM actions a LEFT JOIN outcomes o ON o.action_id = a.id "
            "WHERE a.customer_id = ? AND a.status = 'sent'",
            (customer["id"],)
        ).fetchone()
        open_cases = conn.execute(
            "SELECT count(*) FROM cases WHERE customer_id = ? "
            "AND state IN ('open', 'waiting', 'promised')", (customer["id"],)
        ).fetchone()[0]
        return {
            "contacts_ever": row["sent"], "recovered_ever": row["recovered"],
            "other_open_cases": max(0, open_cases - 1),
            "payday_day_of_month": customer["payday_dom"],
            "payday_phase_now": event["payday_phase"],
            "opted_out": bool(customer["opted_out"]),
        }

    def compliance_preview() -> dict[str, Any]:
        """What the engine would say if we escalated right now."""
        if channel is None:
            return {"verdict": "no rung left"}
        probe = {
            "id": "preview", "proposer_id": "preview", "channel": channel,
            "scheduled_at": context["now_ms"], "incentive_paise": 0,
            "claimed_success_prob": context["posterior"],
            "claimed_value_paise": case["amount_paise"],
            "rationale": "preview", "payload": "{}",
        }
        verdict = evaluate(conn, probe, event, customer, now_ms=context["now_ms"])
        return {
            "would_allow": verdict.allowed,
            "denied_by": verdict.denied_by,
            "reason": verdict.reason,
            "disposition": verdict.disposition,
        }

    def submit_decision(action: str, reason: str, hold_days: int = 3) -> dict:
        sink.update(action=action, reason=reason, hold_days=hold_days)
        return {"recorded": action}

    obj = lambda props, req: {"type": "object", "properties": props,
                              "required": req, "additionalProperties": False}
    return [
        Tool("case_timeline", "Everything that has happened to this case so far.",
             obj({}, []), case_timeline),
        Tool("precedent", "Observed success rate for a root cause on a channel.",
             obj({"channel_name": {"type": "string"}}, ["channel_name"]),
             precedent_for),
        Tool("customer_history",
             "How often this customer has been contacted, across all their cases.",
             obj({}, []), customer_history),
        Tool("compliance_preview",
             "What the compliance engine would say if you escalated now. Ask "
             "before deciding -- a refusal wastes the rung.",
             obj({}, []), compliance_preview),
        Tool("submit_decision",
             "Your answer. Call once with escalate, hold, or stop.",
             obj({"action": {"type": "string",
                             "enum": ["escalate", "hold", "stop"]},
                  "reason": {"type": "string"},
                  "hold_days": {"type": "integer",
                                "description": "only for hold; 1-14"}},
                 ["action", "reason"]),
             submit_decision),
    ]


class RecoveryAgent:
    """A `decide` hook for `caseloop.work_due_cases`.

    `live_budget` caps how many cases the model actually sees. Everything past
    it falls through to the deterministic policy, which is also the behaviour
    when the provider errors -- a demo running out of tokens degrades into a
    working system rather than a broken one.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        client: LLMClient | None = None,
        *,
        live_budget: int = 0,
    ) -> None:
        self.conn = conn
        self.client = client or LLMClient()
        self.live_budget = live_budget
        self.used = 0
        self.transcripts: list[AgentResult] = []

    def __call__(self, case: dict[str, Any], context: dict[str, Any]) -> Move:
        if self.used >= self.live_budget or context.get("rung") is None:
            return policy_decide(case, context)
        self.used += 1

        sink: dict[str, Any] = {}
        tools = build_tools(self.conn, case, context, sink)
        prompt = (
            f"Case {case['id']}: {context['event']['root_cause']} on a "
            f"{case['segment']} account, Rs {case['amount_paise'] / 100:,.0f}. "
            f"{case['attempts']} contacts so far. The next rung is "
            f"{context['rung']} ({context['channel']}). Decide."
        )
        try:
            result = self.client.run_agent(
                system=SYSTEM, prompt=prompt, tools=tools,
                fallback=lambda t: _deterministic(t, case, context),
                max_turns=8, use_cache=False,
            )
            self.transcripts.append(result)
        except Exception:
            # A provider failure must not take the recovery run down with it.
            return policy_decide(case, context)

        return _to_move(sink, case, context)


def _deterministic(tools: dict[str, Tool], case, context) -> AgentResult:
    """The offline routine. Same tools, same three moves, no model.

    Encodes the one judgement the fixed policy does not make: if compliance is
    about to refuse for a reason that will still be true tomorrow, escalating
    only burns the rung.
    """
    preview = tools["compliance_preview"].fn()
    # Carries the output, matching what `LLMClient._invoke` records for the live
    # path. The dashboard renders this trail, and the compliance verdict is the
    # whole reason to look at it -- a fallback trail without it would make the
    # path that always works the one you cannot read.
    trail = [{"tool": "compliance_preview", "input": {}, "error": False,
              "output": _summarize(preview)}]

    if preview.get("would_allow"):
        action, reason = "escalate", (
            f"rung {context['rung']} ({context['channel']}) is permitted, "
            f"posterior {context['posterior']:.2f}"
        )
    elif preview.get("disposition") == "retry_later":
        action, reason = "hold", (
            f"compliance would refuse now ({preview.get('denied_by')}); "
            f"the rung is fine, the timing is not"
        )
    else:
        action, reason = "escalate", (
            f"rung {context['rung']} will be refused by "
            f"{preview.get('denied_by')}; spend it and climb"
        )

    recorded = tools["submit_decision"].fn(
        action=action, reason=reason, hold_days=3)
    trail.append({"tool": "submit_decision",
                  "input": {"action": action, "reason": reason},
                  "error": False, "output": _summarize(recorded)})
    return AgentResult(text=json.dumps({"action": action, "reason": reason}),
                       trail=trail, turns=len(trail))


def _to_move(sink: dict[str, Any], case, context) -> Move:
    action = sink.get("action")
    if action not in {"escalate", "hold", "stop"}:
        # The model answered in prose or not at all. Falling back is safer than
        # guessing at intent from free text.
        return policy_decide(case, context)
    return Move(
        action=action,
        reason=sink.get("reason") or f"agent chose {action}",
        decided_by=DecidedBy.AGENT.value,
        hold_days=max(1, min(14, int(sink.get("hold_days") or 3))),
        detail={"rung": context.get("rung"), "channel": context.get("channel"),
                "posterior": context.get("posterior")},
    )
