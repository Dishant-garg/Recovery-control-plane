"""Subscription proposer: payday-aware retry sequencing.

The central bet: a failed subscription charge is not a dead customer, it is a
timing problem. `insufficient_funds` on the 28th and `insufficient_funds` on the
2nd are the same decline string and completely different situations. This
proposer schedules the retry for just after money lands rather than immediately.

The second bet is knowing when retrying is pointless. A dead mandate or an
expired card will fail identically on every retry, forever -- those need a
message asking the customer to act, not another attempt at the rail.

The numbers below are this proposer's *private prior*, and they are deliberately
a little optimistic. They are a claim, not a measurement; arbiter/calibration.py
discounts them against what actually happened. See ADR-001 and ADR-004.
"""

from __future__ import annotations

import json

from rcp.proposers.base import ProposalContext, make_proposal
from rcp.schema import Channel, Proposal, RootCause
from rcp.timeutil import add_days, add_hours, day_start_ms, next_payday_ms

PROPOSER_ID = "subscription"

# Retrying the rail cannot fix these -- the customer has to do something.
NEEDS_CUSTOMER_ACTION = {
    RootCause.MANDATE_EXPIRED.value,
    RootCause.CARD_EXPIRED.value,
    RootCause.INVALID_ACCOUNT.value,
}

# (channel, base success claim, rationale) per root cause.
PLAYBOOK: dict[str, tuple[Channel, float, str]] = {
    RootCause.INSUFFICIENT_FUNDS.value: (
        Channel.RETRY, 0.55, "retry scheduled just after payday"),
    RootCause.BANK_DOWNTIME.value: (
        Channel.RETRY, 0.72, "transient rail failure, retry after the outage window"),
    RootCause.AUTH_FAILED.value: (
        Channel.SMS, 0.48, "nudge the customer to re-authorize with a fresh OTP"),
    RootCause.LIMIT_EXCEEDED.value: (
        Channel.SMS, 0.36, "ask the customer to raise the limit or split the payment"),
    RootCause.MANDATE_EXPIRED.value: (
        Channel.WHATSAPP, 0.40, "mandate is dead, request re-authorization"),
    RootCause.CARD_EXPIRED.value: (
        Channel.WHATSAPP, 0.44, "card expired, request an updated payment method"),
    RootCause.INVALID_ACCOUNT.value: (
        Channel.EMAIL, 0.12, "account is closed or blocked, low recovery odds"),
    RootCause.UNKNOWN.value: (
        Channel.SMS, 0.25, "unclassified decline, generic reminder"),
}

# Each successive failure is a worse bet.
RETRY_DECAY = 0.62


class SubscriptionProposer:
    id = PROPOSER_ID
    segments = ("subscription",)

    def propose(self, ctx: ProposalContext) -> Proposal | None:
        if ctx.customer["segment"] not in self.segments:
            return None
        if ctx.customer["opted_out"]:
            return None

        channel, base_claim, why = PLAYBOOK[ctx.root_cause]
        channel, why = self._permitted_channel(ctx, channel, why)
        channel = ctx.assigned(channel)
        scheduled_at, timing_note, timing_factor = self._schedule(ctx, channel)

        claim = base_claim * timing_factor * (RETRY_DECAY ** ctx.retry_index)

        return make_proposal(
            ctx,
            proposer_id=self.id,
            channel=channel,
            scheduled_at=scheduled_at,
            claimed_success_prob=claim,
            rationale=f"{why}; {timing_note}",
            payload={
                "root_cause": ctx.root_cause,
                "retry_index": ctx.retry_index,
                "payday_phase": ctx.payday_phase,
                "needs_customer_action": ctx.root_cause in NEEDS_CUSTOMER_ACTION,
            },
        )

    def _permitted_channel(
        self, ctx: ProposalContext, channel: Channel, why: str
    ) -> tuple[Channel, str]:
        """Fall back to SMS where whatsapp has no recorded opt-in.

        A proposer gets one bid per event. Spending it on a channel compliance
        will refuse throws the whole event away -- the customer hears nothing at
        all, when an SMS would have been permitted and useful. Consent is still
        enforced by compliance/rules.py::Consent; this is the proposer being
        realistic about what it can actually get, not a policy check. See ADR-005.
        """
        if channel is not Channel.WHATSAPP:
            return channel, why
        try:
            consent = json.loads(ctx.customer.get("consent") or "{}")
        except json.JSONDecodeError:
            consent = {}
        if consent.get("whatsapp") is True:
            return channel, why
        return Channel.SMS, f"{why} (sms: no whatsapp opt-in)"

    def _schedule(
        self, ctx: ProposalContext, channel: Channel
    ) -> tuple[int, str, float]:
        """When to act, why, and how much the timing is worth.

        The timing factor is the proposer's own belief about its edge. It is a
        claim like any other and gets calibrated downstream.
        """
        now = ctx.now_ms

        if channel is not Channel.RETRY:
            # A message can go out promptly; there is nothing to wait for.
            return add_hours(now, 2), "sent promptly, no rail dependency", 1.0

        if ctx.root_cause == RootCause.BANK_DOWNTIME.value:
            return add_hours(now, 6), "retried after a 6h outage window", 1.0

        payday_dom = ctx.customer["payday_dom"]
        if payday_dom is None:
            return add_days(now, 1), "no payday known, retried next day", 0.85

        # Money just landed: retry now rather than waiting a full cycle.
        if ctx.payday_phase == "post_payday":
            return add_hours(now, 4), "post-payday, funds likely present", 1.25

        target = add_days(next_payday_ms(now, payday_dom), 1)
        if target <= now:
            target = add_days(day_start_ms(now), 1)
        days_out = max(0, (target - now) // 86_400_000)
        return (
            target,
            f"deferred {days_out}d to the day after payday (dom={payday_dom})",
            1.20,
        )
