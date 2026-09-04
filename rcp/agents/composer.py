"""The composer agent: drafts templates a human then registers.

This is the *authoring* half of ADR-007, and it is the half that genuinely
needs a model. Selecting and filling a template is deterministic and lives in
`rcp/compose/render.py`. Writing a new one -- in Hinglish, for a channel and a
purpose that currently falls back to English -- is a language task.

**Nothing this agent produces can reach a customer.** A draft goes to a review
file. A human reads it, registers it with DLT and with WhatsApp, and pastes it
into `rcp/compose/templates.py`. That is not caution for its own sake: an
unregistered template is undeliverable in this market anyway, so the human step
already exists and the agent is feeding it rather than bypassing it.

The interesting tool is `check_draft`. The critic is exposed to the agent, so
it can test a draft against the same rules that will judge it -- coercive
language, length, SMS segment count, a discount nobody approved -- and rewrite
before submitting. An agent that can see its own bounds negotiates with them
instead of being silently rejected.

The deterministic routine reports the coverage gaps and drafts nothing. That is
the honest fallback: which templates are missing is computable, and what they
should say is not.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rcp.compose.critic import blocking, check, sms_segments
from rcp.compose.render import Message, _candidates
from rcp.compose.templates import CHANNELS, PURPOSES, REGISTRY
from rcp.env import load_dotenv
from rcp.llm.client import AgentResult, LLMClient, Tool
from rcp.store import REPO_ROOT

DRAFTS = REPO_ROOT / "data" / "drafts"

SYSTEM = """You write payment-recovery message templates for Indian customers.

The system already has a registry of templates. Your job is to fill gaps in it
-- combinations of channel, language and purpose that currently fall back to a
less specific template.

Rules that are not negotiable:

  - Templates are registered with TRAI's DLT registry and with WhatsApp before
    they can be sent. Write something a compliance reviewer would approve.
  - Never threaten consequences: no legal action, police, court, blacklisting,
    or recovery agents. This is a legal constraint, not a tone preference.
  - Use only these variables: {amount}, {discount}, {days}, {due}, {link}.
    {link} is filled by the executor at send time. Do not invent variables and
    do not use a customer name -- the system deliberately stores none.
  - Only mention a discount if you are drafting an incentive variant, and then
    it must be {discount}, never a number you chose.
  - A voice script is spoken aloud. No URLs.

Hinglish means Hindi written in Latin script, the way people actually message:
"Aapka Rs {amount} ka payment complete nahi hua." Not Devanagari, and not
English with one Hindi word dropped in.

Write Hinglish in Latin script for a specific reason: an SMS segment holds 160
GSM-7 characters, but Devanagari forces UCS-2 and a segment then holds 70. The
same message costs roughly twice as much in Devanagari. `check_draft` reports
the segment count -- keep SMS to one segment where you can.

Purposes:
  notify       first contact about this failure
  remind       a middle contact; they have not responded yet
  final        the last contact before we stop; say so plainly
  promise_ask  ask when they can pay, and commit to waiting

Process: call coverage_gaps, then for each gap you fill, call check_draft and
rewrite until it passes, then call submit_template. Do not submit a draft you
have not checked."""


@dataclass(frozen=True)
class Draft:
    channel: str
    language: str
    purpose: str
    text: str
    mentions_incentive: bool = False

    @property
    def template_id(self) -> str:
        suffix = ".incentive" if self.mentions_incentive else ""
        return f"{self.channel}.{self.language}.{self.purpose}{suffix}"


def coverage_gaps() -> list[dict[str, str]]:
    """Combinations that fall back rather than matching exactly.

    A gap is not a failure -- `_candidates` always terminates in something
    sendable, which is the point of the backoff chain. It is a place where a
    customer gets English when they would have understood Hinglish, or a
    generic reminder where a final notice was due.
    """
    registered = {(t.channel, t.language, t.purpose) for t in REGISTRY}
    gaps = []
    for channel in CHANNELS:
        for language in ("hinglish", "hi", "en"):
            for purpose in PURPOSES:
                if (channel, language, purpose) in registered:
                    continue
                chain = _candidates(channel, language, purpose, None,
                                    has_incentive=False)
                gaps.append({
                    "channel": channel,
                    "language": language,
                    "purpose": purpose,
                    "currently_falls_back_to": chain[0].id if chain else "nothing",
                })
    return gaps


def check_draft(
    channel: str, language: str, purpose: str, text: str,
    mentions_incentive: bool = False,
) -> dict[str, Any]:
    """Run a draft through the critic that will judge it at send time.

    Renders with representative values first: a template is only sendable once
    its variables are filled, and a draft that fits in one SMS segment empty
    can be three once an amount is in it.
    """
    filled = (
        text.replace("{amount}", "2,500")
            .replace("{discount}", "50")
            .replace("{days}", "12")
            .replace("{due}", "14 Sep")
    )
    message = Message(
        template_id=f"draft.{channel}.{language}.{purpose}", channel=channel,
        language=language, purpose=purpose, text=filled,
        mentions_incentive=mentions_incentive,
    )
    findings = check(message, incentive_paise=5_000 if mentions_incentive else 0)
    result: dict[str, Any] = {
        "acceptable": not blocking(findings),
        "findings": [
            {"rule": f.rule, "severity": f.severity, "note": f.note}
            for f in findings
        ],
        "rendered": filled,
        "characters": len(filled),
    }
    if channel == "sms":
        segments, encoding, billable = sms_segments(filled)
        result["sms"] = {"segments": segments, "encoding": encoding,
                         "billable_characters": billable}
    return result


def draft_templates(
    *, client: LLMClient | None = None, max_drafts: int = 6,
    out_dir: Path | None = None,
) -> tuple[list[Draft], AgentResult]:
    """Ask for drafts covering the current gaps. Writes a review file.

    Returns `(accepted, result)`. `accepted` passed the critic; it is still a
    proposal, not a registration.
    """
    client = client or LLMClient()
    accepted: list[Draft] = []

    def submit_template(
        channel: str, language: str, purpose: str, text: str,
        mentions_incentive: bool = False,
    ) -> dict[str, Any]:
        draft = Draft(channel=channel, language=language, purpose=purpose,
                      text=text, mentions_incentive=mentions_incentive)
        verdict = check_draft(channel, language, purpose, text,
                              mentions_incentive)
        if not verdict["acceptable"]:
            # Refuse rather than accept and warn. A draft that fails the critic
            # would fail again at send time, and recording it as accepted would
            # make the review file untrustworthy.
            return {"accepted": False, "reason": "the critic blocks this draft",
                    "findings": verdict["findings"]}
        if len(accepted) >= max_drafts:
            return {"accepted": False, "reason": f"budget of {max_drafts} reached"}
        accepted.append(draft)
        return {"accepted": True, "template_id": draft.template_id,
                "remaining": max_drafts - len(accepted)}

    tools = [
        Tool(
            name="coverage_gaps",
            description="Channel/language/purpose combinations with no exact "
                        "template, and what each currently falls back to.",
            input_schema={"type": "object", "properties": {}},
            fn=lambda: coverage_gaps(),
        ),
        Tool(
            name="existing_templates",
            description="Registered templates, optionally filtered, so a draft "
                        "matches the voice of what is already there.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "language": {"type": "string"},
                },
            },
            fn=lambda channel=None, language=None: [
                {"id": t.id, "text": t.text}
                for t in REGISTRY
                if (channel is None or t.channel == channel)
                and (language is None or t.language == language)
            ][:12],
        ),
        Tool(
            name="check_draft",
            description="Run a draft through the critic. Returns the findings "
                        "and, for SMS, the billable segment count.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "language": {"type": "string"},
                    "purpose": {"type": "string"},
                    "text": {"type": "string"},
                    "mentions_incentive": {"type": "boolean"},
                },
                "required": ["channel", "language", "purpose", "text"],
            },
            fn=check_draft,
        ),
        Tool(
            name="submit_template",
            description="Submit a checked draft for human review. Rejected if "
                        "the critic blocks it.",
            input_schema={
                "type": "object",
                "properties": {
                    "channel": {"type": "string"},
                    "language": {"type": "string"},
                    "purpose": {"type": "string"},
                    "text": {"type": "string"},
                    "mentions_incentive": {"type": "boolean"},
                },
                "required": ["channel", "language", "purpose", "text"],
            },
            fn=submit_template,
        ),
    ]

    def deterministic(by_name: dict[str, Tool]) -> AgentResult:
        """No model: report what is missing and draft nothing.

        Which templates are missing is computable. What they should say is not,
        and generating filler here would put unreviewed copy into the review
        file under the appearance of having been authored.

        Discards anything submitted before the provider failed. A run that
        drafted two templates and then hit a rate limit would otherwise write a
        review file holding a fragment of an interrupted process, with nothing
        on the page saying it was partial.
        """
        accepted.clear()
        gaps = coverage_gaps()
        lines = [f"{len(gaps)} coverage gaps; drafting needs a live provider.",
                 "Set RCP_LLM to a provider and re-run. Gaps:"]
        lines += [
            f"  {g['channel']}.{g['language']}.{g['purpose']} "
            f"-> falls back to {g['currently_falls_back_to']}"
            for g in gaps[:20]
        ]
        return AgentResult(text="\n".join(lines), provider="fallback")

    result = client.run_agent(
        system=SYSTEM,
        prompt=(
            "Fill the most valuable coverage gaps. Prioritise Hinglish, then "
            "Hindi. Start by calling coverage_gaps.\n\n"
            f"Submit at most {max_drafts} templates, and check every one "
            "before submitting it."
        ),
        tools=tools,
        fallback=deterministic,
        max_turns=16,
    )

    if accepted:
        out = (out_dir or DRAFTS)
        out.mkdir(parents=True, exist_ok=True)
        path = out / "templates.json"
        path.write_text(json.dumps(
            {"drafts": [asdict(d) | {"template_id": d.template_id}
                        for d in accepted],
             "provider": result.provider, "model": result.model},
            indent=2, ensure_ascii=False,
        ) + "\n", encoding="utf-8")

    return accepted, result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="draft message templates for the gaps in the registry"
    )
    parser.add_argument("--max-drafts", type=int, default=6)
    args = parser.parse_args()
    load_dotenv()

    accepted, result = draft_templates(max_drafts=args.max_drafts)

    print(f"provider: {result.provider} {result.model}".rstrip())
    if not accepted:
        print(result.text)
        return

    for draft in accepted:
        verdict = check_draft(draft.channel, draft.language, draft.purpose,
                              draft.text, draft.mentions_incentive)
        extra = ""
        if "sms" in verdict:
            extra = (f"  [{verdict['sms']['segments']} segment(s), "
                     f"{verdict['sms']['encoding']}]")
        print(f"\n{draft.template_id}{extra}\n  {draft.text}")

    print(f"\n{len(accepted)} drafts written to {DRAFTS / 'templates.json'}")
    print("Review, register with DLT and WhatsApp, then add to "
          "rcp/compose/templates.py. Nothing here is live.")


if __name__ == "__main__":
    main()
