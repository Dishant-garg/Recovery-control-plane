"""Break-even analysis: how much of the result depends on one assumption?

The control plane's advantage comes largely from avoiding opt-outs, and how
much an opt-out is *worth* is the single most arguable number in the whole
model. `config/scoring.yaml` charges `ltv_fraction` of a customer's lifetime
value per opt-out, defaulting to 1.0 -- which says that opting out of recovery
messages means losing the customer entirely.

That is the harshest available assumption, and conveniently also the one most
flattering to a policy whose main edge is restraint. Quoting a single headline
number on the back of it would be motivated reasoning.

So this sweeps the assumption instead and reports the break-even: the point at
which the control plane stops winning. A reviewer who disagrees with the default
can read off the answer for their own number rather than having to argue about
ours.

Run: `make sensitivity`
"""

from __future__ import annotations

import argparse
import json
import re
from typing import Any

from eval.run import DEFAULT_SEEDS, run_mode
from rcp.config import CONFIG_DIR, load
from rcp.store import DATA_DIR, rcp_db_path
from sim.generate import generate

# Nobody seriously argues an opt-out is free, and nobody argues it costs more
# than the whole relationship. The break-even, if there is one, is in here.
SWEEP = (0.0, 0.1, 0.25, 0.5, 0.75, 1.0)


def _set_ltv_fraction(value: float) -> None:
    """Rewrite just the one line, and drop the config cache.

    Deliberately a targeted substitution rather than a yaml load/dump round
    trip: PyYAML discards comments, so dumping the file back would silently
    strip every explanation in config/scoring.yaml -- and this function is
    called a dozen times per sweep, so the damage would be permanent.

    `rcp.config.load` is lru_cached so a decision pass cannot observe the file
    changing underneath it. That is right for the control plane and exactly
    wrong here, so the cache is cleared explicitly rather than the caching being
    weakened for everyone.
    """
    path = CONFIG_DIR / "scoring.yaml"
    text = path.read_text()
    patched, count = re.subn(
        # [^\S\n] is "whitespace but not a newline": a plain \s*$ under
        # MULTILINE eats the line break and merges this line into the next.
        r"^([^\S\n]*ltv_fraction:[^\S\n]*)[\d.]+",
        lambda m: f"{m.group(1)}{value}",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise RuntimeError("could not locate ltv_fraction in config/scoring.yaml")
    path.write_text(patched)
    load.cache_clear()


def sweep(seeds: list[int], values: tuple[float, ...] = SWEEP) -> list[dict[str, Any]]:
    original = float(load("scoring")["churn"].get("ltv_fraction", 1.0))
    rows = []
    try:
        for value in values:
            _set_ltv_fraction(value)
            runs = {
                mode: [run_mode(mode, seed) for seed in seeds]
                for mode in ("baseline", "control_plane")
            }
            deltas = [
                c["net_value_paise"] - b["net_value_paise"]
                for b, c in zip(runs["baseline"], runs["control_plane"])
            ]
            rows.append({
                "ltv_fraction": value,
                "baseline_paise": sum(r["net_value_paise"] for r in runs["baseline"]) / len(seeds),
                "control_paise": sum(r["net_value_paise"] for r in runs["control_plane"]) / len(seeds),
                "delta_paise": sum(deltas) / len(deltas),
                "wins": sum(1 for d in deltas if d > 0),
                "seeds": len(deltas),
            })
    finally:
        _set_ltv_fraction(original)   # never leave the repo mutated
    return rows


def report(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'opt-out costs':>14}{'baseline':>16}{'control':>16}"
          f"{'delta':>16}{'wins':>10}")
    print("-" * 72)
    for row in rows:
        print(
            f"{row['ltv_fraction']:>13.0%}"
            f"{row['baseline_paise'] / 100:>16,.0f}"
            f"{row['control_paise'] / 100:>16,.0f}"
            f"{row['delta_paise'] / 100:>+16,.0f}"
            f"{row['wins']}/{row['seeds']:>8}"
        )

    ordered = sorted(rows, key=lambda r: r["ltv_fraction"])
    print("-" * 72)

    # Walk the sweep in order and find where the sign flips. Doing this by
    # min/max would report a nonsense bracket whenever the curve is not
    # monotonic, which it need not be.
    flips = [
        (ordered[i]["ltv_fraction"], ordered[i + 1]["ltv_fraction"])
        for i in range(len(ordered) - 1)
        if (ordered[i]["delta_paise"] > 0) != (ordered[i + 1]["delta_paise"] > 0)
    ]

    if not flips:
        verdict = ("wins across the whole range tested"
                   if ordered[0]["delta_paise"] > 0
                   else "does not win anywhere in the range tested")
        print(f"No break-even: the control plane {verdict}.")
    else:
        for lo, hi in flips:
            print(f"Break-even between {lo:.0%} and {hi:.0%} of lifetime value.")
        print("Above that, restraint pays for itself. Below it, contacting "
              "everyone is the better policy.")

    # A positive mean with a coin-flip win rate is not a result.
    weak = [r for r in ordered if r["delta_paise"] > 0 and r["wins"] / r["seeds"] < 0.65]
    if weak:
        print("\nCaution: at "
              + ", ".join(f"{r['ltv_fraction']:.0%}" for r in weak)
              + " the mean delta is positive but the win rate is near a coin "
                "flip -- that is variance, not an established advantage.")


def main() -> None:
    parser = argparse.ArgumentParser(description="churn-assumption break-even")
    parser.add_argument("--seeds", type=str, default=None)
    args = parser.parse_args()

    seeds = ([int(s) for s in args.seeds.split(",")] if args.seeds
             else list(DEFAULT_SEEDS)[:8])   # 8 is enough to rank the sweep
    for seed in seeds:
        if not rcp_db_path(seed).exists():
            generate(seed=seed, quiet=True)

    rows = sweep(seeds)
    report(rows)

    out = DATA_DIR / f"seed_{seeds[0]}" / "sensitivity.json"
    out.write_text(json.dumps({"seeds": seeds, "sweep": rows}, indent=2) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
