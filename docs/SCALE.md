# Scale: what is measured, and what breaks first

Everything below is measured on this repository, on a laptop. Where a limit is
not measured it says so.

## What it does today

```
100,000 events generated + normalized      23.0 s        59 MB
cold rebuild, one seed (generate + eval)    7.8 s
full test suite                            18.7 s        565 tests
```

Query latency at 100,000 events, mean of 20 runs:

```
contact_count (indexed)      0.02 ms
events for one customer      0.04 ms
group-by over all events    63.75 ms
```

The indexed paths — which is every query on the decision path — are
microseconds. The 64 ms number is a full table scan and appears only in
`eval/metrics.py`, which runs once at the end of a batch.

## Where SQLite stops

SQLite is the right choice here and its limits are specific rather than vague.

**One writer.** WAL allows concurrent readers with one writer, and the decision
path holds `BEGIN IMMEDIATE` across the contact-cap read-then-write (ADR-003).
Measured, 8 threads racing a cap of 3:

```
DEFERRED   final=1  committed=1  aborted=7   ("database is locked")
IMMEDIATE  final=3  committed=8  aborted=0
```

Under WAL, DEFERRED does not overshoot the cap — it *drops writes*, which here
means recoverable payments silently never contacted. So the writer is
serialized deliberately, and throughput is bounded by one core doing
transactions. That is thousands per second, far above what a daily dunning
sweep needs, and it is a hard ceiling rather than something that degrades
gracefully.

**One file, one machine.** There is no replication and no failover. A daily
batch that can be re-run from the event log tolerates that; a synchronous
API in front of live payments would not.

**Not measured:** behaviour past ~10 million events, or with the WAL under
sustained concurrent read pressure. Do not quote a number for either.

## Multi-tenancy is not implemented

**One database file is one merchant.** There is no `tenant_id` column anywhere
in `rcp/migrations.py`, and nothing in the code separates one merchant's data
from another's. Running two merchants against one file today would mix their
customers, their contact caps, and their audit trails.

The intended path is **a database per tenant** — `data/tenants/<id>/rcp.db` —
rather than a `tenant_id` column:

- isolation is structural, not a `WHERE` clause somebody forgets
- WAL is per-file, so tenants cannot block each other's writer
- offboarding is `rm -rf`, which makes a deletion request provable

Two things have to change for it, and both are real work rather than
configuration:

1. `store.open_rcp(seed)` becomes `open_tenant(tenant_id)`.
2. `rcp/config.py::load` is `@lru_cache`-keyed on the config name against one
   global `CONFIG_DIR`. Per-tenant policy needs the tenant in the cache key,
   or every merchant silently shares whichever policy loaded first.

Beyond roughly a few thousand tenants on one host, this stops being the right
shape and the answer is Postgres with row-level security. That migration is not
started.

## The audit log

Canonical storage is JSONL; the SQLite `audit_mirror` table is a derived index
for the viewer. The chain is verified by recomputation
(`make verify-audit`, exit 1 on tamper), not by trust.

Growth is roughly one record per decision plus one per case closure. For a
500-event seed over 60 days that is ~5,000 records and about 460 KB.

**This was wrong until recently and the failure is worth recording.** Because
`eval/run.py` forks a fresh database per arm but reopened the existing log,
records accumulated across every run — 136,644 audit records against 1,049 rows
in `decisions`, and 51 MB for a run that sent 577 messages. The chain verified
the whole time. It was internally consistent and described databases that no
longer existed.

`AuditLog(path, reset=True)` fixes it and is documented as valid only when the
database is being replaced too. In production a log outliving its database is
the point.

## Cost, not just capacity

The system is designed so the expensive resources are the ones that are
bounded:

- **No LLM call is on the decision path.** `make eval` runs offline with no API
  key. Agents author; deterministic code executes (ADR-007).
- **No embeddings, no vector index.** Precedent is a feature-keyed
  Beta-Binomial over a SQL view, answering in microseconds (ADR-006).
- **The live agent is budgeted.** `--live N` caps LLM calls per run and falls
  back to the policy past the budget or on a provider error.
- **SMS segments are counted before sending**, so Devanagari's 70-character
  segments show up at compose time rather than on the invoice.

## What would break first, in order

1. **Multi-tenancy**, because it does not exist. This is the first thing a
   second customer requires.
2. **Outcome attribution.** In the eval the simulator resolves outcomes. In
   production a `payment.captured` webhook has to be matched back to an open
   case within an attribution window, and that logic is not written.
3. **Precedent cold start.** A new merchant has no history, `MIN_TRIALS = 20`
   backs off to the `global` tier, and that is empty too. Shadow mode — decide
   without sending — is the intended answer and would populate the tier without
   contacting anyone.
4. **The writer**, at sustained multi-thousand-transactions-per-second. Nothing
   in this problem shape gets close, but it is the ceiling.
