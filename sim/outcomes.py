"""The outcome model. Ground truth -- rcp/ must never import this.

**Policy-blind by construction.** This module is handed a channel, a scheduled
time, the event, and the customer's latent traits. It is never told the score,
the rationale, which proposer won, or whether the action was chosen well. It
answers only: given that this was attempted, at this time, on this rail, what
happens?

That blindness is what makes the eval honest. If the outcome model could see the
control plane's reasoning, a policy could look good by explaining itself
persuasively rather than by choosing well.

Draws are hash-derived rather than drawn from a stream, so an outcome depends
only on the action it belongs to -- not on how many other outcomes were resolved
first. Changing the evaluation order cannot change the result.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from typing import Any

from rcp.timeutil import days_from_payday, payday_phase

# No real channel recovers everything; leave headroom so a "perfect" play still
# loses sometimes.
MAX_SUCCESS_PROB = 0.95

# Each prior failure on the same event makes recovery less likely.
RETRY_DECAY = 0.68


def _draw(*parts: Any) -> float:
    """Deterministic uniform [0, 1) keyed on content."""
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode()).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def incentive_lift(cfg: dict[str, Any], incentive_paise: int, amount_paise: int) -> float:
    """How much a discount raises conversion, as a multiplier."""
    if incentive_paise <= 0 or amount_paise <= 0:
        return 1.0
    pct = 100.0 * incentive_paise / amount_paise
    return min(
        float(cfg.get("max_incentive_lift", 1.35)),
        1.0 + pct * float(cfg.get("incentive_lift_per_pct", 0.018)),
    )


@dataclass(frozen=True)
class Resolution:
    succeeded: int
    recovered_paise: int
    opted_out: int
    probability: float
    # A contact that did not collect can still secure a commitment to a date.
    promised: int = 0
    promise_due_at: int | None = None


def success_probability(
    cfg: dict[str, Any],
    *,
    root_cause: str,
    channel: str,
    scheduled_at: int,
    payday_dom: int | None,
    retry_index: int,
    propensity: float,
    incentive_paise: int = 0,
    amount_paise: int = 0,
) -> float:
    """What this attempt is actually worth, timing included.

    The payday phase is computed at the SCHEDULED time, not the failure time.
    That is the whole reason the subscription proposer's deferral can pay off:
    moving a retry from pre-payday to post-payday changes this number, and
    nothing else in the system tells the model that a deferral happened.
    """
    required = cfg.get("channel_required", {}).get(root_cause)
    if required is not None and channel not in required:
        # A dead mandate or an expired card fails identically on every retry.
        return 0.0

    base = float(cfg["base_rate"][root_cause])
    channel_eff = float(cfg["channel_effectiveness"][channel])
    phase = payday_phase(days_from_payday(scheduled_at, payday_dom))
    timing = float(cfg["payday_multiplier"][phase])

    p = base * channel_eff * timing * propensity * (RETRY_DECAY ** retry_index)
    p *= incentive_lift(cfg, incentive_paise, amount_paise)
    return max(0.0, min(MAX_SUCCESS_PROB, p))


def resolve(
    cfg: dict[str, Any],
    *,
    action_id: str,
    root_cause: str,
    channel: str,
    scheduled_at: int,
    amount_paise: int,
    payday_dom: int | None,
    retry_index: int,
    propensity: float,
    opt_out_sensitivity: float,
    contacts_before: int,
    asks_for_promise: bool = False,
    incentive_paise: int = 0,
) -> Resolution:
    p = success_probability(
        cfg,
        root_cause=root_cause,
        channel=channel,
        scheduled_at=scheduled_at,
        payday_dom=payday_dom,
        retry_index=retry_index,
        propensity=propensity,
        incentive_paise=incentive_paise,
        amount_paise=amount_paise,
    )
    succeeded = int(_draw(action_id, "success") < p)

    # A retry on the rail is invisible to the customer; a message is not.
    if channel == "retry":
        opted_out = 0
    else:
        p_opt = (
            float(cfg["opt_out_base"])
            + contacts_before * float(cfg["opt_out_per_extra_contact"])
        ) * opt_out_sensitivity
        opted_out = int(_draw(action_id, "optout") < p_opt)

    # A contact that failed to collect can still secure a date. Only asked for
    # where the proposer asked -- an SMS reminder does not negotiate terms.
    promised, due_at = 0, None
    if asks_for_promise and not succeeded and not opted_out:
        promise_cfg = cfg["promise"]
        if _draw(action_id, "promise") < float(promise_cfg["accept_rate"]):
            lo = int(promise_cfg["min_horizon_days"])
            hi = int(promise_cfg["max_horizon_days"])
            days = lo + int(_draw(action_id, "promise_horizon") * (hi - lo + 1))
            promised, due_at = 1, scheduled_at + min(days, hi) * 86_400_000

    return Resolution(
        succeeded=succeeded,
        recovered_paise=max(0, amount_paise - incentive_paise) if succeeded else 0,
        opted_out=opted_out,
        probability=round(p, 6),
        promised=promised,
        promise_due_at=due_at,
    )


def promise_kept(cfg: dict[str, Any], promise_id: str) -> bool:
    """Whether an accepted promise is actually honoured.

    Keyed on the promise id so the answer is fixed the moment the promise
    exists, independent of when the settlement sweep happens to run.
    """
    return _draw(promise_id, "kept") < float(cfg["promise"]["kept_rate"])


def load_latents(truth: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    return {
        r["customer_id"]: dict(r)
        for r in truth.execute("SELECT * FROM customer_latent")
    }
