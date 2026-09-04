"""Cart proposer: abandoned checkout, where value evaporates by the hour.

Opposite instincts to `subscription.py`, and deliberately so — if every proposer
reasoned the same way there would be nothing for the arbiter to arbitrate.

A subscription customer is a relationship: waiting three days for payday costs
almost nothing and doubles the odds. An abandoned cart is an impulse. Waiting
three days does not improve the retry, it just means the person has bought the
thing somewhere else. So this proposer is aggressive on time and willing to
spend an incentive — the two things the subscription proposer will not do.

Note the consent check. The proposer is not enforcing policy; it is picking a
channel that will actually be permitted, so it does not waste its single bid on
a plan compliance will refuse. `compliance/rules.py::Consent` remains the
guarantee. See ADR-005.
"""

from __future__ import annotations

import json

from typing import Any

from rcp.config import load
from rcp.proposers.base import ProposalContext, make_proposal
from rcp.schema import Channel, Proposal, RootCause
from rcp.timeutil import MS_PER_HOUR, add_hours

PROPOSER_ID = "cart"

# Value halves roughly every 18 hours. A two-day-old cart is worth ~10% of a
# fresh one, which is why nothing here ever defers.
HALF_LIFE_HOURS = 18.0

# Which causes the rail can fix on its own. A silent retry costs Rs 2, is
# invisible to the customer, and carries zero opt-out risk -- so for these it
# beats any message on every axis. Omitting it here was a real bug: the cart
# proposer used to message for everything, paying 7-17x more per send to
# recover less and annoy more.
RAIL_FIXABLE = {
    RootCause.INSUFFICIENT_FUNDS.value,
    RootCause.BANK_DOWNTIME.value,
    RootCause.UNKNOWN.value,
}

# Recovery odds by root cause. A cart is a softer ask than a subscription
# renewal -- there is no mandate to repair, just a checkout to finish.
BASE_CLAIM = {
    RootCause.INSUFFICIENT_FUNDS.value: 0.30,
    RootCause.BANK_DOWNTIME.value: 0.66,
    RootCause.AUTH_FAILED.value: 0.58,
    RootCause.LIMIT_EXCEEDED.value: 0.34,
    RootCause.CARD_EXPIRED.value: 0.46,
    RootCause.MANDATE_EXPIRED.value: 0.30,
    RootCause.INVALID_ACCOUNT.value: 0.14,
    RootCause.UNKNOWN.value: 0.28,
}

# Spend margin only where the alternative is abandoning the case.
#
# This was a flat 20% for two root causes, and it was economically inert: after
# the compliance ceiling clamped it to min(Rs 200, 15%), a Rs 2,500 cart got an
# 8% discount worth about +5% of expected value. Real enough to cost money,
# never enough to rescue anything.
#
# The sizing now follows the margin. A first contact gets no discount -- ask
# before paying. On a later rung, where a bare reminder recovers ~5% and the
# case is heading for a write-off, the offer is the largest one that still
# leaves the send worth making:
#
#     claim x (amount - incentive)  >=  floor + send cost
#
# Rearranged, that is the ceiling below. `compliance/rules.py::IncentiveCeiling`
# clamps it again, and watching that clamp is still the point.
INCENTIVE_FOR = {
    RootCause.LIMIT_EXCEEDED.value,
    RootCause.CARD_EXPIRED.value,
    RootCause.INSUFFICIENT_FUNDS.value,
    RootCause.AUTH_FAILED.value,
}

# Never offer more than this share of the amount, whatever the arithmetic says.
# A discount larger than this stops being a nudge and starts being a sale.
MAX_INCENTIVE_PCT = 25


def margin_sized_incentive(
    *, amount_paise: int, claim: float, attempts: int, channel: Any,
    root_cause: str,
) -> int:
    """The largest discount that still leaves this send worth making.

    Returns 0 for a silent retry (the customer never sees an offer), for a
    first contact (ask before paying), and for causes where a discount cannot
    address the failure -- an expired mandate needs the customer to fix the
    mandate, and money off does not do that.
    """
    if channel is Channel.RETRY or attempts == 0:
        return 0
    if root_cause not in INCENTIVE_FOR or claim <= 0:
        return 0

    cfg = load("scoring")
    floor = int(cfg.get("min_score_paise", 0))
    cost = int(cfg["channel_cost_paise"].get(
        channel.value if hasattr(channel, "value") else str(channel), 0))

    # claim * (amount - incentive) >= floor + cost
    headroom = amount_paise - int((floor + cost) / claim)
    return max(0, min(headroom, amount_paise * MAX_INCENTIVE_PCT // 100))


class CartProposer:
    id = PROPOSER_ID
    segments = ("cart",)

    def propose(self, ctx: ProposalContext) -> Proposal | None:
        if ctx.customer["segment"] not in self.segments:
            return None
        if ctx.customer["opted_out"]:
            return None

        channel = ctx.assigned(self._channel(ctx))
        age_hours = max(0, (ctx.now_ms - int(ctx.event["occurred_at"])) // MS_PER_HOUR)
        decay = 0.5 ** (age_hours / HALF_LIFE_HOURS)

        claim = BASE_CLAIM[ctx.root_cause] * decay
        incentive = margin_sized_incentive(
            amount_paise=ctx.amount_paise, claim=claim, attempts=ctx.attempts,
            channel=channel, root_cause=ctx.root_cause,
        )

        return make_proposal(
            ctx,
            proposer_id=self.id,
            channel=channel,
            # Always soon. Unlike a subscription there is no payday worth
            # waiting for -- the alternative is the customer buying it
            # elsewhere. A retry gets a few hours in case the decline was
            # transient; a message goes out immediately.
            scheduled_at=add_hours(ctx.now_ms, 4 if channel is Channel.RETRY else 1),
            claimed_success_prob=claim,
            incentive_paise=incentive,
            rationale=(
                f"cart {age_hours}h old, value decayed to {decay:.0%}; "
                f"{'incentive offered' if incentive else 'no incentive'}"
            ),
            payload={
                "root_cause": ctx.root_cause,
                "age_hours": age_hours,
                "decay": round(decay, 4),
                "incentive_requested_paise": incentive,
            },
        )

    def _channel(self, ctx: ProposalContext) -> Channel:
        """Retry where the rail can fix it; message only where it cannot.

        The ordering matters more than it looks. A retry is cheaper than an SMS
        by a factor of seven, invisible to the customer, and on this data
        recovers a higher share than either message channel -- so reaching for
        WhatsApp first was strictly worse on cost, churn, and recovery at once.
        """
        if ctx.root_cause in RAIL_FIXABLE:
            return Channel.RETRY
        try:
            consent = json.loads(ctx.customer.get("consent") or "{}")
        except json.JSONDecodeError:
            consent = {}
        return Channel.WHATSAPP if consent.get("whatsapp") is True else Channel.SMS
