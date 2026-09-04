"""The provider seam: one loop, thin adapters.

The first version of this layer made each provider implement `run_agent` --
the whole tool-use loop. That is the wrong seam. Every vendor would then
re-implement turn accounting, tool dispatch, error handling, and the max-turn
guard, and the four would drift apart in exactly the ways that are hard to test.

What actually differs between vendors is narrow:

  1. how a tool is declared on the wire
  2. how a tool call comes back
  3. how you echo the assistant turn and hand back results

So an adapter translates those three things and nothing else. The loop lives
once, in `LLMClient.run_agent`, and is identical for every provider. Adding a
vendor is ~80 lines with no control flow in it.

`Turn` is the normalized response. `stop_reason` is deliberately a small closed
set rather than each vendor's own vocabulary, because the loop branches on it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# The loop only ever branches on these. Vendor-specific reasons ("stop",
# "end_turn", "STOP", "max_tokens", "length") map onto them in the adapter.
STOP_END = "end"
STOP_TOOL_USE = "tool_use"
STOP_LENGTH = "length"
STOP_REFUSAL = "refusal"


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    call: ToolCall
    content: Any
    is_error: bool = False


@dataclass
class Turn:
    """One assistant response, normalized."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = STOP_END
    usage: dict[str, int] = field(default_factory=dict)
    # The vendor-native assistant payload, kept so the adapter can echo it back
    # verbatim. Reconstructing it from our normalized fields loses information
    # some providers require on the next turn (thinking blocks, signatures).
    raw: Any = None

    @property
    def wants_tools(self) -> bool:
        return self.stop_reason == STOP_TOOL_USE and bool(self.tool_calls)


@runtime_checkable
class ChatAdapter(Protocol):
    """What a provider must supply. Note there is no loop here."""

    name: str
    model: str
    # Vendors disagree enormously about what a reasonable ceiling is, and some
    # count it against a rate limit rather than only against the response --
    # Groq's free tier bills `max_tokens` into its 8k tokens-per-minute budget,
    # so a 16k default 413s on a two-line prompt. It belongs to the adapter.
    max_tokens: int

    def complete(
        self,
        *,
        system: str,
        messages: list[Any],
        tools: list[Any],
        max_tokens: int,
    ) -> Turn:
        """One request. `messages` and `tools` are in this adapter's own wire
        format -- see `encode_tools` and the message builders."""

    def encode_tools(self, tools: list[Any]) -> list[Any]:
        """Our `Tool` list -> this vendor's tool declaration format."""

    def user_message(self, text: str) -> Any:
        """The opening prompt."""

    def assistant_message(self, turn: Turn) -> Any:
        """Echo the assistant turn back into the transcript."""

    def tool_result_messages(self, outcomes: list[ToolOutcome]) -> list[Any]:
        """Hand tool results back.

        Returns a *list* because vendors disagree: Anthropic wants every result
        in one user message (splitting them teaches the model to stop calling
        tools in parallel), while the OpenAI wire format wants one `role: tool`
        message per call.
        """
