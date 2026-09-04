"""Eval analyst: an agent that investigates why the control plane lost.

This is the agent with the clearest job in the project, because it does work
that was genuinely being done by hand. Two real bugs in this system were found
by forming a hypothesis, writing a query, reading the result, and forming the
next hypothesis:

  * `cart.py` never proposed the `retry` channel, so it paid 7-17x more per send
    to recover less  (cost the control plane 21.8% on that segment)
  * the arbiter's opt-out risk ran ~1.7x above reality, so it suppressed
    actions that were worth taking

Neither is the kind of thing a fixed report surfaces -- you only see them if you
go looking, and where you look depends on what the last query said. That is an
agentic loop, so this is an agent.

**Both paths are real.** The deterministic routine below encodes the checks
that caught those two bugs and runs with no API key; the LLM path gets the same
tools and can form hypotheses the rules do not encode. `make analyze` runs the
deterministic one by default -- see ADR-002.

Lives in eval/, not rcp/agents/, on purpose. It reads counterfactuals from
truth.db to price what suppression cost, and `rcp/` is forbidden from touching
ground truth (tests/test_ground_truth_isolation.py). The control-plane agents --
diagnosis, composer, critic -- do belong under rcp/agents/; this one grades the
control plane rather than being part of it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from rcp.config import load
from rcp.env import load_dotenv
from eval.metrics import compute
from rcp.llm.client import AgentResult, LLMClient, Tool
from rcp.store import DATA_DIR, connect
from sim.outcomes import load_latents
from sim.truth_store import open_truth

SEGMENTS = ("subscription", "cart", "receivables")


@dataclass
class Finding:
    severity: str            # high | medium | low
    title: str
    evidence: str
    suggested_fix: str
    where: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)


SYSTEM = """You are an eval analyst for a payment-recovery control plane.

A "control plane" policy is being compared against a naive dunning baseline on
identical events. Your job is to find out WHERE and WHY the control plane loses,
and to report concrete, checkable findings.

Method: form a hypothesis, call a tool to test it, read the numbers, then form
the next hypothesis. Do not guess from priors about what usually works in
payment recovery -- the answer is in the data, and plausible-sounding advice is
exactly how the bugs got in.

Things worth checking, though this list is not exhaustive:
  - segments where the control plane loses
  - channels the baseline uses that the control plane does not, especially
    cheaper ones with a higher recovery rate
  - whether the arbiter's predicted opt-out risk matches what actually happened
  - whether suppression is destroying more value than it saves

This is a Python project. `where` must name a real path from this layout --
anything else is thrown away, so do not invent filenames:

  rcp/proposers/subscription.py   what to do for a failed subscription charge
  rcp/proposers/cart.py           ... an abandoned cart
  rcp/proposers/receivables.py    ... a B2B invoice
  rcp/arbiter/score.py            platform-side valuation, churn term
  rcp/arbiter/calibration.py      discounts proposer claims against history
  rcp/arbiter/select.py           winner selection and suppression
  rcp/compliance/rules.py         quiet hours, consent, caps, promises
  config/scoring.yaml             weights, contact cap, channel costs
  config/policy.yaml              compliance rules

Two constraints on your suggested fixes:

  * Suppression and contact caps exist to avoid opt-outs, and an opt-out costs
    a customer's whole lifetime value. "Send more" and "raise the cap" are the
    obvious reads of a recovery shortfall and are usually wrong here -- the
    baseline already sends more and loses on net value. If you propose relaxing
    a guard, say what it costs in churn.
  * Quote numbers exactly as the tools return them. Do not compute derived
    figures; arithmetic you do in your head will be wrong and it discredits the
    finding it appears in.

When you are done investigating, call `report_findings` once with everything
you found. That call IS your report -- do not also write it out in your reply.

Evidence must contain real numbers you read from a tool. An unquantified finding
is not a finding."""


# Anything a finding may point at. A model that invents a plausible filename
# produces a finding nobody can act on, and the invention is invisible unless
# it is checked -- the first live run against Groq returned four findings citing
# `cart_scoring.go`, `channel_caps.go` and `optout_arbiter.go` in a codebase
# with no Go in it.
def _known_paths() -> set[str]:
    from rcp.store import REPO_ROOT
    return {
        str(path.relative_to(REPO_ROOT))
        for pattern in ("rcp/**/*.py", "eval/*.py", "sim/*.py", "config/*.yaml")
        for path in REPO_ROOT.glob(pattern)
    }


# --------------------------------------------------------------------------
# tools
# --------------------------------------------------------------------------

def _open(seeds: list[int]) -> list[tuple[int, dict]]:
    out = []
    for seed in seeds:
        out.append((seed, {
            mode: connect(DATA_DIR / f"seed_{seed}" / f"rcp_{mode}.db", read_only=True)
            for mode in ("baseline", "control_plane")
        }))
    return out


FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "severity": {"type": "string", "enum": ["high", "medium", "low"]},
        "title": {"type": "string"},
        "evidence": {"type": "string",
                     "description": "numbers quoted verbatim from a tool"},
        "suggested_fix": {"type": "string"},
        "where": {"type": "string", "description": "a real path in this repo"},
    },
    "required": ["severity", "title", "evidence", "suggested_fix", "where"],
}


def build_tools(runs: list[tuple[int, dict]],
                sink: list[dict] | None = None) -> list[Tool]:
    """Tools aggregate across seeds.

    Single-seed analysis was the first version, and it produced confident
    nonsense: receivables carries ~90 events per run, so one seed swings it by
    80% in either direction. An analyst that reports variance as a finding is
    worse than no analyst, because someone acts on it.
    """
    cfg = load("sim")["outcomes"]
    n_seeds = len(runs)

    def _metrics(mode: str, segment: str | None) -> dict:
        total: dict = {}
        for seed, conns in runs:
            truth = open_truth(seed, read_only=True)
            try:
                m = compute(conns[mode], cfg, load_latents(truth),
                            (segment,) if segment else None)
            finally:
                truth.close()
            for key, value in m.items():
                if isinstance(value, (int, float)):
                    total[key] = total.get(key, 0) + value
        return {k: v / n_seeds for k, v in total.items()}

    def segment_breakdown() -> dict:
        out: dict = {"seeds": n_seeds}
        for segment in SEGMENTS:
            b, c = _metrics("baseline", segment), _metrics("control_plane", segment)
            delta = c["net_value_paise"] - b["net_value_paise"]
            out[segment] = {
                "baseline_net_paise": round(b["net_value_paise"]),
                "control_net_paise": round(c["net_value_paise"]),
                "delta_paise": round(delta),
                "delta_pct": round(delta / abs(b["net_value_paise"]) * 100, 1)
                if b["net_value_paise"] else None,
                "events_per_seed": round(b["events_total"], 1),
                "baseline_sent": round(b["actions_sent"], 1),
                "control_sent": round(c["actions_sent"], 1),
                "control_recovered_paise": round(c["recovered_paise"]),
                "baseline_recovered_paise": round(b["recovered_paise"]),
                "control_spend_paise": round(c["spend_paise"]),
                "baseline_spend_paise": round(b["spend_paise"]),
                "control_false_suppression_paise": round(c["false_suppression_paise"]),
            }
        return out

    def channel_mix(segment: str) -> dict:
        costs = load("scoring")["channel_cost_paise"]
        out: dict = {}
        for mode in ("baseline", "control_plane"):
            tally: dict = {}
            for _, conns in runs:
                for r in conns[mode].execute(
                    "SELECT a.channel AS ch, count(*) AS n, "
                    "       COALESCE(SUM(o.succeeded), 0) AS ok "
                    "FROM actions a JOIN outcomes o ON o.action_id = a.id "
                    "JOIN decisions d ON d.id = a.decision_id "
                    "JOIN events e ON e.id = d.event_id "
                    "WHERE e.segment = ? GROUP BY a.channel", (segment,),
                ):
                    slot = tally.setdefault(r["ch"], {"sent": 0, "recovered": 0})
                    slot["sent"] += r["n"]
                    slot["recovered"] += r["ok"]
            out[mode] = {
                ch: {**v,
                     "recovery_rate": round(v["recovered"] / v["sent"], 4)
                     if v["sent"] else 0.0,
                     "cost_per_send_paise": costs.get(ch)}
                for ch, v in sorted(tally.items())
            }
        return out

    def suppression_reasons(segment: str) -> dict:
        tally: dict = {}
        for _, conns in runs:
            for r in conns["control_plane"].execute(
                "SELECT d.reason AS reason, count(*) AS n FROM decisions d "
                "JOIN events e ON e.id = d.event_id "
                "WHERE d.outcome = 'suppressed' AND e.segment = ? "
                "GROUP BY substr(d.reason, 1, 24)", (segment,),
            ):
                key = r["reason"][:90]
                tally[key] = tally.get(key, 0) + r["n"]
        top = sorted(tally.items(), key=lambda kv: -kv[1])[:8]
        return {"segment": segment, "seeds": n_seeds,
                "reasons": [{"reason": k, "count": v} for k, v in top]}

    def calibration_check() -> dict:
        """The arbiter's predicted opt-out risk against what happened.

        Compares like with like. `opt_out_base` is the risk at ZERO prior
        contacts; the raw opt-out rate averages over customers who had several.
        Comparing those two directly was this tool's first bug -- it reported
        the arbiter underestimating when it was in fact running high. This
        computes the arbiter's prediction for each send given that send's own
        contact history, then averages.
        """
        churn = load("scoring")["churn"]
        base = float(churn["opt_out_base"])
        slope = float(churn["opt_out_per_extra_contact"])

        out: dict = {
            "arbiter_belief": {"opt_out_base": base,
                               "opt_out_per_extra_contact": slope},
            "seeds": n_seeds,
        }
        for mode in ("baseline", "control_plane"):
            predicted = actual = sends = 0.0
            for _, conns in runs:
                for r in conns[mode].execute(
                    "SELECT o.opted_out AS oo, ("
                    "  SELECT count(*) FROM actions a2 WHERE a2.customer_id = "
                    "  a.customer_id AND a2.status = \'sent\' AND a2.sent_at < a.sent_at"
                    ") AS before "
                    "FROM outcomes o JOIN actions a ON a.id = o.action_id "
                    "WHERE a.channel <> \'retry\' AND a.status = \'sent\'"
                ):
                    predicted += base + r["before"] * slope
                    actual += r["oo"]
                    sends += 1
            out[mode] = {
                "messaged_sends": int(sends),
                "opt_outs": int(actual),
                "predicted_rate": round(predicted / sends, 5) if sends else 0.0,
                "actual_rate": round(actual / sends, 5) if sends else 0.0,
            }

        b = out["baseline"]
        out["ratio_predicted_over_actual"] = (
            round(b["predicted_rate"] / b["actual_rate"], 2)
            if b["actual_rate"] else None
        )
        return out

    def sample_decision(segment: str, outcome: str = "suppressed") -> dict:
        conns = runs[0][1]
        row = conns["control_plane"].execute(
            "SELECT d.id, d.reason, d.detail FROM decisions d "
            "JOIN events e ON e.id = d.event_id "
            "WHERE d.outcome = ? AND e.segment = ? ORDER BY d.id LIMIT 1",
            (outcome, segment),
        ).fetchone()
        if row is None:
            return {"found": False}
        detail = json.loads(row["detail"])
        return {"found": True, "seed": runs[0][0], "decision_id": row["id"],
                "reason": row["reason"],
                "considered": detail.get("considered", [])[:3],
                "compliance": detail.get("compliance", {})}

    def report_findings(findings: list) -> dict:
        """Submitting the report is itself a tool call.

        Asking for a JSON array in the reply text looks simpler and is a trap:
        with tools in play a model reasonably reads "return JSON" as "call a
        tool", and Groq 400s the whole run when the invented call's arguments
        are an array rather than an object -- losing a correct analysis that had
        already been written. Making the report a real tool removes the
        ambiguity and gets the shape enforced by the schema.
        """
        if sink is not None:
            sink.extend(f for f in findings if isinstance(f, dict))
        return {"accepted": len(findings)}

    schema = lambda props, req: {"type": "object", "properties": props,
                                 "required": req, "additionalProperties": False}
    seg = {"segment": {"type": "string", "enum": list(SEGMENTS)}}

    return [
        Tool("segment_breakdown",
             "Net value, sends, recovery and spend per segment for both "
             "policies, averaged across all seeds.",
             schema({}, []), segment_breakdown),
        Tool("channel_mix",
             "Per-channel sends, recovery rate and cost per send, both "
             "policies, totalled across seeds.",
             schema(seg, ["segment"]), channel_mix),
        Tool("suppression_reasons",
             "Why the control plane suppressed actions in a segment.",
             schema(seg, ["segment"]), suppression_reasons),
        Tool("calibration_check",
             "The arbiter\'s predicted opt-out risk against the observed rate, "
             "matched per send on contact history.",
             schema({}, []), calibration_check),
        Tool("sample_decision",
             "One decision\'s full scoring and compliance detail.",
             schema({**seg, "outcome": {"type": "string",
                                        "enum": ["selected", "suppressed"]}},
                    ["segment"]), sample_decision),
        Tool("report_findings",
             "Submit your findings. Call this once, last, with everything you "
             "found. This is how you report -- do not put the report in your "
             "reply text.",
             schema({"findings": {"type": "array", "items": FINDING_SCHEMA}},
                    ["findings"]),
             report_findings),
    ]


# --------------------------------------------------------------------------
# the deterministic routine -- encodes the checks that caught the real bugs
# --------------------------------------------------------------------------

# A channel the winner ignores is only worth flagging if it is better on BOTH
# axes. Cheaper-but-worse is a real trade-off, not a bug.
CHEAPER_AND_BETTER_MARGIN = 1.05
CALIBRATION_DRIFT = 1.3
FALSE_SUPPRESSION_SHARE = 0.15


def deterministic_analysis(tools: dict[str, Tool]) -> AgentResult:
    findings: list[Finding] = []
    trail: list[dict[str, Any]] = []

    def call(name: str, **kw):
        trail.append({"tool": name, "input": kw, "error": False})
        return tools[name].fn(**kw)

    breakdown = call("segment_breakdown")
    n_seeds = breakdown.get("seeds", 1)

    for segment in SEGMENTS:
        m = breakdown.get(segment)
        if not isinstance(m, dict):
            continue
        losing = m["delta_paise"] < 0
        if losing:
            findings.append(Finding(
                severity="high" if (m["delta_pct"] or 0) < -10 else "medium",
                title=f"{segment}: control plane loses {abs(m['delta_pct'] or 0):.1f}%",
                evidence=(f"net {m['control_net_paise']/100:,.0f} vs baseline "
                          f"{m['baseline_net_paise']/100:,.0f} per seed; sent "
                          f"{m['control_sent']:.0f} vs {m['baseline_sent']:.0f}; "
                          f"{m['events_per_seed']:.0f} events/seed over "
                          f"{n_seeds} seeds"),
                suggested_fix="inspect the channel mix and suppression reasons "
                              "for this segment",
                where=f"rcp/proposers/{segment}.py",
                metrics=m,
            ))

        # The cart bug: a channel the baseline uses, the winner does not, that is
        # cheaper AND recovers a higher share.
        mix = call("channel_mix", segment=segment)
        base, ctrl = mix["baseline"], mix["control_plane"]
        for channel, stats in sorted(base.items()):
            if channel in ctrl or stats["sent"] < 20:
                continue
            used = [(c, s) for c, s in ctrl.items() if s["sent"] >= 20]
            if not used:
                continue
            cheaper_worse = [
                (c, s) for c, s in used
                if (s["cost_per_send_paise"] or 0) > (stats["cost_per_send_paise"] or 0)
                and stats["recovery_rate"] > s["recovery_rate"] * CHEAPER_AND_BETTER_MARGIN
            ]
            if cheaper_worse:
                worst = max(cheaper_worse, key=lambda kv: kv[1]["cost_per_send_paise"])
                findings.append(Finding(
                    severity="high",
                    title=f"{segment}: never proposes '{channel}', which is "
                          f"cheaper and recovers more",
                    evidence=(
                        f"baseline {channel}: {stats['sent']} sends, "
                        f"{stats['recovery_rate']:.1%} recovery at "
                        f"Rs {(stats['cost_per_send_paise'] or 0)/100:.0f}/send; "
                        f"control uses '{worst[0]}': {worst[1]['sent']} sends, "
                        f"{worst[1]['recovery_rate']:.1%} at "
                        f"Rs {(worst[1]['cost_per_send_paise'] or 0)/100:.0f}/send"
                    ),
                    suggested_fix=f"add '{channel}' to the {segment} proposer's "
                                  f"playbook for causes the rail can fix",
                    where=f"rcp/proposers/{segment}.py",
                    metrics={"unused_channel": channel, "baseline": stats,
                             "control_alternative": {worst[0]: worst[1]}},
                ))

        if m["control_spend_paise"] > m["baseline_spend_paise"] * 1.5 and \
                m["control_recovered_paise"] < m["baseline_recovered_paise"]:
            findings.append(Finding(
                severity="medium",
                title=f"{segment}: spends more to recover less",
                evidence=(f"spend {m['control_spend_paise']/100:,.0f} vs "
                          f"{m['baseline_spend_paise']/100:,.0f}, recovered "
                          f"{m['control_recovered_paise']/100:,.0f} vs "
                          f"{m['baseline_recovered_paise']/100:,.0f}"),
                suggested_fix="the proposer is reaching for expensive channels; "
                              "check cost per send against recovery rate",
                where=f"rcp/proposers/{segment}.py",
                metrics=m,
            ))

        recovered = m["control_recovered_paise"] or 1
        if m["control_false_suppression_paise"] > recovered * FALSE_SUPPRESSION_SHARE:
            findings.append(Finding(
                severity="medium",
                title=f"{segment}: suppression is abandoning recoverable money",
                evidence=(f"false suppression "
                          f"{m['control_false_suppression_paise']/100:,.0f} is "
                          f"{m['control_false_suppression_paise']/recovered:.0%} of "
                          f"what was recovered"),
                suggested_fix="lower min_score_paise or re-check the churn term "
                              "in config/scoring.yaml",
                where="rcp/arbiter/score.py",
                metrics={"false_suppression_paise": m["control_false_suppression_paise"]},
            ))

    # The calibration bug: the arbiter's assumed opt-out risk vs reality.
    cal = call("calibration_check")
    ratio = cal.get("ratio_predicted_over_actual")
    if ratio and (ratio > CALIBRATION_DRIFT or ratio < 1 / CALIBRATION_DRIFT):
        direction = "over" if ratio > 1 else "under"
        findings.append(Finding(
            severity="high",
            title=f"arbiter {direction}estimates opt-out risk by {ratio:.1f}x",
            evidence=(f"predicted {cal['baseline']['predicted_rate']:.4f} across "
                      f"{cal['baseline']['messaged_sends']} messaged sends; "
                      f"observed {cal['baseline']['actual_rate']:.4f} "
                      f"({cal['baseline']['opt_outs']} opt-outs, "
                      f"{cal['seeds']} seeds)"),
            suggested_fix="learn the rate from outcomes instead of config -- see "
                          "precedent.observed_opt_out_rate",
            where="rcp/arbiter/score.py",
            metrics=cal,
        ))

    order = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (order[f.severity], f.title))
    return AgentResult(
        text=json.dumps([asdict(f) for f in findings], indent=2),
        trail=trail,
        turns=len(trail),
    )


def _parse(text: str) -> list[Finding]:
    """Findings come back as JSON. A model may wrap them in prose or a fence."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return []
    return _from_items(raw)


def _from_items(raw: list) -> list[Finding]:
    """Validate and normalize finding dicts from either reporting channel."""
    known = _known_paths()
    findings = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        where = (item.get("where") or "").strip()
        if where and where not in known:
            # Keep the finding -- the evidence may still be sound -- but never
            # present an invented path as if it were a location.
            where = f"(unverified: {where})"
        findings.append(Finding(
            severity=item.get("severity", "low"),
            title=item.get("title", ""),
            evidence=item.get("evidence", ""),
            suggested_fix=item.get("suggested_fix", ""),
            where=where,
            metrics=item.get("metrics", {}) or {},
        ))
    return findings


def analyze(
    seeds: list[int] | None = None, client: LLMClient | None = None
) -> tuple[list[Finding], AgentResult]:
    from eval.run import DEFAULT_SEEDS

    seeds = seeds or list(DEFAULT_SEEDS)
    runs = _open(seeds)
    reported: list[dict] = []
    try:
        result = (client or LLMClient()).run_agent(
            system=SYSTEM,
            prompt=(
                f"Analyse {len(seeds)} seeds ({seeds[0]}-{seeds[-1]}). Find where "
                f"the control plane loses to the baseline and why. Every tool "
                f"aggregates across all of them, so a difference you see is not "
                f"one seed's luck. Report findings as the JSON array described."
            ),
            tools=build_tools(runs, sink=reported),
            fallback=deterministic_analysis,
            use_cache=False,   # tool results depend on live database state
        )
        # The tool is the reporting channel; parsing the reply is the fallback
        # for a model that answered in prose anyway.
        findings = _from_items(reported) if reported else _parse(result.text)
        return findings, result
    finally:
        for _, conns in runs:
            for conn in conns.values():
                conn.close()


def main() -> None:
    import argparse

    load_dotenv()

    parser = argparse.ArgumentParser(description="investigate where the control "
                                                 "plane loses")
    parser.add_argument("--seeds", type=str, default=None)
    args = parser.parse_args()
    seeds = [int(s) for s in args.seeds.split(",")] if args.seeds else None

    findings, result = analyze(seeds)
    print(f"\nanalyst: provider={result.provider}  "
          f"tool calls={len(result.trail)}  findings={len(findings)}")
    if result.usage:
        print(f"usage: {result.usage}")
    print("=" * 74)
    for f in findings:
        print(f"\n[{f.severity.upper()}] {f.title}")
        print(f"  evidence: {f.evidence}")
        print(f"  fix:      {f.suggested_fix}")
        if f.where:
            print(f"  where:    {f.where}")
    if not findings:
        print("\nNo findings. The control plane wins in every segment checked.")




if __name__ == "__main__":
    main()
