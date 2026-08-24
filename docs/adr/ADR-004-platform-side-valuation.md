# ADR-004: The platform values actions, not the proposers

**Status:** accepted · **Date:** 2026-08-24

## Context

Every proposer reports a `claimed_success_prob`. Taking the highest one and
acting on it fails for two independent reasons.

First, the claim is advocacy. It comes from the module whose job is to win the
auction, and nothing checks it against what happened.

Second, and worse, success probability is the wrong quantity. Recovering Rs 200
from a customer worth Rs 2,00,000 by sending them a fourth message this week can
be a highly probable success and still be value-destroying. Optimising recovery
rate alone produces a system that recovers more and earns less.

## Decision

The arbiter computes its own number:

```
score = calibrated_prob × amount
      − channel_cost − incentive
      − opt_out_risk × customer_ltv × churn_weight
```

**`calibrated_prob`** blends the claim with the observed posterior from
`precedent.lookup()`, weighted by evidence: a precedent tier holding 3 trials
cannot overrule a proposer, one holding 200 gets the full configured weight
(`rcp/arbiter/calibration.py`). Without that ramp the first few observations
would swing decisions, and the system would look decisive while being
noise-driven.

**The churn term** is what stops the arbiter spending a lifetime value to
recover one invoice. Rail retries are exempt from it — a silent charge attempt
is invisible to the customer, and charging churn risk to retries makes the
arbiter avoid the cheapest and most effective action it has.

Everything that moved the decision is written to `decisions.detail`, so a
reviewer can recompute the winner by hand from the audit log.

## Consequences

Measured over 20 seeds (`make eval`), against a naive dunning baseline on the
same events:

| | baseline | control plane |
|---|---|---|
| actions sent | 247 | 193 |
| recovery rate | 17.3% | 19.2% |
| contacts / customer | 2.70 | 2.21 |
| net value ex-churn | Rs 326,435 | Rs 380,684 |

Higher recovery from **fewer** contacts — the valuation is doing work that a
confidence ranking could not.

**Two honest caveats.**

The churn term is high-variance. It charges a full lifetime value per opt-out,
and with ~1.4 opt-outs per run against LTVs spanning Rs 1k to Rs 2L, that single
term carries more variance than the entire policy effect. Net value including
churn wins 13/20 seeds; net value excluding it wins 18/20. The ex-churn figure
(+16.6%) is the one to trust, and both are reported for exactly this reason.

Suppression needs its own guard. A valuation that can decline to act can win on
cost by abandoning recoverable money, so `eval/metrics.py` computes **false
suppression** — replaying each suppressed decision through the outcome model to
count the times it would in fact have been paid. Currently 6.7 per run, worth
Rs 32,910. That number is part of the scorecard, not a footnote.
