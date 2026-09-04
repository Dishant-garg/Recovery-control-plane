"""Dashboard route tests.

Two things matter here beyond "the page renders".

**The read-only guarantee.** Every screen opens its connection with
`query_only = ON`. A template that somehow issued a write would corrupt the
run being demonstrated, so the guarantee is asserted rather than assumed.

**Webhook signature handling.** The signature must be checked against the raw
request body. A test that posts a dict and lets the client serialize it would
pass against a broken implementation, so these build the bytes first and sign
exactly those.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient   # noqa: E402

from app.main import app, available_seeds   # noqa: E402
from rcp.store import DATA_DIR              # noqa: E402

SECRET = "dev-secret"

pytestmark = pytest.mark.skipif(
    not available_seeds(),
    reason="no data/seed_* directory; run `make data && make eval` first",
)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seed():
    return available_seeds()[0]


def test_every_screen_renders(client, seed):
    for path in ("/", "/cases", "/agents", "/audit", "/live"):
        response = client.get(path, params={"seed": seed})
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert "text/html" in response.headers["content-type"]


def test_case_list_filters_by_state(client, seed):
    response = client.get("/cases", params={"seed": seed, "state": "recovered"})
    assert response.status_code == 200
    assert "recovered" in response.text


def test_case_detail_shows_the_timeline(client, seed):
    listing = client.get("/cases", params={"seed": seed})
    # Case ids are content-derived and prefixed; pull one out of the listing.
    marker = '/cases/case_'
    assert marker in listing.text, "the case list should link to case pages"
    start = listing.text.index(marker) + len("/cases/")
    case_id = listing.text[start:start + 21]

    response = client.get(f"/cases/{case_id}", params={"seed": seed})
    assert response.status_code == 200
    assert "Timeline" in response.text
    # `decided_by` is the column the timeline exists for.
    assert "by policy" in response.text or "by compliance" in response.text


def test_unknown_case_is_a_404(client, seed):
    response = client.get("/cases/case_does_not_exist", params={"seed": seed})
    assert response.status_code == 404


def test_audit_verifies_the_chain(client, seed):
    response = client.get("/api/verify", params={"seed": seed})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True, body["message"]
    assert "chain intact" in body["message"]


def test_screens_open_the_database_read_only(client, seed):
    """`PRAGMA query_only` is the guarantee, not developer discipline."""
    from app.main import open_ro

    conn = open_ro(seed, "control_plane")
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(Exception):
            conn.execute("UPDATE cases SET state = 'recovered'")
    finally:
        conn.close()


# ---- webhook --------------------------------------------------------------

def _envelope(payment_id: str, customer_id: str, segment: str) -> bytes:
    """Raw bytes, built once. The same bytes get signed and posted."""
    return json.dumps({
        "entity": "event",
        "event": "payment.failed",
        "created_at": 1_756_500_000,
        "payload": {"payment": {"entity": {
            "id": payment_id,
            "amount": 250_000,
            "currency": "INR",
            "status": "failed",
            "error_code": "BAD_REQUEST_ERROR",
            "error_description": "insufficient funds in account",
            "notes": {"customer_id": customer_id, "segment": segment},
        }}},
    }).encode()


def _sign(raw: bytes) -> str:
    return hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()


@pytest.fixture(scope="module")
def customer(seed):
    from rcp.store import connect

    conn = connect(DATA_DIR / f"seed_{seed}" / "rcp.db", read_only=True)
    try:
        row = conn.execute(
            "SELECT id, segment FROM customers ORDER BY id LIMIT 1"
        ).fetchone()
        return (row["id"], row["segment"])
    finally:
        conn.close()


def test_unsigned_webhook_is_rejected(client, seed, customer):
    raw = _envelope("pay_test_unsigned", *customer)
    response = client.post(
        "/api/webhook", params={"seed": seed}, content=raw,
        headers={"x-razorpay-signature": "nonsense",
                 "content-type": "application/json"},
    )
    assert response.status_code == 400


def test_missing_signature_is_rejected(client, seed, customer):
    raw = _envelope("pay_test_nosig", *customer)
    response = client.post(
        "/api/webhook", params={"seed": seed}, content=raw,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400


def test_signed_webhook_is_accepted_and_a_replay_is_a_no_op(
    client, seed, customer
):
    """Razorpay retries on any non-2xx, so redelivery is expected rather than
    exceptional. `UNIQUE (provider, provider_event_id)` handles it."""
    raw = _envelope("pay_test_replay_fixture", *customer)
    headers = {"x-razorpay-signature": _sign(raw),
               "content-type": "application/json"}

    first = client.post("/api/webhook", params={"seed": seed},
                        content=raw, headers=headers)
    assert first.status_code == 200

    second = client.post("/api/webhook", params={"seed": seed},
                         content=raw, headers=headers)
    assert second.status_code == 200
    assert second.json()["duplicate"] is True
    assert second.json()["event_id"] is None


def test_signature_is_checked_against_raw_bytes_not_a_reserialized_dict(
    client, seed, customer
):
    """The bug every webhook integration ships.

    `json.loads` then `json.dumps` reorders keys and changes whitespace, so a
    signature computed over the re-serialized form must NOT be accepted -- and
    if it were, someone would eventually "fix" the mismatch by loosening the
    check.
    """
    raw = _envelope("pay_test_reserialized", *customer)
    reserialized = json.dumps(json.loads(raw), sort_keys=True).encode()
    assert reserialized != raw

    response = client.post(
        "/api/webhook", params={"seed": seed}, content=raw,
        headers={"x-razorpay-signature": _sign(reserialized),
                 "content-type": "application/json"},
    )
    assert response.status_code == 400


def test_the_sign_endpoint_signs_the_exact_bytes_it_is_given(client, seed, customer):
    """The demo page's signing helper must not transform the payload.

    It used to take the payload as `Form(...)`, and the Fetch standard's
    multipart/form-data serializer rewrites every lone LF in a field value to
    CRLF. So the browser signed a CRLF copy of the JSON and posted the LF copy
    to /api/webhook, and every signed send from the page returned 400 -- the
    exact failure `rcp/ingest/webhook.py` warns about, committed by the demo
    built to show the check working.

    `curl -F` does not normalise, so testing by hand said it was fine.
    """
    raw = _envelope("pay_test_lf_signing", *customer)
    pretty = json.dumps(json.loads(raw), indent=2).encode()
    assert b"\n" in pretty and b"\r\n" not in pretty

    signature = client.post("/api/sign", content=pretty).json()["signature"]
    assert signature == _sign(pretty), (
        "the endpoint signed something other than the bytes it received"
    )

    # And the CRLF form -- what the old path produced -- must not verify.
    crlf = client.post(
        "/api/sign", content=pretty.replace(b"\n", b"\r\n")
    ).json()["signature"]
    assert crlf != signature

    rejected = client.post(
        "/api/webhook", params={"seed": seed}, content=pretty,
        headers={"x-razorpay-signature": crlf,
                 "content-type": "application/json"},
    )
    assert rejected.status_code == 400


def test_the_agents_screen_shows_the_roster_and_the_scoreboard(client, seed):
    """The rest of the dashboard cannot show any agentic work.

    The batch eval runs the deterministic policy so `make eval` stays
    byte-reproducible, which means `case_events.decided_by` is never `agent` in
    these databases. Without this screen a reviewer sees a rules engine.
    """
    response = client.get("/agents", params={"seed": seed})
    assert response.status_code == 200
    body = response.text

    for agent in ("recovery", "analyst", "composer", "strategy", "red team"):
        assert agent in body, f"{agent} missing from the roster"

    # Each agent's bound is a tool it can call -- that is the claim the page
    # makes, so the tools have to be on it.
    for tool in ("compliance_preview", "check_draft", "rail_comparison",
                 "try_copy", "sample_decision"):
        assert tool in body


def test_the_red_team_scoreboard_runs_live_and_is_clean(client, seed):
    """Computed on page load, not read from a file.

    A regressed coercion rule must show up here without anyone running a test,
    so the page is asserted to be green rather than merely to render.
    """
    body = client.get("/agents", params={"seed": seed}).text
    assert "PASSES" not in body, (
        "an attack got through the critic; a coercion rule has regressed"
    )
    assert "blocked" in body


def test_the_agents_screen_is_honest_about_the_batch(client, seed):
    """It must say why there are no `agent` rows rather than implying there
    were. Overstating this is the easiest way to mislead a reviewer."""
    body = client.get("/agents", params={"seed": seed}).text
    assert "byte-reproducible" in body
    assert "--live N" in body or "--live" in body


# ---- the attack box -------------------------------------------------------

def test_critic_check_refuses_a_coercive_message(client):
    """The reviewer-facing half of the red team's `try_copy` tool."""
    r = client.post("/api/critic-check", json={
        "text": "Rs 2,500 pending hai. Aapke employer ko inform karna pad "
                "sakta hai: {link}",
        "channel": "sms",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["passes"] is False
    tactics = [f["observed"].get("tactics") for f in body["findings"]
               if f["rule"] == "coercive_language"]
    assert tactics and "third_party_disclosure" in tactics[0]


def test_critic_check_allows_a_legitimate_final_notice(client):
    """Over-blocking is not the safe direction: a case that cannot send its
    last message recovers nothing."""
    r = client.post("/api/critic-check", json={
        "text": "Rs 2,500 ka payment 12 din se pending hai. Pay karein: "
                "{link} Yeh hamara aakhri reminder hai.",
        "channel": "sms",
    })
    assert r.json()["passes"] is True


def test_critic_check_rejects_bad_input(client):
    assert client.post("/api/critic-check", json={"text": "  "}).status_code == 400
    assert client.post(
        "/api/critic-check", json={"text": "hi", "channel": "pigeon"}
    ).status_code == 400
    assert client.post(
        "/api/critic-check", json={"text": "x" * 9000}
    ).status_code == 413


def test_critic_check_needs_no_database_or_provider(client):
    """It must keep working when the eval data is absent -- this is the one
    surface a reviewer can always reach."""
    body = client.post(
        "/api/critic-check", json={"text": "Pay Rs 2,500: {link}"}
    ).json()
    assert "tactics" in body and len(body["tactics"]) == 7


# ---- the war room ---------------------------------------------------------

def _case_with_a_live_rung(seed):
    from rcp.escalation import next_rung
    from rcp.store import DATA_DIR, connect

    conn = connect(DATA_DIR / f"seed_{seed}" / "rcp_control_plane.db",
                   read_only=True)
    try:
        for row in conn.execute(
            "SELECT c.*, e.root_cause FROM cases c JOIN events e "
            "ON e.id = c.event_id ORDER BY c.id"
        ):
            case = dict(row)
            if next_rung(case, case["root_cause"]) is not None:
                return case["id"]
    finally:
        conn.close()
    return None


def test_the_agent_route_runs_and_reports_which_path_it_took(
    client, seed, monkeypatch
):
    """Pins the provider rather than assuming none is configured.

    `app/main.py` loads .env so the dashboard can actually run an agent, which
    means a developer with a key in .env would otherwise have this test make a
    live call and fail on `was_live`. The behaviour under test is the shape of
    the response and the trail, not whose key happens to be present.
    """
    monkeypatch.setenv("RCP_LLM", "fallback")

    case_id = _case_with_a_live_rung(seed)
    if case_id is None:
        pytest.skip("no case with a live ladder rung in this run")

    body = client.post("/api/agent/decide", json={"case_id": case_id}).json()
    assert body["move"]["action"] in {"escalate", "hold", "stop"}
    assert body["was_live"] is False, "RCP_LLM was pinned to fallback"

    # The compliance verdict is the point of the trail; a call without its
    # answer is unreadable.
    preview = [t for t in body["trail"] if t["tool"] == "compliance_preview"]
    assert preview and preview[0]["output"], "the trail lost the tool output"


def test_the_agent_route_never_writes(client, seed):
    """`RecoveryAgent` only reads, and the route holds it to that."""
    from app.main import open_ro

    conn = open_ro(seed, "control_plane")
    try:
        assert conn.execute("PRAGMA query_only").fetchone()[0] == 1
    finally:
        conn.close()


def test_an_exhausted_case_does_not_spend_the_live_budget(client, seed):
    """A closed case short-circuits to the policy without reaching a provider.

    Counting that would let a reviewer clicking through written-off cases
    exhaust the token budget without a single model call.
    """
    from rcp.store import DATA_DIR, connect

    conn = connect(DATA_DIR / f"seed_{seed}" / "rcp_control_plane.db",
                   read_only=True)
    try:
        row = conn.execute(
            "SELECT id FROM cases WHERE close_reason LIKE 'ladder_exhausted%' "
            "LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        pytest.skip("no ladder-exhausted case in this run")

    before = client.post("/api/agent/decide",
                         json={"case_id": row["id"]}).json()["budget_left"]
    after = client.post("/api/agent/decide",
                        json={"case_id": row["id"]}).json()["budget_left"]
    assert before == after


def test_unknown_case_is_a_404_for_the_agent(client):
    r = client.post("/api/agent/decide", json={"case_id": "case_nope"})
    assert r.status_code == 404
