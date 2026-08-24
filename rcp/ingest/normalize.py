"""Razorpay webhook envelope -> a row ready for `events`.

Two jobs:

1. **Flatten.** A Razorpay webhook nests the interesting part at
   `payload.payment.entity`. `events.payload` stores that entity, not the
   envelope, so the `gateway_code` generated column can read `$.error_code`
   from the top level.

2. **Classify.** Free-text decline descriptions -> a `RootCause` enum value.

On classification: the corpus is small and bounded -- 8 root causes x ~5
templates x 5 bank prefixes = ~200 distinct strings for the life of the project.
An ordered regex table collapses them deterministically and offline, which is
why `make eval` needs no API key. `classify()` takes an optional `llm` callable
for the strings the table misses; the cache key is
`sha256(gateway|code|description)`, so each unique string costs at most one call
ever. See ADR-006 for why this is a cache and not a vector index.

Rule order is significant and load-bearing -- see the comment on RULES.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Callable

from rcp.schema import RootCause
from rcp.store import canonical_json, content_id
from rcp.timeutil import days_from_payday

# Ordered narrowest -> broadest. First match wins, so the specific causes must
# precede the generic ones:
#   - insufficient_funds first: "low balance" would otherwise be swallowed by
#     the generic decline patterns.
#   - mandate before card: "subscription mandate expired" mentions expiry but is
#     a mandate problem, and retrying it can never work.
#   - auth before invalid_account: "invalid CVV" is an auth failure on a
#     perfectly valid card.
RULES: tuple[tuple[RootCause, re.Pattern[str]], ...] = (
    (RootCause.INSUFFICIENT_FUNDS, re.compile(
        r"insufficient\s+(balance|funds)|enough\s+funds|low\s+balance", re.I)),
    (RootCause.MANDATE_EXPIRED, re.compile(
        r"(mandate|standing\s+instruction|e-?mandate)"
        r".*(expired|cancel|revok|inactive|no\s+longer\s+active)"
        r"|(expired|cancel|revok).*(mandate|standing\s+instruction)", re.I)),
    (RootCause.CARD_EXPIRED, re.compile(
        r"(card|payment\s+instrument).*(expired|expiry)"
        r"|expired\s+(card|payment\s+instrument)", re.I)),
    (RootCause.BANK_DOWNTIME, re.compile(
        r"bank\s+is\s+(currently\s+)?down|issuer\s+unavailable"
        r"|gateway\s+timeout|not\s+responding|temporarily\s+unavailable"
        r"|acquirer", re.I)),
    (RootCause.LIMIT_EXCEEDED, re.compile(
        r"(exceed\w*)\s+(the\s+)?(\w+\s+){0,3}?(limit|threshold)"
        r"|(limit|threshold)\s+exceed", re.I)),
    (RootCause.AUTH_FAILED, re.compile(
        r"(otp|3ds|authentication|cvv|pin)"
        r".*(fail|incorrect|invalid|declin|wrong)"
        r"|(fail|incorrect|invalid|declin|wrong).*(otp|3ds|authentication|cvv|pin)",
        re.I)),
    (RootCause.INVALID_ACCOUNT, re.compile(
        r"(account|card)\s*(number\s*)?(is\s+)?(has\s+been\s+)?"
        r"(invalid|closed|blocked|frozen|not\s+found)"
        r"|(invalid|closed|blocked|frozen|not\s+found).*(account|card)", re.I)),
)


def cache_key(gateway: str, code: str, description: str) -> str:
    """Stable key for one decline string. Identical text from the same gateway
    is classified once, ever."""
    return hashlib.sha256(f"{gateway}|{code}|{description}".encode()).hexdigest()


class NormalizeCache:
    """Memo plus hit/miss counters.

    The counters are not decoration: they are how the claim "~200 unique
    strings, so the LLM path costs ~200 calls total" gets checked rather than
    asserted. eval/ prints the hit rate.
    """

    def __init__(self) -> None:
        self._entries: dict[str, RootCause] = {}
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> RootCause | None:
        value = self._entries.get(key)
        if value is None:
            self.misses += 1
        else:
            self.hits += 1
        return value

    def put(self, key: str, value: RootCause) -> None:
        self._entries[key] = value

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    @property
    def unique_strings(self) -> int:
        return len(self._entries)


def classify_text(description: str) -> RootCause | None:
    """Regex table only. Returns None when nothing matches, so callers can tell
    'no rule fired' from 'confidently unknown'."""
    for cause, pattern in RULES:
        if pattern.search(description):
            return cause
    return None


def classify(
    gateway: str,
    code: str,
    description: str,
    *,
    cache: NormalizeCache | None = None,
    llm: Callable[[str, str, str], RootCause] | None = None,
) -> RootCause:
    """Regex first, optional LLM for the tail, cache in front of both."""
    key = cache_key(gateway, code, description)
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            return cached

    result = classify_text(description)
    if result is None:
        result = llm(gateway, code, description) if llm else RootCause.UNKNOWN

    if cache is not None:
        cache.put(key, result)
    return result


def payment_entity(envelope: dict[str, Any]) -> dict[str, Any]:
    """Pull `payload.payment.entity` out of a Razorpay webhook envelope."""
    try:
        return envelope["payload"]["payment"]["entity"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"not a payment webhook envelope: {exc}") from exc


def normalize_webhook(
    envelope: dict[str, Any],
    *,
    provider: str = "razorpay",
    payday_dom: int | None = None,
    cache: NormalizeCache | None = None,
    llm: Callable[[str, str, str], RootCause] | None = None,
) -> dict[str, Any]:
    """Envelope -> the `normalized` dict that webhook.ingest_event expects.

    `payday_dom` comes from the customer record; a real webhook does not carry
    it. Passing it here keeps the calendar arithmetic in one place instead of
    scattering it across proposers.
    """
    entity = payment_entity(envelope)
    notes = entity.get("notes") or {}

    occurred_at = int(envelope["created_at"]) * 1000
    description = entity.get("error_description") or ""
    code = entity.get("error_code") or ""

    return {
        "id": content_id("evt", provider, entity["id"]),
        "customer_id": notes["customer_id"],
        "segment": notes["segment"],
        "occurred_at": occurred_at,
        "amount_paise": int(entity["amount"]),
        "currency": entity.get("currency", "INR"),
        "root_cause": classify(provider, code, description, cache=cache, llm=llm).value,
        "retry_index": int(notes.get("retry_index", 0)),
        "days_from_payday": days_from_payday(occurred_at, payday_dom),
        "payload": canonical_json(entity),
    }
