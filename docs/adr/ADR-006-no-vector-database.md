# ADR-006: No vector database; precedent is feature-keyed

**Status:** accepted
**Date:** 2026-08-23

## Context

Two places in this system look like they want semantic retrieval:

1. **Decline text → root cause** (`rcp/ingest/normalize.py`). Gateway decline
   strings are free text and vary by bank and rail.
2. **"Similar past cases"** for the diagnosis agent, used to estimate how likely
   a given recovery attempt is to succeed.

The obvious move is to embed both corpora and reach for a vector store — Chroma,
Qdrant, pgvector, or `sqlite-vec` to stay single-file.

## Decision

No vector database, and no embeddings anywhere in the decision path.

**For (1), an exact cache.** The corpus is roughly 8 root causes × ~25 text
variants ≈ 200 unique strings, total, for the life of the project.
`sha256(gateway|code|text)` hits ~100% after the first pass. Approximate nearest
neighbour would replace an O(1) exact lookup with a fuzzy one that is slower,
non-deterministic, and unable to reproduce a prior run byte for byte.

**For (2), a feature-keyed Beta-Binomial posterior** (`rcp/precedent.py`), keyed
on `(root_cause, amount_bucket, payday_phase, channel)` with a fixed backoff
hierarchy and a Beta(1,1) prior, computed over the `precedent_view` SQL view.

## Consequences

**The deciding argument is explainability, not cost.** This system decides
whether to charge someone's card or contact them again. A precedent must be able
to justify why it matched. Compare what each approach can put in an audit line:

| | audit line |
|---|---|
| vector retrieval | `similar to case #4471 (cosine 0.87)` |
| feature-keyed | `3 of 11 prior attempts with root_cause=insufficient_funds, amount 500-2000, pre-payday, channel=sms succeeded → posterior 0.31 (tier=exact)` |

The first is unfalsifiable. The second can be checked by hand with a SQL query,
which is the standard a compliance reviewer should be able to hold us to.

Secondary consequences:

- **Determinism is preserved.** `make eval` produces identical bytes across
  runs, which is what lets the README lead with a reproducible number.
- **Backoff is legible.** When evidence is thin the tier widens and says so
  (`tier=global, thin evidence`) rather than returning a confident-looking
  number from three observations. An ANN index has no equivalent — it always
  returns *k* neighbours regardless of whether they mean anything.
- **Zero infrastructure and zero cost.** No embedding API calls, no index to
  build, no extra dependency to install or explain.
- **Search, if ever needed,** is FTS5, which ships inside SQLite. Roughly five
  lines, no dependency.

## What would change this decision

Feature-keyed precedent works because the feature space is small and known. It
would stop being the right choice if:

- free-text merchant notes or customer replies became a decision input, where
  the useful signal genuinely is unstructured; or
- the root-cause taxonomy grew past a few hundred variants, at which point exact
  caching stops covering the tail.

Neither is true at the scale this system is built and evaluated for.

## Alternatives considered

- **`sqlite-vec`** — stays single-file and needs no server, which addresses the
  operational objection but not the explainability one. Still adds a binary
  dependency and non-determinism to the decision path.
- **In-memory numpy brute force** — no infrastructure at all, and viable at this
  corpus size. Rejected for the same reason: cosine similarity cannot explain
  itself in an audit line.
