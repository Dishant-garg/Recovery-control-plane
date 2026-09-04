"""Receivables proposer: B2B invoices, where the win is often a date, not a payment.

The third distinct set of instincts. A subscription bets on payday timing; a
cart bets on speed before the impulse dies. An invoice bets on neither — a
business does not pay because you messaged at the right hour, it pays when its
own cash cycle allows.

So the goal here is different: **secure a promise-to-pay.** A commitment to a
date is worth more than another unanswered reminder, because it converts an
unknown into a scheduled receivable and, through
`compliance/rules.py::ActivePromise`, stops the whole system from chasing
someone who already committed. That silence is the thing that keeps the promise
worth making.

Voice is reserved for large balances: it is by far the most expensive channel
(Rs 120 vs Rs 15 for SMS) and carries a 1-per-week sub-cap in policy.yaml, so
spending it on a small invoice wastes a scarce slot.
"""

from __future__ import annotations

import json

from rcp.proposers.base import ProposalContext, make_proposal
from rcp.schema import Channel, Proposal, RootCause
from rcp.timeutil import add_days, add_hours

PROPOSER_ID = "receivables"

# Above this, a human voice is worth the cost and the sub-cap slot.
VOICE_THRESHOLD_PAISE = 500_000  # Rs 5,000

# Causes the rail can clear without anyone being contacted. Found by the eval
# analyst (eval/analyst.py), which noticed the baseline recovering 22.4% on
# retries at Rs 2/send while this proposer spent Rs 120/send on voice to
# recover 16.0%. The same omission had already cost the cart proposer 21.8%.
RAIL_FIXABLE = {
    RootCause.INSUFFICIENT_FUNDS.value,
    RootCause.BANK_DOWNTIME.value,
}

# Invoices are slower and less certain than either other segment, but the
# balances are larger, so a modest probability still carries real value.
BASE_CLAIM = {
    RootCause.INSUFFICIENT_FUNDS.value: 0.38,
    RootCause.BANK_DOWNTIME.value: 0.58,
    RootCause.AUTH_FAILED.value: 0.34,
    RootCause.LIMIT_EXCEEDED.value: 0.42,
    RootCause.CARD_EXPIRED.value: 0.30,
    RootCause.MANDATE_EXPIRED.value: 0.33,
    RootCause.INVALID_ACCOUNT.value: 0.10,
    RootCause.UNKNOWN.value: 0.24,
}

# Each prior failure on the same invoice is a worse sign here than elsewhere --
# it usually means a cash-flow problem rather than a technical one.
RETRY_DECAY = 0.70

# How far out a promise is worth accepting.
PROMISE_HORIZON_DAYS = 14


class ReceivablesProposer:
    id = PROPOSER_ID
    segments = ("receivables",)

    def propose(self, ctx: ProposalContext) -> Proposal | None:
        if ctx.customer["segment"] not in self.segments:
            return None
        if ctx.customer["opted_out"]:
            return None

        channel = ctx.assigned(self._channel(ctx))
        asks_for_promise = channel in (Channel.VOICE, Channel.EMAIL)

        claim = BASE_CLAIM[ctx.root_cause] * (RETRY_DECAY ** ctx.retry_index)
        if channel is Channel.VOICE:
            claim *= 1.30  # a person on the phone gets an answer

        # Business hours, next working morning. An invoice chased at 2am reads
        # as automated and gets ignored.
        scheduled = (
            add_hours(ctx.now_ms, 12) if ctx.retry_index == 0
            else add_days(ctx.now_ms, 1)
        )

        return make_proposal(
            ctx,
            proposer_id=self.id,
            channel=channel,
            scheduled_at=scheduled,
            claimed_success_prob=claim,
            rationale=(
                f"invoice chase via {channel.value}"
                + (
                    f"; requesting a promise-to-pay within {PROMISE_HORIZON_DAYS}d"
                    if asks_for_promise else ""
                )
            ),
            payload={
                "root_cause": ctx.root_cause,
                "retry_index": ctx.retry_index,
                "asks_for_promise": asks_for_promise,
                "promise_horizon_days": PROMISE_HORIZON_DAYS,
            },
        )

    def _channel(self, ctx: ProposalContext) -> Channel:
        """Rail first where the rail can clear it, then voice, then email.

        A retry costs Rs 2 against Rs 120 for a call, is invisible to the
        customer, and on this data clears a higher share of the causes it can
        address. Chasing a company by phone over a decline their own bank would
        clear on the next attempt spends a scarce sub-cap slot for nothing.
        """
        if ctx.root_cause in RAIL_FIXABLE:
            return Channel.RETRY

        try:
            consent = json.loads(ctx.customer.get("consent") or "{}")
        except json.JSONDecodeError:
            consent = {}

        if ctx.amount_paise >= VOICE_THRESHOLD_PAISE and consent.get("voice") is True:
            return Channel.VOICE
        return Channel.EMAIL
