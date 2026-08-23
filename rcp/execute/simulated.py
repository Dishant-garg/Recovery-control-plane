"""Default executor. Zero network, fully deterministic.

Delivery success is derived from the action's idempotency_key rather than from
a random draw, so replaying a seed reproduces the same delivery failures. This
is a transport-level simulation only -- whether the customer actually pays is
the outcome model's business (sim/outcomes.py), and lives in truth.db.
"""

from __future__ import annotations

import hashlib
from typing import Any

from rcp.execute.port import ExecResult

# Roughly 2% of sends fail at the transport layer, which is enough to keep the
# outbox retry path exercised in every run.
FAILURE_RATE = 0.02


class SimulatedExecutor:
    name = "simulated"

    def __init__(self, failure_rate: float = FAILURE_RATE) -> None:
        self.failure_rate = failure_rate

    def execute(self, action: dict[str, Any]) -> ExecResult:
        digest = hashlib.sha256(action["idempotency_key"].encode()).digest()
        draw = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
        if draw < self.failure_rate:
            return ExecResult(ok=False, error="network: simulated transport failure")
        return ExecResult(
            ok=True,
            provider_ref=f"sim_{digest.hex()[:14]}",
            raw={"channel": action["channel"], "simulated": True},
        )
