"""Seeded synthetic event generation.

Emits real Razorpay-shaped `payment.failed` webhook envelopes, HMAC-signs them,
and feeds them through `rcp/ingest/webhook.py` exactly as a live webhook would
arrive. That is deliberate: the signature-verify and dedup paths get exercised
on every single run, and switching to live Razorpay becomes a config change
rather than an untested code path.

This module is one of the two places seeded randomness is allowed (the other is
sim/outcomes.py). Everything under rcp/ is forbidden from importing `random` --
see tests/test_ground_truth_isolation.py.

Two databases are written:
  rcp.db    customers + events        -- what the control plane may see
  truth.db  per-customer latents      -- what it must infer, never read
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import random
from hashlib import sha256
from typing import Any

from rcp.audit import AuditLog
from rcp.config import load
from rcp.env import load_dotenv
from rcp.ingest.normalize import NormalizeCache, normalize_webhook
from rcp.ingest.webhook import ingest_event, require_signature
from rcp.migrations import migrate
from rcp.store import (
    audit_path,
    canonical_json,
    close,
    connect,
    content_id,
    rcp_db_path,
    write_txn,
)
from rcp.timeutil import MS_PER_DAY, MS_PER_HOUR
from sim.truth_store import open_truth, put_params, truth_db_path

PROVIDER = "razorpay"


def _weighted(rng: random.Random, weights: dict[str, float]) -> str:
    """Weighted pick over a *sorted* key order, so the draw does not depend on
    dict insertion order."""
    keys = sorted(weights)
    return rng.choices(keys, weights=[weights[k] for k in keys], k=1)[0]


def _envelope(
    *,
    payment_id: str,
    account_id: str,
    customer_id: str,
    segment: str,
    amount_paise: int,
    occurred_at_ms: int,
    error_code: str,
    error_description: str,
    retry_index: int,
    method: str,
) -> dict[str, Any]:
    """A Razorpay `payment.failed` webhook envelope."""
    return {
        "entity": "event",
        "account_id": account_id,
        "event": "payment.failed",
        "contains": ["payment"],
        "created_at": occurred_at_ms // 1000,
        "payload": {
            "payment": {
                "entity": {
                    "id": payment_id,
                    "entity": "payment",
                    "amount": amount_paise,
                    "currency": "INR",
                    "status": "failed",
                    "order_id": content_id("order", payment_id),
                    "method": method,
                    "error_code": error_code,
                    "error_description": error_description,
                    "error_source": "bank",
                    "error_step": "payment_authorization",
                    "notes": {
                        "customer_id": customer_id,
                        "segment": segment,
                        "retry_index": retry_index,
                    },
                }
            }
        },
    }


def generate(seed: int = 42, n_events: int | None = None, quiet: bool = False) -> dict:
    cfg = load("sim")
    rng = random.Random(seed)

    n_customers = int(cfg["n_customers"])
    n_events = int(n_events if n_events is not None else cfg["n_events"])
    epoch_ms = int(cfg["epoch_ms"])
    horizon_ms = int(cfg["horizon_days"]) * MS_PER_DAY
    secret = cfg["webhook_secret"]
    banks = cfg["banks"]

    # Fresh files: events are append-only, so regeneration starts from scratch.
    for path in (rcp_db_path(seed), truth_db_path(seed), audit_path(seed)):
        path.unlink(missing_ok=True)
        for sidecar in ("-wal", "-shm"):
            path.with_name(path.name + sidecar).unlink(missing_ok=True)

    conn = connect(rcp_db_path(seed))
    migrate(conn)
    truth = open_truth(seed)
    log = AuditLog(audit_path(seed))
    cache = NormalizeCache()

    # ---- customers -------------------------------------------------------
    customers: list[dict[str, Any]] = []
    for i in range(n_customers):
        cid = f"cust_{i:04d}"
        customers.append({
            "id": cid,
            "segment": _weighted(rng, cfg["segments"]),
            "payday_dom": rng.choice(cfg["payday_dom"]),
            "language": _weighted(rng, cfg["languages"]),
            "ltv_paise": 0,   # filled below, once the segment is known
            "opted_out": 0,
            "created_at": epoch_ms,
            # Recorded opt-in per channel. compliance/rules.py::Consent treats
            # an absent key as a denial, so this is what gates whatsapp/voice.
            "consent": canonical_json({
                ch: rng.random() < rate
                for ch, rate in sorted(cfg["consent_rates"].items())
            }),
        })
        ltv = cfg["ltv_paise"][customers[-1]["segment"]]
        customers[-1]["ltv_paise"] = rng.randint(ltv["min"], ltv["max"])

    with write_txn(conn):
        conn.executemany(
            "INSERT INTO customers (id, segment, payday_dom, language, ltv_paise, "
            "opted_out, created_at, consent) "
            "VALUES (:id, :segment, :payday_dom, :language, :ltv_paise, "
            ":opted_out, :created_at, :consent)",
            customers,
        )

    prop = cfg["outcomes"]["propensity"]
    with write_txn(truth):
        truth.executemany(
            "INSERT INTO customer_latent VALUES (?, ?, ?, ?)",
            [
                (c["id"],
                 round(rng.uniform(prop["min"], prop["max"]) / prop["max"], 6),
                 round(rng.uniform(0.0, 1.0), 6),
                 c["payday_dom"])
                for c in customers
            ],
        )
    put_params(truth, "outcomes", cfg["outcomes"])
    put_params(truth, "seed", seed)

    # ---- events ----------------------------------------------------------
    by_id = {c["id"]: c for c in customers}
    ingested = replayed = 0

    for i in range(n_events):
        customer = customers[rng.randrange(n_customers)]
        cause = _weighted(rng, cfg["root_causes"])
        code, description = rng.choice(cfg["decline_texts"][cause])
        description = f"{rng.choice(banks)}: {description}"

        retry_index = rng.choices(
            range(len(cfg["retry_index_weights"])),
            weights=cfg["retry_index_weights"], k=1,
        )[0]
        occurred_at = (
            epoch_ms + rng.randrange(horizon_ms // MS_PER_DAY) * MS_PER_DAY
            + rng.randrange(24) * MS_PER_HOUR
        )
        payment_id = "pay_" + hashlib.sha256(
            f"{seed}|{i}|{customer['id']}".encode()
        ).hexdigest()[:14]

        envelope = _envelope(
            payment_id=payment_id,
            account_id=cfg["account_id"],
            customer_id=customer["id"],
            segment=customer["segment"],
            amount_paise=rng.randint(cfg["amount_paise"]["min"],
                                     cfg["amount_paise"]["max"]),
            occurred_at_ms=occurred_at,
            error_code=code,
            error_description=description,
            retry_index=retry_index,
            method=rng.choice(["card", "upi", "netbanking", "emandate"]),
        )

        # Sign and verify exactly as a live delivery would be handled. If this
        # ever fails, the raw-bytes contract in webhook.py has been broken.
        raw = canonical_json(envelope).encode()
        signature = hmac.new(secret.encode(), raw, sha256).hexdigest()
        require_signature(raw, signature, secret)

        normalized = normalize_webhook(
            envelope, provider=PROVIDER,
            payday_dom=by_id[customer["id"]]["payday_dom"], cache=cache,
        )

        with write_txn(conn):
            event_id = ingest_event(
                conn, provider=PROVIDER, provider_event_id=payment_id,
                normalized=normalized,
            )
        if event_id is None:
            replayed += 1
            continue
        ingested += 1
        log.append(
            "event_ingested",
            {"event_id": event_id, "root_cause": normalized["root_cause"],
             "amount_paise": normalized["amount_paise"]},
            ts=normalized["occurred_at"], ref_id=event_id,
        )

    stats = {
        "seed": seed,
        "customers": len(customers),
        "events_ingested": ingested,
        "events_replayed": replayed,
        "unique_decline_strings": cache.unique_strings,
        "normalize_cache_hit_rate": round(cache.hit_rate, 4),
        "unknown_root_cause": conn.execute(
            "SELECT count(*) FROM events WHERE root_cause = 'unknown'"
        ).fetchone()[0],
    }

    close(conn)
    close(truth)

    if not quiet:
        for key, value in stats.items():
            print(f"  {key:28} {value}")
        print(f"  {'rcp.db':28} {rcp_db_path(seed)}")
        print(f"  {'truth.db':28} {truth_db_path(seed)}")
    return stats


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="generate seeded synthetic events")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-events", type=int, default=None)
    parser.add_argument("--scale", type=int, default=None,
                        help="alias for --n-events, for the SCALE.md stress run")
    args = parser.parse_args()
    generate(seed=args.seed, n_events=args.scale or args.n_events)


if __name__ == "__main__":
    main()
