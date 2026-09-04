# ADR-009: A refusal has to say what it means for the case

**Status:** accepted · **Date:** 2026-08-28

## Context

ADR-008 made `cases.rung` mean "the rung to try next", advanced whenever a rung
is consumed. The first version of that rule was: **a compliance refusal consumes
the rung.** The reasoning was that we tried, and trying is what a rung is for.

It was written to fix the opposite bug. When only *sends* advanced the rung, a
case refused by compliance never climbed — it sat on the same rung, came back
after every cooldown, was refused identically, and only stopped when the 45-day
staleness rule closed it. That produced **7,092 held reviews against 513 real
actions**, and one case with 47 timeline entries that never sent anything.

The fix over-corrected, and measurably:

```
95 cases exhausted the entire ladder having sent NOTHING (attempts = 0)
1,030 compliance refusals against 570 real actions
382 of 500 cases closed as `ladder_exhausted`
```

False suppression jumped from Rs 65,259 to Rs 497,030, and the control plane
started losing below 25% `ltv_fraction` when it had previously won across the
whole sweep. A case that never managed to send anything is a bug, not an
outcome.

Both versions are wrong for the same reason: **they treat every refusal
identically, and refusals are not alike.**

- "No WhatsApp consent" — this rung will *never* work. Climb past it.
- "Contact cap reached this week" — the rung is fine, the *timing* is not. Come
  back when the window rolls.
- "Customer opted out" — the case is over.

Collapsing those three into one boolean loses the only information that decides
what to do next.

## Decision

**Every `Deny` carries a disposition** describing what the refusal means for the
case that provoked it:

| disposition | meaning | rules | effect |
|---|---|---|---|
| `channel_unusable` | this rung will never work | `consent`, `channel_eligibility`, `channel_sub_cap` | consume the rung, climb |
| `retry_later` | the rung is fine, the timing is not | `active_promise`, `contact_cap` | **do not** consume; reschedule |
| `stop` | the case is over | `opt_out` | close immediately |

A `retry_later` denial also carries `retry_after_ms` — the moment the answer
could actually differ. `caseloop._refusal` schedules the next review past that
point rather than after the ladder cooldown, so the case does not come back to
be refused for the identical reason.

**The default is `channel_unusable`.** A new rule that forgets to declare a
disposition consumes the rung — the case keeps moving and eventually stops. The
opposite default risks a case that never terminates, which is the worse failure.

## Consequences

**Measured, on the same seeds:**

```
cases exhausting the ladder having sent nothing:  95   →  0
acted / held ratio:                              0.55  →  3.89
```

Currently, held reviews split `channel_unusable` 114 / `retry_later` 44 — the
distinction is doing real work, not decorating the log.

**The contact cap is inferred, not declared.** It is the one rule that lives in
the arbiter's transaction rather than the rule engine, because it needs the
write lock held across a read-then-write (ADR-003, ADR-005). So it never appears
in the compliance trail, and `caseloop._refusal` recognises it from the decision
reason instead. That is a genuine wart: a string match standing in for a typed
value. It is contained to one branch and asserted by a test, but it is the
seam where a reworded reason string would silently change behaviour.

**"Negative platform value" closes the case rather than climbing.** Climbing
after that verdict would be backwards — the ladder only gets more expensive, so
an action already judged not worth Rs 2 cannot become worth Rs 120 one rung up.
This is handled in `_refusal` as a distinct branch, not as a disposition,
because it comes from the arbiter's valuation and not from a rule.

**The disposition is written to the case timeline**, in `detail.disposition`
alongside `rung_spent`. The timeline can therefore answer "why did this case
stall for nine days without sending anything" with the rule id, what it meant,
and whether it cost a rung.

**It is measurable.** `eval/metrics.py` reports `refusals that cost a rung`
next to `refusals that did not`. That is the metric that would have caught the
original defect on the run that introduced it, rather than three days later.
