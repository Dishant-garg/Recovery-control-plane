"""Anthropic adapter.

Translation only -- the tool-use loop lives in `LLMClient`. What is specific to
this vendor:

  * tools declare their schema under `input_schema`
  * the assistant turn is a list of content blocks, echoed back verbatim so
    thinking blocks survive the round trip
  * every tool result for one assistant turn goes back in ONE user message.
    Splitting them across messages teaches the model to stop calling tools in
    parallel, which is a silent and expensive regression.

Caching: the system prompt and tool schemas are the stable prefix and carry the
breakpoint. Everything volatile (the task, tool results) comes after, so the
prefix survives across turns and across agents sharing a system prompt.
"""

from __future__ import annotations

from typing import Any

from rcp.llm.adapter import (
    STOP_END,
    STOP_LENGTH,
    STOP_REFUSAL,
    STOP_TOOL_USE,
    ToolCall,
    ToolOutcome,
    Turn,
)
from rcp.llm.client import json_dumps

# Opus for analysis that has to be right. Bulk classification should pass a
# cheaper model explicitly rather than this being lowered for everyone.
DEFAULT_MODEL = "claude-opus-5"

# Comfortable here: Opus supports far more, and `max_tokens` is a response cap
# rather than something billed against a per-minute budget.
DEFAULT_MAX_TOKENS = 16000

STOP_MAP = {
    "end_turn": STOP_END,
    "stop_sequence": STOP_END,
    "tool_use": STOP_TOOL_USE,
    "max_tokens": STOP_LENGTH,
    "refusal": STOP_REFUSAL,
    "pause_turn": STOP_TOOL_USE,
}


class AnthropicAdapter:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high",
                 max_tokens: int = DEFAULT_MAX_TOKENS) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                "RCP_LLM=anthropic needs the SDK: pip install anthropic"
            ) from exc
        # A bare client also resolves an `ant auth login` profile, so an unset
        # ANTHROPIC_API_KEY is not necessarily an error. Let the SDK decide.
        self.client = anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens

    def encode_tools(self, tools: list[Any]) -> list[Any]:
        return [t.to_schema() for t in tools]

    def user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def assistant_message(self, turn: Turn) -> Any:
        return {"role": "assistant", "content": turn.raw}

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[Any]:
        return [{
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": o.call.id,
                    "content": json_dumps(o.content),
                    "is_error": o.is_error,
                }
                for o in outcomes
            ],
        }]

    def complete(self, *, system: str, messages: list[Any],
                 tools: list[Any], max_tokens: int) -> Turn:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}],
            tools=tools,
            messages=messages,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )
        return Turn(
            text="\n".join(b.text for b in response.content if b.type == "text"),
            tool_calls=[
                ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
                for b in response.content if b.type == "tool_use"
            ],
            stop_reason=STOP_MAP.get(response.stop_reason, STOP_END),
            usage={
                k: getattr(response.usage, k, 0) or 0
                for k in ("input_tokens", "output_tokens",
                          "cache_read_input_tokens", "cache_creation_input_tokens")
            },
            raw=response.content,
        )
