"""Provider-agnostic LLM client. Owns the tool-use loop; adapters own the wire.

Every agent in this project supplies **two** implementations of itself: a prompt
for the model, and a deterministic routine over the same tools. That is not a
hedge against the API being down -- it is what lets `make eval` and
`make analyze` run offline, for free, and produce the same output every time
(ADR-002, ADR-007). The LLM path adds hypotheses the deterministic rules do not
encode; the deterministic path guarantees the pipeline never depends on a
network call.

Selecting a provider:

    RCP_LLM=fallback          (default -- no key, no network)
    RCP_LLM=anthropic         native Anthropic SDK
    RCP_LLM=groq              OpenAI-compatible
    RCP_LLM=gemini            OpenAI-compatible endpoint
    RCP_LLM=openai            OpenAI-compatible
    RCP_LLM=ollama            OpenAI-compatible, local
    RCP_LLM_MODEL=...         override the vendor's default model
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable

from rcp.llm.adapter import (
    STOP_REFUSAL,
    ChatAdapter,
    ToolCall,
    ToolOutcome,
)
from rcp.llm.cache import DiskCache, cache_key


@dataclass(frozen=True)
class Tool:
    """A tool an agent may call. `fn` runs locally; the schema is what the
    model sees. Vendor encoding happens in the adapter, not here."""

    name: str
    description: str
    input_schema: dict[str, Any]
    fn: Callable[..., Any]

    def to_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


@dataclass
class AgentResult:
    text: str
    trail: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    provider: str = "fallback"
    model: str = ""
    cached: bool = False
    turns: int = 0
    # Set when the provider failed and the deterministic routine answered
    # instead. Carries the provider error so a degraded run is never mistaken
    # for a considered one.
    degraded: str | None = None

    def tool_calls(self) -> list[str]:
        return [step["tool"] for step in self.trail]


class LLMClient:
    """Pick an adapter, put the cache in front, run the loop."""

    def __init__(
        self,
        adapter: ChatAdapter | None = None,
        cache: DiskCache | None = None,
    ) -> None:
        self.adapter = adapter or make_adapter()
        self.cache = cache if cache is not None else DiskCache()

    # ---- the loop --------------------------------------------------------

    def _run_loop(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[Tool],
        max_turns: int,
        max_tokens: int | None = None,
    ) -> AgentResult:
        """Identical for every provider. The adapter only translates."""
        adapter = self.adapter
        by_name = {t.name: t for t in tools}
        encoded_tools = adapter.encode_tools(tools)
        messages: list[Any] = [adapter.user_message(prompt)]
        trail: list[dict[str, Any]] = []
        usage: dict[str, int] = {}
        text = ""

        for turn_index in range(max_turns):
            turn = adapter.complete(
                system=system, messages=messages, tools=encoded_tools,
                max_tokens=max_tokens or adapter.max_tokens,
            )
            for key, value in turn.usage.items():
                usage[key] = usage.get(key, 0) + value
            text = turn.text or text

            if turn.stop_reason == STOP_REFUSAL:
                raise RuntimeError(f"{adapter.name} declined the request")
            if not turn.wants_tools:
                return AgentResult(text=text, trail=trail, usage=usage,
                                   provider=adapter.name, model=adapter.model,
                                   turns=turn_index + 1)

            messages.append(adapter.assistant_message(turn))
            outcomes = [self._invoke(by_name, call, trail) for call in turn.tool_calls]
            messages.extend(adapter.tool_result_messages(outcomes))

        return AgentResult(text=text, trail=trail, usage=usage,
                           provider=adapter.name, model=adapter.model,
                           turns=max_turns)

    @staticmethod
    def _invoke(
        by_name: dict[str, Tool], call: ToolCall, trail: list[dict[str, Any]],
    ) -> ToolOutcome:
        tool = by_name.get(call.name)
        if tool is None:
            outcome = ToolOutcome(call, f"no such tool: {call.name}", is_error=True)
        else:
            try:
                outcome = ToolOutcome(call, tool.fn(**call.arguments))
            except Exception as exc:
                # A bad tool call is information for the model, not a crash.
                # Returning the error lets it correct itself on the next turn.
                outcome = ToolOutcome(
                    call, f"{type(exc).__name__}: {exc}", is_error=True
                )
        # The output, not just the call. "called compliance_preview" says
        # nothing a reader can use; "compliance_preview -> DENY, no whatsapp
        # consent" is the entire reason the tool exists. The dashboard renders
        # this trail directly, so a call without its answer is unreadable.
        #
        # Truncated because a `case_timeline` result runs to kilobytes and this
        # is a display record, not the transcript -- the model saw the whole
        # thing either way.
        trail.append({"tool": call.name, "input": call.arguments,
                      "error": outcome.is_error,
                      "output": _summarize(outcome.content)})
        return outcome

    # ---- public ----------------------------------------------------------

    def run_agent(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[Tool],
        fallback: Callable[[dict[str, Tool]], AgentResult] | None = None,
        max_turns: int = 12,
        max_tokens: int | None = None,
        use_cache: bool = True,
    ) -> AgentResult:
        """Run one agent task.

        `fallback` is the deterministic routine. It receives the same tools by
        name and must return an AgentResult. When the fallback adapter is
        selected it is the *only* thing that runs.
        """
        if self.adapter.name == "fallback":
            if fallback is None:
                raise ValueError(
                    "fallback provider selected but this agent supplied no "
                    "deterministic routine; pass fallback= or set RCP_LLM to a "
                    "live provider"
                )
            result = fallback({t.name: t for t in tools})
            result.provider = "fallback"
            return result

        # Tool *results* depend on live database state, so the cache key covers
        # only the request shape. Agents whose tools read a fixed seed's data
        # are reproducible; anything else should pass use_cache=False.
        key = cache_key(
            provider=self.adapter.name,
            model=self.adapter.model,
            system=system,
            prompt=prompt,
            tools=[t.to_schema() for t in tools],
        )
        if use_cache:
            hit = self.cache.get(key)
            if hit is not None:
                return AgentResult(**hit, cached=True)

        try:
            result = self._run_loop(
                system=system, prompt=prompt, tools=tools, max_turns=max_turns,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            # A provider failure is an operational condition, not a bug in the
            # agent: rate limits, rotated model ids, and oversized requests are
            # all routine. Every agent here ships a deterministic routine
            # precisely so one can be survived (ADR-007), so use it rather than
            # crashing a CLI mid-demo.
            #
            # Loud, never silent. `degraded` says the answer came from the
            # fallback and `error` says why, so a caller cannot mistake a
            # rate-limited run for a considered one.
            if fallback is None:
                raise
            result = fallback({t.name: t for t in tools})
            result.provider = "fallback"
            result.degraded = f"{type(exc).__name__}: {exc}"
            return result

        if use_cache:
            self.cache.put(key, {
                "text": result.text, "trail": result.trail, "usage": result.usage,
                "provider": result.provider, "model": result.model,
                "turns": result.turns,
            })
        return result


# --------------------------------------------------------------------------
# adapter registry
# --------------------------------------------------------------------------

# Vendors reachable through the OpenAI wire format: (base_url, api-key env var,
# default model). Adding one is a row here, not a code change.
#
# Model ids drift faster than anything else in this table -- set
# RCP_LLM_MODEL to pin one rather than trusting these defaults.
OPENAI_COMPATIBLE: dict[str, tuple[str, str, str]] = {
    # llama-3.3-70b-versatile was decommissioned; `make llm-check` surfaced it
    # by listing what the key could actually reach. Groq rotates ids faster
    # than any other vendor here -- expect to pin RCP_LLM_MODEL.
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY",
             "openai/gpt-oss-120b"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/",
               "GEMINI_API_KEY", "gemini-2.0-flash"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY", "gpt-4o"),
    "together": ("https://api.together.xyz/v1", "TOGETHER_API_KEY",
                 "meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    "ollama": ("http://localhost:11434/v1", "OLLAMA_API_KEY", "llama3.3"),
}


def available_providers() -> list[str]:
    return ["fallback", "anthropic", *sorted(OPENAI_COMPATIBLE)]


def make_adapter(name: str | None = None, model: str | None = None) -> ChatAdapter:
    """`RCP_LLM` decides. Defaults to fallback so nothing needs a key."""
    name = (name or os.environ.get("RCP_LLM") or "fallback").lower()
    model = model or os.environ.get("RCP_LLM_MODEL") or None

    if name == "fallback":
        from rcp.llm.providers.fallback import FallbackAdapter
        return FallbackAdapter()

    if name == "anthropic":
        from rcp.llm.providers.anthropic import AnthropicAdapter
        return AnthropicAdapter(model=model) if model else AnthropicAdapter()

    if name in OPENAI_COMPATIBLE:
        from rcp.llm.providers.openai_compat import OpenAICompatAdapter
        base_url, key_env, default_model = OPENAI_COMPATIBLE[name]
        return OpenAICompatAdapter(
            name=name, base_url=base_url, api_key_env=key_env,
            model=model or default_model,
        )

    raise ValueError(
        f"unknown provider {name!r}; expected one of {available_providers()}"
    )


def json_dumps(value: Any) -> str:
    """Tool results go over the wire as JSON for every vendor."""
    return json.dumps(value, default=str)


# How much of a tool result the trail keeps for display. Long enough to read a
# compliance verdict or a precedent tally, short enough that a case timeline
# does not bury the decision that followed it.
TRAIL_OUTPUT_CHARS = 400


def _summarize(content: Any) -> str:
    """One tool result, rendered for a human reading the trail."""
    text = content if isinstance(content, str) else json_dumps(content)
    if len(text) <= TRAIL_OUTPUT_CHARS:
        return text
    return text[:TRAIL_OUTPUT_CHARS] + f"… (+{len(text) - TRAIL_OUTPUT_CHARS} chars)"
