"""Individual compliance rules.

Each rule is a pure function of its context and returns one of three verdicts:

    Allow   -- this rule has no objection
    Modify  -- permissible, but not as proposed; here is the change and why
    Deny    -- not permissible, and here is the rule that says so

`Modify` matters more than it looks. The naive design makes every rule a
yes/no gate, which throws away real recovery value over fixable details: a
message that would have gone out at 10pm is still worth sending at 9am. A rule
that can amend a plan suppresses far less than one that can only refuse it.

Every verdict carries the observed value, not just an outcome, so the audit line
explains itself the way `store.Suppressed` already does. "denied: consent" is
not good enough; "denied by consent.whatsapp: no recorded opt-in" is.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from rcp.config import load
from rcp.timeutil import (
    MS_PER_DAY,
    in_quiet_hours,
    local_hour,
    shift_out_of_quiet_hours,
)


@dataclass(frozen=True)
class RuleContext:
    proposal: dict[str, Any]
    event: dict[str, Any]
    customer: dict[str, Any]
    now_ms: int
    conn: sqlite3.Connection

    @property
    def channel(self) -> str:
        return self.proposal["channel"]


@dataclass(frozen=True)
class Allow:
    rule_id: str
    note: str = "ok"
    ok: bool = True


@dataclass(frozen=True)
class Modify:
    rule_id: str
    changes: dict[str, Any]
    note: str
    observed: dict[str, Any] = field(default_factory=dict)
    ok: bool = True


# What a refusal means for the case that provoked it. Without this distinction
# every denial burned a ladder rung, and cases were exhausting the whole ladder
# on refusals having sent nothing at all -- 95 of them in one run. See ADR-009.
CHANNEL_UNUSABLE = "channel_unusable"   # this rung will never work; climb past it
RETRY_LATER = "retry_later"             # the rung is fine, the timing is not
STOP = "stop"                           # the case is over


@dataclass(frozen=True)
class Deny:
    rule_id: str
    note: str
    observed: dict[str, Any] = field(default_factory=dict)
    # Conservative default: assume the channel is the problem. A rule that
    # means "come back later" has to say so explicitly.
    disposition: str = CHANNEL_UNUSABLE
    retry_after_ms: int | None = None
    ok: bool = False


Verdict = Allow | Modify | Deny


class Rule(Protocol):
    id: str

    def check(self, ctx: RuleContext) -> Verdict: ...


# --------------------------------------------------------------------------

class OptOut:
    """A blanket stop. Not overridable by any score, ever."""

    id = "opt_out"

    def check(self, ctx: RuleContext) -> Verdict:
        if ctx.customer.get("opted_out"):
            return Deny(self.id, "customer has opted out of all contact",
                        disposition=STOP)
        return Allow(self.id)


class Consent:
    """Some channels need recorded opt-in before commercial contact.

    An absent key is a denial, not a default-yes. Treating "we have no record"
    as consent is how consent regimes get violated in practice.
    """

    id = "consent"

    def check(self, ctx: RuleContext) -> Verdict:
        cfg = load("policy")["consent"]
        if ctx.channel not in cfg["require_opt_in"]:
            return Allow(self.id, f"{ctx.channel} does not require opt-in")

        try:
            consent = json.loads(ctx.customer.get("consent") or "{}")
        except json.JSONDecodeError:
            consent = {}

        if consent.get(ctx.channel) is True:
            return Allow(self.id, f"opt-in on record for {ctx.channel}")
        return Deny(
            self.id,
            f"no recorded opt-in for {ctx.channel}",
            {"channel": ctx.channel, "recorded": consent.get(ctx.channel)},
        )


class ChannelEligibility:
    """Some root causes cannot be fixed by the rail, ever.

    subscription.py already declines to retry a dead mandate. This rule is what
    makes that a guarantee rather than good manners -- a future proposer written
    in a hurry gets stopped here. See ADR-005.
    """

    id = "channel_eligibility"

    def check(self, ctx: RuleContext) -> Verdict:
        denied = load("policy")["channel_eligibility"]["deny_retry_for"]
        if ctx.channel == "retry" and ctx.event["root_cause"] in denied:
            return Deny(
                self.id,
                f"retry cannot resolve {ctx.event['root_cause']}; "
                f"the customer has to act",
                {"root_cause": ctx.event["root_cause"]},
            )
        return Allow(self.id)


class QuietHours:
    """Shift a contact out of the local overnight window."""

    id = "quiet_hours"

    def check(self, ctx: RuleContext) -> Verdict:
        cfg = load("policy")
        quiet = cfg["quiet_hours"]
        offset = int(cfg["timezone_offset_minutes"]) * 60_000

        if ctx.channel in quiet["exempt_channels"]:
            return Allow(self.id, f"{ctx.channel} is exempt")

        scheduled = int(ctx.proposal["scheduled_at"])
        bounds = {"start_hour": int(quiet["start_hour"]),
                  "end_hour": int(quiet["end_hour"]), "offset_ms": offset}
        if not in_quiet_hours(scheduled, **bounds):
            return Allow(self.id, f"{local_hour(scheduled, offset):02d}:00 local is fine")

        shifted = shift_out_of_quiet_hours(scheduled, **bounds)
        return Modify(
            self.id,
            {"scheduled_at": shifted},
            f"shifted from {local_hour(scheduled, offset):02d}:00 to "
            f"{local_hour(shifted, offset):02d}:00 local",
            {"was": scheduled, "now": shifted},
        )


class ChannelSubCap:
    """Per-channel ceilings on top of the global contact cap.

    A customer can sit inside the overall cap and still be phoned three times a
    week, which the global number cannot see.
    """

    id = "channel_sub_cap"

    def check(self, ctx: RuleContext) -> Verdict:
        cfg = load("policy")["channel_sub_caps"]
        cap = cfg["max"].get(ctx.channel)
        if cap is None:
            return Allow(self.id, f"no sub-cap for {ctx.channel}")

        since = ctx.now_ms - int(cfg["window_days"]) * MS_PER_DAY
        used = ctx.conn.execute(
            "SELECT count(*) FROM actions WHERE customer_id = ? AND channel = ? "
            "AND scheduled_at >= ? AND status IN ('pending', 'sent')",
            (ctx.customer["id"], ctx.channel, since),
        ).fetchone()[0]

        if used >= cap:
            return Deny(
                self.id,
                f"{ctx.channel} sub-cap reached: {used} in "
                f"{cfg['window_days']}d, max {cap}",
                {"channel": ctx.channel, "used": used, "cap": cap},
            )
        return Allow(self.id, f"{used}/{cap} {ctx.channel} used")


class ActivePromise:
    """An accepted promise-to-pay buys silence until it is due.

    Chasing someone who already committed to a date is how a recovery flow turns
    into harassment, and it is the fastest way to lose the promise itself.
    """

    id = "active_promise"

    def check(self, ctx: RuleContext) -> Verdict:
        from rcp.compliance.promise import active_promise

        promise = active_promise(ctx.conn, ctx.customer["id"], ctx.now_ms)
        if promise is None:
            return Allow(self.id, "no active promise")

        grace = int(load("policy")["promise_to_pay"]["grace_days"]) * MS_PER_DAY
        return Deny(
            self.id,
            f"active promise to pay, due in "
            f"{(promise['due_at'] - ctx.now_ms) // MS_PER_DAY}d (+{grace // MS_PER_DAY}d grace)",
            {"promise_id": promise["id"], "due_at": promise["due_at"]},
            # The customer committed to a date. Nothing about the channel is
            # wrong -- come back after the promise resolves.
            disposition=RETRY_LATER,
            retry_after_ms=int(promise["due_at"]) + grace,
        )


class IncentiveCeiling:
    """Clamp discounts to an absolute cap and a share of what is owed.

    Modify rather than Deny: a Rs 500 discount on a Rs 1,000 invoice is not a
    reason to abandon the recovery, it is a reason to offer Rs 150.
    """

    id = "incentive_ceiling"

    def check(self, ctx: RuleContext) -> Verdict:
        cfg = load("policy")["incentive"]
        offered = int(ctx.proposal.get("incentive_paise", 0))
        if offered <= 0:
            return Allow(self.id, "no incentive offered")

        ceiling = min(
            int(cfg["max_paise"]),
            int(ctx.event["amount_paise"] * float(cfg["max_pct_of_amount"]) / 100),
        )
        if offered <= ceiling:
            return Allow(self.id, f"{offered} within ceiling {ceiling}")

        return Modify(
            self.id,
            {"incentive_paise": ceiling},
            f"incentive clamped from {offered} to {ceiling} paise",
            {"offered": offered, "ceiling": ceiling},
        )


# Order matters: cheap absolute refusals first so an opted-out customer is never
# subjected to a database round-trip, and Modify rules last so they only run on
# a plan that is going to survive.
DEFAULT_RULES: tuple[Rule, ...] = (
    OptOut(),
    Consent(),
    ChannelEligibility(),
    ChannelSubCap(),
    ActivePromise(),
    QuietHours(),
    IncentiveCeiling(),
)
