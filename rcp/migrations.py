"""Schema DDL + a 20-line migration runner built on PRAGMA user_version.

Design notes worth keeping in mind when editing:

  - Every table is STRICT. SQLite's default type affinity will happily store
    "abc" in an INTEGER column; STRICT makes that an error.
  - CHECK constraint bodies are generated from the enums in schema.py, so the
    DB and the Python types cannot drift.
  - Every table is append-only except `actions` (status/sent_at/attempts/
    provider_ref) and `promises` (state/updated_at). Triggers enforce it --
    this is ADR-002 as a property you can try to violate, not a claim.
  - amount_bucket and payday_phase are generated columns: the bucketing rule
    lives in exactly one place, and precedent lookup stays pure SQL.
"""

from __future__ import annotations

import sqlite3

from rcp.schema import (
    ActionStatus,
    Channel,
    DecisionOutcome,
    Language,
    PromiseState,
    RootCause,
    Segment,
    sql_in,
)

# Tables that must never see an UPDATE or a DELETE.
APPEND_ONLY = ("events", "proposals", "decisions", "outcomes", "audit_mirror")


def _append_only_triggers() -> str:
    out = []
    for t in APPEND_ONLY:
        out.append(f"""
CREATE TRIGGER {t}_no_update BEFORE UPDATE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;

CREATE TRIGGER {t}_no_delete BEFORE DELETE ON {t}
BEGIN SELECT RAISE(ABORT, '{t} is append-only'); END;
""")
    return "".join(out)


SCHEMA_V1 = f"""
CREATE TABLE customers (
    id          TEXT PRIMARY KEY,
    segment     TEXT NOT NULL CHECK (segment IN ({sql_in(Segment)})),
    payday_dom  INTEGER CHECK (payday_dom BETWEEN 1 AND 31),
    language    TEXT NOT NULL DEFAULT 'en' CHECK (language IN ({sql_in(Language)})),
    ltv_paise   INTEGER NOT NULL CHECK (ltv_paise >= 0),
    opted_out   INTEGER NOT NULL DEFAULT 0 CHECK (opted_out IN (0, 1)),
    created_at  INTEGER NOT NULL
) STRICT;

CREATE TABLE events (
    id                TEXT PRIMARY KEY,
    provider          TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    customer_id       TEXT NOT NULL REFERENCES customers(id),
    segment           TEXT NOT NULL CHECK (segment IN ({sql_in(Segment)})),
    occurred_at       INTEGER NOT NULL,
    amount_paise      INTEGER NOT NULL CHECK (amount_paise >= 0),
    currency          TEXT NOT NULL DEFAULT 'INR',
    root_cause        TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (root_cause IN ({sql_in(RootCause)})),
    retry_index       INTEGER NOT NULL DEFAULT 0 CHECK (retry_index >= 0),
    days_from_payday  INTEGER,
    payload           TEXT NOT NULL CHECK (json_valid(payload)),

    gateway_code TEXT GENERATED ALWAYS AS
        (CAST(json_extract(payload, '$.error_code') AS TEXT)) VIRTUAL,

    -- bucketing rules live here and nowhere else
    amount_bucket TEXT GENERATED ALWAYS AS (
        CASE WHEN amount_paise <   50000 THEN 'lt_500'
             WHEN amount_paise <  200000 THEN '500_2000'
             WHEN amount_paise < 1000000 THEN '2000_10000'
             ELSE 'gte_10000' END) VIRTUAL,

    payday_phase TEXT GENERATED ALWAYS AS (
        CASE WHEN days_from_payday IS NULL          THEN 'unknown'
             WHEN days_from_payday BETWEEN -3 AND 0 THEN 'pre_payday'
             WHEN days_from_payday BETWEEN 1 AND 5  THEN 'post_payday'
             ELSE 'mid_cycle' END) VIRTUAL,

    -- webhook replay dedup, enforced by the database rather than app logic
    UNIQUE (provider, provider_event_id)
) STRICT;

CREATE TABLE proposals (
    id                   TEXT PRIMARY KEY,
    window_id            TEXT NOT NULL,
    event_id             TEXT NOT NULL REFERENCES events(id),
    customer_id          TEXT NOT NULL REFERENCES customers(id),
    proposer_id          TEXT NOT NULL,
    channel              TEXT NOT NULL CHECK (channel IN ({sql_in(Channel)})),
    scheduled_at         INTEGER NOT NULL,
    claimed_success_prob REAL NOT NULL CHECK (claimed_success_prob BETWEEN 0.0 AND 1.0),
    claimed_value_paise  INTEGER NOT NULL,
    incentive_paise      INTEGER NOT NULL DEFAULT 0 CHECK (incentive_paise >= 0),
    rationale            TEXT NOT NULL,
    payload              TEXT NOT NULL CHECK (json_valid(payload)),
    created_at           INTEGER NOT NULL,
    UNIQUE (window_id, proposer_id, event_id)
) STRICT;

CREATE TABLE decisions (
    id                  TEXT PRIMARY KEY,
    window_id           TEXT NOT NULL,
    event_id            TEXT NOT NULL REFERENCES events(id),
    customer_id         TEXT NOT NULL REFERENCES customers(id),
    winning_proposal_id TEXT REFERENCES proposals(id),   -- NULL when all suppressed
    score               REAL,
    outcome             TEXT NOT NULL CHECK (outcome IN ({sql_in(DecisionOutcome)})),
    reason              TEXT NOT NULL,
    policy_version      TEXT NOT NULL,
    decided_at          INTEGER NOT NULL,
    detail              TEXT NOT NULL CHECK (json_valid(detail)),
    UNIQUE (window_id, event_id),
    CHECK ((outcome = 'selected') = (winning_proposal_id IS NOT NULL))
) STRICT;

CREATE TABLE actions (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES decisions(id),
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    idempotency_key TEXT NOT NULL UNIQUE,          -- exactly-once, by construction
    channel         TEXT NOT NULL CHECK (channel IN ({sql_in(Channel)})),
    status          TEXT NOT NULL CHECK (status IN ({sql_in(ActionStatus)})),
    scheduled_at    INTEGER NOT NULL,
    sent_at         INTEGER,
    attempts        INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    provider_ref    TEXT,                          -- plink_xxx / pay_xxx from Razorpay
    body            TEXT NOT NULL CHECK (json_valid(body)),
    created_at      INTEGER NOT NULL
) STRICT;

CREATE TABLE outcomes (
    id              TEXT PRIMARY KEY,
    action_id       TEXT NOT NULL UNIQUE REFERENCES actions(id),
    event_id        TEXT NOT NULL REFERENCES events(id),
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    succeeded       INTEGER NOT NULL CHECK (succeeded IN (0, 1)),
    recovered_paise INTEGER NOT NULL DEFAULT 0 CHECK (recovered_paise >= 0),
    opted_out       INTEGER NOT NULL DEFAULT 0 CHECK (opted_out IN (0, 1)),
    observed_at     INTEGER NOT NULL
) STRICT;

CREATE TABLE promises (
    id           TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    event_id     TEXT NOT NULL REFERENCES events(id),
    state        TEXT NOT NULL CHECK (state IN ({sql_in(PromiseState)})),
    amount_paise INTEGER NOT NULL CHECK (amount_paise >= 0),
    due_at       INTEGER NOT NULL,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
) STRICT;

CREATE TABLE bank_health (
    id           TEXT PRIMARY KEY,
    bank_code    TEXT NOT NULL,
    rail         TEXT NOT NULL,
    window_start INTEGER NOT NULL,
    success_rate REAL NOT NULL CHECK (success_rate BETWEEN 0.0 AND 1.0),
    sample_size  INTEGER NOT NULL CHECK (sample_size >= 0),
    degraded     INTEGER NOT NULL CHECK (degraded IN (0, 1)),
    UNIQUE (bank_code, rail, window_start)
) STRICT;

-- Derived index over audit.jsonl. The JSONL file is canonical; this table
-- exists so viewer/export.py can query instead of re-parsing.
CREATE TABLE audit_mirror (
    seq       INTEGER PRIMARY KEY,
    hash      TEXT NOT NULL UNIQUE,
    prev_hash TEXT NOT NULL,
    kind      TEXT NOT NULL,
    ref_id    TEXT,
    ts        INTEGER NOT NULL,
    body      TEXT NOT NULL CHECK (json_valid(body))
) STRICT;

{_append_only_triggers()}

-- actions is mutable, but only in four columns.
CREATE TRIGGER actions_immutable_cols BEFORE UPDATE ON actions
WHEN OLD.id              <> NEW.id
  OR OLD.decision_id     <> NEW.decision_id
  OR OLD.customer_id     <> NEW.customer_id
  OR OLD.idempotency_key <> NEW.idempotency_key
  OR OLD.channel         <> NEW.channel
  OR OLD.scheduled_at    <> NEW.scheduled_at
  OR OLD.body            <> NEW.body
  OR OLD.created_at      <> NEW.created_at
BEGIN
    SELECT RAISE(ABORT,
        'actions: only status, sent_at, attempts, provider_ref are mutable');
END;

CREATE TRIGGER actions_no_delete BEFORE DELETE ON actions
BEGIN SELECT RAISE(ABORT, 'actions is append-only'); END;

CREATE TRIGGER promises_immutable_cols BEFORE UPDATE ON promises
WHEN OLD.id           <> NEW.id
  OR OLD.customer_id  <> NEW.customer_id
  OR OLD.event_id     <> NEW.event_id
  OR OLD.amount_paise <> NEW.amount_paise
  OR OLD.created_at   <> NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'promises: only state, due_at, updated_at are mutable');
END;

CREATE INDEX idx_events_cust_time  ON events(customer_id, occurred_at);
CREATE INDEX idx_proposals_window  ON proposals(window_id, event_id);
CREATE INDEX idx_decisions_window  ON decisions(window_id);
CREATE INDEX idx_actions_cust_sent ON actions(customer_id, sent_at);
CREATE INDEX idx_outcomes_event    ON outcomes(event_id);
CREATE INDEX idx_promises_cust     ON promises(customer_id, state);

-- The outbox relay polls this constantly; a partial index stays tiny no
-- matter how large the table grows.
CREATE INDEX idx_actions_pending ON actions(scheduled_at)
    WHERE status = 'pending';

-- Feature-keyed precedent. This view is what replaces a vector index:
-- every column is a fact a compliance reviewer can read back.
CREATE VIEW precedent_view AS
SELECT e.root_cause      AS root_cause,
       e.amount_bucket   AS amount_bucket,
       e.payday_phase    AS payday_phase,
       e.retry_index     AS retry_index,
       a.channel         AS channel,
       o.succeeded       AS succeeded,
       o.recovered_paise AS recovered_paise
FROM outcomes o
JOIN actions a ON a.id = o.action_id
JOIN events  e ON e.id = o.event_id;
"""


# Recorded consent, per channel: {"whatsapp": true, "voice": false}.
# `opted_out` is a blanket stop; this is the narrower "may we use this channel
# at all" question that compliance/rules.py::Consent asks. Absent key means no
# recorded consent, which is a denial rather than a default-yes.
SCHEMA_V2 = """
ALTER TABLE customers ADD COLUMN consent TEXT NOT NULL DEFAULT '{}';
"""


MIGRATIONS: list[str] = [SCHEMA_V1, SCHEMA_V2]


def migrate(conn: sqlite3.Connection) -> int:
    """Apply any migrations the file has not seen. Returns the resulting version.

    Note the explicit BEGIN/COMMIT inside the script string: `executescript`
    issues an implicit COMMIT before it runs, so wrapping this call in an
    outer transaction helper would silently break atomicity.
    """
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version):
        conn.executescript(
            f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {i + 1};\nCOMMIT;"
        )
    return len(MIGRATIONS)
