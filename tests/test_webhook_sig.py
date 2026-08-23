"""Webhook HMAC verification and replay dedup."""

from __future__ import annotations

import hmac
import json
from hashlib import sha256

import pytest

from rcp.ingest.webhook import (
    SignatureError,
    ingest_event,
    require_signature,
    verify_signature,
)
from rcp.store import canonical_json, write_txn
from tests.conftest import make_customer

SECRET = "whsec_test_abc123"
BODY = b'{"event":"payment.failed","payload":{"payment":{"entity":{"id":"pay_1"}}}}'


def sign(body: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body, sha256).hexdigest()


def test_valid_signature_passes():
    assert verify_signature(BODY, sign(BODY), SECRET)


def test_tampered_payload_is_rejected():
    tampered = BODY.replace(b"pay_1", b"pay_9")
    assert not verify_signature(tampered, sign(BODY), SECRET)
    with pytest.raises(SignatureError):
        require_signature(tampered, sign(BODY), SECRET)


def test_missing_signature_is_rejected():
    assert not verify_signature(BODY, None, SECRET)
    assert not verify_signature(BODY, "", SECRET)


def test_wrong_secret_is_rejected():
    assert not verify_signature(BODY, sign(BODY, "whsec_wrong"), SECRET)


def test_reserialized_body_does_not_verify():
    """The reason verification must use raw bytes.

    Round-tripping through json reorders keys and drops whitespace, so the HMAC
    no longer matches. This test exists so that if someone switches the FastAPI
    route from `await request.body()` to `await request.json()`, it fails here
    with an explanation rather than in production with a 401 storm.
    """
    signature = sign(BODY)
    reserialized = json.dumps(json.loads(BODY), sort_keys=True).encode()
    assert reserialized != BODY
    assert not verify_signature(reserialized, signature, SECRET)


def test_replayed_delivery_is_deduped_by_the_database(conn):
    """Razorpay retries on any non-2xx, so duplicate deliveries are routine."""
    normalized = {
        "id": "evt_1", "customer_id": "cust_1", "segment": "subscription",
        "occurred_at": 1000, "amount_paise": 150_000, "currency": "INR",
        "root_cause": "insufficient_funds", "retry_index": 0,
        "days_from_payday": -2,
        "payload": canonical_json({"error_code": "BAD_REQUEST_ERROR"}),
    }

    with write_txn(conn):
        make_customer(conn)
        first = ingest_event(
            conn, provider="razorpay", provider_event_id="rzp_evt_1",
            normalized=normalized,
        )
    with write_txn(conn):
        second = ingest_event(
            conn, provider="razorpay", provider_event_id="rzp_evt_1",
            normalized=dict(normalized, id="evt_2"),
        )

    assert first == "evt_1"
    assert second is None, "replayed delivery must be a no-op"
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1
