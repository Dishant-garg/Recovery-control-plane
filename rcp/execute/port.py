"""Executor protocol -- the seam between decisions and the outside world.

Razorpay sits behind this and nowhere else. The decision path (proposers ->
arbiter -> compliance) never imports an executor, never knows one exists, and
never makes a network call. That is what keeps `make eval` reproducible,
offline, and free: the default executor is the simulated one, and live
execution is opt-in via `--executor razorpay --limit 5`.

See ADR-001 (proposers never execute) and ADR-003 (outbox and idempotency).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel


class ExecResult(BaseModel):
    """Outcome of one delivery attempt. Never raises past the relay."""

    ok: bool
    provider_ref: str | None = None
    error: str | None = None
    raw: dict[str, Any] = {}

    @property
    def retryable(self) -> bool:
        """Network and 5xx failures are worth another attempt; a rejected
        payload is not, and retrying it just burns the contact budget."""
        return not self.ok and self.error is not None and self.error.startswith(
            ("network:", "timeout:", "5")
        )


@runtime_checkable
class Executor(Protocol):
    """Implementations: simulated.py (default), razorpay_rest.py.

    `action` is a row from the actions table. Implementations MUST treat
    `action["idempotency_key"]` as the deduplication key when the provider
    supports one, but must not rely on the provider for exactly-once -- the
    UNIQUE constraint on actions.idempotency_key is the real guarantee.
    """

    name: str

    def execute(self, action: dict[str, Any]) -> ExecResult: ...
