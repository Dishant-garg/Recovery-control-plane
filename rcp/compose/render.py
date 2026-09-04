"""Selecting and filling a template. Deterministic, offline, no model call.

This is the executing half of ADR-007. A model may author a template and a
human registers it with the gateway; this code decides which registered
template applies to one action and fills its variables. Nothing here can invent
a sentence, which is what makes the output safe to send without a review step
in the loop.

`{link}` is deliberately left unfilled. The payment link does not exist at
compose time -- the executor creates it against the provider (see
`rcp/execute/razorpay_rest.py`) and substitutes it at send time. The critic
knows this and allows exactly that one placeholder to survive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from rcp.compose.templates import (
    FINAL,
    NOTIFY,
    PROMISE_ASK,
    REMIND,
    REGISTRY,
    STOP_FOOTER,
    Template,
)
from rcp.timeutil import MS_PER_DAY, to_utc

# Filled by the executor, not here. See the module docstring.
EXECUTOR_FILLED = frozenset({"link"})

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True)
class Message:
    """What goes into `actions.body.message`.

    `template_id` is the audit-relevant field: it ties a delivered message back
    to a registered, approved template, which is what a compliance reviewer
    actually needs. The rendered text is a convenience.
    """

    template_id: str
    channel: str
    language: str
    purpose: str
    text: str
    variables: dict[str, str] = field(default_factory=dict)
    # The template's own declaration that it names a discount. The critic
    # cross-checks it against the approved incentive, and independently
    # sniffs the text -- so a drafted template that offers money without
    # declaring it is caught too.
    mentions_incentive: bool = False

    def to_body(self) -> dict[str, Any]:
        return {
            "template_id": self.template_id,
            "language": self.language,
            "purpose": self.purpose,
            "text": self.text,
            "variables": dict(sorted(self.variables.items())),
        }


def purpose_for(
    *, attempts: int, is_last_rung: bool, asks_for_promise: bool
) -> str:
    """What this contact is for, derived from case state.

    Order matters. A promise ask is a promise ask even on the last rung -- it
    is the one contact that can still change the outcome after the ladder runs
    out, so it outranks `final`.
    """
    if asks_for_promise:
        return PROMISE_ASK
    if attempts == 0:
        return NOTIFY
    if is_last_rung:
        return FINAL
    return REMIND


def _candidates(
    channel: str, language: str, purpose: str, root_cause: str | None,
    *, has_incentive: bool,
) -> list[Template]:
    """The backoff chain, narrowest first.

    Same shape as the precedent lookup in ADR-006, and for the same reason:
    a specific match is better, a general one is acceptable, and silence is
    not. A missing Hindi template falls back to English -- sending the wrong
    language is recoverable, sending nothing is not.

    `has_incentive` is a hard filter, not a preference. A template that names a
    discount is unsendable when no discount was approved: it would render as
    "Rs 0 off" and promise something the arbiter never agreed to. The critic
    catches that as `incentive_mismatch`, but the selection should never
    produce it in the first place.
    """
    def pick(lang: str, purp: str, cause_specific: bool) -> list[Template]:
        out = []
        for t in REGISTRY:
            if (t.channel, t.language, t.purpose) != (channel, lang, purp):
                continue
            if t.mentions_incentive and not has_incentive:
                continue
            if cause_specific:
                if t.root_causes and root_cause in t.root_causes:
                    out.append(t)
            elif t.root_causes is None:
                out.append(t)
        # With an incentive approved, spend it: a discount that is offered
        # recovers more than one that sits unused in the payload.
        return sorted(out, key=lambda t: (not t.mentions_incentive, t.id))

    chain: list[Template] = []
    for lang in (language, "en"):
        for purp in (purpose, REMIND):
            chain += pick(lang, purp, True)
            chain += pick(lang, purp, False)
    return chain


def _fill(text: str, values: dict[str, str]) -> str:
    for key, value in sorted(values.items()):
        text = text.replace("{" + key + "}", value)
    return text


def compose(
    *,
    channel: str,
    language: str,
    root_cause: str | None = None,
    amount_paise: int = 0,
    incentive_paise: int = 0,
    attempts: int = 0,
    is_last_rung: bool = False,
    asks_for_promise: bool = False,
    now_ms: int = 0,
    opened_at_ms: int | None = None,
) -> Message | None:
    """Render the message for one action, or None if the channel carries none.

    A `retry` is a silent attempt on the rail. The customer sees nothing, so
    there is nothing to write -- returning an empty string instead would put a
    meaningless template id in the audit trail.
    """
    if channel == "retry":
        return None

    purpose = purpose_for(
        attempts=attempts, is_last_rung=is_last_rung,
        asks_for_promise=asks_for_promise,
    )
    chain = _candidates(channel, language, purpose, root_cause,
                        has_incentive=incentive_paise > 0)
    if not chain:
        return None
    template = chain[0]

    days = (
        max(0, (now_ms - int(opened_at_ms)) // MS_PER_DAY)
        if opened_at_ms is not None else 0
    )
    due = to_utc(now_ms + 3 * MS_PER_DAY)
    values = {
        "amount": f"{amount_paise // 100:,}",
        "discount": f"{incentive_paise // 100:,}",
        "days": str(days),
        "due": f"{due.day} {MONTHS[due.month - 1]}",
    }

    text = _fill(template.text, values)

    # A discount makes the message promotional rather than transactional, and
    # promotional contact needs an opt-out path. Appended here rather than
    # written into every template so the rule is in one place and the critic
    # can check it independently.
    if template.mentions_incentive and channel in ("sms", "whatsapp"):
        footer = STOP_FOOTER.get(template.language, STOP_FOOTER["en"])
        text = f"{text} {footer}" if channel == "sms" else f"{text}\n\n{footer}"

    return Message(
        template_id=template.id, channel=channel, language=template.language,
        purpose=purpose, text=text, variables=values,
        mentions_incentive=template.mentions_incentive,
    )
