"""The escalation ladder and the stopping rules.

Two ideas, both pure functions of case state.

**The ladder owns channel order.** A proposer free to pick any channel each time
could reach for voice on the first attempt; a ladder cannot. Rungs run cheapest
and least intrusive first, so an expensive or intrusive channel is only reached
by a case that has genuinely earned it. That is what makes escalation
*compliant* rather than merely repeated.

**Stopping rules decide when to give up.** Knowing when to stop is half of what
separates recovery from harassment, and it is the half nobody builds. Every rule
here returns a `Stop` carrying its rule id and the numbers behind it -- the same
shape as `store.Suppressed` and `compliance.rules.Deny`, so every refusal in the
system explains itself the same way.

`hard_stop_on_opt_out` is checked first and is never overridable, by any score,
policy, or agent.
"""

from __future__ import annotations

from typing import Any

from rcp.config import load
from rcp.schema import CaseState, Channel, Stop
from rcp.timeutil import MS_PER_DAY

# Silent rail retries need less breathing room than a message, so cooldown is
# per-channel rather than a single number.
RETRY = Channel.RETRY.value


def ladder_for(segment: str) -> list[str]:
    return list(load("policy")["escalation"]["ladder"][segment])


def channel_at(segment: str, rung: int) -> str | None:
    """The channel for a rung, or None once the ladder is exhausted."""
    ladder = ladder_for(segment)
    return ladder[rung] if 0 <= rung < len(ladder) else None


def _viable(channel: str, root_cause: str | None) -> bool:
    """Could this channel ever work for this cause?

    Not a policy check -- `compliance/rules.py::ChannelEligibility` remains the
    guarantee. This is the ladder being polite in the ADR-005 sense: proposing a
    rung that will certainly be refused wastes it, and the ladder is the scarce
    resource. Measured: 900 of 900 channel_eligibility refusals landed on rung
    0, because every ladder opens with `retry` and roughly a third of root
    causes can never be fixed by one.
    """
    if root_cause is None or channel != RETRY:
        return True
    return root_cause not in load("policy")["channel_eligibility"]["deny_retry_for"]


def next_rung(case: dict[str, Any], root_cause: str | None = None) -> int | None:
    """The rung to try next, or None when the ladder has run out.

    `cases.rung` means "the rung to try next", and `cases.advance` moves it on
    whenever a rung is *consumed* -- whether it produced an action or was
    refused by compliance.

    An earlier version derived the rung from `attempts` instead, which counts
    only sends. A case refused by compliance therefore never climbed: it sat on
    the same rung, got re-reviewed after every cooldown, and was refused again
    until the 45-day staleness rule finally closed it. That single mistake
    produced 7,092 held reviews against 513 real actions.
    """
    ladder = ladder_for(case["segment"])
    rung = int(case["rung"])
    while rung < len(ladder):
        if _viable(ladder[rung], root_cause):
            return rung
        rung += 1
    return None


def cooldown_ms(segment: str, rung: int) -> int:
    cfg = load("policy")["escalation"]
    channel = channel_at(segment, rung)
    days = (cfg["retry_cooldown_days"] if channel == RETRY
            else cfg["cooldown_days"])
    return int(days) * MS_PER_DAY


def in_cooldown(case: dict[str, Any], *, now_ms: int) -> bool:
    review = case.get("next_review_at")
    return review is not None and int(review) > now_ms


def should_stop(
    case: dict[str, Any],
    *,
    now_ms: int,
    opted_out: bool,
    expected_value_paise: int | None = None,
    root_cause: str | None = None,
) -> Stop | None:
    """The first rule that fires wins. Order is deliberate.

    Opt-out is checked before anything else because it is the one signal that
    cannot be traded off against value -- a customer who asked not to be
    contacted is not a scoring input.
    """
    cfg = load("policy")["stopping"]

    if opted_out and cfg.get("hard_stop_on_opt_out", True):
        return Stop(
            rule="opt_out",
            reason="customer opted out of contact",
            close_state=CaseState.OPTED_OUT,
        )

    attempts = int(case["attempts"])
    max_attempts = int(cfg["max_attempts"])
    if attempts >= max_attempts:
        return Stop(
            rule="max_attempts",
            reason=f"{attempts} attempts made, limit {max_attempts}",
            observed={"attempts": attempts, "max": max_attempts},
        )

    if next_rung(case, root_cause) is None:
        return Stop(
            rule="ladder_exhausted",
            reason=f"every rung of the {case['segment']} ladder has been tried",
            observed={"ladder": ladder_for(case["segment"]),
                      "rung": int(case["rung"])},
        )

    age_days = (now_ms - int(case["opened_at"])) // MS_PER_DAY
    max_age = int(cfg["write_off_after_days"])
    if age_days >= max_age:
        return Stop(
            rule="stale",
            reason=f"case is {age_days}d old, written off after {max_age}d",
            observed={"age_days": age_days, "max_age_days": max_age},
        )

    # Asked last because it is the only rule needing a valuation, which costs a
    # database round trip the earlier rules make unnecessary.
    floor = int(cfg["min_expected_value_paise"])
    if expected_value_paise is not None and expected_value_paise < floor:
        return Stop(
            rule="not_worth_chasing",
            reason=f"expected value {expected_value_paise} paise is below the "
                   f"{floor} paise floor for continuing",
            observed={"expected_value_paise": expected_value_paise,
                      "floor_paise": floor},
        )

    return None


def expected_value(case: dict[str, Any], posterior: float) -> int:
    """What is still on the table, given the odds of the next attempt.

    Deliberately gross: the per-action costs are the arbiter's business
    (arbiter/score.py). This answers the coarser question of whether the case is
    worth another look at all.
    """
    return int(posterior * int(case["amount_paise"]))
