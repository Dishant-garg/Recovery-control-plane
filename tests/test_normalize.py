"""Decline-text classification and the Razorpay envelope contract.

The corpus test is the important one: it holds the regex table to every string
the simulator can produce. If a rule is loosened and starts swallowing a
neighbouring cause, this fails with the exact string that broke.
"""

from __future__ import annotations

import hmac
from hashlib import sha256

import pytest

from rcp.config import load
from rcp.ingest.normalize import (
    NormalizeCache,
    cache_key,
    classify,
    classify_text,
    normalize_webhook,
    payment_entity,
)
from rcp.ingest.webhook import verify_signature
from rcp.schema import RootCause
from rcp.store import canonical_json
from sim.generate import _envelope

CFG = load("sim")


def corpus():
    for cause, entries in sorted(CFG["decline_texts"].items()):
        for code, description in entries:
            for bank in CFG["banks"]:
                yield cause, code, f"{bank}: {description}"


def test_every_generated_decline_string_classifies_correctly():
    wrong = [
        (expected, classify_text(text), text)
        for expected, _, text in corpus()
        if (classify_text(text).value if classify_text(text) else "unknown") != expected
    ]
    assert not wrong, f"{len(wrong)} misclassified, first: {wrong[0]}"


def test_corpus_is_the_size_the_design_claims():
    """~200 unique strings is what makes an exact cache the right call
    instead of a vector index (ADR-006). If the corpus grows by an order of
    magnitude that reasoning needs revisiting."""
    assert len(set(text for _, _, text in corpus())) == 200


def test_unmatched_text_is_unknown_not_a_wrong_guess():
    assert classify_text("the goldfish declined politely") is None
    assert classify(
        "razorpay", "X", "the goldfish declined politely"
    ) is RootCause.UNKNOWN


def test_llm_is_consulted_only_for_misses():
    calls = []

    def fake_llm(gateway, code, description):
        calls.append(description)
        return RootCause.BANK_DOWNTIME

    cache = NormalizeCache()
    assert classify("razorpay", "X", "HDFC: Your card has expired",
                    cache=cache, llm=fake_llm) is RootCause.CARD_EXPIRED
    assert calls == [], "regex hit must not reach the LLM"

    assert classify("razorpay", "X", "inscrutable",
                    cache=cache, llm=fake_llm) is RootCause.BANK_DOWNTIME
    assert len(calls) == 1


def test_repeat_strings_hit_the_cache():
    cache = NormalizeCache()
    for _ in range(5):
        classify("razorpay", "X", "HDFC: Your card has expired", cache=cache)
    assert cache.unique_strings == 1
    assert (cache.hits, cache.misses) == (4, 1)
    assert cache.hit_rate == pytest.approx(0.8)


def test_cache_key_separates_gateway_code_and_text():
    a = cache_key("razorpay", "BAD_REQUEST_ERROR", "card expired")
    assert a != cache_key("stripe", "BAD_REQUEST_ERROR", "card expired")
    assert a != cache_key("razorpay", "GATEWAY_ERROR", "card expired")
    assert a == cache_key("razorpay", "BAD_REQUEST_ERROR", "card expired")


def sample_envelope(**kw):
    defaults = dict(
        payment_id="pay_abc123", account_id="acc_1", customer_id="cust_0001",
        segment="subscription", amount_paise=150_000,
        occurred_at_ms=1_735_689_600_000 + 6 * 86_400_000,
        error_code="BAD_REQUEST_ERROR",
        error_description="HDFC: Your card has insufficient balance",
        retry_index=1, method="card",
    )
    return _envelope(**{**defaults, **kw})


def test_normalize_flattens_to_the_payment_entity():
    """events.payload must hold the entity, not the envelope -- the
    gateway_code generated column reads $.error_code from the top level."""
    envelope = sample_envelope()
    row = normalize_webhook(envelope, payday_dom=5)

    assert row["payload"] == canonical_json(payment_entity(envelope))
    assert row["root_cause"] == "insufficient_funds"
    assert row["amount_paise"] == 150_000
    assert row["retry_index"] == 1
    assert row["days_from_payday"] == 2  # failed on the 7th, payday the 5th


def test_normalize_is_deterministic():
    a = normalize_webhook(sample_envelope(), payday_dom=5)
    b = normalize_webhook(sample_envelope(), payday_dom=5)
    assert a == b


def test_non_payment_envelope_is_rejected():
    with pytest.raises(ValueError, match="not a payment webhook"):
        payment_entity({"event": "refund.created", "payload": {}})


def test_generated_envelopes_carry_a_valid_signature():
    """The sim signs what it sends; if this breaks, the raw-bytes contract in
    webhook.py has been broken too."""
    raw = canonical_json(sample_envelope()).encode()
    secret = CFG["webhook_secret"]
    assert verify_signature(
        raw, hmac.new(secret.encode(), raw, sha256).hexdigest(), secret
    )
