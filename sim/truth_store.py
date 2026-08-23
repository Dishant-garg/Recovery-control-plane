"""Ground truth store -- the ONLY accessor for truth.db.

Nothing under rcp/ may import this module or name truth.db. That is enforced by
tests/test_ground_truth_isolation.py, and it is why the two databases are
separate files rather than separate tables: a path the control plane has no way
to construct is a stronger guarantee than a naming convention.

What counts as ground truth:
  - the outcome model's parameters (per-root-cause base rates, decay curves)
  - each customer's latent propensity and opt-out sensitivity
  - counterfactuals: what WOULD have happened for actions never taken

What does NOT: the realized outcomes of actions actually executed. Those live in
rcp.db.outcomes, because a real control plane legitimately observes the results
of its own sends -- that is history, not an oracle.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from rcp.store import DATA_DIR, canonical_json, connect, write_txn

TRUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS truth_params (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value))
) STRICT;

CREATE TABLE IF NOT EXISTS customer_latent (
    customer_id         TEXT PRIMARY KEY,
    propensity          REAL NOT NULL CHECK (propensity BETWEEN 0.0 AND 1.0),
    opt_out_sensitivity REAL NOT NULL CHECK (opt_out_sensitivity BETWEEN 0.0 AND 1.0),
    true_payday_dom     INTEGER CHECK (true_payday_dom BETWEEN 1 AND 31)
) STRICT;

CREATE TABLE IF NOT EXISTS counterfactuals (
    event_id        TEXT NOT NULL,
    channel         TEXT NOT NULL,
    offset_days     INTEGER NOT NULL,
    would_succeed   INTEGER NOT NULL CHECK (would_succeed IN (0, 1)),
    would_opt_out   INTEGER NOT NULL CHECK (would_opt_out IN (0, 1)),
    recovered_paise INTEGER NOT NULL CHECK (recovered_paise >= 0),
    PRIMARY KEY (event_id, channel, offset_days)
) STRICT;
"""


def truth_db_path(seed: int = 42) -> Path:
    return DATA_DIR / f"seed_{seed}" / "truth.db"


def open_truth(seed: int = 42, *, read_only: bool = False) -> sqlite3.Connection:
    conn = connect(truth_db_path(seed), read_only=read_only)
    if not read_only:
        conn.executescript(f"BEGIN IMMEDIATE;\n{TRUTH_SCHEMA}\nCOMMIT;")
    return conn


def put_params(conn: sqlite3.Connection, key: str, value: object) -> None:
    with write_txn(conn):
        conn.execute(
            "INSERT INTO truth_params (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, canonical_json(value)),
        )
