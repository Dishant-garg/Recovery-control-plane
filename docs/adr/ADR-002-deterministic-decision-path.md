# ADR-002: The decision path is deterministic

**Status:** accepted · **Date:** 2026-08-24

## Context

This project's central claim is a number: the control plane beats a naive
baseline by a stated margin. A reviewer has to be able to reproduce that number
on their own machine, offline, with no API key. A decision path that varies
between runs makes the claim unfalsifiable.

Non-determinism has more entry points than it first appears:

- `random` / `uuid` for identifiers
- wall-clock reads (`datetime.now()`, `time.time()`)
- `ORDER BY score DESC` with tied scores — **SQLite guarantees nothing on ties,
  and the order can change after `ANALYZE` alters the query plan**
- SQL `random()`, `datetime('now')`, `CURRENT_TIMESTAMP`
- set and dict iteration order leaking into query parameters

## Decision

Nothing under `rcp/` may be non-deterministic.

1. **IDs are content-derived**, not random — `store.content_id()` returns
   `sha256(parts)[:16]`. Re-running a window produces byte-identical rows, so
   the UNIQUE constraints turn a double-run into a no-op.
2. **Time is an argument.** `now_ms` is threaded from the caller. `rcp/timeutil.py`
   may use `datetime` for calendar arithmetic but never asks what time it is.
3. **Every decision-feeding query carries a total order** —
   `ORDER BY score DESC, proposer_id ASC, proposal_id ASC`.
4. **Seeded randomness lives in `sim/`** and nowhere else.
5. **LLM output is cached** on `sha256(gateway|code|text)`, and the default
   `--llm=fallback` path makes no network call at all.

## Consequences

`tests/test_ground_truth_isolation.py` walks the AST of every file under `rcp/`
and fails the build on a `random`/`uuid` import, a `.now()`/`.utcnow()` call, or
a SQL string containing `random()` / `datetime('now')` / `CURRENT_TIMESTAMP`.
Comments and docstrings are excluded, so prose explaining the rule does not trip
it.

`tests/test_pipeline.py::test_eval_is_byte_reproducible` runs the full eval
twice and compares `results.json` byte for byte.

The tie-breaking rule is the subtle one and the easiest to regress: a plain
sort by score looks correct and passes every test that does not have ties in it.
`test_ties_break_deterministically` constructs the tie on purpose.

The cost is that convenience calls are unavailable inside `rcp/`. Threading
`now_ms` through every signature is tedious, and retrofitting it later would be
far worse — which is why the check fails the build rather than warning.
