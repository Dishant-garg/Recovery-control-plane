"""The dashboard: what the control plane did, and why it did not do the rest.

Server-rendered Jinja2 over the same SQLite files `make eval` writes. No build
step, no frontend toolchain, no API layer to keep in sync -- the templates read
the database directly, so a screen cannot disagree with the run that produced
it.

**Read-only, except one route.** Every page opens its connection with
`store.connect(..., read_only=True)`, which sets `PRAGMA query_only = ON`. The
dashboard cannot corrupt a run mid-demo no matter what a template does. The
webhook route is the single exception and it writes to `rcp.db`, never to the
forked per-arm databases the results were computed from.

Four surfaces:

    /            what was recovered, and what was not, with the reason
    /cases       every case; click through to its timeline
    /audit       the hash chain, recomputed live
    /live        POST a signed webhook and watch a case open

The case timeline is the one that matters. It is the audit trail as a human
artifact: every rung, who decided it (`policy` / `agent` / `compliance` /
`stopping_rule`), every refusal with the rule that produced it, and the message
that actually went out.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from rcp.audit import read_all, verify
from rcp.compose import sms_segments
from rcp.store import DATA_DIR, connect

HERE = Path(__file__).resolve().parent
app = FastAPI(title="Recovery Control Plane")
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")

DEFAULT_SEED = int(os.getenv("RCP_SEED", "42"))
DEFAULT_ARM = "control_plane"
ARMS = ("control_plane", "capped", "baseline")


# --------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------

def db_path(seed: int, arm: str) -> Path:
    """The per-arm database `eval/run.py` forked, falling back to the base one.

    `rcp.db` holds the generated events but no decisions -- it is what the
    webhook route writes into. The arm databases are where a completed run
    lives, so they are what the read-only screens want.
    """
    forked = DATA_DIR / f"seed_{seed}" / f"rcp_{arm}.db"
    return forked if forked.exists() else DATA_DIR / f"seed_{seed}" / "rcp.db"


def open_ro(seed: int, arm: str) -> sqlite3.Connection:
    path = db_path(seed, arm)
    if not path.exists():
        raise HTTPException(
            404,
            f"no database at {path}. Run `make data && make eval` first.",
        )
    return connect(path, read_only=True)


def available_seeds() -> list[int]:
    if not DATA_DIR.exists():
        return []
    seeds = []
    for child in sorted(DATA_DIR.glob("seed_*")):
        try:
            seeds.append(int(child.name.split("_")[1]))
        except (IndexError, ValueError):
            continue
    return seeds


def available_arms(seed: int) -> list[str]:
    return [
        arm for arm in ARMS
        if (DATA_DIR / f"seed_{seed}" / f"rcp_{arm}.db").exists()
    ]


def results_json(seed: int) -> dict[str, Any] | None:
    path = DATA_DIR / f"seed_{seed}" / "results.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def audit_file(seed: int, arm: str) -> Path:
    forked = DATA_DIR / f"seed_{seed}" / f"audit_{arm}.jsonl"
    return forked if forked.exists() else DATA_DIR / f"seed_{seed}" / "audit.jsonl"


def ctx(request: Request, seed: int, arm: str, **extra: Any) -> dict[str, Any]:
    """Everything every page needs: the seed/arm switcher and the active tab."""
    return {
        "request": request, "seed": seed, "arm": arm,
        "seeds": available_seeds(), "arms": available_arms(seed) or [arm],
        "path": request.url.path,
        **extra,
    }


def rupees(paise: Any) -> str:
    try:
        return f"{int(paise) // 100:,}"
    except (TypeError, ValueError):
        return "-"


def when(ms: Any) -> str:
    """Sim-clock milliseconds as a readable UTC stamp.

    Deliberately not localized: the run's clock is UTC by construction
    (ADR-002), and rendering it in the viewer's zone would put a timestamp on
    screen that does not match the one in the audit log.
    """
    try:
        stamp = time.gmtime(int(ms) / 1000)
    except (TypeError, ValueError):
        return "-"
    return time.strftime("%d %b %Y %H:%M", stamp)


templates.env.filters["rupees"] = rupees
templates.env.filters["when"] = when


# --------------------------------------------------------------------------
# overview
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def overview(request: Request, seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM):
    """Money recovered, and money *not* recovered broken out by reason.

    The second half is the point. Any dunning tool can report what it
    collected; the claim this project has to defend is "we deliberately did not
    contact these customers, and here is what that decision cost".
    """
    conn = open_ro(seed, arm)
    try:
        states = {
            r["state"]: r["n"] for r in conn.execute(
                "SELECT state, count(*) AS n FROM cases GROUP BY state"
            )
        }
        money = conn.execute(
            "SELECT COALESCE(SUM(o.recovered_paise), 0) AS recovered, "
            "       COUNT(*) FILTER (WHERE o.succeeded = 1) AS wins, "
            "       COALESCE(SUM(o.opted_out), 0) AS opt_outs "
            "FROM outcomes o"
        ).fetchone()
        sent = conn.execute(
            "SELECT count(*) FROM actions WHERE status = 'sent'"
        ).fetchone()[0]

        # Why an event did not produce an action. These four reasons are
        # mutually exclusive by construction in arbiter/select.py.
        suppression = conn.execute(
            "SELECT CASE "
            "  WHEN reason LIKE 'compliance:%'             THEN 'compliance refused' "
            "  WHEN reason LIKE 'contact cap%'             THEN 'contact cap' "
            "  WHEN reason LIKE 'negative platform value%' THEN 'not worth sending' "
            "  ELSE 'no proposal' END AS bucket, "
            "count(*) AS n, "
            "COALESCE(SUM(e.amount_paise), 0) AS paise "
            "FROM decisions d JOIN events e ON e.id = d.event_id "
            "WHERE d.outcome = 'suppressed' GROUP BY bucket ORDER BY n DESC"
        ).fetchall()

        write_offs = conn.execute(
            "SELECT substr(close_reason, 1, instr(close_reason || ':', ':') - 1) "
            "       AS rule, count(*) AS n, COALESCE(SUM(amount_paise), 0) AS paise "
            "FROM cases WHERE close_reason IS NOT NULL "
            "GROUP BY rule ORDER BY n DESC"
        ).fetchall()

        channels = conn.execute(
            "SELECT channel, count(*) AS n FROM actions WHERE status = 'sent' "
            "GROUP BY channel ORDER BY n DESC"
        ).fetchall()

        languages = conn.execute(
            "SELECT json_extract(body, '$.message.language') AS lang, "
            "count(*) AS n FROM actions WHERE status = 'sent' "
            "AND json_extract(body, '$.message.language') IS NOT NULL "
            "GROUP BY lang ORDER BY n DESC"
        ).fetchall()
    finally:
        conn.close()

    return templates.TemplateResponse(request, "overview.html", ctx(
        request, seed, arm,
        states=states, money=money, sent=sent, suppression=suppression,
        write_offs=write_offs, channels=channels, languages=languages,
        results=results_json(seed),
        total_cases=sum(states.values()),
    ))


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------

@app.get("/cases", response_class=HTMLResponse)
def case_list(
    request: Request,
    seed: int = DEFAULT_SEED,
    arm: str = DEFAULT_ARM,
    state: str = "",
    segment: str = "",
    limit: int = 100,
):
    conn = open_ro(seed, arm)
    try:
        where, params = ["1 = 1"], []
        if state:
            where.append("c.state = ?")
            params.append(state)
        if segment:
            where.append("c.segment = ?")
            params.append(segment)

        rows = conn.execute(
            "SELECT c.*, e.root_cause AS root_cause, "
            "  (SELECT count(*) FROM case_events ce WHERE ce.case_id = c.id) "
            "    AS timeline_len "
            "FROM cases c JOIN events e ON e.id = c.event_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY c.amount_paise DESC, c.id ASC LIMIT ?",
            params + [min(limit, 500)],
        ).fetchall()

        facets = {
            "state": conn.execute(
                "SELECT state AS v, count(*) AS n FROM cases GROUP BY v "
                "ORDER BY n DESC"
            ).fetchall(),
            "segment": conn.execute(
                "SELECT segment AS v, count(*) AS n FROM cases GROUP BY v "
                "ORDER BY n DESC"
            ).fetchall(),
        }
    finally:
        conn.close()

    return templates.TemplateResponse(request, "cases.html", ctx(
        request, seed, arm, rows=rows, facets=facets,
        state=state, segment=segment,
    ))


@app.get("/cases/{case_id}", response_class=HTMLResponse)
def case_detail(
    request: Request, case_id: str,
    seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM,
):
    """One case, end to end.

    Every timeline row is decorated with what it produced: an `acted` row gets
    the message that went out, a `held` row gets the rule that refused it and
    whether that cost a ladder rung (ADR-009). Without those two joins the
    timeline says "held" fourteen times and explains nothing.
    """
    conn = open_ro(seed, arm)
    try:
        row = conn.execute(
            "SELECT c.*, e.root_cause AS root_cause, e.amount_paise AS amount, "
            "       e.occurred_at AS occurred_at, cu.language AS language, "
            "       cu.ltv_paise AS ltv_paise, cu.consent AS consent "
            "FROM cases c JOIN events e ON e.id = c.event_id "
            "JOIN customers cu ON cu.id = c.customer_id WHERE c.id = ?",
            (case_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"no case {case_id}")
        case = dict(row)

        events = [dict(r) for r in conn.execute(
            "SELECT * FROM case_events WHERE case_id = ? ORDER BY seq ASC",
            (case_id,),
        )]

        # Actions for this case, keyed by the window that produced them, so an
        # `acted` timeline row can show the copy it sent.
        actions = {
            r["window_id"]: dict(r) for r in conn.execute(
                "SELECT a.*, d.window_id AS window_id FROM actions a "
                "JOIN decisions d ON d.id = a.decision_id WHERE d.event_id = ?",
                (case["event_id"],),
            )
        }

        outcomes = [dict(r) for r in conn.execute(
            "SELECT * FROM outcomes WHERE event_id = ? ORDER BY observed_at ASC",
            (case["event_id"],),
        )]

        for entry in events:
            entry["detail"] = json.loads(entry["detail"] or "{}")
            window = entry["detail"].get("window_id")
            action = actions.get(window) if window else None
            entry["action"] = action
            entry["message"] = None
            if action:
                body = json.loads(action["body"] or "{}")
                entry["message"] = body.get("message")
                entry["rationale"] = body.get("rationale")

        contacts = conn.execute(
            "SELECT count(*) FROM actions WHERE customer_id = ? "
            "AND status = 'sent'", (case["customer_id"],),
        ).fetchone()[0]
    finally:
        conn.close()

    for entry in events:
        message = entry.get("message")
        if message and message.get("text"):
            entry["segments"] = sms_segments(message["text"])

    return templates.TemplateResponse(request, "case.html", ctx(
        request, seed, arm, case=case, events=events, outcomes=outcomes,
        contacts=contacts,
    ))


# --------------------------------------------------------------------------
# agents
# --------------------------------------------------------------------------

# What each agent decides and which tool lets it interrogate its own bound.
# Kept here rather than imported so the page renders even when a provider SDK
# is not installed.
AGENTS = [
    ("recovery", "rcp/agents/recovery.py", "--live N",
     "escalate / hold / stop, for one case", "compliance_preview",
     "what would the engine say if I escalated now?"),
    ("analyst", "eval/analyst.py", "make analyze",
     "where the control plane loses money", "sample_decision",
     "show me one decision's full scoring"),
    ("composer", "rcp/agents/composer.py", "make drafts",
     "message templates for gaps in the registry", "check_draft",
     "would the critic block this copy?"),
    ("strategy", "rcp/agents/strategy.py", "make strategy",
     "what the proposers' probability tables should say", "rail_comparison",
     "does retry actually beat messaging here?"),
    ("red team", "rcp/agents/redteam.py", "make redteam",
     "copy that passes the compliance critic and should not", "try_copy",
     "would the critic let this through?"),
]

DRAFT_FILES = [
    ("composer", "templates.json", "drafts", "make drafts"),
    ("strategy", "strategy.json", "revisions", "make strategy"),
    ("red team", "redteam.json", "attacks", "make redteam"),
]


def _drafts() -> list[dict[str, Any]]:
    """Whatever the authoring agents have proposed, read from disk.

    These files are gitignored: they are proposals awaiting review, and
    committing one would make it look like a decision. A fresh clone shows an
    empty panel with the command that fills it.
    """
    out = []
    for agent, filename, key, command in DRAFT_FILES:
        path = DATA_DIR / "drafts" / filename
        entry = {"agent": agent, "command": command, "items": [],
                 "provider": None, "model": ""}
        if path.exists():
            try:
                body = json.loads(path.read_text())
                entry["items"] = body.get(key) or []
                entry["provider"] = body.get("provider")
                entry["model"] = body.get("model") or ""
            except json.JSONDecodeError:
                pass
        out.append(entry)
    return out


def _voice_scripts() -> list[dict[str, Any]]:
    """The spoken templates, with audio where it has been generated.

    Audio is a committed build artifact (`make voice`, macOS only), so this
    reports `audio: None` rather than failing on a machine that has never run
    it -- the scripts are still worth reading.
    """
    from rcp.compose.critic import blocking, check
    from rcp.compose.render import Message
    from rcp.compose.templates import REGISTRY
    from scripts.generate_voice import render

    out = []
    for template in sorted(
        (t for t in REGISTRY if t.channel == "voice"), key=lambda t: t.id
    ):
        text = render(template.text)
        audio = HERE / "static" / "audio" / f"{template.id}.m4a"
        out.append({
            "id": template.id,
            "language": template.language,
            "purpose": template.purpose,
            "text": text,
            "chars": len(text),
            "audio": f"/static/audio/{template.id}.m4a" if audio.exists() else None,
            # A spoken script with a link in it is the mistake `url_in_voice`
            # exists to catch, so the verdict rides along with the player.
            "blocked_by": ", ".join(sorted({
                f.rule for f in blocking(check(Message(
                    template_id=template.id, channel="voice",
                    language=template.language, purpose=template.purpose,
                    text=text,
                )))
            })),
        })
    return out


def _redteam_scoreboard() -> list[dict[str, Any]]:
    """Run the attack corpus through the critic, now, on page load.

    Deterministic and instant, so the safety layer is demonstrated rather than
    asserted -- and if a coercion rule ever regresses, this table says so
    without anyone running a test.
    """
    from rcp.agents.redteam import TACTICS, deterministic_attacks
    from rcp.compose.critic import blocking, check
    from rcp.compose.render import Message

    rows = []
    for attack in deterministic_attacks():
        findings = check(Message(
            template_id="redteam", channel=attack.channel, language="hinglish",
            purpose="final", text=attack.text,
        ))
        blockers = blocking(findings)
        rows.append({
            "text": attack.text, "tactic": attack.tactic,
            "why": attack.why_abusive,
            "blocked": bool(blockers),
            "by": ", ".join(sorted({f.rule for f in blockers})),
            "description": TACTICS.get(attack.tactic, ""),
        })
    return rows


@app.post("/api/critic-check")
async def critic_check(request: Request):
    """Run arbitrary copy through the compliance critic.

    A pure function over `rcp.compose.critic.check` -- no database, no writes,
    no provider, no key. It is what `rcp/agents/redteam.py` calls as `try_copy`,
    exposed so a human can attack the filter the same way the agent does.

    Handing a reviewer the tactic list along with the box is deliberate. The red
    team agent is given `critic_rules()` openly, on the reasoning that a real
    adversary knows the filter anyway; showing the model more than the person
    would be theatre.
    """
    from rcp.compose.critic import COERCION_PATTERNS, blocking, check
    from rcp.compose.render import Message

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON") from None

    text = str(body.get("text") or "")
    if not text.strip():
        raise HTTPException(400, "nothing to check")
    if len(text) > 8000:
        raise HTTPException(413, "too long to be a message")

    channel = str(body.get("channel") or "sms")
    if channel not in MAX_CHANNELS:
        raise HTTPException(400, f"unknown channel: {channel}")

    findings = check(Message(
        template_id="reviewer", channel=channel, language="hinglish",
        purpose="final", text=text,
    ))
    blockers = blocking(findings)
    return JSONResponse({
        "passes": not blockers,
        "findings": [
            {"rule": f.rule, "severity": f.severity, "note": f.note,
             "observed": f.observed}
            for f in findings
        ],
        "tactics": sorted(COERCION_PATTERNS),
    })


# Channels the critic knows how to judge. `retry` is absent because a silent
# rail attempt carries no copy at all.
MAX_CHANNELS = ("sms", "whatsapp", "email", "voice")

# Shown as one-click examples, so a reviewer who cannot think of an attack still
# sees the filter work. Two are refused, one passes -- a box that refuses
# everything teaches nothing about where the line is.
PRESET_ATTACKS = [
    ("threatens the employer",
     "Rs 2,500 pending hai. Aapke employer ko inform karna pad sakta hai: {link}"),
    ("threatens the credit score",
     "Rs 2,500 is pending. If not cleared it could hurt your borrowing score "
     "and future loan approvals: {link}"),
    ("a legitimate final notice",
     "Rs 2,500 ka payment 12 din se pending hai. Pay karein: {link} "
     "Yeh hamara aakhri reminder hai."),
]


@app.get("/agents", response_class=HTMLResponse)
def agents(request: Request, seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM):
    """The agents, what they decided, and what they found.

    Needed because the rest of the dashboard cannot show any of it. The batch
    eval runs the deterministic policy so `make eval` stays byte-reproducible
    and free, which means `case_events.decided_by` is never `agent` in these
    databases -- the agentic work happens beside the batch, not inside it.
    """
    conn = open_ro(seed, arm)
    try:
        deciders = {
            r["decided_by"]: r["n"] for r in conn.execute(
                "SELECT decided_by, count(*) AS n FROM case_events "
                "GROUP BY decided_by ORDER BY n DESC"
            )
        }
    finally:
        conn.close()

    from rcp.agents.redteam import TACTICS

    return templates.TemplateResponse(request, "agents.html", ctx(
        request, seed, arm, agents=AGENTS, drafts=_drafts(),
        scoreboard=_redteam_scoreboard(), deciders=deciders,
        tactics=TACTICS, presets=PRESET_ATTACKS, channels=MAX_CHANNELS,
        voices=_voice_scripts(),
    ))


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

# How many live agent runs this process will pay for. A reviewer clicking
# repeatedly should not be able to spend a token budget, and the deterministic
# path past the limit is the same one the batch uses -- so hitting it degrades
# the demo rather than breaking it.
AGENT_RUN_BUDGET = 20
_agent_runs = 0


@app.post("/api/agent/decide")
async def agent_decide(request: Request):
    """Run the recovery agent on one case and return its reasoning.

    The rest of the dashboard cannot show this. `make eval` runs the
    deterministic policy so the headline number stays byte-reproducible and
    needs no API key, which means `case_events.decided_by` is never `agent` in
    any database here. The agent's judgement happens beside the batch.

    **Read-only.** `RecoveryAgent` only reads -- `submit_decision` writes into a
    local dict and `caseloop` does the persisting -- so this opens the database
    with `query_only` like every other screen and cannot alter the run being
    demonstrated.

    With no provider configured, `run_agent` runs the deterministic routine and
    the response says so. That is the same path a rate limit takes mid-demo, so
    the failure mode is a rehearsed screen rather than a traceback.
    """
    global _agent_runs

    from rcp.agents.recovery import RecoveryAgent
    from rcp.env import load_dotenv
    from rcp.escalation import channel_at, next_rung
    from rcp.precedent import lookup

    # Loaded here rather than at import. Without it the "run the agent" button
    # reported the deterministic path forever with a working key sitting in
    # .env -- a failure indistinguishable from the feature working as designed.
    #
    # At import it would be worse: `load_dotenv` writes os.environ directly, so
    # importing this module for a route test set RCP_LLM for the whole pytest
    # process and three unrelated tests started reaching for a provider.
    # `override=False` also means an explicitly set RCP_LLM still wins.
    load_dotenv()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not JSON") from None

    case_id = str(body.get("case_id") or "")
    seed = int(body.get("seed", DEFAULT_SEED))
    arm = str(body.get("arm") or DEFAULT_ARM)

    conn = open_ro(seed, arm)
    try:
        row = conn.execute("SELECT * FROM cases WHERE id = ?", (case_id,)).fetchone()
        if row is None:
            raise HTTPException(404, f"no case {case_id}")
        case = dict(row)
        event = dict(conn.execute(
            "SELECT * FROM events WHERE id = ?", (case["event_id"],)).fetchone())
        customer = dict(conn.execute(
            "SELECT * FROM customers WHERE id = ?", (case["customer_id"],)).fetchone())

        # The same context `caseloop.work_due_cases` builds before calling the
        # decide hook. Rebuilt rather than refactored out: the loop's version
        # runs inside a write transaction, and this one must not.
        rung = next_rung(case, event["root_cause"])
        channel = channel_at(case["segment"], rung) if rung is not None else None
        posterior = 0.0
        if channel is not None:
            posterior = lookup(
                conn, root_cause=event["root_cause"],
                amount_bucket=event["amount_bucket"],
                payday_phase=event["payday_phase"], channel=channel,
            ).posterior

        now_ms = int(case["next_review_at"] or case["opened_at"])
        agent = RecoveryAgent(
            conn, live_budget=1 if _agent_runs < AGENT_RUN_BUDGET else 0
        )
        move = agent(case, {
            "rung": rung, "channel": channel, "posterior": posterior,
            "event": event, "customer": customer, "now_ms": now_ms,
        })
        result = agent.transcripts[-1] if agent.transcripts else None

        # Charged only for a run that actually reached a provider. A case whose
        # ladder is exhausted short-circuits to the policy without a model call,
        # and counting that would let a reviewer exhaust the budget clicking
        # through closed cases.
        if result is not None and result.provider != "fallback":
            _agent_runs += 1
    finally:
        conn.close()

    return JSONResponse({
        "case_id": case_id,
        "rung": rung,
        "channel": channel,
        "posterior": round(posterior, 4),
        "move": {"action": move.action, "reason": move.reason,
                 "decided_by": move.decided_by, "hold_days": move.hold_days},
        "provider": result.provider if result else "policy",
        "model": result.model if result else "",
        "degraded": result.degraded if result else None,
        "trail": result.trail if result else [],
        "budget_left": max(0, AGENT_RUN_BUDGET - _agent_runs),
        # Said plainly rather than inferred from `provider`, because "the model
        # decided this" and "the fallback decided this" must never look alike.
        "was_live": bool(result and result.provider != "fallback"),
    })


@app.get("/audit", response_class=HTMLResponse)
def audit(request: Request, seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM,
          limit: int = 50):
    path = audit_file(seed, arm)
    ok, message, records, total = False, f"no audit log at {path}", [], 0
    if path.exists():
        ok, message = verify(path)
        every = read_all(path)
        total = len(every)
        records = every[-limit:][::-1]
    return templates.TemplateResponse(request, "audit.html", ctx(
        request, seed, arm, ok=ok, message=message, records=records,
        total=total, path=str(path),
    ))


@app.get("/api/verify")
def api_verify(seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM):
    """Recompute the chain. The demo is: tamper a line, refresh, watch it fail."""
    path = audit_file(seed, arm)
    if not path.exists():
        return JSONResponse({"ok": False, "message": f"no audit log at {path}"})
    ok, message = verify(path)
    return JSONResponse({"ok": ok, "message": message, "path": str(path)})


# --------------------------------------------------------------------------
# live webhook
# --------------------------------------------------------------------------

@app.get("/live", response_class=HTMLResponse)
def live(request: Request, seed: int = DEFAULT_SEED, arm: str = DEFAULT_ARM):
    # The base rcp.db, not an arm fork: the webhook writes there, and a live
    # event should show up next to the ones the generator produced.
    base = DATA_DIR / f"seed_{seed}" / "rcp.db"
    if not base.exists():
        raise HTTPException(404, f"no database at {base}. Run `make data` first.")
    conn = connect(base, read_only=True)
    try:
        recent = [dict(r) for r in conn.execute(
            "SELECT id, customer_id, segment, root_cause, amount_paise, "
            "occurred_at FROM events ORDER BY occurred_at DESC LIMIT 10"
        )]
        customer = conn.execute(
            "SELECT id, segment, language, payday_dom FROM customers "
            "ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    return templates.TemplateResponse(request, "live.html", ctx(
        request, seed, arm, recent=recent,
        customer=dict(customer) if customer else None,
        secret_set=bool(os.getenv("RCP_WEBHOOK_SECRET")),
    ))


@app.post("/api/webhook")
async def webhook(request: Request):
    """Razorpay-shaped intake. The signature is verified against the RAW body.

    `json.loads` followed by `json.dumps` reorders keys and changes whitespace,
    so an HMAC computed over a re-serialized dict will never match -- and
    someone will eventually "fix" that by loosening the check. See
    `rcp/ingest/webhook.py`.
    """
    from rcp.ingest.normalize import normalize_webhook, payment_entity
    from rcp.ingest.webhook import SignatureError, ingest_event, require_signature
    from rcp.store import write_txn

    raw = await request.body()
    secret = os.getenv("RCP_WEBHOOK_SECRET", "dev-secret")
    try:
        require_signature(raw, request.headers.get("x-razorpay-signature"), secret)
    except SignatureError as exc:
        raise HTTPException(400, str(exc)) from exc

    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "body is not JSON") from exc

    seed = int(request.query_params.get("seed", DEFAULT_SEED))
    path = DATA_DIR / f"seed_{seed}" / "rcp.db"
    if not path.exists():
        raise HTTPException(404, f"no database at {path}; run `make data`")

    conn = connect(path)
    try:
        row = conn.execute(
            "SELECT payday_dom FROM customers WHERE id = ?",
            ((envelope.get("payload", {}).get("payment", {})
              .get("entity", {}).get("notes", {}) or {}).get("customer_id"),),
        ).fetchone()
        normalized = normalize_webhook(
            envelope, payday_dom=row["payday_dom"] if row else None
        )
        # The provider's own id for the payment. It is the dedup key, so it
        # stays separate from the normalized body rather than being derived
        # from it -- `UNIQUE (provider, provider_event_id)` is what makes a
        # redelivery a no-op.
        with write_txn(conn):
            event_id = ingest_event(
                conn, provider="razorpay",
                provider_event_id=payment_entity(envelope)["id"],
                normalized=normalized,
            )
    except KeyError as exc:
        raise HTTPException(422, f"missing field: {exc}") from exc
    finally:
        conn.close()

    # A replay returns None rather than an error: Razorpay retries on any
    # non-2xx, so duplicate deliveries are expected rather than exceptional.
    return JSONResponse({
        "accepted": True,
        "event_id": event_id,
        "duplicate": event_id is None,
        "received_at": int(time.time() * 1000),
    })


@app.post("/api/sign")
async def sign(request: Request):
    """Sign the RAW request body with the dashboard's own webhook secret.

    A convenience for the demo page so a reviewer can fire a valid webhook
    without shelling out to `openssl`. It signs only what it is given and
    reveals nothing about the secret, but it is still a development
    affordance -- a deployment taking real Razorpay traffic should not expose
    it.

    **The payload arrives as the body, never as a form field.** It used to be
    `Form(...)`, and that was broken in a way worth recording: the Fetch
    standard's multipart/form-data serializer rewrites every lone LF in a field
    value to CRLF. So this endpoint signed a CRLF version of the JSON while the
    browser posted the LF version to /api/webhook, and every signed send from
    the page returned 400.

    It is the same failure `rcp/ingest/webhook.py` warns about -- a signature
    computed over anything but the exact bytes that get sent -- committed by
    the demo built to show the check working. `curl -F` does not normalise, so
    testing with curl said it was fine.
    """
    import hashlib
    import hmac

    secret = os.getenv("RCP_WEBHOOK_SECRET", "dev-secret")
    digest = hmac.new(
        secret.encode(), await request.body(), hashlib.sha256
    ).hexdigest()
    return JSONResponse({"signature": digest})
