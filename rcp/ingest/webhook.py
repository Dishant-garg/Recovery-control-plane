"""Webhook intake: HMAC verification, then dedup by database constraint.

Deliberately framework-agnostic -- these functions take raw bytes and a dict, so
they are testable without standing up a server, and the FastAPI route is a thin
wrapper.

The one rule that matters here: verify the signature against the RAW request
body, never against a re-serialized dict. `json.loads` followed by `json.dumps`
reorders keys and changes whitespace, so the HMAC will not match and, worse,
someone will eventually "fix" it by loosening the check. In FastAPI that means
`await request.body()`, not `await request.json()`.
"""

from __future__ import annotations

import hmac
import sqlite3
from hashlib import sha256
from typing import Any

from rcp.store import canonical_json


class SignatureError(Exception):
    """Raised on a missing or invalid signature. Callers should return 400."""


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    """Razorpay signs the raw body with HMAC-SHA256 and sends it as
    `X-Razorpay-Signature`.

    Compared with `compare_digest` so the comparison is constant-time -- a naive
    `==` leaks the correct prefix through timing.
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), raw_body, sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def require_signature(raw_body: bytes, signature: str | None, secret: str) -> None:
    if not verify_signature(raw_body, signature, secret):
        raise SignatureError("invalid or missing webhook signature")


def ingest_event(
    conn: sqlite3.Connection,
    *,
    provider: str,
    provider_event_id: str,
    normalized: dict[str, Any],
) -> str | None:
    """Insert a normalized event, or return None if this delivery was a replay.

    Razorpay retries on any non-2xx response, so duplicate deliveries are
    expected rather than exceptional. `UNIQUE (provider, provider_event_id)`
    handles it -- there is no application-level dedup logic to get wrong, and
    no dedup cache to fall out of sync.

    Must be called inside an open transaction.
    """
    params = dict(normalized)
    params.update(provider=provider, provider_event_id=provider_event_id)
    params.setdefault("payload", canonical_json({}))

    row = conn.execute(
        """
        INSERT INTO events (id, provider, provider_event_id, customer_id, segment,
                            occurred_at, amount_paise, currency, root_cause,
                            retry_index, days_from_payday, payload)
        VALUES (:id, :provider, :provider_event_id, :customer_id, :segment,
                :occurred_at, :amount_paise, :currency, :root_cause,
                :retry_index, :days_from_payday, :payload)
        ON CONFLICT (provider, provider_event_id) DO NOTHING
        RETURNING id
        """,
        params,
    ).fetchone()
    return None if row is None else row["id"]
