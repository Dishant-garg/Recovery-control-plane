"""Contract layer: enums + pydantic models.

The enums here are the single source of truth. `rcp/migrations.py` builds its
SQL CHECK constraints from these same objects via `sql_in()`, so the database
and the Python types cannot drift apart silently.

Conventions enforced across the whole project:
  - money is INTEGER paise, never float
  - timestamps are INTEGER unix milliseconds from the sim clock, never wall clock
  - ids are deterministic strings (see rcp/store.py), never random
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------
# enums
# --------------------------------------------------------------------------

class RootCause(str, Enum):
    INSUFFICIENT_FUNDS = "insufficient_funds"
    MANDATE_EXPIRED = "mandate_expired"
    CARD_EXPIRED = "card_expired"
    BANK_DOWNTIME = "bank_downtime"
    LIMIT_EXCEEDED = "limit_exceeded"
    AUTH_FAILED = "auth_failed"
    INVALID_ACCOUNT = "invalid_account"
    UNKNOWN = "unknown"


class Segment(str, Enum):
    SUBSCRIPTION = "subscription"
    CART = "cart"
    RECEIVABLES = "receivables"


class Channel(str, Enum):
    SMS = "sms"
    WHATSAPP = "whatsapp"
    EMAIL = "email"
    VOICE = "voice"
    RETRY = "retry"


class ActionStatus(str, Enum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class DecisionOutcome(str, Enum):
    SELECTED = "selected"
    SUPPRESSED = "suppressed"


class PromiseState(str, Enum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    KEPT = "kept"
    BROKEN = "broken"
    EXPIRED = "expired"


class Language(str, Enum):
    EN = "en"
    HI = "hi"
    HINGLISH = "hinglish"


def sql_in(enum_cls: type[Enum]) -> str:
    """Render an enum as a SQL `IN (...)` body. Used to build CHECK constraints."""
    return ", ".join(f"'{m.value}'" for m in enum_cls)


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class Row(BaseModel):
    """Base for anything that round-trips through SQLite."""

    model_config = ConfigDict(frozen=True, extra="ignore", use_enum_values=True)


class Customer(Row):
    id: str
    segment: Segment
    payday_dom: int | None = Field(default=None, ge=1, le=31)
    language: Language = Language.EN
    ltv_paise: int = Field(ge=0)
    opted_out: int = Field(default=0, ge=0, le=1)
    created_at: int


class Event(Row):
    """A payment failure, as normalized from a webhook or the simulator."""

    id: str
    provider: str
    provider_event_id: str
    customer_id: str
    segment: Segment
    occurred_at: int
    amount_paise: int = Field(ge=0)
    currency: str = "INR"
    root_cause: RootCause = RootCause.UNKNOWN
    retry_index: int = Field(default=0, ge=0)
    days_from_payday: int | None = None
    payload: str  # JSON text; gateway_code/amount_bucket/payday_phase derive from it


class Proposal(Row):
    """A proposer's bid. Proposers never execute -- see ADR-001."""

    id: str
    window_id: str
    event_id: str
    customer_id: str
    proposer_id: str
    channel: Channel
    scheduled_at: int
    claimed_success_prob: float = Field(ge=0.0, le=1.0)
    claimed_value_paise: int
    incentive_paise: int = Field(default=0, ge=0)
    rationale: str
    payload: str
    created_at: int


class Decision(Row):
    """The arbiter's verdict for one event in one window."""

    id: str
    window_id: str
    event_id: str
    customer_id: str
    winning_proposal_id: str | None = None
    score: float | None = None
    outcome: DecisionOutcome
    reason: str
    policy_version: str
    decided_at: int
    detail: str  # JSON: per-proposal scores + suppression reasons


class Action(Row):
    """Outbox row. The only table with mutable columns, and only these four:
    status, sent_at, attempts, provider_ref."""

    id: str
    decision_id: str
    customer_id: str
    idempotency_key: str
    channel: Channel
    status: ActionStatus
    scheduled_at: int
    sent_at: int | None = None
    attempts: int = Field(default=0, ge=0)
    provider_ref: str | None = None
    body: str
    created_at: int


class Outcome(Row):
    """What actually happened after an action was executed.

    This is observed history, not ground truth -- a production control plane
    legitimately knows the result of actions it took. The generative model
    parameters and counterfactuals live in truth.db, which rcp/ never opens.
    """

    id: str
    action_id: str
    event_id: str
    customer_id: str
    succeeded: int = Field(ge=0, le=1)
    recovered_paise: int = Field(default=0, ge=0)
    opted_out: int = Field(default=0, ge=0, le=1)
    observed_at: int


class Promise(Row):
    id: str
    customer_id: str
    event_id: str
    state: PromiseState
    amount_paise: int = Field(ge=0)
    due_at: int
    created_at: int
    updated_at: int


class BankHealth(Row):
    id: str
    bank_code: str
    rail: str
    window_start: int
    success_rate: float = Field(ge=0.0, le=1.0)
    sample_size: int = Field(ge=0)
    degraded: int = Field(ge=0, le=1)


class AuditRecord(Row):
    """One line of audit.jsonl. The JSONL file is canonical; the SQLite
    audit_mirror table is a derived index for the viewer."""

    seq: int
    hash: str
    prev_hash: str
    kind: str
    ref_id: str | None = None
    ts: int
    body: dict[str, Any]


# --------------------------------------------------------------------------
# non-persisted results
# --------------------------------------------------------------------------

class Suppressed(Row):
    """Returned when a guard refuses an action. Carries the observed numbers so
    the audit line is self-explaining rather than just a reason code."""

    reason: str
    observed: int | None = None
    cap: int | None = None
    detail: str | None = None


class Precedent(Row):
    """Feature-keyed prior over recovery success. Replaces vector retrieval --
    see ADR-006. Every field here is meant to be readable in an audit line."""

    posterior: float
    successes: int
    trials: int
    level: str  # which backoff tier answered
    key: str    # the feature tuple that matched
    explanation: str
