"""The critic: what must be true of a message before it can be sent.

Deterministic checks over a rendered `Message`. Same shape as everything else
that refuses in this system -- a rule id, a written note, and the observed
value (`compliance.rules.Deny`, `escalation.Stop`, `store.Suppressed`).

This is the gate on the authoring path. A model may draft a template
(`rcp/agents/composer.py`); nothing it drafts reaches the registry until these
checks pass, and they run again at render time so a template that was fine in
isolation cannot become oversized once its variables are filled.

## The SMS segment check is a money check

GSM-7 packs 160 characters into one SMS segment. Devanagari is not in GSM-7, so
a message containing it encodes as UCS-2 and a segment holds **70**. Gateways
bill per segment, so the same sentence costs a little over twice as much in
हिन्दी as in Hinglish.

That is the actual reason Hinglish matters here, and it is checkable rather
than asserted: `sms_segments` reports what each rendering will be billed for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from rcp.compose.render import EXECUTOR_FILLED, Message

# GSM 03.38 basic character set. Anything outside this forces UCS-2.
GSM7_BASIC = set(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
# These exist in GSM-7 but cost two characters each.
GSM7_EXTENDED = set("^{}\\[~]|€")

SEGMENT_LIMITS = {"gsm7": (160, 153), "ucs2": (70, 67)}

# Per-channel ceilings on the rendered message.
#
# `voice` is a spoken script: roughly 150 words a minute, so 600 characters is
# about 40 seconds, which is as long as an automated call can run before people
# hang up. `sms` is capped at four segments -- past that a long-form channel is
# cheaper and reads better.
MAX_CHARS = {"sms": 640, "whatsapp": 1024, "email": 4000, "voice": 600}

# Language that turns collection into intimidation. Not a stylistic preference:
# RBI's recovery-agent guidance and the consumer-protection rules both prohibit
# threats of consequences the sender has no standing to deliver, and a recovery
# flow that sends these is a legal problem regardless of what it recovers.
#
# **Tactics, not a word list.** This was sixteen banned phrases, and
# `rcp/agents/redteam.py` put seven of seven attacks straight through it -- a
# CIBIL threat, a field visit, telling an employer, "other remedies", a shouted
# FINAL WARNING. Every one describes prohibited conduct; not one contained a
# listed word. Abuse is a set of tactics and a blacklist is a set of words, and
# the gap between those is where the copy lives.
#
# Grouped by tactic so a refusal names the conduct rather than the wording,
# which is what a compliance reviewer actually needs to see.
#
# **This is still lexical, and lexical checks get paraphrased around.** The live
# red team went straight past the rewritten rules with "hurt your borrowing
# score and future loan approvals" -- the same bureau threat with none of the
# matched terms. That pattern is closed now, and the next paraphrase is not.
#
# So this filter is ADR-005's layer 1, not the guarantee. The guarantee is that
# no template reaches a customer without a human registering it with DLT and
# with WhatsApp (`rcp/compose/templates.py`). The critic's job is to raise the
# floor so review catches less; it is not the last line, and treating it as one
# is how a paraphrase becomes a sent message.
COERCION_PATTERNS: dict[str, str] = {
    "credit_threat":
        r"cibil|credit\s*(score|bureau|report|worthiness)|creditworthi"
        r"|bureau\s*(ko|me|mein)?\s*report|defaulter|credit\s*kharab"
        # Added after a live red-team run paraphrased straight past the line
        # above: "hurt your borrowing score and future loan approvals". Same
        # threat, none of the words.
        r"|borrowing\s*score|loan\s*(approval|eligib|milna)|future\s*loans",
    "third_party_disclosure":
        r"(employer|boss|company|family|parivaar|ghar\s*wal|padosi|neighbou?r"
        r"|rishtedar|reference)\w*\s*(ko|to)?\s*"
        r"(inform|bata|batay|contact|report|call|tell)",
    "physical_visit":
        r"(field\s*team|recovery\s*agent|hamare\s*log|our\s*team|agent)\w*"
        r"[^.]{0,40}(visit|aayeng|aayega|aajayeng|pahunch|ghar)"
        r"|ghar\s*(par|pe)?\s*(aayeng|aayega|visit)|home\s*visit"
        r"|address\s*(par|pe)[^.]{0,20}(visit|aayeng)",
    "veiled_legal":
        r"legal\s*action|kanooni|court|police|arrest|jail|warrant|criminal"
        r"|seiz|other\s*remed|further\s*steps|forced\s*to\s*(act|take)"
        r"|appropriate\s*action|karyawahi|notice\s*under\s*section",
    "false_urgency":
        r"final\s*warning|last\s*warning|aakhri\s*chetav|warna"
        r"|or\s*else|consequences|serious\s*action",
    "account_penalty":
        r"blacklist|account\s*(band|block|close|closed|suspend|terminate)"
        r"|permanently\s*band|service\s*band",
    "impersonation":
        r"\b(advocate|lawyer|law\s*firm|legal\s*department|on\s*behalf\s*of"
        r"\s*the\s*court)\b",
}

# Kept as a flat view for anything that wants to show a reviewer what is
# refused. `rcp/agents/redteam.py` reads it when briefing the model.
COERCIVE = tuple(sorted(COERCION_PATTERNS))

PLACEHOLDER = re.compile(r"\{([a-z_]+)\}")


@dataclass(frozen=True)
class Finding:
    rule: str
    severity: str          # "error" blocks the send; "warn" is reported only
    note: str
    observed: dict[str, Any] = field(default_factory=dict)


def encoding_of(text: str) -> str:
    return "gsm7" if all(
        ch in GSM7_BASIC or ch in GSM7_EXTENDED for ch in text
    ) else "ucs2"


def sms_segments(text: str) -> tuple[int, str, int]:
    """How many SMS segments this text bills for.

    Returns (segments, encoding, billable_length). The billable length is not
    `len(text)`: GSM-7 extension characters occupy two positions each, which is
    how a message that looks like 158 characters silently becomes two segments.
    """
    encoding = encoding_of(text)
    if encoding == "gsm7":
        length = sum(2 if ch in GSM7_EXTENDED else 1 for ch in text)
    else:
        length = len(text)

    single, multi = SEGMENT_LIMITS[encoding]
    if length <= single:
        return (1 if length else 0), encoding, length
    return -(-length // multi), encoding, length


def check(message: Message, *, incentive_paise: int = 0) -> list[Finding]:
    """Every objection to sending this message, worst first.

    An empty list means it is safe to send. Callers should treat any `error`
    as blocking -- `compose_for_action` does.
    """
    findings: list[Finding] = []
    text = message.text

    leftover = sorted(set(PLACEHOLDER.findall(text)) - EXECUTOR_FILLED)
    if leftover:
        findings.append(Finding(
            "unfilled_placeholder", "error",
            f"template variables were never filled: {', '.join(leftover)}",
            {"placeholders": leftover},
        ))

    limit = MAX_CHARS.get(message.channel)
    if limit is not None and len(text) > limit:
        findings.append(Finding(
            "too_long", "error",
            f"{len(text)} characters exceeds the {message.channel} limit "
            f"of {limit}",
            {"length": len(text), "limit": limit},
        ))

    lowered = text.lower()
    hits = {
        tactic: match.group(0)
        for tactic, pattern in COERCION_PATTERNS.items()
        if (match := re.search(pattern, lowered))
    }
    if hits:
        findings.append(Finding(
            "coercive_language", "error",
            f"uses prohibited collection tactics: "
            f"{', '.join(f'{t} ({q!r})' for t, q in sorted(hits.items()))}",
            {"tactics": sorted(hits)},
        ))

    # Copy and money must agree. A template promising a discount that the
    # arbiter never approved is a commitment the system did not make.
    # The declared flag is authoritative; the regex catches a drafted template
    # that offers money without declaring it.
    names_discount = message.mentions_incentive or bool(
        re.search(r"\b(?:off|chhoot|discount)\b|छूट", lowered)
    )
    if names_discount and incentive_paise <= 0:
        findings.append(Finding(
            "incentive_mismatch", "error",
            "the copy offers a discount but no incentive was approved",
            {"incentive_paise": incentive_paise},
        ))
    if names_discount and incentive_paise > 0:
        stated = message.variables.get("discount", "")
        if stated.replace(",", "") != str(incentive_paise // 100):
            findings.append(Finding(
                "incentive_mismatch", "error",
                f"copy states Rs {stated} but the approved incentive is "
                f"Rs {incentive_paise // 100}",
                {"stated": stated, "approved_paise": incentive_paise},
            ))

    if message.channel in ("sms", "whatsapp", "email") and "{link}" not in text:
        findings.append(Finding(
            "no_call_to_action", "warn",
            f"a {message.channel} message with no payment link asks the "
            f"customer to do something without saying how",
        ))

    if message.channel == "voice" and ("{link}" in text or "http" in lowered):
        findings.append(Finding(
            "url_in_voice", "error",
            "a spoken script cannot contain a URL; nobody writes one down",
        ))

    if names_discount and message.channel in ("sms", "whatsapp"):
        if "stop" not in lowered:
            findings.append(Finding(
                "missing_opt_out", "error",
                "a promotional message needs an opt-out path",
            ))

    if message.channel == "sms":
        segments, encoding, length = sms_segments(text)
        if segments > 2:
            findings.append(Finding(
                "expensive_encoding", "warn",
                f"{segments} SMS segments ({encoding}, {length} billable "
                f"characters); Hinglish in Latin script would bill fewer",
                {"segments": segments, "encoding": encoding},
            ))

    return sorted(findings, key=lambda f: (f.severity != "error", f.rule))


def blocking(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if f.severity == "error"]
