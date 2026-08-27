"""Platform-side valuation.

The question this module answers is deliberately not "which proposer is most
confident". It is "what is this action worth to the platform, all in" --
including the cost of the send and the expected cost of annoying the customer
into leaving. See ADR-004.

    score = calibrated_prob * amount
          - channel_cost - incentive
          - opt_out_risk * customer_ltv * churn_weight

The churn term is what stops the arbiter from spending a customer's lifetime
value to recover one invoice. Without it, more contact always looks better.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from rcp.arbiter.calibration import Calibrated, calibrate
from rcp.config import load
from rcp.precedent import observed_opt_out_rate
from rcp.schema import Channel


@dataclass(frozen=True)
class Scored:
    proposal: dict[str, Any]
    calibration: Calibrated
    gross_paise: int
    channel_cost_paise: int
    incentive_paise: int
    churn_cost_paise: int
    score_paise: int

    @property
    def proposal_id(self) -> str:
        return self.proposal["id"]

    @property
    def proposer_id(self) -> str:
        return self.proposal["proposer_id"]

    def to_audit(self) -> dict[str, Any]:
        """The per-proposal record written into decisions.detail.

        Every number that moved the decision is here, so a reviewer can
        recompute the winner by hand from the audit log alone.
        """
        return {
            "proposal_id": self.proposal_id,
            "proposer_id": self.proposer_id,
            "channel": self.proposal["channel"],
            "scheduled_at": self.proposal["scheduled_at"],
            "claimed_prob": self.calibration.claimed,
            "calibrated_prob": self.calibration.calibrated,
            "precedent": self.calibration.precedent.explanation,
            "gross_paise": self.gross_paise,
            "channel_cost_paise": self.channel_cost_paise,
            "incentive_paise": self.incentive_paise,
            "churn_cost_paise": self.churn_cost_paise,
            "score_paise": self.score_paise,
        }


def opt_out_risk(
    contacts_so_far: int, channel: str, conn: sqlite3.Connection | None = None
) -> float:
    """Probability this contact tips the customer into opting out.

    Rises with each additional contact in the window: the first message is
    routine, the fourth is harassment.

    A rail retry is exempt. Nobody opts out because a silent charge attempt
    happened -- the customer never sees it. This is a fact about the domain,
    not a peek at the outcome model, and getting it wrong is expensive: charging
    churn risk to retries makes the arbiter avoid the cheapest, safest, and
    (once timed against payday) most effective action it has.
    """
    if channel == Channel.RETRY.value:
        return 0.0

    churn = load("scoring")["churn"]
    base = float(churn["opt_out_base"])
    if conn is not None:
        # Learn the level from history; keep the per-contact slope as a prior,
        # since `outcomes` does not record how many contacts preceded each send.
        base, _, _ = observed_opt_out_rate(conn, prior=base)
    return base + max(0, contacts_so_far) * float(churn["opt_out_per_extra_contact"])


def score_proposal(
    conn: sqlite3.Connection,
    proposal: dict[str, Any],
    event: dict[str, Any],
    customer: dict[str, Any],
    *,
    contacts_so_far: int,
) -> Scored:
    cfg = load("scoring")
    channel = proposal["channel"]

    calibration = calibrate(
        conn,
        claimed_success_prob=float(proposal["claimed_success_prob"]),
        root_cause=event["root_cause"],
        amount_bucket=event["amount_bucket"],
        payday_phase=event["payday_phase"],
        channel=channel,
    )

    gross = int(calibration.calibrated * int(proposal["claimed_value_paise"]))
    channel_cost = int(cfg["channel_cost_paise"][channel])
    incentive = int(proposal["incentive_paise"])
    churn_cost = int(
        opt_out_risk(contacts_so_far, channel, conn)
        * int(customer["ltv_paise"])
        * float(cfg["churn"]["weight"])
        * float(cfg["churn"].get("ltv_fraction", 1.0))
    )

    return Scored(
        proposal=proposal,
        calibration=calibration,
        gross_paise=gross,
        channel_cost_paise=channel_cost,
        incentive_paise=incentive,
        churn_cost_paise=churn_cost,
        score_paise=gross - channel_cost - incentive - churn_cost,
    )


def rank(scored: list[Scored]) -> list[Scored]:
    """Total order, always.

    SQLite gives no guarantee on ties and neither does a plain sort by score.
    Breaking ties on proposer_id then proposal_id makes the winner a pure
    function of the inputs, which is what keeps `make eval` byte-reproducible.
    """
    return sorted(
        scored,
        key=lambda s: (-s.score_paise, s.proposer_id, s.proposal_id),
    )
