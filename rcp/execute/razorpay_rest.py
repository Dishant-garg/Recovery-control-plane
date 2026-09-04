"""Razorpay Payment Links executor.

One class, one method, satisfying the `Executor` protocol. Retry, crash safety,
and exactly-once are already handled by `execute/outbox.py` and the
`UNIQUE(idempotency_key)` constraint (ADR-003) -- this only has to make the
call and report what happened.

**Razorpay's `reference_id` is not an idempotency key.** The docs are explicit
that it is a tracking identifier and must be unique per link; sending the same
one twice creates two links or errors, it does not deduplicate. That is exactly
why ADR-003 puts the guarantee in our own schema rather than trusting the
provider. `reference_id` is still sent, truncated to the documented 40-character
limit, because it makes a live link traceable back to the action that created
it.

Fixtures: the first live call for a given request shape is recorded into
`data/fixtures/`, and replayed thereafter. Tests and CI never touch the network;
the demo runs live with `--executor razorpay`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from rcp.execute.port import ExecResult
from rcp.store import REPO_ROOT, canonical_json

API_URL = "https://api.razorpay.com/v1/payment_links"
FIXTURES = REPO_ROOT / "data" / "fixtures"

# A payment link is a thing you send someone. There is no link to send for a
# silent rail retry -- that is the Subscriptions/Orders surface, and pretending
# otherwise would quietly turn retries into messages.
LINK_CHANNELS = {"sms", "whatsapp", "email"}

REFERENCE_ID_MAX = 40  # documented limit


def _reference_id(idempotency_key: str) -> str:
    """Traceable and inside Razorpay's 40-character limit.

    Our keys look like `cust_0001:evt_9f2a...:sms:w_0012` and run past 40, so
    long ones are hashed rather than truncated -- a truncated key can collide
    with another action's, and `reference_id` has to stay unique per link.
    """
    if len(idempotency_key) <= REFERENCE_ID_MAX:
        return idempotency_key
    digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:32]
    return f"rcp_{digest}"


class RazorpayRestExecutor:
    name = "razorpay_rest"

    def __init__(
        self,
        key_id: str | None = None,
        key_secret: str | None = None,
        *,
        fixtures: Path | None = None,
        record: bool = True,
        live: bool = True,
    ) -> None:
        self.key_id = key_id or os.environ.get("RAZORPAY_KEY_ID", "")
        self.key_secret = key_secret or os.environ.get("RAZORPAY_KEY_SECRET", "")
        self.fixtures = Path(fixtures or FIXTURES)
        self.record = record
        self.live = live

        if self.live and self.key_id and not self.key_id.startswith("rzp_test_"):
            # A live key here would move real money. Nothing in this project is
            # ready for that, and the failure should be loud.
            raise RuntimeError(
                f"refusing to run with a non-test Razorpay key ({self.key_id[:12]}...); "
                f"this executor is test-mode only"
            )

    # ---- fixtures --------------------------------------------------------

    def _fixture_path(self, payload: dict[str, Any]) -> Path:
        key = hashlib.sha256(canonical_json(payload).encode()).hexdigest()[:16]
        return self.fixtures / f"payment_link_{key}.json"

    def _replay(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        path = self._fixture_path(payload)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def _record(self, payload: dict[str, Any], response: dict[str, Any]) -> None:
        if not self.record:
            return
        path = self._fixture_path(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(response) + "\n")

    # ---- the call --------------------------------------------------------

    def _payload(self, action: dict[str, Any]) -> dict[str, Any]:
        body = json.loads(action.get("body") or "{}")
        amount = int(body.get("amount_paise") or 0)
        return {
            "amount": amount,
            "currency": "INR",
            "accept_partial": False,
            "description": f"Payment retry: {body.get('root_cause', 'payment failed')}",
            "reference_id": _reference_id(action["idempotency_key"]),
            "notify": {"sms": action["channel"] == "sms",
                       "email": action["channel"] == "email"},
            "reminder_enable": False,
            "notes": {
                "rcp_action_id": action["id"],
                "rcp_channel": action["channel"],
                "rcp_root_cause": str(body.get("root_cause", "")),
            },
        }

    def execute(self, action: dict[str, Any]) -> ExecResult:
        if action["channel"] not in LINK_CHANNELS:
            return ExecResult(
                ok=False,
                error=f"razorpay_rest handles {sorted(LINK_CHANNELS)}; "
                      f"'{action['channel']}' needs the Subscriptions API",
            )

        payload = self._payload(action)
        if payload["amount"] <= 0:
            return ExecResult(ok=False, error="amount must be positive")

        cached = self._replay(payload)
        if cached is not None:
            return ExecResult(ok=True, provider_ref=cached.get("id"),
                              raw={**cached, "replayed": True})

        if not self.live:
            return ExecResult(
                ok=False,
                error="no fixture for this request and live calls are disabled",
            )
        if not (self.key_id and self.key_secret):
            return ExecResult(
                ok=False,
                error="RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set",
            )

        try:
            import httpx
        except ImportError:
            return ExecResult(ok=False, error="pip install httpx")

        token = base64.b64encode(
            f"{self.key_id}:{self.key_secret}".encode()
        ).decode()
        try:
            response = httpx.post(
                API_URL, json=payload, timeout=20.0,
                headers={"Authorization": f"Basic {token}",
                         "Content-Type": "application/json"},
            )
        except Exception as exc:
            # network: prefix marks it retryable to outbox.ExecResult.retryable
            return ExecResult(ok=False, error=f"network: {type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            return ExecResult(
                ok=False,
                error=f"{response.status_code}: {response.text[:200]}",
                raw={"status_code": response.status_code},
            )

        body = response.json()
        self._record(payload, body)
        return ExecResult(ok=True, provider_ref=body.get("id"), raw=body)
