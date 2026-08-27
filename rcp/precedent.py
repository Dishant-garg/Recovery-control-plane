"""Feature-keyed precedent lookup -- what this project uses instead of a vector index.

A vector retriever answers "similar to case #4471 (cosine 0.87)", which nobody
can check. This answers "3 of 11 prior attempts with root_cause=insufficient_funds,
amount 500-2000, pre-payday, channel=sms succeeded -> posterior 0.31", which a
compliance reviewer can verify by hand. In a system that decides whether to
charge someone's card, the precedent has to explain why it matched.

It is also deterministic, needs no embeddings or API calls, and runs in
microseconds against `precedent_view`. See ADR-006.

Consumed by agents/diagnosis.py (as tool output) and arbiter/calibration.py
(as the prior against which proposer claims are discounted).
"""

from __future__ import annotations

import sqlite3

from rcp.schema import Precedent

# Beta(1, 1) -- a uniform prior. With zero observations the posterior is 0.5,
# and it moves only as fast as the evidence warrants.
ALPHA = 1.0
BETA = 1.0

# Below this many observations a tier is too thin to trust; back off to a
# coarser one rather than acting on noise.
MIN_TRIALS = 20

# Ordered narrowest -> broadest. Each tier drops the least predictive feature.
LEVELS: list[tuple[str, tuple[str, ...]]] = [
    ("exact",         ("root_cause", "amount_bucket", "payday_phase", "channel")),
    ("no_payday",     ("root_cause", "amount_bucket", "channel")),
    ("cause_channel", ("root_cause", "channel")),
    ("cause_only",    ("root_cause",)),
    ("global",        ()),
]


def _tally(
    conn: sqlite3.Connection, features: tuple[str, ...], values: dict[str, object]
) -> tuple[int, int]:
    where = " AND ".join(f"{f} = :{f}" for f in features) or "1 = 1"
    row = conn.execute(
        f"SELECT COALESCE(SUM(succeeded), 0) AS s, COUNT(*) AS n "
        f"FROM precedent_view WHERE {where}",
        {f: values[f] for f in features},
    ).fetchone()
    return int(row["s"]), int(row["n"])


def observed_opt_out_rate(
    conn: sqlite3.Connection, *, prior: float, full_confidence_trials: int = 200
) -> tuple[float, int, int]:
    """Opt-out rate learned from what actually happened.

    The system already learns success probability from its own outcomes; there
    is no principled reason for opt-out risk to stay a hardcoded guess. A guess
    that runs 1.7x high does not fail loudly -- it just suppresses actions that
    were worth taking, and the resulting restraint looks like good judgement.

    Rail retries are excluded: the customer never sees them, so they belong in
    neither the numerator nor the denominator.

    Returns (rate, opt_outs, sends). Blends toward `prior` while evidence is
    thin, so early windows are not driven by three observations.
    """
    row = conn.execute(
        "SELECT count(*) AS n, COALESCE(SUM(o.opted_out), 0) AS k "
        "FROM outcomes o JOIN actions a ON a.id = o.action_id "
        "WHERE a.channel <> 'retry'"
    ).fetchone()
    sends, opt_outs = int(row["n"]), int(row["k"])

    posterior = (opt_outs + ALPHA) / (sends + ALPHA + BETA)
    confidence = min(1.0, sends / full_confidence_trials) if full_confidence_trials else 1.0
    return (1 - confidence) * prior + confidence * posterior, opt_outs, sends


def lookup(
    conn: sqlite3.Connection,
    *,
    root_cause: str,
    amount_bucket: str,
    payday_phase: str,
    channel: str,
) -> Precedent:
    """Posterior success probability for this feature tuple, with backoff.

    Walks LEVELS from narrowest to broadest and returns the first tier holding
    at least MIN_TRIALS observations. If none does, returns the broadest tier
    reached -- with its real (small) trial count, so callers can see the
    estimate is thin rather than being handed a confident-looking number.
    """
    values = {
        "root_cause": root_cause,
        "amount_bucket": amount_bucket,
        "payday_phase": payday_phase,
        "channel": channel,
    }

    successes = trials = 0
    level, features = LEVELS[-1]
    for level, features in LEVELS:
        successes, trials = _tally(conn, features, values)
        if trials >= MIN_TRIALS:
            break

    posterior = (successes + ALPHA) / (trials + ALPHA + BETA)
    key = ", ".join(f"{f}={values[f]}" for f in features)
    scope = f"with {key}" if key else "across all history (no feature match)"
    caveat = ", thin evidence" if trials < MIN_TRIALS else ""
    return Precedent(
        posterior=posterior,
        successes=successes,
        trials=trials,
        level=level,
        key=key or "*",
        explanation=(
            f"{successes} of {trials} prior attempts {scope} succeeded "
            f"-> posterior {posterior:.2f} (tier={level}{caveat})"
        ),
    )
