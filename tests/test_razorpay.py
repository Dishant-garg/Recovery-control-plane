"""The Razorpay executor: guards, fixtures, and what it refuses to do.

The most important tests here are the refusals. This is the only component that
can move real money, and every guard in it exists because the alternative is a
live charge nobody intended.
"""

from __future__ import annotations

import json

import pytest

from rcp.execute.razorpay_rest import (
    LINK_CHANNELS,
    REFERENCE_ID_MAX,
    RazorpayRestExecutor,
    _reference_id,
)
from rcp.store import canonical_json


def action(**over):
    row = {
        "id": "act_abc123", "idempotency_key": "cust_0001:evt_1:sms:w_0001",
        "channel": "sms", "customer_id": "cust_0001",
        "body": canonical_json({"amount_paise": 150_000,
                                "root_cause": "insufficient_funds"}),
    }
    row.update(over)
    return row


def offline(tmp_path, **kw):
    return RazorpayRestExecutor(key_id="rzp_test_x", key_secret="s",
                                fixtures=tmp_path, live=False, **kw)


# ---- guards --------------------------------------------------------------

def test_a_live_key_is_refused_outright():
    """The one component that can move real money. A live key is not a
    configuration choice here, it is a mistake."""
    with pytest.raises(RuntimeError, match="non-test Razorpay key"):
        RazorpayRestExecutor(key_id="rzp_live_realmoney", key_secret="s")


def test_test_keys_are_fine():
    RazorpayRestExecutor(key_id="rzp_test_abc", key_secret="s", live=False)


def test_retry_channel_is_refused_not_silently_messaged(tmp_path):
    """There is no payment link to send for a silent rail retry. Quietly
    turning retries into messages would break the whole channel-cost model."""
    result = offline(tmp_path).execute(action(channel="retry"))
    assert not result.ok
    assert "Subscriptions API" in result.error


@pytest.mark.parametrize("channel", sorted(LINK_CHANNELS))
def test_link_channels_are_accepted(tmp_path, channel):
    result = offline(tmp_path).execute(action(channel=channel))
    # No fixture and live disabled -- but it got past the channel guard.
    assert "no fixture" in result.error


def test_zero_amount_is_refused(tmp_path):
    result = offline(tmp_path).execute(
        action(body=canonical_json({"amount_paise": 0})))
    assert not result.ok and "positive" in result.error


def test_missing_credentials_fail_clearly(tmp_path, monkeypatch):
    monkeypatch.delenv("RAZORPAY_KEY_ID", raising=False)
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)
    ex = RazorpayRestExecutor(key_id="", key_secret="", fixtures=tmp_path)
    assert "not set" in ex.execute(action()).error


# ---- reference_id --------------------------------------------------------

def test_short_keys_pass_through():
    assert _reference_id("short:key") == "short:key"


def test_long_keys_are_hashed_not_truncated():
    """Razorpay caps reference_id at 40 characters and requires it to be
    unique. Truncating two long keys with a shared prefix would collide."""
    a = "cust_0001:evt_" + "a" * 40 + ":sms:w_0001"
    b = "cust_0001:evt_" + "a" * 40 + ":sms:w_0002"
    assert len(a) > REFERENCE_ID_MAX

    ref_a, ref_b = _reference_id(a), _reference_id(b)
    assert len(ref_a) <= REFERENCE_ID_MAX
    assert ref_a != ref_b, "truncation would have collided here"


def test_reference_id_is_stable():
    key = "cust_0001:evt_" + "z" * 40
    assert _reference_id(key) == _reference_id(key)


# ---- fixtures ------------------------------------------------------------

def test_a_recorded_response_is_replayed_without_network(tmp_path):
    ex = offline(tmp_path)
    payload = ex._payload(action())
    ex.record = True
    ex._record(payload, {"id": "plink_recorded", "status": "created",
                         "short_url": "https://rzp.io/i/abc"})

    result = ex.execute(action())
    assert result.ok
    assert result.provider_ref == "plink_recorded"
    assert result.raw["replayed"] is True


def test_different_actions_do_not_share_a_fixture(tmp_path):
    ex = offline(tmp_path)
    ex.record = True
    ex._record(ex._payload(action()), {"id": "plink_one"})

    other = action(idempotency_key="cust_0002:evt_2:sms:w_0001", id="act_two")
    assert "no fixture" in ex.execute(other).error


def test_payload_carries_traceability_back_to_the_action(tmp_path):
    payload = offline(tmp_path)._payload(action())
    assert payload["amount"] == 150_000
    assert payload["currency"] == "INR"
    assert payload["notes"]["rcp_action_id"] == "act_abc123"
    assert payload["reference_id"] == "cust_0001:evt_1:sms:w_0001"
    assert payload["accept_partial"] is False


def test_notify_matches_the_channel(tmp_path):
    ex = offline(tmp_path)
    assert ex._payload(action(channel="sms"))["notify"] == {"sms": True, "email": False}
    assert ex._payload(action(channel="email"))["notify"] == {"sms": False, "email": True}


# ---- the contract with the outbox ---------------------------------------

def test_it_satisfies_the_executor_protocol():
    from rcp.execute.port import Executor
    assert isinstance(RazorpayRestExecutor(key_id="rzp_test_x", key_secret="s",
                                           live=False), Executor)


def test_network_errors_are_marked_retryable(tmp_path):
    """outbox.py retries on a `network:` prefix and gives up otherwise."""
    from rcp.execute.port import ExecResult
    assert ExecResult(ok=False, error="network: ConnectError").retryable
    assert not ExecResult(ok=False, error="400: bad amount").retryable
