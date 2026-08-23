# ADR-003: Transactional outbox, and idempotency as a database constraint

**Status:** accepted
**Date:** 2026-08-23

## Context

A decision to contact a customer or retry a charge has to become a real call to
Razorpay. The naive shape — decide, then call the provider, then record what
happened — has two failure modes that both cost real money:

- crash after the call but before recording → the retry double-charges
- crash after recording but before the call → the recovery never goes out

Razorpay also retries webhook deliveries on any non-2xx response, so duplicate
inbound events are routine rather than exceptional.

## Decision

**1. Outbox.** The decision and the intent-to-send commit in one transaction.
The provider call happens afterwards, from a separate relay.

```
BEGIN IMMEDIATE
  INSERT INTO decisions ...
  INSERT INTO actions (status='pending', idempotency_key=...)
COMMIT
-- transaction closed before any network call

relay: poll idx_actions_pending → executor.execute() → mark status
```

**No network call ever happens inside a transaction.** SQLite allows exactly one
writer; holding that lock across an HTTP round-trip would stall every other
writer for its duration, and a timeout would roll back a decision that may
already have been delivered.

**2. Idempotency is a UNIQUE constraint**, not a code path:

```sql
idempotency_key TEXT NOT NULL UNIQUE
```

with `INSERT ... ON CONFLICT(idempotency_key) DO NOTHING RETURNING id`. A `None`
return means this action already exists and must not be sent again.

**3. Inbound dedup is also a constraint** — `UNIQUE (provider, provider_event_id)`
on `events`. A replayed webhook delivery is a no-op with no application logic and
no dedup cache to fall out of sync.

**4. Providers are not trusted for exactly-once.** Executors pass
`idempotency_key` through where the provider supports one, but correctness rests
on our constraint, not theirs.

## Consequences

Crash safety falls out of the ordering rather than being reasoned about
per-call site:

- crash after COMMIT, before the call → the row is still `pending`; the relay
  retries
- crash after the call, before marking → the relay retries, and the UNIQUE key
  means the provider is asked to do the *same* thing under the *same* key rather
  than a second, new thing

`tests/test_idempotency.py` exercises the database, not the caller. The
application could be rewritten around it and exactly-once would still hold.

`actions` is the only table with mutable columns, and only four of them —
`status`, `sent_at`, `attempts`, `provider_ref`. A trigger rejects an UPDATE to
anything else, so a careless write elsewhere fails loudly instead of quietly
rewriting history.

The relay is bounded (`MAX_ATTEMPTS = 3`, `drain(max_rounds=4)`) so a
permanently failing executor settles rather than spinning.

## Note on `BEGIN IMMEDIATE`

The outbox insert and the contact-cap check share a transaction, and it must be
IMMEDIATE. Measured with 8 threads racing a cap of 3 on one file:

```
DEFERRED   final=1  committed=1  aborted=7   ("database is locked")
IMMEDIATE  final=3  committed=8  aborted=0
```

The DEFERRED failure is not the intuitive one. Under WAL it does not overshoot
the cap — snapshot isolation prevents that. It drops writes instead: a
transaction that read a snapshot and then tries to write gets
`SQLITE_BUSY_SNAPSHOT`, and `busy_timeout` cannot help, because waiting does not
make a stale snapshot fresh. Those lost writes are recoverable payments that
were within budget and never got contacted — false suppression, which
`eval/metrics.py` reports. IMMEDIATE takes the write lock before reading, so
`busy_timeout` can wait on it and every caller gets a correct answer.

See `tests/test_contact_cap_invariant.py::test_deferred_silently_loses_writes`.
