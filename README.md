# Recovery Control Plane

A payment-recovery system where competing strategies **propose**, a single
arbiter **decides**, and a compliance engine can refuse or amend the result —
with a written reason for everything that did not happen.

Every number below is reproducible offline, with no API key, in about 20
seconds.

```bash
make setup && make data && make eval
```

## Results

20 seeds, 500 events each, all three segments. Baseline is naive dunning: act
immediately on every failure, no cap, no valuation, no rules. Both policies run
through identical machinery on the identical event set — only the policy differs.

| | baseline | control plane |
|---|---|---|
| actions sent | 486 | **337** |
| recovery rate | 18.4% | 15.4% |
| contacts / customer | 2.68 | **2.22** |
| recovered | Rs 697,552 | Rs 615,610 |
| recovered via promise-to-pay | — | Rs 41,928 |
| opt-outs | 2.9 | **1.8** |
| churn cost | Rs 303,679 | **Rs 89,636** |
| **net value** | Rs 390,920 | **Rs 563,629** |
| net value ex-churn | Rs 694,599 | Rs 653,265 |
| false suppressions | — | 16.9 (Rs 95,126) |
| compliance suppressions | — | 4.3 (Rs 5,681) |

**+44.2% net value, winning 16 of 20 seeds.**

### It is one number hiding three very different businesses

| segment | baseline net | control plane | delta |
|---|---|---|---|
| subscription | 199,672 | 316,134 | **+58.3%** |
| receivables | −9,863 | 55,574 | **negative → positive** |
| cart | 201,112 | 191,921 | −4.6% |

Naive dunning on B2B receivables is **net-negative** — 96 sends per run that
destroy more value than they recover. The control plane turns that into a profit
with 22.

Cart is the honest counter-example, and it is not a bug to be tuned away: a
one-off cart shopper is worth Rs 7,229 here against Rs 101,344 for a subscriber,
so churn avoidance — this system's entire edge — has almost nothing to buy.
**The value scales with customer lifetime value.** Sell it to subscription and
receivables merchants; for high-volume low-LTV checkout, naive dunning is fine.

### The result comes with a condition, and the condition is the interesting part

The control plane **does not recover more money.** It recovers less (Rs 616k vs
Rs 698k) from 31% fewer contacts. Its advantage is avoiding expensive churn and
converting unpaid invoices into promises. Excluding churn it still *loses* by 6%.

So the honest question is what an opt-out actually costs. `make sensitivity`
sweeps that assumption instead of picking one:

| opt-out costs | baseline | control plane | delta | wins |
|---|---|---|---|---|
| 0% of LTV | 724,195 | 800,193 | +75,997 | 6/8 |
| 10% | 686,187 | 738,568 | +52,381 | 7/8 |
| 25% | 629,174 | 652,330 | +23,155 | 5/8 |
| 50% | 534,153 | 543,128 | +8,975 | 5/8 |
| 75% | 439,132 | 548,258 | +109,126 | 7/8 |
| 100% | 344,111 | 528,323 | **+184,212** | **7/8** |

It wins across the whole range, but for two different reasons at the two ends.
Where opt-outs are nearly free, the arbiter stops suppressing and its **timing**
edge shows: retries scheduled just after payday recover **36.7%** against the
baseline's fire-immediately **23.2%** — 1.6x, same rail, same customers. Where
opt-outs are expensive, **restraint** dominates. The 25–50% middle is the
thinnest margin (5/8 seeds), and it is where the valuation is worst calibrated.

An earlier version of this table had a break-even at 25% and a U-shaped curve.
That was not a property of the approach — it was `cart.py` never considering the
`retry` channel, so it paid 7–17x more per send to recover less. The sweep is
what surfaced it.

## How it works

```
Razorpay webhook (HMAC-signed)
   └─ ingest/webhook.py      signature + dedup by UNIQUE constraint
   └─ ingest/normalize.py    ~200 decline strings → 8 root causes
        ↓
   proposers/               subscription · cart · receivables    [ADR-001]
        ↓                   propose only, never execute
   compliance/              allow · modify · deny, before scoring [ADR-005]
        ↓
   arbiter/                 calibrate → value → select            [ADR-004]
        ↓
   execute/outbox.py        commit, then send; exactly-once       [ADR-003]
        ↓
   audit.jsonl              hash-chained, canonical
```

Three proposers with deliberately opposed instincts, so the arbiter has
something real to arbitrate: subscription **waits** for payday; cart **cannot
wait** because the impulse dies; receivables wants a **date**, not a payment.

## What makes it defensible

**Invariants live in the schema.** Append-only tables enforced by triggers,
exactly-once as a `UNIQUE` constraint, webhook dedup as another. Try it:

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

**Suppression is measured, not trusted.** A system allowed to decline to act can
win on cost by abandoning recoverable money. Every suppressed decision is
replayed through the outcome model: 18.9 false suppressions per run worth
Rs 104,159, reported alongside the wins.

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
| `make eval` | baseline vs control plane, 20 seeds |
| `make sensitivity` | break-even on the churn assumption |
| `make test` | 211 tests, ~4s |
| `make verify-audit` | recompute the hash chain, exit 1 on tamper |

## Decisions

- [ADR-001](docs/adr/ADR-001-proposers-never-execute.md) — proposers never execute
- [ADR-002](docs/adr/ADR-002-deterministic-decision-path.md) — deterministic decision path
- [ADR-003](docs/adr/ADR-003-outbox-and-idempotency.md) — outbox and idempotency
- [ADR-004](docs/adr/ADR-004-platform-side-valuation.md) — platform-side valuation
- [ADR-005](docs/adr/ADR-005-layered-failure-policy.md) — layered failure policy
- [ADR-006](docs/adr/ADR-006-no-vector-database.md) — no vector database

## Status

Built: storage, webhook ingest, normalization, three proposers, compliance
engine + promise-to-pay, arbiter, outbox, simulator, eval, sensitivity.

Not yet built: LLM agents (`rcp/llm/`, `rcp/agents/`), live Razorpay executors,
viewer. `make live` fails loudly rather than pretending.

**Known gap:** the arbiter's opt-out slope (`opt_out_per_extra_contact`) is
still a config prior. Only the base rate is learned from outcomes
(`precedent.observed_opt_out_rate`). Learning the slope too is the most likely
fix for the weak middle of the sensitivity curve.
