"""Discount proposer claims against observed history.

A proposer's `claimed_success_prob` is an advocacy number. It is produced by the
module whose job is to win the auction, so it must not be taken at face value.
This is where it gets marked to what actually happened.

Thin by design: `rcp/precedent.py` already computes the posterior and explains
itself. All that is left is the blend, and the rule that evidence earns trust
rather than being granted it.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from rcp.config import load
from rcp.precedent import Precedent, lookup


@dataclass(frozen=True)
class Calibrated:
    claimed: float
    posterior: float
    calibrated: float
    weight: float
    precedent: Precedent

    @property
    def discount(self) -> float:
        """How much the claim was marked down. Negative means marked up."""
        return self.claimed - self.calibrated

    def explain(self) -> str:
        return (
            f"claimed {self.claimed:.2f} -> calibrated {self.calibrated:.2f} "
            f"(precedent weight {self.weight:.2f}); {self.precedent.explanation}"
        )


def calibrate(
    conn: sqlite3.Connection,
    *,
    claimed_success_prob: float,
    root_cause: str,
    amount_bucket: str,
    payday_phase: str,
    channel: str,
) -> Calibrated:
    """Blend the claim with the precedent posterior.

    The weight scales with evidence: a tier holding 3 trials cannot overrule a
    proposer, and a tier holding 200 gets the full configured weight. Without
    that ramp, the very first observations would swing decisions wildly, and the
    system would look decisive while being noise-driven.
    """
    cfg = load("scoring")["calibration"]
    ceiling = float(cfg["precedent_weight"])
    full_at = int(cfg["full_confidence_trials"])

    precedent = lookup(
        conn,
        root_cause=root_cause,
        amount_bucket=amount_bucket,
        payday_phase=payday_phase,
        channel=channel,
    )

    confidence = min(1.0, precedent.trials / full_at) if full_at else 1.0
    weight = ceiling * confidence
    calibrated = (1 - weight) * claimed_success_prob + weight * precedent.posterior

    return Calibrated(
        claimed=round(claimed_success_prob, 6),
        posterior=round(precedent.posterior, 6),
        calibrated=round(calibrated, 6),
        weight=round(weight, 6),
        precedent=precedent,
    )
