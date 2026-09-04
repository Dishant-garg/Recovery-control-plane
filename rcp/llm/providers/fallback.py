"""The zero-network adapter, and the default.

It never completes anything. `LLMClient.run_agent` short-circuits to the agent's
own deterministic routine before the loop starts -- this class exists so
provider selection has something to return and so `adapter.name` reads
`"fallback"` everywhere.

Keeping this the default is a deliberate constraint: no part of the pipeline can
quietly grow a hard dependency on an API key without the failure being immediate
and obvious.
"""

from __future__ import annotations

from typing import Any

from rcp.llm.adapter import ToolOutcome, Turn


class FallbackAdapter:
    name = "fallback"
    model = "deterministic"
    max_tokens = 0

    def _unreachable(self) -> Any:
        raise RuntimeError(
            "FallbackAdapter should never be reached -- LLMClient routes to the "
            "agent's deterministic routine instead."
        )

    def complete(self, *, system: str, messages: list[Any],
                 tools: list[Any], max_tokens: int) -> Turn:
        return self._unreachable()

    def encode_tools(self, tools: list[Any]) -> list[Any]:
        return self._unreachable()

    def user_message(self, text: str) -> Any:
        return self._unreachable()

    def assistant_message(self, turn: Turn) -> Any:
        return self._unreachable()

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[Any]:
        return self._unreachable()
