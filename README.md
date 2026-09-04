# Recovery Control Plane

An agent that works a failed payment as a **case** — escalating up a ladder,
refused by a compliance engine when it should be, and stopped by rules that
decide when to give up — with a written reason for everything that did not
happen.

Every number below is reproducible offline, with no API key, in about 30
seconds.

```bash
make setup && make data && make eval
```

## Results

20 seeds, 500 events each, three segments. **Three arms**, because two was not an
honest comparison:

- `baseline` — naive dunning: re-send the **same channel** every review (retry
  on the rail, or SMS where a retry cannot work), contact cap of 5, no
  valuation, no escalation. It does not climb the ladder at all
- `capped` — the same, under the **control plane's own** contact cap. The one
  guard a competent team adds without building any of this
- `control_plane` — payday-aware proposers, platform-side valuation, compliance,
  escalation, stopping rules

| | baseline | capped | control plane |
|---|---|---|---|
| actions sent | 1,356 | 1,337 | **656** |
| contacts / customer | 7.4 | 7.3 | **3.8** |
| recovery / send | 13.8% | 13.9% | **20.0%** |
| **margin / send** | Rs 1,043 | Rs 1,044 | **Rs 1,602** |
| recovered | Rs 1,418,905 | Rs 1,402,436 | Rs 1,031,543 |
| opt-outs | 15.5 | 16.1 | **8.8** |
| churn cost | Rs 1,567,241 | Rs 1,659,896 | **Rs 166,221** |
| **net value** | Rs −155,346 | Rs −264,404 | **Rs 885,651** |
| false suppressions | 13.9 (Rs 109,498) | 73.0 (Rs 560,491) | 55.5 (Rs 397,772) |

**vs `capped`: +435.0%, winning 20/20 seeds.** On margin per send, also 20/20.

Event-level metrics cannot distinguish 500 cases worked properly from 100 cases
thrashed, so the workflow is reported separately:

| | baseline | capped | control plane |
|---|---|---|---|
| mean rungs climbed | 3.17 | 3.13 | **1.76** |
| attempts / case | 2.77 | 2.73 | **1.34** |
| refusals costing a rung | 0 | 0 | 94.2 |
| refusals deferred | 72.5 | 356.5 | 79.5 |
| **never acted** | 0 | 0 | **0** |

`never acted` counts cases that exhausted the entire ladder without ever
sending anything. It must be zero — that is a bug, not an outcome — and it is
the row that would have caught the defect in §2 on the run that introduced it.

`capped` is the arm that matters. Beating `baseline` only shows that unlimited
contact is bad, which nobody disputes.

### What the number does and does not say

The control plane **recovers less money** (Rs 1,032k vs Rs 1,419k) from **half
the contacts**. Its edge is targeting — Rs 1,602 of margin per send against
Rs 1,043 — and avoiding churn, not volume.

So the honest question is what an opt-out costs, and `make sensitivity` sweeps
that assumption instead of picking one:

| opt-out costs | baseline | control plane | delta | wins |
|---|---|---|---|---|
| 0% (churn free) | 1,450,802 | 1,310,114 | −140,688 | 0/8 |
| 10% of LTV | 1,293,790 | 1,076,769 | −217,021 | 1/8 |
| 25% | 1,058,273 | 956,249 | −102,024 | 2/8 |
| 50% | 665,744 | 924,452 | +258,708 | 7/8 |
| 75% | 273,214 | 912,971 | +639,756 | 8/8 |
| 100% | −119,315 | 922,728 | **+1,042,043** | **8/8** |

The 0% row is the control: price an opt-out at nothing and restraint loses
0/8, by arithmetic rather than by being wrong. Every send has positive
marginal value when losing the customer is free.

**Break-even sits between 25% and 50% of lifetime value.** A reviewer who thinks
an opt-out costs a quarter of a customer should read this as "no advantage" —
and that is the correct reading. Quote the condition, not just the number.

---

## The part we would rather you read

Eight things went wrong in this project. **Two were bugs we introduced, one was
in our own dataset, two were agents confidently proposing changes that would
have made things worse, one was found because a number looked absurd, one was a
safety check that an agent walked straight through, and one was an assumption
nobody ever wrote down.** None were caught by anyone's intuition.

### 1. The outcome model gave every attempt the same odds

Found by asking the boring question first: *is the dataset even right?*

`RETRY_DECAY` was applied to the event's `retry_index` — how many times the
*gateway* retried before the webhook fired. It was never applied to how many
rungs **we** had already climbed. So the fourth contact on a case had the same
success probability as the first: four near-independent shots at ~20% compound
to ~59%.

That is why the naive arms were "recovering" 48% of everything. Somebody who
ignored three messages is not a fresh coin flip on the fourth.

```
recovery rate, baseline:   48.4%  →  36.3%
```

Every headline number before this fix was inflated, and it inflated the
high-volume arms most — which is exactly the direction that flattered the wrong
policy.

### 2. A compliance refusal burned a ladder rung

Fixing an infinite-loop bug, we made every refused rung "consumed". That
over-corrected:

```
95 cases exhausted the entire ladder having sent NOTHING
1,030 compliance refusals against 570 real actions
```

The missing distinction is *why* the refusal happened. "No WhatsApp consent"
means the rung will never work — consume it. "Contact cap reached this week"
means the rung is fine and the timing is not — reschedule it. Every `Deny` now
carries a **disposition** (`channel_unusable` / `retry_later` / `stop`).

```
cases exhausting the ladder having sent nothing:  95  →  0
acted / held ratio:                             0.55  →  3.89
```

### 3. Every ladder opened on a rung a third of cases could never use

An agent (`make analyze`) reported that cart was losing money to excessive
suppression, with 181 `channel_eligibility` denials as evidence. **Its suggested
fix was to relax that rule** — which would have sent retries against dead
mandates, where the recovery probability is literally zero.

The evidence was right and the fix was backwards. Digging in:

```
900 of 900 channel_eligibility refusals landed on rung 0
```

Every ladder opens with `retry`, and roughly a third of root causes
(`mandate_expired`, `card_expired`, `invalid_account`) can never be fixed by
one. A third of all cases were burning their first rung on a guaranteed failure.

The fix was not to relax the rule — it was for the **ladder to skip rungs that
are structurally impossible** for that root cause. The compliance guarantee is
untouched; the ladder simply stopped provoking it. That is ADR-005's layering:
the proposer is polite, the engine is the guarantee.

### 4. Ex-churn was a metric restraint could not win

We spent two rounds trying to "fix" the control plane losing on net-value-
ex-churn, before checking the arithmetic:

```
margin per send, churn excluded:  Rs 1,157 (baseline)  vs  Rs 5 send cost
```

With churn removed, every send has enormously positive marginal value, so "send
everything" wins that comparison **by arithmetic, not by being better**. It was
a useful noise control when churn was 1–2 opt-outs; once churn became the signal
it became meaningless.

Replaced with **margin per send**, which measures targeting and which a
restraint strategy can actually win — and does, 20/20.

### 5. The audit log described runs that no longer existed

Noticed while sizing the repo for deployment: the audit files were **51 MB for
a run that sent 577 messages.**

```
audit records of kind `decision`:  136,644
rows in the `decisions` table:       1,049
```

`eval/run.py` forks a fresh database per arm on every run, and then opened the
existing audit log and appended to it. The chain stayed internally valid — it
verified — while describing roughly 130 previous runs whose databases had been
overwritten. An audit trail that outlives its subject is exactly the failure an
audit trail is supposed to prevent.

`AuditLog(path, reset=True)` now truncates when the database it describes is
being replaced, which is legitimate only because the fork makes a new world.
The flag is documented as never valid in production, where a log outliving its
database is the point.

```
audit_control_plane.jsonl:  51 MB  →  464 KB
data/ total:               712 MB  →  233 MB
```

### 6. An agent drew opposite conclusions from neighbouring numbers

The strategy agent's first live run, on Groq. It proposed **adding**
`auth_failed` to `RAIL_FIXABLE` and **removing** `insufficient_funds` — in the
same run, each with a confident justification:

```
add auth_failed          53 retry trials, 20.8% success
                         "silent retries can recover a notable fraction"
remove insufficient_funds  145 retry trials, 16.6% success
                         "silent retries rarely succeed"
```

Two opposite readings of 20.8% and 16.6%, with no threshold stated anywhere.
And in the dangerous direction: `RAIL_FIXABLE` decides which *channel* gets
bid, before any calibration runs, so an error there is damped by nothing.

The prompt already asked for care. Asking was not enough. `submit_revision`
now checks an addition against `rail_comparison` and refuses when retry is
untested, thin, or worse than messaging — the same shape as the composer's
critic gating a draft rather than trusting the instructions.

**And then that guard turned out to be inert.** Written as
`if message_p is not None and retry_p < message_p`, it never fired, because
`message_p` is always `None`. The fix for §3 is why: `escalation._viable` skips
the retry rung for causes retry cannot fix, so a cause receives retries *or*
messages, almost never both.

```
cart, baseline arm:
  auth_failed      retry 11/53  p=0.22    messages 0/0  p=None
  limit_exceeded   retry  3/40  p=0.095   messages 0/0  p=None
```

With one side of the comparison empty, the next live run added `limit_exceeded`
to `RAIL_FIXABLE` on a **9.5%** retry success rate, citing "40 trials — retried
enough to justify automatic retry". Trial count read as evidence of quality.

An absent comparison now reads as **"cannot tell"**, never as "no objection" —
the same distinction the tool already drew between an untested cause and a bad
one, applied to the side nobody thought to check. Fixing §3 quietly destroyed
the natural experiment this tool depends on, and nothing said so until an agent
walked into the gap.

**The same run also exposed a defect in the fallback.** The provider hit its
daily token limit partway through; the deterministic sweep ran and *appended*
to the revisions the model had already submitted, so the review file held both
procedures blended, with `cart.BASE_CLAIM bank_downtime` listed twice under two
different justifications. The fallback now discards partial model output — a
degraded run must produce one procedure's answer, not a mixture nobody can
attribute.

### 7. The compliance filter was a word list, and abuse is a set of tactics

`rcp/compose/critic.py` blocks coercive copy — the check the whole message
pipeline rests on, because a recovery flow that threatens people is a legal
problem regardless of what it recovers.

It was sixteen banned phrases. `make redteam` put **seven of seven** attacks
through it on the first sweep:

```
BLOCKED  "aapka account defaulter list mein ja sakta hai"   ← a listed word
PASSES   "aapka CIBIL score affect ho sakta hai"
PASSES   "hamari field team aapke address par visit karegi"
PASSES   "aapke employer ko inform karna pad sakta hai"
PASSES   "hum other remedies explore karenge"
PASSES   "FINAL WARNING!! ... warna consequences honge"
PASSES   "aapka naam bureau ko report kiya jaayega"
```

Every one describes conduct the RBI's recovery guidance prohibits — credit
bureau threats, third-party disclosure, home visits, veiled legal consequences.
Only the first contained a listed word.

**A blacklist enumerates vocabulary; abuse is a repertoire of tactics**, and all
the copy lives in the gap. The check now matches seven tactic categories, and a
refusal names the conduct rather than the wording:

```
coercive_language: uses prohibited collection tactics: credit_threat ('cibil')
```

**Then the live agent went past the rewrite.** Run against the hardened rules
on Groq, it produced:

```
PASSES  "Rs 2,500 is still pending. If not cleared soon it could hurt your
         borrowing score and future loan approvals."
```

The same bureau threat with none of the matched terms. Tactic *patterns* are
still vocabulary, one level up — and a model paraphrases around vocabulary for
a living. That pattern is closed now; the next paraphrase is not.

Which fixes where this check belongs. It is ADR-005's **layer 1**, not the
guarantee. The guarantee is that no template reaches a customer without a human
registering it with DLT and with WhatsApp. The critic's job is to raise the
floor so review catches less — treating it as the last line is how a paraphrase
becomes a sent message.

The corpus is kept as a regression suite — it must report zero, and anything
that comes back is a rule that regressed.

**Attribution, since it matters for this one:** the first seven attacks were
written by hand while testing, not by a model. The eighth — the paraphrase that
beat the fix — is the agent's.

The red team cannot edit the critic it attacks, which is asserted by a test. An
agent that both finds a hole and writes the rule closing it can drive its own
scoreboard to zero without anything being safer.

### 8. Every ladder rung was a different channel, and nobody had justified that

Cart was losing. Per send it recovered **12.4%** against the baseline's 17.1%,
and it caused *more* opt-outs from *fewer* messages — the one shape that cannot
be explained as restraint.

The first two explanations were both wrong. It was not the discount logic:
sizing the incentive by margin instead of a flat 20% moved cart from 12.4% to
13.4%, one extra recovery in five seeds. Under this outcome model a discount
caps out around **+8%** of expected value and the compliance ceiling clamps the
average offer to Rs 53, which is worth 1.03x. **A correctly built feature that
the arithmetic makes inert.**

Nor was it the escalation to WhatsApp, though that helped a little.

The channel table said it plainly:

```
cart retry  →  20.4% recovery  ·  Rs 2   ·  zero opt-out risk
cart sms    →   4.8% recovery  ·  Rs 15  ·  carries all the churn
```

**The ladder gave cart exactly one retry.** A second attempt on the rail beats a
first SMS on every axis, and the ladder forbade it — because every ladder in
`config/policy.yaml` had been written with one rung per channel, an assumption
that was never argued for and never tested. `[retry, retry, sms, whatsapp]`:

```
cart recovery / send:   13.4%  →  15.6%   (baseline 13.6%)
opt-outs, all segments:   9.5  →    8.8
vs capped:             +365.8% → +435.0%,  still 20/20
```

**How it was found:** by being asked why cart looked bad, and answering with a
per-segment breakdown instead of the headline. The aggregate had been positive
the whole time — subscription (25.7% per send) was carrying a segment that was
losing money, and no reported number separated them.

The same question also caught a **false description in this README**: the
`baseline` arm was documented as chasing "up the ladder". It does no such
thing — `BaselineProposer` picks a channel from the root cause and re-sends it
forever, which on cart is accidentally a retry-only policy, which is why it was
winning there.

**The pattern across all eight:** the thing that caught them was always a
measurement, never anyone's judgement. Four separate times a plausible,
confident, wrong answer was available and the arithmetic disagreed.

And the last three were found by the agents themselves — the analyst against the
policy, the strategy agent against its own guard rail, the red team against the
compliance filter. That is what the agents are for here. They are not a
narration layer over a working system; they are the part that finds out it is
not working.

---

## The agents

Four, each with a prompt *and* a deterministic routine over the same tools
([ADR-007](docs/adr/ADR-007-agents-author-code-executes.md) — agents author,
deterministic code executes). The deterministic one is the default, so every
number in this README is reproducible with no API key.

| | | |
|---|---|---|
| **recovery** | `--live N` | decides escalate / hold / stop for one case |
| **analyst** | `make analyze` | investigates where the control plane loses |
| **composer** | `make drafts` | drafts message templates for gaps in the registry |
| **strategy** | `make strategy` | audits the proposers' hand-written probability tables |

Only `recovery` touches a decision. The other three **write proposals to a
review file and patch nothing** — because a system that rewrites its own priors
from its own outcomes is a feedback loop, not a calibration.

The bound each one works inside is a tool it can call: `compliance_preview` for
recovery, `check_draft` for composer. An agent that can see its own constraints
negotiates with them instead of being silently overruled.

**A provider failure falls back rather than crashing.** Rate limits and rotated
model ids are routine, and every agent already ships a deterministic routine —
so `run_agent` uses it and sets `degraded` with the provider error, which the
CLIs print. A rate-limited run can never be mistaken for a considered one.

### What the analyst found

`make analyze` has found real defects — including one nobody knew about:

```
[HIGH] receivables: never proposes 'retry', which is cheaper and recovers more
  evidence: baseline retry: 1301 sends, 22.4% recovery at Rs 2/send;
            control uses 'voice': 125 sends, 16.0% at Rs 120/send
  where:    rcp/proposers/receivables.py
```

It also independently rediscovered a 1.7x opt-out miscalibration found earlier
by hand, and produced the wrong-fix finding described above.

`make strategy` audits the same tables from a different direction, and its
docstring records **why its own numbers are biased**: observed rates are
conditioned on what the arm chose to send, and later attempts drag the average
down, so the drift always reads as "shipped value too high". It is a prompt to
re-measure, not an answer — and the deterministic routine will only ever
*remove* a cause from `RAIL_FIXABLE`, never add one, because adding starts
silent retries on a cause nobody tested.

**Any provider.** The tool-use loop is written once and is provider-agnostic;
adapters translate only the wire format (`rcp/llm/adapter.py`). Switching is one
env var — `RCP_LLM=anthropic | groq | gemini | openai | together | ollama`.
Validated live on Groq. `make llm-check` does a one-tool round trip so a bad key
or a rotated model id surfaces in seconds rather than mid-run.

---

## Hinglish, and why it is an economic decision

An SMS segment holds 160 characters in GSM-7. Devanagari is not in GSM-7, so a
message containing it encodes as UCS-2 and a segment holds **70**. Gateways bill
per segment, so the same reminder costs roughly twice as much in हिन्दी as in
Hinglish — which is also what a large share of Indian users actually write in.

That is checkable rather than asserted. `rcp/compose/critic.py` computes the
billable segment count, and the dashboard shows it next to every message.

**Templates, not free text — and that is a regulatory fact.** WhatsApp will not
deliver an unregistered business-initiated message, and Indian SMS goes through
TRAI's DLT registry where the template is approved *before* it can be sent. A
composer writing novel prose per customer produces messages no gateway in this
market will accept.

So the split follows ADR-007 exactly:

| | |
|---|---|
| **authoring** | `rcp/agents/composer.py` drafts templates for gaps in the registry |
| **gating** | `critic.py` blocks coercive language, oversized copy, a URL in a voice script, a discount nobody approved |
| **executing** | `render.py` selects and fills a registered template, deterministically |

A draft goes to a review file. A human registers it and pastes it in. Nothing
the model writes can reach a customer — and since an unregistered template is
undeliverable anyway, the agent is feeding a review step that already exists
rather than bypassing one.

The critic is also a *tool the agent can call*, so it tests a draft against the
rules that will judge it and rewrites before submitting. Live on Groq, from a
cold start:

```
sms.hi.final  [1 segment, ucs2]
  आपका Rs {amount} बकाया है। {link} पर भुगतान करें। यह अंतिम नोटिस है।
```

## The dashboard

```bash
make dashboard      # http://localhost:8000
```

Server-rendered Jinja2 over the same SQLite files `make eval` writes. No React,
no build step, nothing to go stale — and no API layer, so a screen cannot
disagree with the run that produced it. Every page opens its connection with
`PRAGMA query_only = ON`, asserted by a test.

- **overview** — recovered, and *not* recovered with the reason and amount
- **cases** — every case; click through to the timeline, and **run the recovery
  agent on it live** — its tool calls, the compliance verdict it asked for, and
  the move it chose
- **agents** — the roster, what each one proposed, the red team's scoreboard
  recomputed on page load, and a box to **attack the compliance filter
  yourself**
- **audit** — the hash chain, recomputed every four seconds
- **live** — POST a signed Razorpay webhook and watch the event land

Three of those are worth pointing a reviewer at directly.

**Try to break the compliance filter.** A textarea on `/agents`, wired to the
same `try_copy` tool the red team agent uses. Type what a collections agent
might send and watch it refused by *conduct* — `third_party_disclosure`,
`credit_threat` — rather than by matched words. No model, no key, no database;
it works on a clone with no data generated. The page concedes upfront that
something will eventually get through, because the guarantee is human DLT
registration, not this filter.

**Hear the Hinglish.** `make voice` renders the spoken templates with macOS
`say` and commits the audio, so every reviewer can play them. Latin script is
the deliberate choice: Devanagari forces UCS-2 and bills at 70 characters a
segment against 160, and an en_IN voice reads “Aapke account par” the way a
person does.

**Ask the agent.** On any case, run the recovery agent and read its trail —
`compliance_preview` returning a denial, and the agent respecting it. With no
provider it runs the deterministic routine and says so; a rate limit produces
the same page with the provider error, which is the failure mode rehearsed
rather than discovered.

The case timeline is the one that matters: every rung, who decided it (`policy` /
`agent` / `compliance` / `stopping_rule`), every refusal with the rule and
whether it cost a ladder rung, and the message that actually went out.

## How it works

```
Razorpay webhook (HMAC-signed)
   └─ ingest/webhook.py     signature + dedup by UNIQUE constraint
   └─ ingest/normalize.py   ~200 decline strings → 8 root causes
        ↓
   cases.py                 every failure becomes a case, worked over days
        ↓
   escalation.py            ladder: retry → sms → whatsapp → voice   [ADR-008]
        ↓                   stopping rules decide when to give up
   proposers/               propose only, never execute              [ADR-001]
        ↓
   compliance/              allow · modify · deny, before scoring    [ADR-005]
        ↓
   arbiter/                 calibrate → value → select               [ADR-004]
        ↓
   compose/                 registered template → fill → critic
        ↓
   execute/outbox.py        commit, then send; exactly-once          [ADR-003]
        ↓
   audit.jsonl              hash-chained, canonical
```

Every case carries a timeline recording **who decided** each step — `policy`,
`agent`, `compliance`, or `stopping_rule` — so the trail answers "why did this
customer hear from us a fourth time" and "why did we give up on this invoice".

## What makes it defensible

**Invariants live in the schema.** Append-only tables enforced by triggers,
exactly-once as a `UNIQUE` constraint, webhook dedup as another:

```bash
sqlite3 data/seed_42/rcp.db "UPDATE events SET amount_paise = 0;"
#  Error: events is append-only
```

**The decision path cannot be non-deterministic.** The suite parses the AST of
every file under `rcp/` and fails the build on a `random` import, a `.now()`
call, or SQL containing `datetime('now')`. `make eval` twice produces
byte-identical output — checked by a test.

**Ground truth is a separate database file.** `rcp/` has no code path that can
construct `truth.db`.

**Suppression is priced, not trusted.** Every suppressed decision is replayed
through the outcome model: 49 false suppressions per run worth Rs 334,986,
reported next to the wins.

**`BEGIN IMMEDIATE`, measured.** 8 threads racing a contact cap of 3:

```
DEFERRED   final=1  committed=1  aborted=7   ("database is locked")
IMMEDIATE  final=3  committed=8  aborted=0
```

Under WAL, DEFERRED does not overshoot the cap — it *drops writes*, which here
means recoverable payments silently never contacted. See ADR-003.

## Commands

| | |
|---|---|
| `make data` | generate seeded synthetic events |
| `make eval` | three arms, 20 seeds |
| `make sensitivity` | break-even on the churn assumption |
| `make analyze` | agent investigates where the control plane loses |
| `make dashboard` | live dashboard on http://localhost:8000 |
| `make drafts` | composer agent drafts templates for gaps in the registry |
| `make redteam` | red team attacks the compliance critic to find its holes |
| `make voice` | render the spoken templates to audio (macOS; output committed) |
| `make llm-check` | one live tool-use round trip (`RCP_LLM=groq` etc.) |
| `make test` | 565 tests, ~19s |
| `make verify-audit` | recompute the hash chain, exit 1 on tamper |
| `make scale` | 100k-event stress generation |

## Decisions

- [ADR-001](docs/adr/ADR-001-proposers-never-execute.md) — proposers never execute
- [ADR-002](docs/adr/ADR-002-deterministic-decision-path.md) — deterministic decision path
- [ADR-003](docs/adr/ADR-003-outbox-and-idempotency.md) — outbox and idempotency
- [ADR-004](docs/adr/ADR-004-platform-side-valuation.md) — platform-side valuation
- [ADR-005](docs/adr/ADR-005-layered-failure-policy.md) — layered failure policy
- [ADR-006](docs/adr/ADR-006-no-vector-database.md) — no vector database
- [ADR-007](docs/adr/ADR-007-agents-author-code-executes.md) — agents author, deterministic code executes
- [ADR-008](docs/adr/ADR-008-cases-and-bounded-escalation.md) — the case as the unit of recovery work
- [ADR-009](docs/adr/ADR-009-refusal-dispositions.md) — a refusal says what it means for the case

The agent contract: [AGENTS.md](docs/AGENTS.md) — four rules every agent
follows, enforced by `tests/test_agent_contract.py` rather than only stated.

Limits and measurements: [SCALE.md](docs/SCALE.md) — including the fact that
**multi-tenancy is not implemented**: one database file is one merchant.

## Status

Built: storage, webhook ingest, normalization, cases and escalation, three
proposers, compliance engine with promise-to-pay, arbiter, outbox, simulator,
three-arm eval, sensitivity, the LLM layer, four agents (recovery, analyst,
composer, strategy), the Razorpay REST executor, the message composer and
critic, and the dashboard.

Not yet built: live SMS / WhatsApp / voice providers behind the executor port,
and outcome attribution from a real `payment.captured` webhook — in the eval the
simulator resolves outcomes.

**Known gaps**, both documented rather than hidden:

**Multi-tenancy does not exist.** One database file is one merchant; there is no
`tenant_id` column anywhere. The intended path is a database per tenant, and
what has to change is written down in [SCALE.md](docs/SCALE.md).

**The contact cap barely binds.** Cases spread over 60 days rarely hit
3-in-7-days, so `capped` and `baseline` behave almost identically (6.98 vs 7.07
contacts per customer). The cap is not what separates them; the valuation is.

## Verify it yourself

The strongest thing here is not a screenshot — it is that a reviewer can
reproduce the number without an API key in under ten seconds.

```bash
git clone … && make setup && make data && make eval
```

For zero local setup, this repository has a devcontainer: **Code → Codespaces →
Create** gives a browser terminal with everything installed, and `make eval`
runs there unchanged.
