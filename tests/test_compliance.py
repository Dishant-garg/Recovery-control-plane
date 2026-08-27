"""Compliance rules: allow, modify, deny -- and the trail each leaves behind."""

from __future__ import annotations

import json

import pytest

from rcp.compliance.engine import evaluate, policy_version
from rcp.compliance.rules import (
    ActivePromise,
    ChannelEligibility,
    ChannelSubCap,
    Consent,
    Deny,
    IncentiveCeiling,
    Modify,
    OptOut,
    QuietHours,
    RuleContext,
)
from rcp.config import load
from rcp.store import canonical_json, content_id, write_txn
from rcp.timeutil import IST_OFFSET_MS, MS_PER_DAY, MS_PER_HOUR, local_hour
from tests.conftest import action_row, make_chain, make_customer, make_event

JAN_1 = 1_735_689_600_000


def ctx(conn, *, channel="sms", scheduled_at=None, incentive=0,
        root_cause="insufficient_funds", consent=None, opted_out=0, amount=150_000):
    # ctx() is called several times per test; seed the rows only once.
    # ctx() is called several times per test, and `events` is append-only, so
    # each distinct (cause, amount) gets its own row rather than being mutated.
    eid = content_id("evt", root_cause, amount)
    if not conn.execute("SELECT 1 FROM events WHERE id=?", (eid,)).fetchone():
        with write_txn(conn):
            make_customer(conn)
            make_event(conn, eid, provider_event_id=f"rzp_{eid}",
                       root_cause=root_cause, amount_paise=amount,
                       payload=canonical_json({}))
    customer = dict(conn.execute(
        "SELECT * FROM customers WHERE id='cust_1'").fetchone())
    customer["consent"] = canonical_json(consent or {})
    customer["opted_out"] = opted_out
    event = dict(conn.execute("SELECT * FROM events WHERE id=?", (eid,)).fetchone())

    now = JAN_1 + 10 * MS_PER_DAY
    proposal = {
        "id": "prop_1", "proposer_id": "test", "channel": channel,
        "scheduled_at": now if scheduled_at is None else scheduled_at,
        "incentive_paise": incentive, "claimed_success_prob": 0.4,
        "claimed_value_paise": amount, "rationale": "t", "payload": "{}",
    }
    return RuleContext(proposal=proposal, event=event, customer=customer,
                       now_ms=now, conn=conn)


# ---- individual rules ----------------------------------------------------

def test_opt_out_is_absolute(conn):
    assert isinstance(OptOut().check(ctx(conn, opted_out=1)), Deny)
    assert OptOut().check(ctx(conn, opted_out=0)).ok


def test_consent_absent_is_a_denial_not_a_default_yes(conn):
    """The failure mode that matters: treating 'no record' as permission."""
    result = Consent().check(ctx(conn, channel="whatsapp", consent={}))
    assert isinstance(result, Deny)
    assert "no recorded opt-in" in result.note

    assert Consent().check(
        ctx(conn, channel="whatsapp", consent={"whatsapp": True})).ok
    assert isinstance(
        Consent().check(ctx(conn, channel="whatsapp", consent={"whatsapp": False})),
        Deny)


def test_channels_not_requiring_opt_in_pass_without_consent(conn):
    for channel in ("sms", "email", "retry"):
        assert Consent().check(ctx(conn, channel=channel, consent={})).ok


@pytest.mark.parametrize("cause", ["mandate_expired", "card_expired", "invalid_account"])
def test_retry_is_denied_where_it_cannot_possibly_work(conn, cause):
    assert isinstance(
        ChannelEligibility().check(ctx(conn, channel="retry", root_cause=cause)), Deny)


def test_retry_is_allowed_for_causes_it_can_fix(conn):
    for cause in ("insufficient_funds", "bank_downtime"):
        assert ChannelEligibility().check(
            ctx(conn, channel="retry", root_cause=cause)).ok


# ---- quiet hours ---------------------------------------------------------

def at_local_hour(hour: int) -> int:
    """A UTC instant that is `hour` o'clock in IST."""
    return JAN_1 + 10 * MS_PER_DAY + hour * MS_PER_HOUR - IST_OFFSET_MS


@pytest.mark.parametrize("hour", [21, 23, 0, 3, 8])
def test_quiet_hours_shifts_overnight_contacts(conn, hour):
    result = QuietHours().check(ctx(conn, channel="sms",
                                    scheduled_at=at_local_hour(hour)))
    assert isinstance(result, Modify)
    shifted = result.changes["scheduled_at"]
    assert local_hour(shifted) == 9, "must land exactly at the window opening"
    assert shifted > at_local_hour(hour), "never shift backwards"


@pytest.mark.parametrize("hour", [9, 12, 17, 20])
def test_daytime_contacts_are_untouched(conn, hour):
    assert QuietHours().check(
        ctx(conn, channel="sms", scheduled_at=at_local_hour(hour))).ok


def test_quiet_hours_uses_local_time_not_utc(conn):
    """22:00 IST is 16:30 UTC. A rule reading the UTC hour would wave it through."""
    scheduled = at_local_hour(22)
    from rcp.timeutil import hour_of_day
    assert hour_of_day(scheduled) == 16, "sanity: this is daytime in UTC"
    assert isinstance(QuietHours().check(
        ctx(conn, channel="sms", scheduled_at=scheduled)), Modify)


def test_exempt_channels_are_never_shifted(conn):
    for channel in load("policy")["quiet_hours"]["exempt_channels"]:
        assert QuietHours().check(
            ctx(conn, channel=channel, scheduled_at=at_local_hour(2))).ok


# ---- caps, promises, incentives -----------------------------------------

def test_voice_sub_cap_denies_the_second_call(conn):
    make_chain(conn)   # provides cust_1 + a decision for the action FK
    cap = load("policy")["channel_sub_caps"]["max"]["voice"]
    now = JAN_1 + 10 * MS_PER_DAY

    context = ctx(conn, channel="voice")
    assert ChannelSubCap().check(context).ok

    with write_txn(conn):
        for i in range(cap):
            conn.execute(
                "INSERT INTO actions VALUES (:id,:decision_id,:customer_id,"
                ":idempotency_key,:channel,:status,:scheduled_at,:sent_at,"
                ":attempts,:provider_ref,:body,:created_at)",
                action_row(f"v{i}", channel="voice", status="sent",
                           scheduled_at=now - MS_PER_DAY, sent_at=now - MS_PER_DAY),
            )
    result = ChannelSubCap().check(ctx(conn, channel="voice"))
    assert isinstance(result, Deny)
    assert result.observed["cap"] == cap


def test_active_promise_silences_contact(conn):
    from rcp.compliance.promise import create
    make_chain(conn)   # provides cust_1 + evt_1 for the promise FK
    now = JAN_1 + 10 * MS_PER_DAY

    assert ActivePromise().check(ctx(conn, channel="sms")).ok
    with write_txn(conn):
        create(conn, customer_id="cust_1", event_id="evt_1", amount_paise=1000,
               due_at=now + 5 * MS_PER_DAY, now_ms=now, state="accepted")

    assert isinstance(ActivePromise().check(ctx(conn, channel="sms")), Deny)


def test_incentive_is_clamped_not_refused(conn):
    """A too-generous discount is a reason to offer less, not to give up."""
    cfg = load("policy")["incentive"]
    amount = 1_000_00
    result = IncentiveCeiling().check(
        ctx(conn, incentive=amount, amount=amount))

    assert isinstance(result, Modify)
    clamped = result.changes["incentive_paise"]
    assert clamped <= cfg["max_paise"]
    assert clamped <= amount * cfg["max_pct_of_amount"] / 100


def test_modest_incentive_passes_untouched(conn):
    assert IncentiveCeiling().check(ctx(conn, incentive=100, amount=1_000_00)).ok


# ---- the engine ----------------------------------------------------------

def test_engine_records_rules_that_passed_too(conn):
    """A log that only records refusals cannot tell an approval from a rule
    that never ran."""
    verdict = evaluate(conn, ctx(conn).proposal,
                       ctx(conn).event, ctx(conn).customer, now_ms=JAN_1)
    assert verdict.allowed
    assert len(verdict.trail) >= 5
    assert all("rule" in e and "note" in e for e in verdict.trail)


def test_engine_short_circuits_on_the_first_denial(conn):
    context = ctx(conn, opted_out=1)
    verdict = evaluate(conn, context.proposal, context.event, context.customer,
                       now_ms=context.now_ms)
    assert not verdict.allowed
    assert verdict.denied_by == "opt_out"
    assert len(verdict.trail) == 1, "must not keep evaluating after a denial"
    assert "denied by opt_out" in verdict.explain()


def test_modifications_compose(conn):
    """A quiet-hours shift and an incentive clamp can both apply at once."""
    context = ctx(conn, channel="sms", scheduled_at=at_local_hour(23),
                  incentive=1_000_00, amount=1_000_00)
    verdict = evaluate(conn, context.proposal, context.event, context.customer,
                       now_ms=context.now_ms)

    assert verdict.allowed and verdict.modified
    assert local_hour(verdict.proposal["scheduled_at"]) == 9
    assert verdict.proposal["incentive_paise"] < 1_000_00
    assert {m["rule"] for m in verdict.modifications} == {
        "quiet_hours", "incentive_ceiling"}


def test_policy_version_comes_from_the_policy_file(conn):
    assert policy_version() == load("policy")["version"]
    assert policy_version() != load("scoring")["version"]
