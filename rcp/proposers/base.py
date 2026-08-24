"""Proposer protocol.

**A proposer never executes.** It returns a `Proposal` describing what it would
like to happen and why, and that is the end of its authority. It does not touch
an `Executor`, does not write to `actions`, and does not know whether it won.
See ADR-001.

A proposer also gets exactly one bid per event per window -- enforced by
`UNIQUE (window_id, proposer_id, event_id)` on the proposals table. That is a
deliberate contract, not a limitation: it forces each proposer to resolve its
own internal trade-offs and commit to a single best play, so the arbiter
arbitrates *between* strategies rather than refereeing one strategy's shortlist.

Proposers are permitted to be optimistic about their own success probability.
`claimed_success_prob` is an input to valuation, not the answer -- the arbiter
discounts it against observed history in arbiter/calibration.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from rcp.schema import Channel, Proposal
from rcp.store import canonical_json, content_id


@dataclass(frozen=True)
class ProposalContext:
    """Everything a proposer is allowed to see.

    Note what is absent: no executor, no truth.db, no outcome model, and no
    wall clock. `now_ms` is passed in.
    """

    event: dict[str, Any]
    customer: dict[str, Any]
    window_id: str
    now_ms: int

    @property
    def root_cause(self) -> str:
        return self.event["root_cause"]

    @property
    def amount_paise(self) -> int:
        return int(self.event["amount_paise"])

    @property
    def retry_index(self) -> int:
        return int(self.event["retry_index"])

    @property
    def payday_phase(self) -> str:
        return self.event["payday_phase"]


@runtime_checkable
class Proposer(Protocol):
    id: str
    segments: tuple[str, ...]

    def propose(self, ctx: ProposalContext) -> Proposal | None: ...


def make_proposal(
    ctx: ProposalContext,
    *,
    proposer_id: str,
    channel: Channel,
    scheduled_at: int,
    claimed_success_prob: float,
    claimed_value_paise: int | None = None,
    incentive_paise: int = 0,
    rationale: str,
    payload: dict[str, Any] | None = None,
) -> Proposal:
    """Build a Proposal with a content-derived id.

    The id is derived from (window, proposer, event) rather than generated, so
    re-running a window produces byte-identical proposal rows and the UNIQUE
    constraint turns a double-run into a no-op instead of a duplicate.
    """
    return Proposal(
        id=content_id("prop", ctx.window_id, proposer_id, ctx.event["id"]),
        window_id=ctx.window_id,
        event_id=ctx.event["id"],
        customer_id=ctx.event["customer_id"],
        proposer_id=proposer_id,
        channel=channel,
        scheduled_at=scheduled_at,
        claimed_success_prob=round(max(0.0, min(1.0, claimed_success_prob)), 6),
        claimed_value_paise=(
            ctx.amount_paise if claimed_value_paise is None else claimed_value_paise
        ),
        incentive_paise=incentive_paise,
        rationale=rationale,
        payload=canonical_json(payload or {}),
        created_at=ctx.now_ms,
    )


def insert_proposals(conn, proposals: list[Proposal]) -> int:
    """Persist proposals. Must be called inside an open transaction.

    `ON CONFLICT DO NOTHING` makes re-proposing a window idempotent.
    """
    if not proposals:
        return 0
    rows = [p.model_dump() for p in proposals]
    before = conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    conn.executemany(
        "INSERT INTO proposals (id, window_id, event_id, customer_id, proposer_id, "
        "channel, scheduled_at, claimed_success_prob, claimed_value_paise, "
        "incentive_paise, rationale, payload, created_at) VALUES "
        "(:id, :window_id, :event_id, :customer_id, :proposer_id, :channel, "
        ":scheduled_at, :claimed_success_prob, :claimed_value_paise, "
        ":incentive_paise, :rationale, :payload, :created_at) "
        "ON CONFLICT DO NOTHING",
        rows,
    )
    after = conn.execute("SELECT count(*) FROM proposals").fetchone()[0]
    return after - before
