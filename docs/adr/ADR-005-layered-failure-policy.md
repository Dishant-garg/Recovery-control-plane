# ADR-005: Layered failure policy — politeness, guarantee, and lock

**Status:** accepted · **Date:** 2026-08-26

## Context

"Do not retry a dead mandate" is a rule three different layers could enforce,
and picking one is not obviously right:

- the **proposer** can simply decline to propose it
- the **compliance engine** can refuse the proposal
- the **database** can reject the write

Putting it in only one place fails in a specific way each time. Only in the
proposer, and the next proposer written in a hurry reintroduces the bug. Only in
the engine, and every proposer wastes its single bid on plans that will be
refused. Only in the database, and the system cannot explain *why* it declined.

## Decision

Three layers, each with a different job, and deliberate duplication between them.

**1. Proposers are polite.** `subscription.py` will not retry a dead mandate;
`cart.py` and `receivables.py` check recorded consent before choosing WhatsApp
or voice. This is not policy enforcement — it is a proposer being realistic
about what it can actually get, because it has **one bid per event** (ADR-001).
Spending that bid on a plan compliance will refuse means the customer hears
nothing at all, when a permitted SMS would have worked.

**2. The compliance engine is the guarantee.** `compliance/rules.py` re-checks
what the proposers were polite about, and adds what they cannot see —
quiet hours, sub-caps, active promises, incentive ceilings. Every verdict
carries the rule id and the observed value, so a refusal explains itself.

**3. The database is the lock.** Append-only triggers, `UNIQUE` on
`idempotency_key`, and `BEGIN IMMEDIATE` around the contact cap. No amount of
application-layer confusion produces a double-charge or a rewritten history.

**The duplication is the point.** Layer 1 makes the common case efficient,
layer 2 makes it correct, layer 3 makes it safe. Removing layer 1 costs
recovery value; removing layer 2 makes correctness a matter of proposer
discipline; removing layer 3 puts money at risk.

## Consequences

**Compliance runs before scoring.** A quiet-hours shift can move an action into
a different payday phase, which changes what it is worth. Scoring first would
value a plan that is not going to be executed. The order in
`arbiter/select.py` is: evaluate → drop denials and apply modifications → score
the survivors → rank → cap → commit.

**Rules can amend, not just refuse.** `Modify` matters more than it looks. A
message worth sending at 10pm is still worth sending at 9am; denying it discards
real recovery value over a fixable detail. Quiet hours shift, incentives clamp.

**The contact cap stays out of the engine.** It is the one rule needing the
write lock held across a read-then-write (ADR-003). Folding it into a stateless
rule engine would quietly make it advisory. It lives in `select.py`, inside the
same IMMEDIATE transaction as the decision and action rows.

**A denial is a decision.** When every proposal for an event is refused, a
`decisions` row is still written with `outcome='suppressed'` and a
compliance-specific reason, distinct from an economic suppression. Conflating
the two would hide the rules behind an economic-sounding explanation.

**It is measured, not asserted.** `eval/metrics.py` computes
`compliance_cost_paise` by replaying each compliance-suppressed decision through
the outcome model. Layer 1 working well shows up directly: making
`subscription.py` consent-aware dropped compliance denials from ~67 per run to
~6, because the proposer stopped bidding WhatsApp at customers who had never
opted in.
