"""LLM layer: cache behaviour and provider routing.

The load-bearing property here is that nothing in the pipeline can quietly grow
a dependency on an API key. The fallback provider is the default, and an agent
that forgets to supply a deterministic routine fails loudly rather than trying
to reach the network.
"""

from __future__ import annotations

import pytest

from rcp.llm.cache import DiskCache, cache_key
from rcp.llm.client import (
    OPENAI_COMPATIBLE, AgentResult, LLMClient, Tool, available_providers,
    make_adapter,
)


def test_cache_roundtrip(tmp_path):
    cache = DiskCache(tmp_path)
    key = cache_key(model="m", prompt="p")

    assert cache.get(key) is None
    cache.put(key, {"text": "hello"})
    assert cache.get(key) == {"text": "hello"}
    assert cache.stats() == {"hits": 1, "misses": 1, "writes": 1, "hit_rate": 0.5}


def test_cache_key_covers_every_part(tmp_path):
    base = cache_key(model="m", prompt="p", tools=[])
    assert base != cache_key(model="other", prompt="p", tools=[])
    assert base != cache_key(model="m", prompt="other", tools=[])
    assert base != cache_key(model="m", prompt="p", tools=[{"name": "t"}])
    assert base == cache_key(prompt="p", tools=[], model="m")  # order-independent


def test_corrupt_entry_is_a_miss_not_a_crash(tmp_path):
    """One half-written file must not fail an entire run."""
    cache = DiskCache(tmp_path)
    key = cache_key(model="m", prompt="p")
    cache.put(key, {"text": "ok"})
    cache._path(key).write_text("{not json")

    assert cache.get(key) is None


def test_writes_are_atomic(tmp_path):
    """Write-then-rename: no .tmp files survive a successful put."""
    cache = DiskCache(tmp_path)
    cache.put(cache_key(model="m", prompt="p"), {"text": "ok"})
    assert list(tmp_path.rglob("*.tmp")) == []


def test_disabled_cache_never_touches_disk(tmp_path):
    cache = DiskCache(tmp_path, enabled=False)
    key = cache_key(model="m", prompt="p")
    cache.put(key, {"text": "ok"})
    assert cache.get(key) is None
    assert list(tmp_path.rglob("*.json")) == []


def test_default_provider_needs_no_api_key():
    assert make_adapter().name == "fallback"


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        make_adapter("not-a-vendor")


def test_every_registered_vendor_is_selectable():
    """A row in OPENAI_COMPATIBLE must not need a code change to reach."""
    assert set(available_providers()) == {"fallback", "anthropic",
                                          *OPENAI_COMPATIBLE}


def test_fallback_routing_runs_the_deterministic_routine():
    client = LLMClient(cache=DiskCache(enabled=False))
    tool = Tool("t", "d", {"type": "object", "properties": {}}, lambda: 42)

    seen = {}

    def routine(tools):
        seen["names"] = sorted(tools)
        return AgentResult(text="done")

    result = client.run_agent(system="s", prompt="p", tools=[tool], fallback=routine)
    assert result.text == "done"
    assert result.provider == "fallback"
    assert seen["names"] == ["t"]


def test_agent_without_a_deterministic_routine_fails_loudly():
    """An agent that only works with an API key is a broken agent here."""
    client = LLMClient(cache=DiskCache(enabled=False))
    with pytest.raises(ValueError, match="no deterministic routine"):
        client.run_agent(system="s", prompt="p", tools=[])


def test_tool_schema_shape():
    tool = Tool("lookup", "does a thing",
                {"type": "object", "properties": {"x": {"type": "string"}},
                 "required": ["x"]}, lambda x: x)
    schema = tool.to_schema()
    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["input_schema"]["required"] == ["x"]


# --------------------------------------------------------------------------
# the loop, exercised through a scripted adapter
# --------------------------------------------------------------------------

from rcp.llm.adapter import STOP_END, STOP_REFUSAL, STOP_TOOL_USE, ToolCall, Turn


class ScriptedAdapter:
    """Replays a fixed list of Turns.

    This is what makes the loop testable at all: no SDK, no network, no key.
    Because the loop is provider-agnostic (rcp/llm/adapter.py), proving it here
    proves it for every adapter -- each real one only has to translate.
    """

    name = "scripted"
    model = "scripted-1"
    max_tokens = 1234

    def __init__(self, turns):
        self.turns = list(turns)
        self.requests = []

    def complete(self, *, system, messages, tools, max_tokens):
        self.requests.append({"system": system, "messages": list(messages),
                              "tools": tools, "max_tokens": max_tokens})
        return self.turns.pop(0)

    def encode_tools(self, tools):
        return [{"encoded": t.name} for t in tools]

    def user_message(self, text):
        return {"role": "user", "content": text}

    def assistant_message(self, turn):
        return {"role": "assistant", "raw": turn.raw}

    def tool_result_messages(self, outcomes):
        return [{"role": "tool", "id": o.call.id, "content": o.content,
                 "error": o.is_error} for o in outcomes]


def client_with(turns):
    return LLMClient(adapter=ScriptedAdapter(turns), cache=DiskCache(enabled=False))


ECHO = Tool("echo", "echoes", {"type": "object",
                               "properties": {"v": {"type": "string"}}},
            lambda v: f"echoed:{v}")


def test_loop_executes_a_tool_and_feeds_the_result_back():
    adapter_turns = [
        Turn(tool_calls=[ToolCall("c1", "echo", {"v": "hi"})],
             stop_reason=STOP_TOOL_USE, raw="assistant-1"),
        Turn(text="all done", stop_reason=STOP_END),
    ]
    client = client_with(adapter_turns)
    result = client.run_agent(system="s", prompt="p", tools=[ECHO])

    assert result.text == "all done"
    assert result.tool_calls() == ["echo"]
    assert result.turns == 2
    assert result.model == "scripted-1"

    second_request = client.adapter.requests[1]["messages"]
    assert second_request[-1] == {"role": "tool", "id": "c1",
                                  "content": "echoed:hi", "error": False}


def test_loop_runs_parallel_tool_calls_in_one_turn():
    client = client_with([
        Turn(tool_calls=[ToolCall("a", "echo", {"v": "1"}),
                         ToolCall("b", "echo", {"v": "2"})],
             stop_reason=STOP_TOOL_USE),
        Turn(text="done", stop_reason=STOP_END),
    ])
    client.run_agent(system="s", prompt="p", tools=[ECHO])

    results = [m for m in client.adapter.requests[1]["messages"]
               if m.get("role") == "tool"]
    assert [m["content"] for m in results] == ["echoed:1", "echoed:2"]


def test_a_raising_tool_is_reported_to_the_model_not_raised():
    """A bad call is information the model can correct on the next turn."""
    boom = Tool("boom", "raises", {"type": "object", "properties": {}},
                lambda: 1 / 0)
    client = client_with([
        Turn(tool_calls=[ToolCall("c1", "boom", {})], stop_reason=STOP_TOOL_USE),
        Turn(text="recovered", stop_reason=STOP_END),
    ])
    result = client.run_agent(system="s", prompt="p", tools=[boom])

    assert result.text == "recovered"
    assert result.trail[0]["error"] is True
    tool_msg = client.adapter.requests[1]["messages"][-1]
    assert "ZeroDivisionError" in tool_msg["content"]


def test_an_unknown_tool_is_reported_not_raised():
    client = client_with([
        Turn(tool_calls=[ToolCall("c1", "nope", {})], stop_reason=STOP_TOOL_USE),
        Turn(text="ok", stop_reason=STOP_END),
    ])
    result = client.run_agent(system="s", prompt="p", tools=[ECHO])
    assert result.trail[0]["error"] is True
    assert "no such tool" in client.adapter.requests[1]["messages"][-1]["content"]


def test_loop_stops_at_max_turns():
    """A model that keeps calling tools forever must not spin."""
    forever = [Turn(tool_calls=[ToolCall(f"c{i}", "echo", {"v": "x"})],
                    stop_reason=STOP_TOOL_USE) for i in range(20)]
    client = client_with(forever)
    result = client.run_agent(system="s", prompt="p", tools=[ECHO], max_turns=3)
    assert result.turns == 3
    assert len(result.trail) == 3


def test_refusal_is_surfaced():
    client = client_with([Turn(stop_reason=STOP_REFUSAL)])
    with pytest.raises(RuntimeError, match="declined"):
        client.run_agent(system="s", prompt="p", tools=[ECHO])


def test_usage_accumulates_across_turns():
    client = client_with([
        Turn(tool_calls=[ToolCall("c1", "echo", {"v": "x"})],
             stop_reason=STOP_TOOL_USE, usage={"input_tokens": 10,
                                               "output_tokens": 3}),
        Turn(text="done", stop_reason=STOP_END,
             usage={"input_tokens": 15, "output_tokens": 4}),
    ])
    result = client.run_agent(system="s", prompt="p", tools=[ECHO])
    assert result.usage == {"input_tokens": 25, "output_tokens": 7}


def test_tools_are_encoded_by_the_adapter_not_the_loop():
    """The loop must never see a vendor's wire format."""
    client = client_with([Turn(text="ok", stop_reason=STOP_END)])
    client.run_agent(system="s", prompt="p", tools=[ECHO])
    assert client.adapter.requests[0]["tools"] == [{"encoded": "echo"}]


def test_cached_result_skips_the_adapter(tmp_path):
    cache = DiskCache(tmp_path)
    turns = [Turn(text="first", stop_reason=STOP_END)]
    first = LLMClient(adapter=ScriptedAdapter(turns), cache=cache)
    assert first.run_agent(system="s", prompt="p", tools=[]).text == "first"

    # An empty script would IndexError if the adapter were reached again.
    second = LLMClient(adapter=ScriptedAdapter([]), cache=cache)
    result = second.run_agent(system="s", prompt="p", tools=[])
    assert result.cached is True and result.text == "first"


def test_adapter_supplies_its_own_token_ceiling():
    """Vendors disagree wildly, and Groq bills max_tokens against a per-minute
    budget -- so a single global constant 413s on a two-line prompt."""
    client = client_with([Turn(text="ok", stop_reason=STOP_END)])
    client.run_agent(system="s", prompt="p", tools=[])
    assert client.adapter.requests[0]["max_tokens"] == 1234


def test_caller_can_override_the_ceiling():
    client = client_with([Turn(text="ok", stop_reason=STOP_END)])
    client.run_agent(system="s", prompt="p", tools=[], max_tokens=512)
    assert client.adapter.requests[0]["max_tokens"] == 512
