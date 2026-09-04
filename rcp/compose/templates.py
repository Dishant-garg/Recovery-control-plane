"""The message template registry.

**Templates, not free text, and that is a regulatory fact rather than a design
preference.** WhatsApp Business will not deliver an unregistered
business-initiated message, and Indian SMS goes through TRAI's DLT registry
where the template is approved *before* it can be sent. A composer that writes
novel prose per customer produces messages no gateway in this market will
accept.

So the composer picks a registered template and fills its variables. That is
also exactly ADR-007's shape: the LLM may *author* a template, which a human
registers with the gateway; deterministic code selects and renders it at send
time. The generated artifact is reviewable before it can ever reach a customer.

## Why Hinglish is an economic decision, not a cosmetic one

An SMS segment holds 160 characters in GSM-7. Devanagari is not in GSM-7, so a
message containing it encodes as UCS-2 and a segment holds **70**. The same
sentence therefore costs roughly twice as much to send in हिन्दी as in Hinglish,
and Hinglish is what a large share of Indian users actually write in.

`critic.sms_segments` computes this, and `critic.check` reports the segment
count, so the cost difference is visible at compose time rather than on the
gateway invoice.

## Selection

Templates are chosen by a backoff chain, deliberately the same shape as the
precedent lookup in ADR-006: try the most specific key, widen until something
matches. A missing Hindi template falls back to English rather than failing --
sending the wrong language is recoverable, sending nothing is not.
"""

from __future__ import annotations

from dataclasses import dataclass

# What the message is trying to do. Derived from case state in render.py, never
# chosen by a caller directly.
NOTIFY = "notify"            # first contact on this case
REMIND = "remind"            # a middle rung
FINAL = "final"              # the last rung before write-off
PROMISE_ASK = "promise_ask"  # asking for a commitment to a date

PURPOSES = (NOTIFY, REMIND, FINAL, PROMISE_ASK)

# `retry` is a silent rail attempt and carries no copy at all.
CHANNELS = ("sms", "whatsapp", "email", "voice")


@dataclass(frozen=True)
class Template:
    """One registered template.

    `id` is what a DLT or WhatsApp registration maps to, so it is stable and
    structured: <channel>.<language>.<purpose>[.<root_cause>].

    `root_causes` narrows a template to causes where the *ask* genuinely
    differs. An expired card needs the customer to update it; insufficient funds
    needs them to fund the account and wait. Telling someone to "update your
    card" when their balance was short is worse than a generic message.
    """

    id: str
    channel: str
    language: str
    purpose: str
    text: str
    root_causes: frozenset[str] | None = None
    # Set when the copy names a discount. The critic cross-checks this against
    # the action's actual incentive, so copy and money cannot drift apart.
    mentions_incentive: bool = False


def _t(
    channel: str, language: str, purpose: str, text: str, *,
    root_causes: tuple[str, ...] | None = None,
    incentive: bool = False,
) -> Template:
    suffix = f".{sorted(root_causes)[0]}" if root_causes else ""
    # Incentive variants are separate registrations. They have to be: the
    # discount changes what the message promises, and a gateway approves the
    # promise, not the intent.
    suffix += ".incentive" if incentive else ""
    return Template(
        id=f"{channel}.{language}.{purpose}{suffix}",
        channel=channel, language=language, purpose=purpose, text=text,
        root_causes=frozenset(root_causes) if root_causes else None,
        mentions_incentive=incentive,
    )


# Variables available to every template. Deliberately not a customer name:
# `customers` holds no name and no phone number, because the decision path has
# no use for either. The executor merges contact details at send time, so PII
# never enters the tables the audit log mirrors.
#
#   {amount}    rupees owed, formatted with thousands separators
#   {discount}  rupees off, when an incentive was approved
#   {due}       a date, "14 Sep"
#   {link}      payment link, substituted by the executor
#   {days}      age of the case in days
VARIABLES = ("amount", "discount", "due", "link", "days")

# The STOP footer. Required by the critic only when the message carries an
# incentive -- a discount makes it promotional, and promotional messages need an
# opt-out path. A pure payment reminder is transactional and does not.
STOP_FOOTER = {
    "en": "Reply STOP to opt out.",
    "hi": "बंद करने के लिए STOP भेजें।",
    "hinglish": "Band karne ke liye STOP bhejein.",
}


REGISTRY: tuple[Template, ...] = (
    # ---- sms · hinglish ---------------------------------------------------
    _t("sms", "hinglish", NOTIFY,
       "Aapka Rs {amount} ka payment complete nahi hua. "
       "Abhi pay karein: {link}"),
    _t("sms", "hinglish", NOTIFY,
       "Aapka Rs {amount} ka payment nahi hua - card expire ho gaya hai. "
       "Naya card add karein: {link}",
       root_causes=("card_expired", "mandate_expired")),
    _t("sms", "hinglish", NOTIFY,
       "Rs {amount} ka payment balance kam hone se nahi hua. "
       "Account fund karein, hum dobara try karenge: {link}",
       root_causes=("insufficient_funds",)),
    _t("sms", "hinglish", REMIND,
       "Reminder: Rs {amount} abhi pending hai. 2 minute mein pay karein: {link}"),
    _t("sms", "hinglish", FINAL,
       "Last reminder: Rs {amount} {days} din se pending hai. "
       "Aaj pay karein: {link}"),
    _t("sms", "hinglish", PROMISE_ASK,
       "Rs {amount} pending hai. Kab tak pay kar payenge? "
       "Reply karein ya abhi pay karein: {link}"),
    _t("sms", "hinglish", PROMISE_ASK,
       "Rs {amount} pending hai. Rs {discount} ki chhoot aaj tak. "
       "Pay karein: {link}", incentive=True),

    # ---- sms · en ---------------------------------------------------------
    _t("sms", "en", NOTIFY,
       "Your payment of Rs {amount} did not go through. Pay now: {link}"),
    _t("sms", "en", NOTIFY,
       "Your payment of Rs {amount} failed because your card has expired. "
       "Add a new card: {link}",
       root_causes=("card_expired", "mandate_expired")),
    _t("sms", "en", REMIND,
       "Reminder: Rs {amount} is still pending. Pay in 2 minutes: {link}"),
    _t("sms", "en", FINAL,
       "Final reminder: Rs {amount} has been pending {days} days. "
       "Pay today: {link}"),
    _t("sms", "en", PROMISE_ASK,
       "Rs {amount} is pending. When can you pay? Reply with a date, "
       "or pay now: {link}"),
    _t("sms", "en", PROMISE_ASK,
       "Rs {amount} is pending. Rs {discount} off if you pay today: {link}",
       incentive=True),

    # ---- sms · hi ---------------------------------------------------------
    _t("sms", "hi", NOTIFY,
       "आपका Rs {amount} का भुगतान पूरा नहीं हुआ। अभी भुगतान करें: {link}"),
    _t("sms", "hi", REMIND,
       "याद दिलाना: Rs {amount} बाकी है। अभी भुगतान करें: {link}"),

    # ---- whatsapp · hinglish ----------------------------------------------
    _t("whatsapp", "hinglish", NOTIFY,
       "Namaste! Aapka Rs {amount} ka payment complete nahi ho paya.\n\n"
       "Aap yahan se 2 minute mein pay kar sakte hain: {link}\n\n"
       "Koi dikkat ho to is message ka reply karein."),
    _t("whatsapp", "hinglish", REMIND,
       "Namaste! Rs {amount} ka payment abhi tak pending hai ({days} din).\n\n"
       "Pay karein: {link}\n\n"
       "Agar aapne pay kar diya hai to is message ko ignore karein."),
    _t("whatsapp", "hinglish", PROMISE_ASK,
       "Namaste! Rs {amount} ka payment pending hai.\n\n"
       "Aap kab tak pay kar payenge? Reply mein date bata dein, hum tab tak "
       "wait kar lenge.\n\n"
       "Ya abhi pay karein: {link}"),
    _t("whatsapp", "hinglish", FINAL,
       "Rs {amount} ka payment {days} din se pending hai.\n\n"
       "Pay karein: {link}\n\n"
       "Yeh hamara aakhri reminder hai - iske baad hum aapko is bare mein "
       "contact nahi karenge."),
    _t("whatsapp", "hinglish", FINAL,
       "Rs {amount} ka payment {days} din se pending hai.\n\n"
       "Aaj pay karne par Rs {discount} ki chhoot: {link}\n\n"
       "Iske baad hum aapko is bare mein contact nahi karenge.",
       incentive=True),

    # ---- whatsapp · en ----------------------------------------------------
    _t("whatsapp", "en", NOTIFY,
       "Hello! Your payment of Rs {amount} could not be completed.\n\n"
       "You can pay here in 2 minutes: {link}\n\n"
       "Reply to this message if you need help."),
    _t("whatsapp", "en", REMIND,
       "Hello! Rs {amount} has been pending for {days} days.\n\n"
       "Pay here: {link}\n\n"
       "Please ignore this if you have already paid."),
    _t("whatsapp", "en", PROMISE_ASK,
       "Hello! Rs {amount} is pending.\n\n"
       "When can you pay? Reply with a date and we will wait until then.\n\n"
       "Or pay now: {link}"),
    _t("whatsapp", "en", FINAL,
       "Rs {amount} has been pending for {days} days.\n\n"
       "Pay here: {link}\n\n"
       "This is our final reminder - we will not contact you about this "
       "again."),
    _t("whatsapp", "en", FINAL,
       "Rs {amount} has been pending for {days} days.\n\n"
       "Pay today and get Rs {discount} off: {link}\n\n"
       "We will not contact you about this again.",
       incentive=True),

    # ---- email · en -------------------------------------------------------
    _t("email", "en", NOTIFY,
       "We could not collect Rs {amount} for your account.\n\n"
       "You can settle it here: {link}\n\n"
       "If you have already paid, no action is needed."),
    _t("email", "en", REMIND,
       "Rs {amount} has been outstanding for {days} days.\n\n"
       "Settle here: {link}\n\n"
       "Reply to this email if the amount looks wrong and we will check."),
    _t("email", "en", FINAL,
       "Rs {amount} has been outstanding for {days} days and this is the last "
       "reminder we will send.\n\n"
       "Settle here: {link}\n\n"
       "If the invoice is disputed, reply and we will put it on hold."),
    _t("email", "en", PROMISE_ASK,
       "Rs {amount} is outstanding.\n\n"
       "If you can tell us a date you expect to pay by, we will hold the "
       "account until then. Reply with a date, or settle here: {link}"),

    # ---- email · hinglish -------------------------------------------------
    _t("email", "hinglish", REMIND,
       "Rs {amount} ka payment {days} din se pending hai.\n\n"
       "Yahan settle karein: {link}\n\n"
       "Agar amount galat lag raha hai to reply karein, hum check kar lenge."),

    # ---- voice · hinglish -------------------------------------------------
    # Spoken. Short sentences, no URLs -- nobody writes down a link from a call.
    _t("voice", "hinglish", REMIND,
       "Namaste. Aapke account par Rs {amount} ka payment pending hai. "
       "Payment link aapke registered number par SMS kar diya gaya hai. "
       "Dhanyavaad."),
    _t("voice", "hinglish", FINAL,
       "Namaste. Aapke account par Rs {amount} ka payment {days} din se "
       "pending hai. Yeh hamara aakhri reminder hai. "
       "Payment link aapke registered number par bhej diya gaya hai. "
       "Dhanyavaad."),
    _t("voice", "hinglish", PROMISE_ASK,
       "Namaste. Aapke account par Rs {amount} ka payment pending hai. "
       "Agar aap ek date bata sakein, to hum tab tak wait kar lenge. "
       "Ek dabaiye baat karne ke liye. Dhanyavaad."),

    # ---- voice · en -------------------------------------------------------
    _t("voice", "en", REMIND,
       "Hello. There is a pending payment of Rs {amount} on your account. "
       "We have sent a payment link to your registered number. Thank you."),
    _t("voice", "en", FINAL,
       "Hello. A payment of Rs {amount} has been pending for {days} days. "
       "This is our final reminder. We have sent a payment link to your "
       "registered number. Thank you."),
    _t("voice", "en", PROMISE_ASK,
       "Hello. There is a pending payment of Rs {amount} on your account. "
       "If you can give us a date, we will wait until then. "
       "Press one to speak to us. Thank you."),
)


def by_id(template_id: str) -> Template | None:
    for template in REGISTRY:
        if template.id == template_id:
            return template
    return None


def registered_ids() -> list[str]:
    """Every template id, sorted. This is the list a merchant registers with
    DLT and with WhatsApp before going live."""
    return sorted(t.id for t in REGISTRY)
