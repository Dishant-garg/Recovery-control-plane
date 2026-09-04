# ADR-007: Agents author, deterministic code executes

**Status:** accepted · **Date:** 2026-08-27

## Context

Two constraints pull in opposite directions.

ADR-002 requires the decision path to be byte-reproducible: `make eval` twice
must produce identical output, offline, with no API key. An LLM in that path
kills the claim the whole project rests on.

But plenty of work here genuinely cannot be enumerated in advance. Somebody has
to decide that `mandate_expired` deserves a WhatsApp rather than a retry, write
recovery copy in Hinglish that does not read like a threat, and — hardest of all
— work out *why* the control plane is losing on a segment, which requires
forming a hypothesis, testing it, and forming the next one based on the answer.

Resolving this by keeping LLMs out entirely is the wrong call. It leaves that
work to a human's unexamined guess, and the guesses have already been wrong:
`cart.py` and `receivables.py` both shipped without ever proposing the `retry`
channel, and the arbiter shipped believing opt-out risk was ~1.7x its real value.

## Decision

**Agents author. Deterministic code executes.**

An agent's output is a strategy, a message, a rule, or a finding — never a live
decision about a specific customer. That output is cached or committed, and the
runtime reads it deterministically.

Concretely:

| Agent | Authors | Executes deterministically as |
|---|---|---|
| eval analyst (`eval/analyst.py`) | findings about where the system loses | a human applies the fix |
| strategy | proposer playbooks per `(root_cause × segment × bucket × phase)` | a lookup table in `rcp/proposers/` |
| composer | ~40 message templates per `(cause × language × tone)` | template fill at send time |
| critic | a pass/fail rubric verdict on copy | gate at authoring time, not send time |
| diagnosis | a natural-language reading of `decisions.detail` | display only |
| policy | a rule config + tests from a regulation document | `rcp/compliance/rules.py`, human-approved |

**Every agent ships two implementations of itself**: a prompt, and a
deterministic routine over the same tools (`LLMClient.run_agent(..., fallback=)`).
`RCP_LLM=fallback` is the default, so the deterministic path is what runs unless
someone opts in. An agent that supplies no deterministic routine raises rather
than reaching for the network — a hard stop against the pipeline quietly growing
a dependency on an API key.

**What agents never do**: choose who to contact (the arbiter, which must be
testable), choose a channel at runtime (the proposer, which must be measurable),
or enforce compliance (which must be deterministic). ADR-005's three layers
still hold; an agent authoring a proposer's playbook is still subject to every
one of them.

## The provider seam

The first version made each provider implement `run_agent` -- the entire
tool-use loop. That is the wrong seam: every vendor would re-implement turn
accounting, tool dispatch, error handling, and the max-turn guard, and the four
copies would drift in ways that are hard to test.

What actually differs between vendors is narrow: how a tool is declared, how a
tool call comes back, and how you echo the assistant turn and hand back results.
So `rcp/llm/adapter.py` normalizes exactly those three things, the loop lives
once in `LLMClient.run_agent`, and an adapter contains no control flow.

| Provider | Adapter | Notes |
|---|---|---|
| fallback (default) | `providers/fallback.py` | never reached; the loop short-circuits to the agent's deterministic routine |
| Anthropic | `providers/anthropic.py` | native; echoes content blocks verbatim so thinking survives the round trip |
| Groq, Gemini, OpenAI, Together, Ollama, vLLM | `providers/openai_compat.py` | one adapter; each vendor is a row of `(base_url, key env, model)` in `client.OPENAI_COMPATIBLE` |

Switching provider is one env var (`RCP_LLM`); adding an OpenAI-compatible
vendor is a row of config, not code. Gemini goes through the compatibility
endpoint rather than a native adapter because that surface is stable while the
native tool-calling shape moves -- a native adapter is ~80 lines against this
seam if it ever proves too limiting.

Three habits that are bugs if carried across vendors, and which the adapters
handle rather than the loop:

  * Anthropic wants every tool result for one turn in a **single** user message
    -- splitting them teaches the model to stop calling tools in parallel. The
    OpenAI format wants one `role: "tool"` message per call. Hence
    `tool_result_messages` returns a *list*.
  * OpenAI-format tool arguments arrive as a JSON **string**, not an object.
  * Schema key is `input_schema` for Anthropic, `parameters` for OpenAI.

Because the loop is provider-agnostic, it is tested through a scripted adapter
with no SDK, no network, and no key -- `tests/test_llm.py` covers parallel calls,
raising tools, unknown tools, the max-turn guard, refusals, and usage
accumulation. Proving the loop there proves it for every adapter.

## What a live provider actually did

Validated end to end against Groq (`openai/gpt-oss-120b`), 6 tool calls, ~14.5k
tokens. It is worth recording honestly, because it is the argument for the
architecture rather than against it.

**It found something the deterministic rules do not encode**: WhatsApp sub-cap
denials in the cart segment, quantified from `suppression_reasons`. That is the
additive value the LLM path is for.

**It also invented every file path it cited** -- `cart_scoring.go`,
`channel_caps.go`, `optout_arbiter.go`, in a repository with no Go in it -- got
its arithmetic wrong ("7.7 M paise" for a 743k difference; "1,066 far exceeding
3,500"), and proposed raising contact caps and lowering the value floor, which
is precisely the change the whole system exists to avoid.

Two fixes, both cheap:

  * the system prompt now carries the real module layout, and `_parse` rewrites
    any `where` it cannot find on disk to `(unverified: ...)`. The finding
    survives -- its evidence may be sound -- but an invented path never gets to
    look real.
  * the prompt states the churn constraint explicitly ("send more" is the
    obvious read and is usually wrong here) and forbids derived arithmetic.

After both: every path real, the arithmetic correct, the churn cost of its own
suggestion acknowledged, in fewer tokens.

The lesson is the one ADR-007 is built on. A model given tools over real data
produces genuinely new hypotheses **and** confident inventions in the same
breath, and the two are indistinguishable in tone. That is survivable when the
agent authors and deterministic code executes; it would not be survivable if the
agent were deciding who to contact.

## Consequences

**This is not "LLMs at the edges."** The analyst does the hardest work in the
project, and it did it: on its first real run it found that
`ReceivablesProposer` never proposed `retry`, evidenced by the baseline
recovering 22.4% on retries at Rs 2/send while the proposer spent Rs 120/send on
voice to recover 16.0%. That bug was not known. Fixing it moved the headline
from +44.2% to +51.2% and ex-churn from −6.0% to −0.0%.

It also independently rediscovered the 1.7x opt-out miscalibration that had
previously been found by hand.

**The agent is auditable too.** The manual tool loop in
`rcp/llm/providers/anthropic.py` captures every tool call and its arguments. A
system whose selling point is a written reason for every decision cannot have an
unauditable agent grading it.

**Cost stays near zero.** The corpora are bounded — ~200 decline strings, ~40
message segments, one analyst run per eval — and `data/llm_cache/` is committed,
so each unique input costs one call for the life of the project rather than one
per run.

**A wrong finding is worse than no finding.** The analyst's first calibration
check compared `opt_out_base` (risk at zero prior contacts) against the average
rate across customers who had several, and confidently reported the arbiter
*under*estimating when it was running high. Its first version also analysed a
single seed, which made receivables — ~90 events per run — swing 80% either way.
Both are fixed: the check now predicts per send from that send's own contact
history, and every tool aggregates across all seeds. `tests/test_analyst.py`
tests the detectors for *silence* on healthy input as carefully as it tests them
for firing.
