"""The strategy agent: what the proposers' hand-written tables should have said.

Every proposer ships two constants that were originally guessed:

    RAIL_FIXABLE   which root causes a silent retry can clear on its own
    BASE_CLAIM     recovery odds per root cause

**These are not equally consequential, and the difference is the point.**
`BASE_CLAIM` feeds a claimed probability that `arbiter/calibration.py` then
discounts against observed precedent, so an error there is damped before it
reaches a decision. `RAIL_FIXABLE` decides *which channel the proposer bids* --
and that happens before calibration sees anything. A mistake there is not
damped by anything.

Both shipped bugs were in `RAIL_FIXABLE`. `cart.py` omitted it entirely and
messaged for everything, paying 7-17x more per send; `receivables.py` had the
same omission and was found only by the eval analyst noticing the baseline
recovering 22.4% on Rs 2 retries while this proposer spent Rs 120 per voice call
to recover 16.0%.

So this agent recomputes both from what actually happened. Like the composer
(`rcp/agents/composer.py`) it writes a proposal to a review file and never
patches a source file: these constants are the system's priors, and a prior
that rewrites itself from its own outcomes without a human looking is a
feedback loop, not a calibration.

The deterministic routine does the arithmetic, which is genuinely all
arithmetic. What the model adds is judgement about *which* differences are
worth acting on -- a tier with 14 trials and a 30-point gap is noise, and
telling that apart from a real miscalibration is the part that is not a
formula.

## The observed rates are biased, and the bias has a direction

This has to be said plainly, because the numbers look more authoritative than
they are.

**Selection.** The rates come from actions the arm chose to send. The control
plane sends where it expects to succeed, so its observed rate is conditioned on
its own judgement being right, not on a random sample of cases.

**Attempt order.** A cause chased four times contributes four rows, and later
attempts are systematically worse (`RETRY_DECAY` compounds). A cause that gets
escalated more therefore looks worse than it is, and `BASE_CLAIM` describes a
*first* attempt.

Both biases push the observed rate **below** the true first-attempt
probability, which is why the drift below always reads as "shipped value too
high". Treat these as a prompt to re-measure, not as the answer -- and prefer
the `baseline` arm, which sends far more indiscriminately and is therefore the
less selected sample.

**And `rail_comparison` is thinner than it looks.** `escalation._viable` skips
the retry rung for causes retry cannot fix (ADR-008), so in practice a cause
receives retries *or* messages, almost never both. Measured on the baseline
arm, every rail-fixable cause had **zero** message trials. The fix for bug #3
in the README removed the natural experiment this tool wants.

That is why `submit_revision` refuses a RAIL_FIXABLE addition when the
messaging side is empty rather than skipping the check: an absent comparison
must read as "cannot tell", never as "no objection".
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from rcp.env import load_dotenv
from rcp.llm.client import AgentResult, LLMClient, Tool
from rcp.precedent import ALPHA, BETA, MIN_TRIALS
from rcp.store import REPO_ROOT

DRAFTS = REPO_ROOT / "data" / "drafts"

# The shipped tables, imported rather than duplicated so this cannot drift from
# what the proposers actually use.
SHIPPED: dict[str, dict[str, Any]] = {}


def _shipped() -> dict[str, dict[str, Any]]:
    if not SHIPPED:
        from rcp.proposers import cart, receivables

        SHIPPED.update({
            "cart": {"rail_fixable": set(cart.RAIL_FIXABLE),
                     "base_claim": dict(cart.BASE_CLAIM)},
            "receivables": {"rail_fixable": set(receivables.RAIL_FIXABLE),
                            "base_claim": dict(receivables.BASE_CLAIM)},
        })
    return SHIPPED


SYSTEM = """You audit the hand-written probability tables inside a payment
recovery system, against what actually happened.

Two tables per segment:

  RAIL_FIXABLE  which root causes a silent retry can clear without contacting
                anyone. This decides which CHANNEL gets bid, before any
                calibration runs. An error here is not damped by anything, and
                both bugs this system has shipped were here.

  BASE_CLAIM    recovery odds per root cause. A downstream calibrator already
                discounts these against observed precedent, so an error here is
                damped. Treat it as lower priority.

Your job is to decide which entries the evidence actually justifies changing.

The hard part is not the arithmetic -- the tools do that. It is telling a real
miscalibration from noise. A tier with 12 trials and a 30-point gap is noise. A
tier with 400 trials and a 6-point gap may not be. Below """ + str(MIN_TRIALS) + """
trials, say so and leave the entry alone.

Be especially careful with RAIL_FIXABLE. Adding a cause means silent retries
will be attempted for it. If retry has never been tried for that cause there is
no evidence either way, and the honest answer is "untested", not "add it".
Removing a cause is the safer direction and needs less evidence than adding.

Call observed_rates and rail_comparison first. Then call submit_revision once
per change you want, with the evidence. Do not submit a change you cannot
point at a trial count for."""


def _posterior(successes: int, trials: int) -> float:
    """Beta-Binomial, the same prior `rcp/precedent.py` uses."""
    return (successes + ALPHA) / (trials + ALPHA + BETA)


def observed_rates(
    conn: sqlite3.Connection, segment: str | None = None
) -> list[dict[str, Any]]:
    """Success rate per (segment, root_cause, channel) from real outcomes."""
    sql = (
        "SELECT e.segment AS segment, e.root_cause AS root_cause, "
        "       a.channel AS channel, count(*) AS trials, "
        "       COALESCE(SUM(o.succeeded), 0) AS successes "
        "FROM outcomes o "
        "JOIN actions a ON a.id = o.action_id "
        "JOIN events  e ON e.id = o.event_id "
    )
    params: list[Any] = []
    if segment:
        sql += "WHERE e.segment = ? "
        params.append(segment)
    sql += "GROUP BY segment, root_cause, channel ORDER BY segment, root_cause, channel"

    # Compact keys. These rows go into a model's context as JSON, and the
    # verbose form of this table alone can exceed a small per-minute token
    # budget before the prompt is even counted.
    return [
        {
            "segment": r["segment"], "cause": r["root_cause"],
            "channel": r["channel"], "n": r["trials"], "wins": r["successes"],
            "p": round(_posterior(r["successes"], r["trials"]), 3),
            "thin": r["trials"] < MIN_TRIALS,
        }
        for r in conn.execute(sql, params)
    ]


def rail_comparison(
    conn: sqlite3.Connection, segment: str | None = None
) -> list[dict[str, Any]]:
    """Retry against messaging, per root cause. The RAIL_FIXABLE question.

    A silent retry costs Rs 2, is invisible to the customer, and carries zero
    opt-out risk. Where it recovers comparably it beats any message on every
    axis -- which is exactly the comparison both shipped bugs failed to make.
    """
    rows = observed_rates(conn, segment)
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["segment"], row["cause"])
        bucket = grouped.setdefault(key, {
            "segment": row["segment"], "root_cause": row["cause"],
            "retry_trials": 0, "retry_successes": 0,
            "message_trials": 0, "message_successes": 0,
        })
        prefix = "retry" if row["channel"] == "retry" else "message"
        bucket[f"{prefix}_trials"] += row["n"]
        bucket[f"{prefix}_successes"] += row["wins"]

    out = []
    for bucket in grouped.values():
        shipped = _shipped().get(bucket["segment"], {})
        listed = bucket["root_cause"] in shipped.get("rail_fixable", set())
        retry_p = (_posterior(bucket["retry_successes"], bucket["retry_trials"])
                   if bucket["retry_trials"] else None)
        message_p = (_posterior(bucket["message_successes"], bucket["message_trials"])
                     if bucket["message_trials"] else None)
        out.append({
            **bucket,
            "retry_posterior": round(retry_p, 4) if retry_p is not None else None,
            "message_posterior": round(message_p, 4) if message_p is not None else None,
            "currently_rail_fixable": listed,
            "retry_untested": bucket["retry_trials"] == 0,
        })
    return sorted(out, key=lambda r: (r["segment"], r["root_cause"]))


def claim_drift(
    conn: sqlite3.Connection, segment: str | None = None
) -> list[dict[str, Any]]:
    """Shipped BASE_CLAIM against the observed rate, marginal over channels."""
    rows = observed_rates(conn, segment)
    totals: dict[tuple[str, str], list[int]] = {}
    for row in rows:
        key = (row["segment"], row["cause"])
        acc = totals.setdefault(key, [0, 0])
        acc[0] += row["wins"]
        acc[1] += row["n"]

    out = []
    for (seg, cause), (successes, trials) in sorted(totals.items()):
        shipped = _shipped().get(seg, {}).get("base_claim", {})
        if cause not in shipped:
            continue
        observed = _posterior(successes, trials)
        out.append({
            "segment": seg, "root_cause": cause,
            "shipped": shipped[cause], "observed": round(observed, 4),
            "delta": round(observed - shipped[cause], 4),
            "trials": trials, "thin": trials < MIN_TRIALS,
        })
    return sorted(out, key=lambda r: -abs(r["delta"]))


def revise_tables(
    conn: sqlite3.Connection, *, client: LLMClient | None = None,
    out_dir: Path | None = None,
) -> tuple[list[dict[str, Any]], AgentResult]:
    """Ask what the tables should say. Writes a review file, patches nothing."""
    client = client or LLMClient()
    revisions: list[dict[str, Any]] = []

    def submit_revision(
        segment: str, table: str, root_cause: str, action: str,
        value: float | None = None, evidence: str = "",
    ) -> dict[str, Any]:
        if table not in ("RAIL_FIXABLE", "BASE_CLAIM"):
            return {"accepted": False, "reason": f"no table named {table}"}
        if table == "BASE_CLAIM" and not isinstance(value, (int, float)):
            return {"accepted": False,
                    "reason": "BASE_CLAIM revisions need a numeric value"}
        if not evidence:
            return {"accepted": False,
                    "reason": "every revision must cite a trial count"}

        # Adding to RAIL_FIXABLE is checked against the data, not just against
        # the prose the model wrote about it. Observed on Groq: the model
        # proposed adding auth_failed at a 20.8% retry rate while removing
        # insufficient_funds at 16.6% -- two opposite conclusions from
        # neighbouring numbers, each with a confident justification.
        #
        # An add means silent retries start for that cause, and nothing
        # downstream damps it. So the bound is a tool-side check, the same way
        # the composer's critic gates a draft rather than trusting the prompt.
        if table == "RAIL_FIXABLE" and action == "add":
            row = next(
                (r for r in rail_comparison(conn, segment)
                 if r["root_cause"] == root_cause), None,
            )
            if row is None or row["retry_untested"]:
                return {"accepted": False,
                        "reason": f"retry has never been tried for "
                                  f"{root_cause} in {segment}; untested is not "
                                  f"the same as good"}
            if row["retry_trials"] < MIN_TRIALS:
                return {"accepted": False,
                        "reason": f"{row['retry_trials']} retry trials is "
                                  f"below the {MIN_TRIALS} threshold"}
            retry_p = row["retry_posterior"] or 0.0
            message_p = row["message_posterior"]

            # No messaging data is a refusal, not a free pass.
            #
            # The entire justification for RAIL_FIXABLE is "retry beats
            # messaging on every axis" -- cheaper, silent, no opt-out risk. You
            # cannot assert that with one side of the comparison empty, and an
            # `is not None` guard below would simply skip the check.
            #
            # It is empty by construction, which is the trap: `escalation._viable`
            # skips the retry rung for causes retry cannot fix (ADR-008), so a
            # cause gets retries or messages but almost never both. Measured on
            # the baseline arm, every rail-fixable cause had 0 message trials.
            # A model reading "40 retry trials" as sufficient evidence then
            # proposed adding limit_exceeded at a 9.5% success rate.
            if message_p is None:
                return {"accepted": False,
                        "reason": f"no messaging outcomes for {root_cause} in "
                                  f"{segment}, so 'retry beats messaging' "
                                  f"cannot be checked; the ladder skips one or "
                                  f"the other for most causes"}
            if retry_p < message_p:
                return {"accepted": False,
                        "reason": f"retry recovers {retry_p} against "
                                  f"{message_p} for messaging; adding this "
                                  f"would trade recovery for cost"}

        revisions.append({
            "segment": segment, "table": table, "root_cause": root_cause,
            "action": action, "value": value, "evidence": evidence,
        })
        return {"accepted": True, "recorded": len(revisions)}

    tools = [
        Tool(
            name="observed_rates",
            description="Success rate per root cause and channel for ONE "
                        "segment, with trial counts and a `thin` flag below "
                        "the evidence threshold.",
            input_schema={
                "type": "object",
                "properties": {"segment": {"type": "string"}},
                "required": ["segment"],
            },
            # Required rather than optional on purpose: unfiltered this returns
            # every segment x cause x channel tier, which is large enough to
            # exceed a small per-minute token budget on its own.
            fn=lambda segment: observed_rates(conn, segment),
        ),
        # `segment` is required on all three, though the functions accept None.
        #
        # Declared optional, a model fills it with an explicit `null` and
        # strict tool validators reject the call outright -- observed on Groq:
        # "`/segment`: expected string, but got null". Making it required
        # removes the ambiguity rather than relying on every provider being
        # lenient about it.
        Tool(
            name="rail_comparison",
            description="Retry against messaging per root cause for ONE "
                        "segment, and whether the cause is currently in "
                        "RAIL_FIXABLE.",
            input_schema={
                "type": "object",
                "properties": {"segment": {"type": "string",
                                           "enum": ["cart", "receivables"]}},
                "required": ["segment"],
            },
            fn=lambda segment: rail_comparison(conn, segment),
        ),
        Tool(
            name="claim_drift",
            description="Shipped BASE_CLAIM against the observed rate for ONE "
                        "segment, largest absolute difference first.",
            input_schema={
                "type": "object",
                "properties": {"segment": {"type": "string",
                                           "enum": ["cart", "receivables"]}},
                "required": ["segment"],
            },
            fn=lambda segment: claim_drift(conn, segment),
        ),
        Tool(
            name="submit_revision",
            description="Propose one change, with the evidence for it. "
                        "action is add/remove for RAIL_FIXABLE, set for "
                        "BASE_CLAIM.",
            input_schema={
                "type": "object",
                "properties": {
                    "segment": {"type": "string"},
                    "table": {"type": "string",
                              "enum": ["RAIL_FIXABLE", "BASE_CLAIM"]},
                    "root_cause": {"type": "string"},
                    "action": {"type": "string",
                               "enum": ["add", "remove", "set"]},
                    "value": {"type": "number"},
                    "evidence": {"type": "string"},
                },
                "required": ["segment", "table", "root_cause", "action",
                             "evidence"],
            },
            fn=submit_revision,
        ),
    ]

    def deterministic(by_name: dict[str, Tool]) -> AgentResult:
        """No model: propose only what needs no judgement.

        Two rules, both conservative. A `BASE_CLAIM` entry with enough trials
        and a gap wider than 10 points is proposed at the observed value.
        A `RAIL_FIXABLE` entry is only ever proposed for *removal*, and only
        where retry has been tried enough times to have failed convincingly --
        removing a cause makes the system send messages it already sends, while
        adding one starts silent retries on evidence nobody checked.

        Discards anything the model submitted before it failed. A degraded run
        that blended half a model's reasoning with a rule sweep would produce
        duplicate entries under two different justifications, and a reviewer
        could not tell which procedure produced which line. Observed: a
        provider hit its daily token limit mid-run and the review file came out
        with `cart.BASE_CLAIM bank_downtime` twice.
        """
        revisions.clear()

        for row in claim_drift(conn):
            if row["thin"] or abs(row["delta"]) < 0.10:
                continue
            revisions.append({
                "segment": row["segment"], "table": "BASE_CLAIM",
                "root_cause": row["root_cause"], "action": "set",
                "value": row["observed"],
                "evidence": f"{row['trials']} trials, shipped "
                            f"{row['shipped']} vs observed {row['observed']}",
            })

        for row in rail_comparison(conn):
            if not row["currently_rail_fixable"] or row["retry_untested"]:
                continue
            if row["retry_trials"] < MIN_TRIALS:
                continue
            if row["retry_posterior"] is not None and row["retry_posterior"] < 0.02:
                revisions.append({
                    "segment": row["segment"], "table": "RAIL_FIXABLE",
                    "root_cause": row["root_cause"], "action": "remove",
                    "value": None,
                    "evidence": f"{row['retry_trials']} retries, posterior "
                                f"{row['retry_posterior']}",
                })

        return AgentResult(
            text=f"{len(revisions)} revisions proposed by the deterministic "
                 f"rules. Additions to RAIL_FIXABLE are never proposed without "
                 f"a model: they start silent retries, and the arithmetic "
                 f"cannot tell an untested cause from a bad one.",
            provider="fallback",
        )

    result = client.run_agent(
        system=SYSTEM,
        prompt=(
            "Audit two segments: cart, then receivables. Every tool takes "
            "exactly one segment, so call each one twice.\n\n"
            "Start with rail_comparison, since RAIL_FIXABLE is the table that "
            "is not damped by calibration.\n\n"
            "Submit only revisions you can point at a trial count for."
        ),
        tools=tools,
        fallback=deterministic,
        max_turns=16,
    )

    if revisions:
        out = out_dir or DRAFTS
        out.mkdir(parents=True, exist_ok=True)
        (out / "strategy.json").write_text(
            json.dumps({"revisions": revisions, "provider": result.provider,
                        "model": result.model}, indent=2) + "\n",
            encoding="utf-8",
        )
    return revisions, result


def main() -> None:
    import argparse

    from rcp.store import DATA_DIR, connect

    parser = argparse.ArgumentParser(
        description="audit the proposers' hand-written probability tables"
    )
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--arm", default="control_plane")
    args = parser.parse_args()
    load_dotenv()

    path = DATA_DIR / f"seed_{args.seed}" / f"rcp_{args.arm}.db"
    if not path.exists():
        raise SystemExit(f"no database at {path}; run `make eval` first")

    conn = connect(path, read_only=True)
    try:
        revisions, result = revise_tables(conn)
    finally:
        conn.close()

    print(f"provider: {result.provider} {result.model}".rstrip())
    if result.degraded:
        print(f"DEGRADED -- the provider failed and the deterministic routine "
              f"answered instead:\n  {result.degraded}")
    print(result.text)
    for rev in revisions:
        value = "" if rev["value"] is None else f" -> {rev['value']}"
        print(f"\n{rev['segment']}.{rev['table']}: "
              f"{rev['action']} {rev['root_cause']}{value}"
              f"\n  {rev['evidence']}")
    if revisions:
        print(f"\nwritten to {DRAFTS / 'strategy.json'}; review before "
              f"editing rcp/proposers/. Nothing here is applied.")


if __name__ == "__main__":
    main()
