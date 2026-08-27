"""Baseline vs control plane, over the same events and the same outcome model.

Each mode runs on its own fork of the generated database, so both see an
identical world and neither can contaminate the other. The daily loop is what
makes deferral meaningful: an action scheduled for the day after payday simply
does not get relayed until that day arrives.

    for each day:
        collect proposals for events that failed that day
        arbitrate -> decisions (+ actions, if permitted)
        relay actions whose scheduled_at has arrived
        resolve outcomes for whatever was just sent

The loop runs past the event horizon so deferred actions actually fire. Cutting
it at the horizon would silently score the control plane's patience as a failure
to act.

**Two fairness rules, both deliberate:**

1. Both policies are held to the same event set -- only segments the control
   plane actually has a proposer for. Otherwise the baseline would get credit
   for acting on `cart` events that the control plane cannot see yet, and the
   comparison would measure coverage rather than policy.

2. Results are averaged over several seeds by default. Churn cost charges a full
   lifetime value per opt-out, so a single run's headline number is dominated by
   *which* customers happened to churn -- with 4-5 opt-outs that is noise, not
   signal.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sqlite3
from pathlib import Path
from typing import Any

from eval.baseline import BaselineProposer
from eval.metrics import compute
from rcp.arbiter.collect import collect, window_id_for
from rcp.arbiter.select import select_window
from rcp.audit import AuditLog
from rcp.config import load
from rcp.execute.outbox import drain
from rcp.execute.simulated import SimulatedExecutor
from rcp.migrations import migrate
from rcp.compliance.promise import create as create_promise, transition
from rcp.proposers.cart import CartProposer
from rcp.proposers.receivables import ReceivablesProposer
from rcp.proposers.subscription import SubscriptionProposer
from rcp.schema import PromiseState
from rcp.store import (
    DATA_DIR,
    close,
    connect,
    content_id,
    rcp_db_path,
    write_txn,
)
from rcp.timeutil import MS_PER_DAY, day_start_ms
from sim.outcomes import load_latents, promise_kept, resolve
from sim.truth_store import open_truth

# Days to keep running after the last event, so deferred retries land.
TAIL_DAYS = 45

UNCAPPED = 10**9
NO_FLOOR = -(10**15)

DEFAULT_SEEDS = tuple(range(42, 62))  # 20 seeds, ~11s end to end


def control_plane_proposers() -> list:
    return [SubscriptionProposer(), CartProposer(), ReceivablesProposer()]


def covered_segments() -> tuple[str, ...]:
    """The event set both policies are judged on. Widens by itself as
    cart.py and receivables.py land."""
    segments: set[str] = set()
    for proposer in control_plane_proposers():
        segments.update(proposer.segments)
    return tuple(sorted(segments))


def fork_db(src: Path, dst: Path) -> None:
    """Snapshot the generated database. `backup()` handles WAL correctly; a
    plain file copy would miss un-checkpointed pages."""
    dst.unlink(missing_ok=True)
    for sidecar in ("-wal", "-shm"):
        dst.with_name(dst.name + sidecar).unlink(missing_ok=True)
    source = connect(src, read_only=True)
    target = connect(dst)
    try:
        source.backup(target)
    finally:
        source.close()
        close(target)


def resolve_outcomes(
    conn: sqlite3.Connection,
    cfg_outcomes: dict[str, Any],
    latents: dict[str, dict[str, Any]],
    *,
    now_ms: int,
) -> int:
    """Score every action that was sent but has no outcome yet."""
    rows = conn.execute(
        """
        SELECT a.id, a.customer_id, a.channel, a.scheduled_at, a.sent_at, a.body,
               d.event_id, e.root_cause, e.amount_paise, e.retry_index,
               c.payday_dom
        FROM actions a
        JOIN decisions d ON d.id = a.decision_id
        JOIN events    e ON e.id = d.event_id
        JOIN customers c ON c.id = a.customer_id
        LEFT JOIN outcomes o ON o.action_id = a.id
        WHERE a.status = 'sent' AND o.action_id IS NULL
        ORDER BY a.sent_at ASC, a.id ASC
        """
    ).fetchall()
    if not rows:
        return 0

    resolved = []
    for row in rows:
        latent = latents.get(row["customer_id"])
        if latent is None:
            continue
        contacts_before = conn.execute(
            "SELECT count(*) FROM actions WHERE customer_id = ? AND status = 'sent' "
            "AND sent_at < ?",
            (row["customer_id"], row["sent_at"]),
        ).fetchone()[0]

        outcome = resolve(
            cfg_outcomes,
            action_id=row["id"],
            root_cause=row["root_cause"],
            channel=row["channel"],
            scheduled_at=row["scheduled_at"],
            amount_paise=row["amount_paise"],
            payday_dom=row["payday_dom"],
            retry_index=row["retry_index"],
            propensity=latent["propensity"],
            opt_out_sensitivity=latent["opt_out_sensitivity"],
            contacts_before=contacts_before,
            asks_for_promise=bool(
                json.loads(row["body"]).get("asks_for_promise", False)
            ),
            incentive_paise=int(json.loads(row["body"]).get("incentive_paise", 0)),
        )
        resolved.append((row, outcome))

    with write_txn(conn):
        for row, outcome in resolved:
            conn.execute(
                "INSERT INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (content_id("out", row["id"]), row["id"], row["event_id"],
                 row["customer_id"], outcome.succeeded, outcome.recovered_paise,
                 outcome.opted_out, now_ms),
            )
            if outcome.opted_out:
                conn.execute(
                    "UPDATE customers SET opted_out = 1 WHERE id = ?",
                    (row["customer_id"],),
                )
            if outcome.promised:
                create_promise(
                    conn,
                    customer_id=row["customer_id"],
                    event_id=row["event_id"],
                    amount_paise=row["amount_paise"],
                    due_at=outcome.promise_due_at,
                    now_ms=now_ms,
                    state=PromiseState.ACCEPTED,
                )
    return len(resolved)


def settle_promises(
    conn: sqlite3.Connection, cfg_outcomes: dict[str, Any], *, now_ms: int
) -> dict[str, int]:
    """Resolve promises whose due date has passed.

    Without this an accepted promise silences its customer forever -- the most
    expensive possible failure mode for a rule whose job is to protect them.
    """
    due = conn.execute(
        "SELECT id FROM promises WHERE state = 'accepted' AND due_at < ? "
        "ORDER BY id ASC",
        (now_ms,),
    ).fetchall()
    tally = {"kept": 0, "broken": 0}
    for row in due:
        kept = promise_kept(cfg_outcomes, row["id"])
        transition(conn, row["id"],
                   PromiseState.KEPT if kept else PromiseState.BROKEN, now_ms=now_ms)
        tally["kept" if kept else "broken"] += 1
    return tally


def run_mode(mode: str, seed: int) -> dict[str, Any]:
    sim_cfg = load("sim")
    epoch_ms = int(sim_cfg["epoch_ms"])
    horizon_days = int(sim_cfg["horizon_days"])
    segments = covered_segments()

    db = DATA_DIR / f"seed_{seed}" / f"rcp_{mode}.db"
    fork_db(rcp_db_path(seed), db)

    conn = connect(db)
    migrate(conn)
    truth = open_truth(seed, read_only=True)
    latents = load_latents(truth)
    cfg_outcomes = sim_cfg["outcomes"]

    log = AuditLog(DATA_DIR / f"seed_{seed}" / f"audit_{mode}.jsonl")
    executor = SimulatedExecutor()

    if mode == "baseline":
        proposers = [BaselineProposer()]
        guards = {"cap": UNCAPPED, "min_score_paise": NO_FLOOR,
                  "policy_version": "baseline"}
    else:
        proposers = control_plane_proposers()
        guards = {}

    totals = {"selected": 0, "suppressed_value": 0, "suppressed_cap": 0,
              "suppressed_compliance": 0, "compliance_modified": 0,
              "skipped": 0, "sent": 0, "resolved": 0,
              "promises_kept": 0, "promises_broken": 0}

    for day in range(horizon_days + TAIL_DAYS):
        start = day_start_ms(epoch_ms) + day * MS_PER_DAY
        end = start + MS_PER_DAY
        window = window_id_for(start, epoch_ms=epoch_ms)

        collect(conn, proposers, window_id=window, start_ms=start, end_ms=end,
                now_ms=end, segments=segments)
        stats = select_window(conn, window_id=window, now_ms=end, log=log, **guards)
        for key, value in stats.items():
            totals[key] += value

        relay = drain(conn, executor, now_ms=end)
        totals["sent"] += relay["sent"]
        totals["resolved"] += resolve_outcomes(conn, cfg_outcomes, latents, now_ms=end)
        settled = settle_promises(conn, cfg_outcomes, now_ms=end)
        totals["promises_kept"] += settled["kept"]
        totals["promises_broken"] += settled["broken"]

    metrics = compute(conn, cfg_outcomes, latents, segments)
    metrics["pipeline"] = totals
    metrics["mode"] = mode
    metrics["seed"] = seed
    metrics["segments"] = list(segments)

    close(conn)
    truth.close()
    return metrics


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def _rupees(paise: float) -> str:
    return f"Rs {paise / 100:,.0f}"


ROWS = [
    ("events (in scope)", "events_total", str),
    ("actions sent", "actions_sent", lambda v: f"{v:,.0f}"),
    ("payments recovered", "recovered_count", lambda v: f"{v:,.0f}"),
    ("recovery rate", "recovery_rate", lambda v: f"{v:.1%}"),
    ("contacts / customer", "contacts_per_customer", lambda v: f"{v:.2f}"),
    ("recovered", "recovered_paise", _rupees),
    ("promises secured", "promises_secured", lambda v: f"{v:,.1f}"),
    ("promises kept", "promises_kept", lambda v: f"{v:,.1f}"),
    ("recovered via promise", "promised_recovered_paise", _rupees),
    ("send spend", "spend_paise", _rupees),
    ("net value ex-churn", "net_value_ex_churn_paise", _rupees),
    ("opt-outs", "opt_outs", lambda v: f"{v:,.1f}"),
    ("churn cost", "churn_cost_paise", _rupees),
    ("net value", "net_value_paise", _rupees),
    ("suppressed (value)", "suppressed_value", lambda v: f"{v:,.1f}"),
    ("suppressed (cap)", "suppressed_cap", lambda v: f"{v:,.1f}"),
    ("suppressed (compliance)", "compliance_denied_count", lambda v: f"{v:,.1f}"),
    ("compliance cost", "compliance_cost_paise", _rupees),
    ("false suppressions", "false_suppression_count", lambda v: f"{v:,.1f}"),
    ("false suppression cost", "false_suppression_paise", _rupees),
]


def average(runs: list[dict[str, Any]]) -> dict[str, float]:
    keys = [k for k, v in runs[0].items() if isinstance(v, (int, float))]
    return {k: statistics.fmean(r[k] for r in runs) for k in keys}


def report(results: dict[str, dict[str, float]], seeds: list[int],
           per_seed: dict[str, list[dict]]) -> None:
    modes = [m for m in ("baseline", "control_plane") if m in results]
    width = max(len(label) for label, _, _ in ROWS) + 2

    scope = ", ".join(per_seed[modes[0]][0]["segments"])
    print(f"\nseeds: {seeds}   segments in scope: {scope}")
    print(f"{'':<{width}}{''.join(f'{m:>20}' for m in modes)}")
    print("-" * (width + 20 * len(modes)))
    for label, key, fmt in ROWS:
        print(f"{label:<{width}}"
              + "".join(f"{fmt(results[m].get(key, 0)):>20}" for m in modes))

    if len(modes) != 2:
        return

    base, ctrl = results["baseline"], results["control_plane"]
    print("-" * (width + 20 * len(modes)))
    for label, key in [("net value delta", "net_value_paise"),
                       ("  ex-churn delta", "net_value_ex_churn_paise")]:
        delta = ctrl[key] - base[key]
        pct = (delta / abs(base[key]) * 100) if base[key] else 0.0
        print(f"{label:<{width}}{_rupees(delta):>20} ({pct:+.1f}%)")

    # Per-seed spread: one averaged number hides whether the win is consistent.
    #
    # The two win-rates below usually disagree, and the gap is the point.
    # Churn charges a full lifetime value per opt-out, and with ~1.5 opt-outs
    # per run against LTVs spanning Rs 1k to Rs 2L, that single term carries
    # more variance than the entire policy effect. The ex-churn win-rate
    # isolates what the policy actually did; the net-value one shows how much
    # of the headline is a churn lottery. Quote both.
    def wins(key: str) -> tuple[int, list[int]]:
        deltas = [
            c[key] - b[key]
            for b, c in zip(per_seed["baseline"], per_seed["control_plane"])
        ]
        return sum(1 for d in deltas if d > 0), deltas

    net_wins, net_deltas = wins("net_value_paise")
    ex_wins, _ = wins("net_value_ex_churn_paise")
    n = len(net_deltas)
    print(f"{'  wins / seeds (net)':<{width}}{f'{net_wins}/{n}':>20}")
    print(f"{'  wins / seeds (ex-churn)':<{width}}{f'{ex_wins}/{n}':>20}")
    if n <= 8:
        print(f"{'  per-seed net delta':<{width}}"
              f"{'[' + ', '.join(f'{d / 100:,.0f}' for d in net_deltas) + ']':>20}")


def main() -> None:
    parser = argparse.ArgumentParser(description="baseline vs control plane")
    parser.add_argument("--mode", choices=["baseline", "control_plane", "both"],
                        default="both")
    parser.add_argument("--seed", type=int, default=None,
                        help="single seed; shorthand for --seeds <n>")
    parser.add_argument("--seeds", type=str, default=None,
                        help=f"comma-separated (default {','.join(map(str, DEFAULT_SEEDS))})")
    args = parser.parse_args()

    if args.seeds:
        seeds = [int(s) for s in args.seeds.split(",")]
    elif args.seed is not None:
        seeds = [args.seed]
    else:
        seeds = list(DEFAULT_SEEDS)

    modes = ["baseline", "control_plane"] if args.mode == "both" else [args.mode]

    # Seeds other than the primary one need their world generated first.
    from sim.generate import generate
    for seed in seeds:
        if not rcp_db_path(seed).exists():
            print(f"generating seed {seed} ...")
            generate(seed=seed, quiet=True)

    per_seed: dict[str, list[dict]] = {m: [] for m in modes}
    for seed in seeds:
        for mode in modes:
            per_seed[mode].append(run_mode(mode, seed))

    results = {m: average(runs) for m, runs in per_seed.items()}
    report(results, seeds, per_seed)

    out = DATA_DIR / f"seed_{seeds[0]}" / "results.json"
    out.write_text(json.dumps(
        {"seeds": seeds, "mean": results, "per_seed": per_seed},
        indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
