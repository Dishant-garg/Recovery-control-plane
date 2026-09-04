"""One adapter for every vendor that speaks the OpenAI wire format.

Covers Groq, Gemini (through its OpenAI-compatible endpoint), OpenAI, Together,
Fireworks, vLLM, and Ollama. They differ only in base URL, key env var, and
model id -- all three are rows in `client.OPENAI_COMPATIBLE`, so adding one is
config rather than code.

Gemini goes through here rather than through a native adapter on purpose: the
OpenAI-compatible surface is stable and well understood, while the native
`google-genai` tool-calling shape moves. A native Gemini adapter is ~80 lines
against the seam in `rcp/llm/adapter.py` if the compatibility layer ever proves
too limiting.

Three differences from Anthropic worth naming, because each one is a bug if you
carry the other vendor's habits across:

  * tools nest under `{"type": "function", "function": {...}}`, and the schema
    key is `parameters`, not `input_schema`
  * tool arguments arrive as a JSON **string**, not a parsed object
  * each result is its own `role: "tool"` message -- unlike Anthropic, which
    wants them batched into one user message
"""

from __future__ import annotations

import json
import os
from typing import Any

from rcp.llm.adapter import (
    STOP_END,
    STOP_LENGTH,
    STOP_TOOL_USE,
    ToolCall,
    ToolOutcome,
    Turn,
)
from rcp.llm.client import json_dumps

# Modest on purpose. Groq's free tier counts `max_tokens` against an 8k
# tokens-per-minute budget, so a large ceiling 413s before the model is even
# reached. Raise it per-vendor once you know the tier you are on.
DEFAULT_MAX_TOKENS = 4096

STOP_MAP = {
    "stop": STOP_END,
    "tool_calls": STOP_TOOL_USE,
    "function_call": STOP_TOOL_USE,
    "length": STOP_LENGTH,
    "content_filter": STOP_END,
}


class OpenAICompatAdapter:
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key_env: str,
        model: str,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - depends on install
            raise RuntimeError(
                f"RCP_LLM={name} needs the OpenAI-compatible client: "
                f"pip install openai"
            ) from exc

        api_key = os.environ.get(api_key_env)
        if not api_key:
            # Local servers accept anything; hosted ones will 401 with a clear
            # message, which beats a confusing None-key traceback.
            api_key = "not-needed" if "localhost" in base_url else None
        if api_key is None:
            raise RuntimeError(f"RCP_LLM={name} requires {api_key_env} to be set")

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.name = name
        self.model = model
        self.max_tokens = max_tokens

    def encode_tools(self, tools: list[Any]) -> list[Any]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                },
            }
            for t in tools
        ]

    def user_message(self, text: str) -> Any:
        return {"role": "user", "content": text}

    def assistant_message(self, turn: Turn) -> Any:
        """Echo back only the fields every vendor accepts.

        `model_dump()` on the SDK's message object carries whatever the server
        sent -- `annotations`, `refusal`, `audio`, `function_call` -- and Groq
        rejects an assistant turn containing them with a 400 naming the field.
        The portable subset is role, content, and tool_calls, so that is what
        goes back. Tool-call ids are preserved exactly, which is the only part
        the next turn actually depends on.
        """
        raw = turn.raw if isinstance(turn.raw, dict) else {}
        message: dict[str, Any] = {"role": "assistant",
                                   "content": raw.get("content") or turn.text or ""}
        tool_calls = raw.get("tool_calls")
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    },
                }
                for call in tool_calls
            ]
        return message

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[Any]:
        return [
            {
                "role": "tool",
                "tool_call_id": o.call.id,
                "content": json_dumps(o.content),
            }
            for o in outcomes
        ]

    def complete(self, *, system: str, messages: list[Any],
                 tools: list[Any], max_tokens: int) -> Turn:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, *messages],
            tools=tools or None,
        )
        choice = response.choices[0]
        message = choice.message
        usage = getattr(response, "usage", None)

        return Turn(
            text=message.content or "",
            tool_calls=[
                ToolCall(
                    id=call.id,
                    name=call.function.name,
                    arguments=_parse_arguments(call.function.arguments),
                )
                for call in (message.tool_calls or [])
            ],
            stop_reason=STOP_MAP.get(choice.finish_reason, STOP_END),
            usage={
                "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
            },
            raw=message.model_dump() if hasattr(message, "model_dump") else message,
        )


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    """Arguments arrive as a JSON string, and smaller models sometimes emit
    something that is not quite JSON. An unparseable call should reach the model
    as a tool error it can correct, not kill the run."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"__unparsed_arguments__": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
