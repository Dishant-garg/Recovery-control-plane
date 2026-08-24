# Recovery Control Plane

A payment-recovery system where competing strategies **propose**, and a single
arbiter **decides** — under contact caps, budgets, and a written reason for
everything it chose not to do.

Every number below is reproducible offline, with no API key, in about 11
seconds.

```bash
make setup && make data && make eval
```

## Results

20 seeds, ~254 subscription events each, against a naive dunning baseline on the
identical event set and the identical outcome model.

| | baseline | control plane |
|---|---|---|
| actions sent | 247 | **193** |
| payments recovered | 44 | **49** |
| recovery rate | 17.3% | **19.2%** |
| contacts / customer | 2.70 | **2.21** |
| recovered | Rs 327,943 | **Rs 382,891** |
| net value ex-churn | Rs 326,435 | **Rs 380,684** |
| false suppressions | — | 6.7 (Rs 32,910) |

**+16.6% net value ex-churn, from 22% fewer contacts.** Wins 18/20 seeds.

The mechanism is timing, not volume. Across all 20 seeds, retries scheduled for
just after payday recover **34.3%** of the time (732 of 2,134); the baseline's
fire-immediately retries recover **21.3%** (722 of 3,389). Same rail, same
customers — 1.6x the hit rate from waiting for the money to land.

### Reading these numbers honestly

- **Two win-rates are reported, and they disagree.** Net value including churn
  wins 13/20 seeds; excluding it, 18/20. Churn charges a full customer lifetime
  value per opt-out, and with ~1.4 opt-outs per run against LTVs spanning Rs 1k
  to Rs 2L, that one term carries more variance than the whole policy effect.
  The ex-churn figure is the trustworthy one.
- **Scope is the `subscription` segment only** — the one segment with a proposer
  built so far. Both policies are held to that same event set, so the comparison
  measures policy rather than coverage. It widens by itself as `cart.py` and
  `receivables.py` land.
- **False suppression is on the scorecard.** A system allowed to decline to act
  can win on cost by quietly abandoning recoverable money, so every suppressed
  decision is replayed through the outcome model to count what it cost.

## How it works

```
Razorpay webhook (HMAC-signed)
   └─ ingest/webhook.py      signature + dedup by UNIQUE constraint
   └─ ingest/normalize.py    ~200 decline strings → 8 root causes
        ↓
   proposers/               propose only, never execute        [ADR-001]
        ↓
   arbiter/                 calibrate → value → select         [ADR-004]
        ↓
   execute/outbox.py        commit, then send; exactly-once    [ADR-003]
        ↓
   audit.jsonl              hash-chained, canonical
```

## What makes it defensible

**Invariants live in the schema, not in code review.** Append-only tables
enforced by triggers; exactly-once as a `UNIQUE` constraint; webhook dedup as
another. Try to break one:

```bash
sqlite3 data/seed_42/rcp.db "UPDATE events SET amount_paise = 0;"
#  Error: events is append-only
```

**The decision path cannot be non-deterministic.** The test suite parses the AST
of every file under `rcp/` and fails the build on a `random` import, a `.now()`
call, or SQL containing `datetime('now')`. `make eval` twice produces
byte-identical output — checked by a test.

**Ground truth is a separate database file.** `rcp/` has no code path that can
construct `truth.db`. Two filesystem paths beat any amount of discipline.

**`BEGIN IMMEDIATE`, measured.** 8 threads racing a contact cap of 3:

```
DEFERRED   final=1  committed=1  aborted=7   ("database is locked")
IMMEDIATE  final=3  committed=8  aborted=0
```

Under WAL, DEFERRED does not overshoot the cap — it *drops writes*, which in
this domain means recoverable payments silently never contacted. See ADR-003.

## Commands

| | |
|---|---|
| `make data` | generate seeded synthetic events |
| `make eval` | baseline vs control plane, 20 seeds |
| `make test` | 157 tests, ~2s |
| `make verify-audit` | recompute the hash chain, exit 1 on tamper |

## Decisions

- [ADR-001](docs/adr/ADR-001-proposers-never-execute.md) — proposers never execute
- [ADR-002](docs/adr/ADR-002-deterministic-decision-path.md) — deterministic decision path
- [ADR-003](docs/adr/ADR-003-outbox-and-idempotency.md) — outbox and idempotency
- [ADR-004](docs/adr/ADR-004-platform-side-valuation.md) — platform-side valuation
- [ADR-006](docs/adr/ADR-006-no-vector-database.md) — no vector database

## Status

Built: storage layer, webhook ingest, normalization, subscription proposer,
arbiter (collect / calibrate / score / select), outbox, simulator, eval.

Not yet built: `compliance/`, remaining proposers, LLM agents, live Razorpay
executors, viewer. `make live` and `make sensitivity` fail loudly rather than
pretending.
