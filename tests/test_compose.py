"""Composer, critic, and registry tests.

The sweep at the top is the important one: it renders every registered template
against every input shape that can reach it and asserts the critic passes. A
template that only breaks on a Rs 0 incentive, or only in Devanagari, or only
on the last rung, is exactly the kind of defect that reaches a customer rather
than a test.
"""

from __future__ import annotations

import pytest

from rcp.compose import (
    REGISTRY,
    blocking,
    check,
    compose,
    purpose_for,
    registered_ids,
    sms_segments,
)
from rcp.compose.critic import Finding, encoding_of
from rcp.compose.render import Message
from rcp.compose.templates import CHANNELS, FINAL, NOTIFY, PROMISE_ASK, REMIND
from rcp.schema import Language, RootCause

NOW = 1_700_000_000_000

# (attempts, is_last_rung, asks_for_promise) -> each purpose is reachable
CASE_SHAPES = [(0, False, False), (2, False, False),
               (3, True, False), (1, False, True)]


def _compose(channel, language, root_cause, incentive, shape):
    attempts, last, promise = shape
    return compose(
        channel=channel, language=language, root_cause=root_cause,
        amount_paise=250_000, incentive_paise=incentive, attempts=attempts,
        is_last_rung=last, asks_for_promise=promise, now_ms=NOW,
        opened_at_ms=NOW - 12 * 86_400_000,
    )


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize("language", [m.value for m in Language])
@pytest.mark.parametrize("shape", CASE_SHAPES)
@pytest.mark.parametrize("incentive", [0, 5_000])
def test_every_reachable_rendering_passes_the_critic(
    channel, language, shape, incentive
):
    for cause in [m.value for m in RootCause]:
        message = _compose(channel, language, cause, incentive, shape)
        assert message is not None, (
            f"no template for {channel}/{language}/{shape}; the backoff chain "
            f"must always terminate in something sendable"
        )
        assert not blocking(check(message, incentive_paise=incentive))


def test_template_ids_are_unique():
    ids = [t.id for t in REGISTRY]
    assert len(ids) == len(set(ids)), "a duplicate id would shadow a template"
    assert registered_ids() == sorted(ids)


def test_retry_has_no_copy():
    """A silent rail attempt the customer never sees must not get a template
    id -- that would put a message in the audit trail that was never sent."""
    assert _compose("retry", "hinglish", "insufficient_funds", 0, CASE_SHAPES[0]) is None


def test_incentive_templates_are_unreachable_without_an_incentive():
    """The selection filter, not just the critic, has to hold this.

    A discount template rendered with no approved incentive says "Rs 0 off" and
    promises something the arbiter never agreed to.
    """
    for channel in ("sms", "whatsapp"):
        for shape in CASE_SHAPES:
            message = _compose(channel, "hinglish", "insufficient_funds", 0, shape)
            assert message is not None
            assert not message.mentions_incentive


def test_incentive_is_spent_when_one_was_approved():
    message = _compose("whatsapp", "hinglish", "insufficient_funds",
                       5_000, (3, True, False))
    assert message.mentions_incentive
    assert "50" in message.text          # Rs 50, from 5000 paise


def test_promotional_message_carries_an_opt_out():
    message = _compose("sms", "hinglish", "insufficient_funds",
                       5_000, (1, False, True))
    assert message.mentions_incentive
    assert "STOP" in message.text


def test_root_cause_specific_template_wins():
    """An expired card needs a different ask from a short balance."""
    expired = _compose("sms", "hinglish", "card_expired", 0, CASE_SHAPES[0])
    funds = _compose("sms", "hinglish", "insufficient_funds", 0, CASE_SHAPES[0])
    assert expired.template_id.endswith(".card_expired")
    assert funds.template_id.endswith(".insufficient_funds")
    assert expired.text != funds.text


def test_missing_language_falls_back_to_english():
    """Sending the wrong language is recoverable; sending nothing is not.

    There is no Hindi voice template, so the chain must widen to English rather
    than leaving the case uncontacted.
    """
    message = _compose("voice", "hi", "insufficient_funds", 0, (2, False, False))
    assert message is not None
    assert message.language == "en"


def test_purpose_order_puts_promise_above_final():
    """A promise ask is the one contact that can still change the outcome
    after the ladder runs out, so it outranks `final`."""
    assert purpose_for(attempts=3, is_last_rung=True, asks_for_promise=True) == PROMISE_ASK
    assert purpose_for(attempts=3, is_last_rung=True, asks_for_promise=False) == FINAL
    assert purpose_for(attempts=0, is_last_rung=False, asks_for_promise=False) == NOTIFY
    assert purpose_for(attempts=2, is_last_rung=False, asks_for_promise=False) == REMIND


def test_composition_is_deterministic():
    args = ("whatsapp", "hinglish", "insufficient_funds", 5_000, (3, True, False))
    assert _compose(*args) == _compose(*args)


# ---- the critic -----------------------------------------------------------

def _msg(text, channel="sms", **kw):
    return Message(template_id="t", channel=channel, language="en",
                   purpose=REMIND, text=text, **kw)


def _rules(findings: list[Finding]) -> set[str]:
    return {f.rule for f in findings}


def test_critic_catches_unfilled_placeholder():
    findings = check(_msg("Pay Rs {amount} here: {link}"))
    assert "unfilled_placeholder" in _rules(blocking(findings))


def test_critic_allows_the_executor_filled_link():
    """`{link}` survives on purpose -- the payment link does not exist until
    the executor creates it against the provider."""
    assert "unfilled_placeholder" not in _rules(check(_msg("Pay here: {link}")))


def test_critic_catches_coercive_language():
    findings = check(_msg("Pay Rs 500 or we will take legal action: {link}"))
    assert "coercive_language" in _rules(blocking(findings))
    findings = check(_msg("Pay karein warna kanooni karyawahi hogi: {link}"))
    assert "coercive_language" in _rules(blocking(findings))


def test_critic_catches_a_discount_nobody_approved():
    findings = check(_msg("Rs 50 off today: {link}"), incentive_paise=0)
    assert "incentive_mismatch" in _rules(blocking(findings))


def test_critic_catches_a_discount_that_does_not_match():
    message = _msg("Rs 90 off today: {link} Reply STOP to opt out.",
                   variables={"discount": "90"}, mentions_incentive=True)
    findings = check(message, incentive_paise=5_000)   # Rs 50, not Rs 90
    assert "incentive_mismatch" in _rules(blocking(findings))


def test_critic_rejects_a_url_in_a_voice_script():
    findings = check(_msg("Visit {link} to pay", channel="voice"))
    assert "url_in_voice" in _rules(blocking(findings))


def test_critic_rejects_an_oversized_message():
    findings = check(_msg("x" * 5_000 + " {link}", channel="whatsapp"))
    assert "too_long" in _rules(blocking(findings))


def test_critic_warns_without_a_call_to_action():
    findings = check(_msg("Your payment did not go through."))
    assert "no_call_to_action" in _rules(findings)
    assert not blocking(findings)     # a warning, not a refusal


# ---- SMS segments: the money check ----------------------------------------

def test_devanagari_forces_ucs2_and_costs_more_per_character():
    """The economic argument for Hinglish, as an assertion rather than a claim.

    GSM-7 holds 160 characters per segment; Devanagari is outside it, so the
    message encodes as UCS-2 and a segment holds 70. Gateways bill per segment.
    """
    hinglish = "Aapka Rs 2,500 ka payment complete nahi hua. Abhi pay karein."
    hindi = "आपका Rs 2,500 का भुगतान पूरा नहीं हुआ। अभी भुगतान करें।"

    assert encoding_of(hinglish) == "gsm7"
    assert encoding_of(hindi) == "ucs2"

    # The Hindi text is the shorter string and still bills for more segments
    # once repeated to a realistic reminder length.
    assert len(hindi) < len(hinglish)
    assert sms_segments(hinglish * 2)[0] < sms_segments(hindi * 2)[0]


def test_gsm7_extension_characters_cost_two():
    """A message that looks like 160 characters can still be two segments."""
    assert sms_segments("a" * 160)[0] == 1
    assert sms_segments("a" * 159 + "[")[0] == 2      # '[' occupies two


def test_empty_text_bills_nothing():
    assert sms_segments("")[0] == 0


# ---- the spoken templates -------------------------------------------------

VOICE_TEMPLATES = [t for t in REGISTRY if t.channel == "voice"]


@pytest.mark.parametrize("template", VOICE_TEMPLATES, ids=lambda t: t.id)
def test_every_voice_script_is_sayable(template):
    """A spoken script is a different artifact from a written one.

    `make voice` renders these to audio with macOS `say`, and the audio is
    committed. A script that the critic blocks would be generated into a file
    nobody notices is wrong, so the same check runs here.
    """
    text = (template.text.replace("{amount}", "2,500")
                         .replace("{days}", "12")
                         .replace("{discount}", "50"))
    findings = check(Message(
        template_id=template.id, channel="voice", language=template.language,
        purpose=template.purpose, text=text,
    ))
    assert not blocking(findings), [f.rule for f in blocking(findings)]

    # Roughly 150 words a minute: 600 characters is about 40 seconds, which is
    # as long as an automated call runs before people hang up.
    assert len(text) <= 600
    assert "{link}" not in text, "nobody writes down a URL from a phone call"


def test_hinglish_voice_exists_for_every_purpose_english_has():
    """The brief names Hinglish voice recovery. English coverage without
    Hinglish coverage would make the fallback chain quietly do the work."""
    hinglish = {t.purpose for t in VOICE_TEMPLATES if t.language == "hinglish"}
    english = {t.purpose for t in VOICE_TEMPLATES if t.language == "en"}
    assert english <= hinglish, f"missing Hinglish voice for {english - hinglish}"
