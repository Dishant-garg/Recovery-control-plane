"""Message composition: pick a registered template, fill it, check it.

    from rcp.compose import compose_for_action
    body, findings = compose_for_action(conn, proposal=p, event=e,
                                        customer=c, now_ms=now)

Three modules behind it:

    templates.py  the registry -- what a merchant registers with DLT and
                  WhatsApp before going live
    render.py     selection and variable filling, deterministic
    critic.py     what must be true before it can be sent

The split matters because the registry is the part a regulator sees, the
renderer is the part that has to be reproducible, and the critic is the part
that has to run on anything a model drafts.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from rcp.compose.critic import Finding, blocking, check, sms_segments
from rcp.compose.render import Message, compose, purpose_for
from rcp.compose.templates import REGISTRY, Template, registered_ids

__all__ = [
    "Finding", "Message", "Template", "REGISTRY", "blocking", "check",
    "compose", "compose_for_action", "purpose_for", "registered_ids",
    "sms_segments",
]


def compose_for_action(
    conn: sqlite3.Connection,
    *,
    proposal: dict[str, Any],
    event: dict[str, Any],
    customer: dict[str, Any],
    now_ms: int,
) -> tuple[dict[str, Any] | None, list[Finding]]:
    """Compose the message for a proposal the arbiter is about to select.

    Returns `(body_fragment, findings)`. The fragment is None when the channel
    carries no copy (a `retry`) or when the critic blocks -- and a blocked
    message is a defect in the registry, not a runtime condition, so the
    findings ride along in the action body rather than being swallowed.

    Reads the case to find out how far along this contact is. That is a query
    the arbiter would otherwise not need, and it is worth it: "first contact"
    and "fourth contact" are different messages, and a system that cannot tell
    them apart writes the same reminder four times.
    """
    from rcp.cases import by_event
    from rcp.escalation import ladder_for

    case = by_event(conn, event["id"])
    attempts = int(case["attempts"]) if case else 0
    opened_at = int(case["opened_at"]) if case else None
    rung = int(case["rung"]) if case else 0
    ladder = ladder_for(event["segment"])

    try:
        payload = json.loads(proposal.get("payload") or "{}")
    except json.JSONDecodeError:
        payload = {}

    message = compose(
        channel=proposal["channel"],
        language=customer.get("language") or "en",
        root_cause=event.get("root_cause"),
        amount_paise=int(event["amount_paise"]),
        incentive_paise=int(proposal.get("incentive_paise") or 0),
        attempts=attempts,
        is_last_rung=rung >= len(ladder) - 1,
        asks_for_promise=bool(payload.get("asks_for_promise")),
        now_ms=now_ms,
        opened_at_ms=opened_at,
    )
    if message is None:
        return None, []

    findings = check(
        message, incentive_paise=int(proposal.get("incentive_paise") or 0)
    )
    fragment = message.to_body()
    if findings:
        fragment["findings"] = [
            {"rule": f.rule, "severity": f.severity, "note": f.note}
            for f in findings
        ]
    if blocking(findings):
        return None, findings
    return fragment, findings
