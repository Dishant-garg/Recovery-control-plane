"""Smoke-test a live provider with one tiny tool-use round trip.

Point of this existing at all: the agents in this project make several tool
calls over large payloads, so "does my key work and is my model id right" is a
terrible thing to discover halfway through `make analyze`. This does the
smallest possible real exercise of the adapter -- one tool, one call, a handful
of tokens -- and prints exactly which part broke when it breaks.

    RCP_LLM=groq GROQ_API_KEY=gsk_... make llm-check

Model ids drift faster than anything else in the stack, so a bad-model error
falls through to listing what the provider actually offers.
"""

from __future__ import annotations

import argparse
import os
import sys

from rcp.llm.client import (
    OPENAI_COMPATIBLE,
    LLMClient,
    Tool,
    available_providers,
    make_adapter,
)
from rcp.llm.cache import DiskCache
from rcp.env import load_dotenv

# Deliberately something a model cannot answer from its own knowledge, so a
# correct answer proves the tool actually round-tripped rather than the model
# guessing plausibly.
SECRET = 4671


def _lookup(name: str) -> dict:
    return {"reference_number": SECRET if name == "invoice" else 0}


PROBE = Tool(
    name="lookup_reference",
    description="Look up the internal reference number for a named record.",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string",
                                "description": "record name, e.g. 'invoice'"}},
        "required": ["name"],
        "additionalProperties": False,
    },
    fn=_lookup,
)

SYSTEM = ("You are a terse assistant. Use the provided tool when asked about a "
          "reference number. Answer with the number and nothing else.")
PROMPT = "What is the reference number for the record named 'invoice'?"


def _list_models(adapter) -> None:
    client = getattr(adapter, "client", None)
    models = getattr(client, "models", None)
    if models is None:
        return
    try:
        available = sorted(m.id for m in models.list())
    except Exception as exc:
        print(f"  (could not list models: {type(exc).__name__}: {exc})")
        return
    print(f"\n  {len(available)} models this key can reach:")
    for model_id in available[:40]:
        print(f"    {model_id}")
    print("\n  Pin one with RCP_LLM_MODEL=<id>")


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="one live tool-use round trip")
    parser.add_argument("--provider", default=None)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()

    name = args.provider or os.environ.get("RCP_LLM") or "fallback"
    if name == "fallback":
        print("RCP_LLM is unset or 'fallback', so there is nothing live to check.")
        print(f"Set one of: {', '.join(available_providers()[1:])}")
        print("\n  RCP_LLM=groq GROQ_API_KEY=gsk_... make llm-check")
        return 1

    try:
        adapter = make_adapter(name, args.model)
    except (ValueError, RuntimeError) as exc:
        print(f"could not build the {name} adapter:\n  {exc}")
        if name in OPENAI_COMPATIBLE:
            print(f"\n  export {OPENAI_COMPATIBLE[name][1]}=...")
        return 1

    print(f"provider : {adapter.name}")
    print(f"model    : {adapter.model}")
    print(f"tool     : {PROBE.name}  (returns {SECRET} for 'invoice')")
    print("-" * 60)

    client = LLMClient(adapter=adapter, cache=DiskCache(enabled=False))
    try:
        # A few hundred tokens is plenty for "call one tool, say a number",
        # and it keeps the probe inside the tightest free-tier budget.
        result = client.run_agent(system=SYSTEM, prompt=PROMPT, tools=[PROBE],
                                  max_turns=4, max_tokens=512, use_cache=False)
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        blob = f"{exc}".lower()
        if "model" in blob and ("not found" in blob or "decommission" in blob
                                or "does not exist" in blob):
            _list_models(adapter)
        return 1

    called = result.tool_calls()
    answered = str(SECRET) in result.text

    print(f"turns        : {result.turns}")
    print(f"tool calls   : {called or 'none'}")
    print(f"usage        : {result.usage}")
    print(f"reply        : {result.text.strip()[:200]}")
    print("-" * 60)

    if not called:
        print("PARTIAL: the model replied without calling the tool. The key and "
              "the model work, but this model is weak at tool use -- try a "
              "larger one before pointing an agent at it.")
        return 1
    if not answered:
        print("PARTIAL: the tool ran but the answer does not contain the value "
              "it returned. The round trip works; the model ignored the result.")
        return 1

    print(f"OK: tool called, {SECRET} round-tripped back into the answer.")
    print(f"\nNow try:  RCP_LLM={adapter.name} make analyze")
    return 0


if __name__ == "__main__":
    sys.exit(main())
