"""Compliance engine: allow / modify / deny, with a written trail.

Runs the rules in a fixed order, accumulating `Modify`s onto the proposal and
short-circuiting on the first `Deny`.

The trail records **every rule that ran, including the ones that passed**. That
is deliberate: "we checked consent and it was on record" is exactly what an
auditor wants to see, and a log that only records refusals cannot distinguish a
rule that approved from a rule that never executed.

Modifications compose. A quiet-hours shift and an incentive clamp can both apply
to the same proposal, and the effective proposal carries both -- which is why
this returns a rewritten proposal rather than a boolean.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rcp.compliance.rules import (
    CHANNEL_UNUSABLE,
    DEFAULT_RULES,
    Deny,
    Modify,
    Rule,
    RuleContext,
)
from rcp.config import load


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    proposal: dict[str, Any]          # effective, after any modifications
    trail: list[dict[str, Any]]
    denied_by: str | None = None
    reason: str | None = None
    # Only meaningful when `allowed` is False. Tells the caller whether the
    # refusal was about the channel, the timing, or the customer -- which is
    # what decides whether a ladder rung was spent. See ADR-009.
    disposition: str | None = None
    retry_after_ms: int | None = None

    @property
    def modified(self) -> bool:
        return any(entry["verdict"] == "modify" for entry in self.trail)

    @property
    def modifications(self) -> list[dict[str, Any]]:
        return [e for e in self.trail if e["verdict"] == "modify"]

    def explain(self) -> str:
        if not self.allowed:
            return f"denied by {self.denied_by}: {self.reason}"
        changes = "; ".join(e["note"] for e in self.modifications)
        return f"allowed ({changes})" if changes else "allowed"


def policy_version() -> str:
    """Stamped onto every decision, so a replay can be tied to the rules that
    produced it."""
    return str(load("policy")["version"])


def evaluate(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    event: dict[str, Any],
    customer: dict[str, Any],
    *,
    now_ms: int,
    rules: tuple[Rule, ...] = DEFAULT_RULES,
) -> Verdict:
    """Check one proposal against the policy.

    Called BEFORE scoring, not after. If a quiet-hours rule moves an action from
    10pm to 9am the next day, the payday phase may change and so does what the
    action is worth. Scoring first would value a plan the system is not going to
    execute.
    """
    effective = dict(proposal)
    trail: list[dict[str, Any]] = []

    for rule in rules:
        result = rule.check(
            RuleContext(
                proposal=effective, event=event, customer=customer,
                now_ms=now_ms, conn=conn,
            )
        )
        entry = {
            "rule": result.rule_id,
            "verdict": type(result).__name__.lower(),
            "note": result.note,
        }
        observed = getattr(result, "observed", None)
        if observed:
            entry["observed"] = observed

        if isinstance(result, Deny):
            entry["verdict"] = "deny"
            trail.append(entry)
            entry["disposition"] = result.disposition
            return Verdict(
                allowed=False, proposal=effective, trail=trail,
                denied_by=result.rule_id, reason=result.note,
                disposition=result.disposition,
                retry_after_ms=result.retry_after_ms,
            )

        if isinstance(result, Modify):
            entry["verdict"] = "modify"
            entry["changes"] = result.changes
            effective.update(result.changes)

        trail.append(entry)

    return Verdict(allowed=True, proposal=effective, trail=trail)
