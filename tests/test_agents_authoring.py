"""The two authoring agents: composer and strategy.

Both follow ADR-007 -- a prompt *and* a deterministic routine over the same
tools -- and both write proposals to a review file rather than patching a
source file. These tests cover the deterministic half and the guard rails,
which are the parts that must hold with no provider configured.

The guard rails matter more than the drafting. An authoring agent that can
silently widen `RAIL_FIXABLE` starts sending silent retries on evidence nobody
checked, and one that can register a template bypasses a DLT approval that is a
legal requirement rather than a workflow step.
"""

from __future__ import annotations

import json

import pytest

from rcp.agents import composer, strategy
from rcp.llm.client import LLMClient
from rcp.llm.providers.fallback import FallbackAdapter


@pytest.fixture
def offline() -> LLMClient:
    """No provider. The deterministic routine is the only thing that runs."""
    return LLMClient(adapter=FallbackAdapter(), cache=None)


# ---- composer -------------------------------------------------------------

def test_coverage_gaps_names_what_each_gap_falls_back_to():
    gaps = composer.coverage_gaps()
    assert gaps, "the registry is not exhaustive; gaps are expected"
    for gap in gaps:
        assert gap["currently_falls_back_to"] != "nothing", (
            f"{gap} has no fallback; the backoff chain must always terminate "
            f"in something sendable"
        )


def test_check_draft_renders_before_judging():
    """A draft that fits empty can be oversized once its variables are filled."""
    verdict = composer.check_draft(
        "sms", "hinglish", "remind",
        "Rs {amount} pending hai. Pay karein: {link}",
    )
    assert verdict["acceptable"]
    assert "2,500" in verdict["rendered"]
    assert "{amount}" not in verdict["rendered"]
    assert verdict["sms"]["segments"] == 1


def test_check_draft_reports_the_encoding_penalty():
    """Devanagari bills at 70 characters per segment, Latin script at 160."""
    hinglish = composer.check_draft(
        "sms", "hinglish", "remind",
        "Rs {amount} ka payment pending hai, {days} din ho gaye. "
        "Kripya jaldi pay karein yahan se: {link}",
    )
    hindi = composer.check_draft(
        "sms", "hi", "remind",
        "Rs {amount} का भुगतान बाकी है, {days} दिन हो गए। "
        "कृपया जल्दी भुगतान करें यहाँ से: {link}",
    )
    assert hinglish["sms"]["encoding"] == "gsm7"
    assert hindi["sms"]["encoding"] == "ucs2"
    assert hindi["sms"]["segments"] > hinglish["sms"]["segments"]


def test_composer_drafts_nothing_without_a_provider(offline, tmp_path):
    """Which templates are missing is computable. What they say is not, and
    generating filler would put unreviewed copy into the review file."""
    accepted, result = composer.draft_templates(
        client=offline, out_dir=tmp_path
    )
    assert accepted == []
    assert result.provider == "fallback"
    assert "coverage gaps" in result.text
    assert not (tmp_path / "templates.json").exists()


def test_a_coercive_draft_cannot_be_submitted(offline, tmp_path):
    """The critic gates the authoring path, not just the send path."""
    verdict = composer.check_draft(
        "sms", "en", "final",
        "Pay Rs {amount} today or we will take legal action: {link}",
    )
    assert not verdict["acceptable"]
    assert any(f["rule"] == "coercive_language" for f in verdict["findings"])


# ---- strategy -------------------------------------------------------------

@pytest.fixture
def arm_db():
    """A completed eval arm to read outcomes from.

    Discovers the seed directly rather than importing `app.main` for its
    helper: these tests exercise the agent layer, and coupling them to the
    dashboard means a missing FastAPI turns eight agent tests into errors.
    That is exactly what happened -- the suite passed under an interpreter
    that had FastAPI and errored under the project's own venv.
    """
    from rcp.store import DATA_DIR, connect

    path = next(iter(sorted(DATA_DIR.glob("seed_*/rcp_baseline.db"))), None)
    if path is None:
        pytest.skip("no completed baseline arm; run `make data && make eval`")
    conn = connect(path, read_only=True)
    try:
        yield conn
    finally:
        conn.close()


def test_observed_rates_flag_thin_evidence(arm_db):
    rows = strategy.observed_rates(arm_db)
    assert rows
    for row in rows:
        assert row["thin"] == (row["n"] < strategy.MIN_TRIALS)
        assert 0.0 <= row["p"] <= 1.0


def test_rail_comparison_distinguishes_untested_from_bad(arm_db):
    """`retry_untested` is not the same as "retry does not work".

    Conflating them is how a table gets a cause added on no evidence, which is
    the direction that starts silent retries nobody checked.
    """
    rows = strategy.rail_comparison(arm_db)
    assert rows
    for row in rows:
        if row["retry_untested"]:
            assert row["retry_trials"] == 0
            assert row["retry_posterior"] is None


def test_deterministic_strategy_never_adds_to_rail_fixable(arm_db, offline, tmp_path):
    """The asymmetry is deliberate.

    Removing a cause makes the system send messages it already sends. Adding
    one starts silent retries on a cause nobody has tested, and arithmetic
    cannot tell an untested cause from a bad one.
    """
    revisions, result = strategy.revise_tables(
        arm_db, client=offline, out_dir=tmp_path
    )
    assert result.provider == "fallback"
    additions = [
        r for r in revisions
        if r["table"] == "RAIL_FIXABLE" and r["action"] == "add"
    ]
    assert additions == []


def test_every_revision_cites_its_evidence(arm_db, offline, tmp_path):
    revisions, _ = strategy.revise_tables(
        arm_db, client=offline, out_dir=tmp_path
    )
    for revision in revisions:
        assert revision["evidence"], revision
        assert "trials" in revision["evidence"] or "retries" in revision["evidence"]


def test_revisions_are_written_for_review_not_applied(arm_db, offline, tmp_path):
    from rcp.proposers import cart

    before = dict(cart.BASE_CLAIM), set(cart.RAIL_FIXABLE)
    revisions, _ = strategy.revise_tables(
        arm_db, client=offline, out_dir=tmp_path
    )
    assert (dict(cart.BASE_CLAIM), set(cart.RAIL_FIXABLE)) == before, (
        "a prior that rewrites itself from its own outcomes is a feedback "
        "loop, not a calibration"
    )
    if revisions:
        written = json.loads((tmp_path / "strategy.json").read_text())
        assert len(written["revisions"]) == len(revisions)


# ---- what a degraded run must not produce ---------------------------------

class _FailsAfterOneTurn:
    """An adapter that accepts submissions, then fails like a rate limit."""

    name = "flaky"
    model = "test"
    max_tokens = 1024

    def __init__(self, submit):
        self.submit = submit
        self.calls = 0

    def encode_tools(self, tools):
        return []

    def user_message(self, prompt):
        return {"role": "user", "content": prompt}

    def complete(self, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.submit()
        raise RuntimeError("429: rate limit reached")


def test_a_degraded_strategy_run_discards_partial_model_output(arm_db, tmp_path):
    """A provider that dies mid-run must not leave a blended review file.

    Observed live: Groq hit its daily token limit after the model had submitted
    seven revisions, the deterministic sweep then ran and appended twelve more,
    and `cart.BASE_CLAIM bank_downtime` appeared twice under two different
    justifications. A reviewer could not tell which procedure wrote which line.
    """
    captured: dict = {}

    def submit_a_revision():
        captured["tools"]["submit_revision"].fn(
            segment="cart", table="BASE_CLAIM", root_cause="auth_failed",
            action="set", value=0.99, evidence="from the model, 10 trials",
        )

    adapter = _FailsAfterOneTurn(submit_a_revision)
    client = LLMClient(adapter=adapter, cache=None)

    real_run = client._run_loop

    def spy(*, tools, **kwargs):
        captured["tools"] = {t.name: t for t in tools}
        return real_run(tools=tools, **kwargs)

    client._run_loop = spy

    revisions, result = strategy.revise_tables(
        arm_db, client=client, out_dir=tmp_path
    )

    assert result.degraded, "a provider failure must be recorded, not hidden"
    assert not any(r["value"] == 0.99 for r in revisions), (
        "the model's partial output must be discarded, not merged"
    )
    keys = [(r["segment"], r["table"], r["root_cause"]) for r in revisions]
    assert len(keys) == len(set(keys)), f"duplicate revisions: {keys}"


def test_rail_fixable_additions_are_checked_against_the_data(arm_db, offline, tmp_path):
    """The prompt asks for care; the tool enforces it.

    Observed live: the model proposed adding `auth_failed` at a 20.8% retry
    rate while removing `insufficient_funds` at 16.6% -- opposite conclusions
    from neighbouring numbers, each confidently justified. An add starts silent
    retries and nothing downstream damps it, so it is checked here.
    """
    captured: dict = {}

    def grab():
        captured["submit"] = captured["tools"]["submit_revision"].fn

    adapter = _FailsAfterOneTurn(grab)
    client = LLMClient(adapter=adapter, cache=None)
    real_run = client._run_loop

    def spy(*, tools, **kwargs):
        captured["tools"] = {t.name: t for t in tools}
        return real_run(tools=tools, **kwargs)

    client._run_loop = spy
    strategy.revise_tables(arm_db, client=client, out_dir=tmp_path)
    submit = captured["submit"]

    untested = submit(
        segment="cart", table="RAIL_FIXABLE", root_cause="invalid_account",
        action="add", evidence="looks fine to me",
    )
    assert untested["accepted"] is False
    assert "untested" in untested["reason"] or "below the" in untested["reason"]

    # Removal is the safe direction and stays available.
    removal = submit(
        segment="cart", table="RAIL_FIXABLE", root_cause="insufficient_funds",
        action="remove", evidence="145 retry trials",
    )
    assert removal["accepted"] is True


def test_an_addition_needs_both_sides_of_the_comparison(arm_db, tmp_path):
    """An absent comparison must read as "cannot tell", not "no objection".

    `escalation._viable` skips the retry rung for causes retry cannot fix
    (ADR-008), so a cause receives retries or messages, almost never both.
    Measured on the baseline arm: every rail-fixable cause has zero message
    trials. A guard written as `if message_p is not None and retry_p < message_p`
    therefore never fires, and a live run had the model adding `limit_exceeded`
    to RAIL_FIXABLE on a 9.5% retry success rate.
    """
    captured: dict = {}

    def grab():
        captured["submit"] = captured["tools"]["submit_revision"].fn

    adapter = _FailsAfterOneTurn(grab)
    client = LLMClient(adapter=adapter, cache=None)
    real_run = client._run_loop

    def spy(*, tools, **kwargs):
        captured["tools"] = {t.name: t for t in tools}
        return real_run(tools=tools, **kwargs)

    client._run_loop = spy
    strategy.revise_tables(arm_db, client=client, out_dir=tmp_path)
    submit = captured["submit"]

    # Confirms the premise rather than assuming it: if messaging data ever
    # appears for these causes, this test should be revisited, not deleted.
    rows = {r["root_cause"]: r for r in strategy.rail_comparison(arm_db, "cart")}
    one_sided = [
        cause for cause, r in rows.items()
        if r["retry_trials"] >= strategy.MIN_TRIALS and r["message_posterior"] is None
    ]
    if not one_sided:
        pytest.skip("messaging data now exists for every retried cause")

    for cause in one_sided:
        verdict = submit(
            segment="cart", table="RAIL_FIXABLE", root_cause=cause,
            action="add", evidence=f"{rows[cause]['retry_trials']} retry trials",
        )
        assert verdict["accepted"] is False, (
            f"{cause} was added with no messaging side to compare against"
        )
        assert "messaging" in verdict["reason"]
