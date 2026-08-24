# ADR-001: Proposers never execute

**Status:** accepted · **Date:** 2026-08-24

## Context

Each recovery strategy (subscription retries, abandoned carts, receivables) has
its own idea of what should happen when a payment fails. The obvious structure
gives each one the ability to act: it decides, it sends.

That structure has no place to put the questions that actually matter. Who
enforces the contact cap when three strategies each independently decide today
is the day to message the same customer? Who decides whether a Rs 200 recovery
is worth spending a Rs 2,00,000 customer's patience? Nobody owns that, because
every strategy is locally correct.

## Decision

A proposer returns a `Proposal` and that is the end of its authority. It never
touches an `Executor`, never writes to `actions`, and is never told whether it
won.

It also gets **exactly one bid per event per window**, enforced by
`UNIQUE (window_id, proposer_id, event_id)`. That forces each proposer to
resolve its own internal trade-offs and commit, so the arbiter arbitrates
*between* strategies rather than refereeing one strategy's shortlist.

Proposers may be optimistic. `claimed_success_prob` is advocacy, produced by the
module whose job is to win the auction; it is an input to valuation, not the
answer. See ADR-004.

## Consequences

Contact caps, budgets, and suppression become expressible, because exactly one
component decides and it sees every bid at once.

Enforced structurally rather than by convention:
- `tests/test_pipeline.py::test_proposers_never_reach_an_executor` parses the
  AST of everything under `rcp/proposers/` and fails on any import naming
  `execute`.
- `ProposalContext` is a frozen dataclass carrying only the event and the
  customer. No connection, no executor, no clock.

The cost is a layer of indirection: adding a strategy means writing a proposer
*and* making sure the arbiter can value it. That is the intended trade.
