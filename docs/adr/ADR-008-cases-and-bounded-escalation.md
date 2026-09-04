# ADR-008: The case as the unit of recovery work

**Status:** accepted · **Date:** 2026-08-28

## Context

The daily loop collected events that occurred *that day*, decided once, and
never looked at them again. A failure on day 3 was invisible on day 10.

That is not a small gap. Two of the four things the brief asks a recovery system
to demonstrate — **compliant escalation** and **stopping rules** — are
statements about a *sequence*. Nothing persisted between days, so there was no
sequence: nothing to escalate, and nothing to stop. The system could answer "was
this one action worth sending" and could not answer "should we still be chasing
this invoice three weeks later", which is the question a recovery workflow
exists to answer.

The obvious patch — re-query old events each day — fails immediately. Without
somewhere to record what was already tried, every day looks like the first, so
the system re-proposes the cheapest channel forever and no rule can tell an
opening attempt from a fifth one.

## Decision

**A case is one unit of recovery work, carried across days.** One row in
`cases` per event, worked until it settles or is abandoned. `rcp/cases.py` owns
the lifecycle; `rcp/caseloop.py` runs one day over it.

**Channel order lives in the ladder, not in proposers.** `config/policy.yaml`
holds a per-segment list — `subscription: [retry, sms, whatsapp, voice]` —
ordered cheapest and least intrusive first. A proposer free to pick any channel
each time could reach for voice on the first attempt; a ladder cannot. This is
what makes escalation *compliant* rather than merely repeated.

**`cases.rung` means "the rung to try next", not "the rung last tried."**
`cases.advance` moves it on whenever a rung is *consumed*. See ADR-009 for what
consumes one.

**Stopping rules are a layer, not an `if`.** `escalation.should_stop` runs five
rules in a deliberate order and returns a `Stop` carrying its rule id and the
observed numbers — the same shape as `store.Suppressed` and
`compliance.rules.Deny`. Every refusal in this system explains itself the same
way.

| rule | fires when |
|---|---|
| `opt_out` | customer asked not to be contacted — never overridable |
| `max_attempts` | 4 actions have gone out |
| `ladder_exhausted` | no viable rung remains |
| `stale` | case is 45 days old |
| `not_worth_chasing` | expected value below the floor |

**Every state change records who decided it.** `case_events.decided_by` is one
of `policy`, `agent`, `compliance`, `stopping_rule`. That column is the whole
point of the table.

## Consequences

**The timeline is the audit artifact.** "This customer heard from us four times"
is not an audit trail. "The agent escalated to WhatsApp on day 6 because two SMS
attempts went unanswered and precedent put recovery at 0.31, and compliance
refused voice on day 9 for lack of recorded consent" is. `decided_by` is what
lets a reader tell an agent's judgement call from a fixed policy's output, and
both from a rule firing.

**Opt-out is checked before valuation.** A customer who asked not to be
contacted is not a scoring input, so `hard_stop_on_opt_out` is first and takes
no arguments about expected value. `not_worth_chasing` is checked last because
it is the only rule needing a database round trip.

**Stopping rules are priced against the rung we would actually try next**, not
the current one. Asking "is this worth chasing" against a channel we are not
going to use answers the wrong question — a case with a dead mandate should be
valued on the SMS it will actually get, not the retry it will skip.

**The ladder skips rungs that are structurally impossible.** Every ladder opens
with `retry`, and roughly a third of root causes (`mandate_expired`,
`card_expired`, `invalid_account`) can never be fixed by one. Measured: **900 of
900** `channel_eligibility` refusals landed on rung 0. `escalation._viable`
skips those rungs. This is ADR-005's layer 1, applied to the ladder: the
compliance guarantee is untouched, the ladder simply stopped provoking it.

**An event now produces many decisions.** `decisions` is keyed on
`(window_id, event_id)`, so a case reviewed on eight days writes eight rows.
Anything reading `decisions` as one-row-per-event — metrics especially — had to
learn the difference between "events decided" and "actions sent".

**The daily sweep must stay cheap as closed cases accumulate.** `due_for_review`
is served by a partial index on `next_review_at`, and closed cases set it to
NULL, so they leave the index rather than being filtered out of it.

**Total order on `(next_review_at, id)`.** SQLite guarantees nothing on ties.
An unstable order here would make the entire run non-reproducible, which ADR-002
forbids.
