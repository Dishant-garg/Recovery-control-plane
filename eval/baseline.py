"""The naive dunning policy, as a proposer.

This is what most recovery flows actually do: something goes out the moment a
charge fails, on a fixed channel, with no timing, no valuation, and no ceiling
on how often one customer hears from you.

It lives in eval/ rather than rcp/proposers/ deliberately -- it is the control
group, not a strategy the control plane offers. It runs through the same
collect -> score -> select machinery with the cap and the value floor switched
off, so the comparison isolates the policy rather than the plumbing.
"""

from __future__ import annotations

from rcp.proposers.base import ProposalContext, make_proposal
from rcp.schema import Channel, Proposal, RootCause
from rcp.timeutil import add_hours

# Retrying the rail is free-ish, so the naive policy reaches for it first and
# only messages when a retry obviously cannot work.
MESSAGE_INSTEAD = {
    RootCause.MANDATE_EXPIRED.value,
    RootCause.CARD_EXPIRED.value,
    RootCause.INVALID_ACCOUNT.value,
}


class BaselineProposer:
    id = "baseline"
    segments = ("subscription", "cart", "receivables")

    def propose(self, ctx: ProposalContext) -> Proposal | None:
        if ctx.customer["opted_out"]:
            return None

        needs_message = ctx.root_cause in MESSAGE_INSTEAD
        channel = Channel.SMS if needs_message else Channel.RETRY

        return make_proposal(
            ctx,
            proposer_id=self.id,
            channel=channel,
            # Immediately. No payday awareness, no outage window.
            scheduled_at=add_hours(ctx.now_ms, 1),
            claimed_success_prob=0.5,
            rationale="naive dunning: act immediately on failure",
            payload={"root_cause": ctx.root_cause, "policy": "baseline"},
        )
