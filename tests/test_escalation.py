"""The ladder and the stopping rules.

Both halves matter equally. A ladder that never escalates is useless; stopping
rules that fire on healthy cases are worse than none, because they abandon
recoverable money silently. Every rule below is tested for firing *and* for
staying quiet.
"""

from __future__ import annotations

import pytest

from rcp.config import load
from rcp.escalation import (
    channel_at,
    cooldown_ms,
    expected_value,
    in_cooldown,
    ladder_for,
    next_rung,
    should_stop,
)
from rcp.schema import CaseState
from rcp.timeutil import MS_PER_DAY

NOW = 1_735_689_600_000


def case(**over):
    row = {"segment": "subscription", "rung": 0, "attempts": 0,
           "amount_paise": 150_000, "opened_at": NOW, "next_review_at": None}
    row.update(over)
    return row


# ---- the ladder ----------------------------------------------------------

@pytest.mark.parametrize("segment", ["subscription", "cart", "receivables"])
def test_every_segment_starts_on_the_cheapest_rung(segment):
    """A ladder that opened on voice would be escalation in name only."""
    ladder = ladder_for(segment)
    costs = load("scoring")["channel_cost_paise"]
    assert ladder[0] == "retry"
    assert costs[ladder[0]] == min(costs[c] for c in ladder)


@pytest.mark.parametrize("segment", ["subscription", "cart", "receivables"])
def test_ladders_never_get_cheaper_as_they_climb(segment):
    costs = load("scoring")["channel_cost_paise"]
    rungs = [costs[c] for c in ladder_for(segment)]
    assert rungs == sorted(rungs), f"{segment} ladder is not monotonic: {rungs}"


def test_rung_is_the_next_one_to_try():
    """`cases.rung` means "try this next", and advance() moves it on whenever a
    rung is consumed. Deriving it from `attempts` instead -- which counts only
    sends -- meant a case compliance refused never climbed, and the loop
    re-tried the same refused rung until staleness closed it."""
    assert next_rung(case()) == 0
    assert next_rung(case(rung=1)) == 1
    assert next_rung(case(rung=2, attempts=0)) == 2, (
        "a rung refused by compliance was still tried, so it still counts"
    )


def test_ladder_runs_out():
    assert next_rung(case(segment="cart", rung=len(ladder_for("cart")))) is None


def test_channel_at_is_bounded():
    assert channel_at("subscription", 0) == "retry"
    assert channel_at("subscription", 99) is None
    assert channel_at("subscription", -1) is None


def test_retry_cools_down_faster_than_a_message():
    """A silent rail attempt does not need the breathing room a message does."""
    assert cooldown_ms("subscription", 0) < cooldown_ms("subscription", 1)


def test_in_cooldown_reads_the_scheduled_review():
    assert in_cooldown(case(next_review_at=NOW + MS_PER_DAY), now_ms=NOW)
    assert not in_cooldown(case(next_review_at=NOW), now_ms=NOW)
    assert not in_cooldown(case(next_review_at=None), now_ms=NOW)


# ---- stopping rules ------------------------------------------------------

def test_a_healthy_case_is_not_stopped():
    """The silence case. A rule that fires here abandons recoverable money."""
    assert should_stop(case(), now_ms=NOW, opted_out=False,
                       expected_value_paise=50_000) is None


def test_opt_out_wins_over_everything():
    """Not a scoring input. A customer who asked not to be contacted is not
    traded off against value."""
    stop = should_stop(case(amount_paise=10_000_000), now_ms=NOW, opted_out=True,
                       expected_value_paise=9_000_000)
    assert stop.rule == "opt_out"
    assert stop.close_state == CaseState.OPTED_OUT.value


def test_max_attempts_fires():
    limit = int(load("policy")["stopping"]["max_attempts"])
    assert should_stop(case(attempts=limit - 1), now_ms=NOW, opted_out=False) is None

    stop = should_stop(case(attempts=limit), now_ms=NOW, opted_out=False)
    assert stop.rule == "max_attempts"
    assert stop.observed["attempts"] == limit


def test_ladder_exhaustion_fires():
    exhausted = len(ladder_for("cart"))
    assert should_stop(case(segment="cart", rung=exhausted - 1),
                       now_ms=NOW, opted_out=False) is None
    stop = should_stop(case(segment="cart", rung=exhausted),
                       now_ms=NOW, opted_out=False)
    assert stop.rule == "ladder_exhausted"


def test_stale_cases_are_written_off():
    days = int(load("policy")["stopping"]["write_off_after_days"])
    assert should_stop(case(), now_ms=NOW + (days - 1) * MS_PER_DAY,
                       opted_out=False) is None

    stop = should_stop(case(), now_ms=NOW + days * MS_PER_DAY, opted_out=False)
    assert stop.rule == "stale"
    assert stop.close_state == CaseState.WRITTEN_OFF.value


def test_value_floor_fires_and_names_the_numbers():
    floor = int(load("policy")["stopping"]["min_expected_value_paise"])
    assert should_stop(case(), now_ms=NOW, opted_out=False,
                       expected_value_paise=floor) is None

    stop = should_stop(case(), now_ms=NOW, opted_out=False,
                       expected_value_paise=floor - 1)
    assert stop.rule == "not_worth_chasing"
    assert stop.observed == {"expected_value_paise": floor - 1,
                             "floor_paise": floor}


def test_missing_valuation_does_not_stop_a_case():
    """The value rule is asked last precisely because it costs a query. A
    caller that skipped it must not accidentally get a write-off."""
    assert should_stop(case(), now_ms=NOW, opted_out=False,
                       expected_value_paise=None) is None


def test_every_stop_explains_itself():
    """Same contract as store.Suppressed and compliance.rules.Deny."""
    stops = [
        should_stop(case(), now_ms=NOW, opted_out=True),
        should_stop(case(attempts=99), now_ms=NOW, opted_out=False),
        should_stop(case(), now_ms=NOW + 999 * MS_PER_DAY, opted_out=False),
        should_stop(case(), now_ms=NOW, opted_out=False, expected_value_paise=0),
    ]
    for stop in stops:
        assert stop is not None and stop.rule and stop.reason
        assert stop.close_state in {s.value for s in CaseState}


def test_expected_value_is_gross():
    """Per-action costs are the arbiter's business; this answers the coarser
    question of whether the case is worth another look at all."""
    assert expected_value(case(amount_paise=100_000), 0.25) == 25_000
    assert expected_value(case(amount_paise=100_000), 0.0) == 0
