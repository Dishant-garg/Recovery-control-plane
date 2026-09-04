# The agents, and the contract they share

Five agents. Each one ships a **prompt and a deterministic routine over the same
tools** ([ADR-007](adr/ADR-007-agents-author-code-executes.md)), which is why
every number in the README is reproducible with no API key.

The rules below are enforced by `tests/test_agent_contract.py`, not just stated
here. A rule that lives only in prose is a rule the next agent breaks.

| agent | entry point | what it decides |
|---|---|---|
| **recovery** | `rcp/agents/recovery.py` · `--live N` | escalate / hold / stop, for one case |
| **analyst** | `eval/analyst.py` · `make analyze` | where the control plane loses money |
| **composer** | `rcp/agents/composer.py` · `make drafts` | message templates for gaps in the registry |
| **strategy** | `rcp/agents/strategy.py` · `make strategy` | what the proposers' probability tables should say |
| **red team** | `rcp/agents/redteam.py` · `make redteam` | copy that passes the compliance critic and should not |

Only **recovery** touches a live decision, and even it is bounded on three
sides: it cannot choose a channel (the ladder owns that, ADR-008), cannot exceed
the contact cap (the arbiter holds it inside a transaction, ADR-003), and cannot
contact someone who opted out (compliance runs after it, ADR-005).

---

## Rule 1 — every agent ships a deterministic routine

`run_agent(..., fallback=...)` is not optional. The routine receives the same
tools by name and returns the same `AgentResult`.

This buys three things at once:

- **`make eval` runs offline and free.** The batch never calls a provider.
- **A provider failure is survivable.** Rate limits and rotated model ids are
  routine; `run_agent` catches the error, runs the fallback, and sets
  `AgentResult.degraded` with the provider's message.
- **The honest fallback is sometimes "do nothing".** The composer's routine
  reports which templates are missing and drafts none, because *which* is
  computable and *what they should say* is not. Generating filler would put
  unreviewed copy into a review file under the appearance of having been
  authored.

The prompt lives in the module, as a `SYSTEM` constant, next to the tools it
describes. Not in a separate markdown file — the prompt and the routine are two
halves of one contract, and separating them is how they drift.

## Rule 2 — a degraded run produces one procedure's answer, not a blend

When the fallback fires mid-run, it **discards whatever the model already
submitted**.

This was a real defect. Groq hit its daily token limit after the strategy agent
had submitted seven revisions; the deterministic sweep then ran and appended
twelve more, and the review file listed `cart.BASE_CLAIM bank_downtime` twice
under two different justifications. A reviewer could not tell which procedure
wrote which line.

## Rule 3 — authoring agents propose, patch nothing

The composer and the strategy agent write to `data/drafts/`. Neither may edit
`rcp/proposers/` or `rcp/compose/templates.py`.

For strategy this is a correctness argument: a prior that rewrites itself from
its own outcomes is a feedback loop, not a calibration — and its observed rates
are biased by the arm's own selection, which its docstring records in detail.

For the composer it is a legal one. WhatsApp will not deliver an unregistered
business-initiated message, and Indian SMS goes through TRAI's DLT registry
where a template is approved before it can be sent. The human review step
already exists; the agent feeds it rather than bypassing it.

## Rule 4 — the bound is a tool the agent can call

An agent that cannot see its own constraints gets silently overruled. Each one
can interrogate the thing that will judge it:

| agent | tool | asks |
|---|---|---|
| recovery | `compliance_preview` | "what would the engine say if I escalated now?" |
| composer | `check_draft` | "would the critic block this copy?" |
| strategy | `rail_comparison` | "does retry actually beat messaging here?" |
| analyst | `sample_decision` | "show me one decision's full scoring" |
| red team | `try_copy` | "would the critic let this through?" |

**And the tool enforces, it does not merely inform.** Asking the model to be
careful is not a bound. On its first live run the strategy agent proposed adding
`auth_failed` to `RAIL_FIXABLE` at a 20.8% retry rate while removing
`insufficient_funds` at 16.6% — opposite conclusions from neighbouring numbers,
each confidently justified, in the direction nothing downstream damps.

`submit_revision` now checks additions against the data and refuses when retry
is untested, thin, or worse than messaging. `submit_template` refuses a draft
the critic blocks. The prompt asks; the tool decides.

**Two of these bounds are open to a human too.** The dashboard exposes
`try_copy` as an attack box on `/agents` and `compliance_preview` as a *run the
agent* button on any case, so a reviewer can interrogate the same constraints
the agents do. Showing a model more than the person reading its output would be
theatre.

---

## Rule 5 — one agent's job is to break another's

`rcp/agents/redteam.py` attacks `rcp/compose/critic.py`. It writes copy a
compliance officer would refuse and the critic would pass, so the critic can be
fixed. An attack the critic already blocks is **rejected** by `submit_attack` —
only copy that got through is a finding.

It earned its place immediately. The critic's coercion check was sixteen banned
phrases, and the first sweep put **seven of seven** attacks through it:

```
BLOCKED  "aapka account defaulter list mein ja sakta hai"   ← a listed word
PASSES   "aapka CIBIL score affect ho sakta hai"
PASSES   "hamari field team aapke address par visit karegi"
PASSES   "aapke employer ko inform karna pad sakta hai"
PASSES   "hum other remedies explore karenge"
PASSES   "FINAL WARNING!! ... warna consequences honge"
```

Every one describes conduct the RBI's recovery guidance prohibits. Not one
contained a listed word — **abuse is a set of tactics and a blacklist is a set
of words**, and the copy lives in the gap.

The critic now matches seven tactic categories rather than phrases, and a
refusal names the conduct: `credit_threat`, `third_party_disclosure`,
`physical_visit`, `veiled_legal`, `false_urgency`, `account_penalty`,
`impersonation`. The corpus is kept as a regression suite: it should report
zero, and anything that comes back is a rule that regressed.

**Red proposes; a human confirms.** "Is this message coercive" is exactly the
judgement that should not be delegated to the model writing the attacks, so
findings go to `data/drafts/redteam.json` and a rule change is a human edit.

## Adding an agent

1. Put it in `rcp/agents/` — or `eval/` if it reads ground truth (ADR-002).
2. Define `SYSTEM` at module level.
3. Write the tools, then the deterministic routine over the same tools.
4. Pass both to `run_agent(system=SYSTEM, tools=..., fallback=...)`.
5. If it authors anything, write to `data/drafts/` and gate submissions on a
   check, not on the prompt.

`tests/test_agent_contract.py` discovers agent modules from the filesystem, so
steps 2–4 are checked the moment the file exists, and this document is checked
for naming it.

## Providers

The tool-use loop is written once in `rcp/llm/client.py` and is
provider-agnostic; adapters translate only the wire format
(`rcp/llm/adapter.py`). Switching is one env var:

```bash
RCP_LLM=anthropic | groq | gemini | openai | together | ollama | fallback
make llm-check      # one live tool-use round trip, so a bad key surfaces early
```

`fallback` is the default, which is what makes "no API key" the working case
rather than the broken one.

**Schemas should not leave a parameter optional if the agent will always have a
value for it.** Declared optional, a model fills it with an explicit `null` and
strict validators reject the call — observed on Groq as
`` `/segment`: expected string, but got null ``. Marking it required removed the
ambiguity rather than relying on every provider being lenient.
