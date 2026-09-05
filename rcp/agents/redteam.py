"""The red team: copy that a compliance officer would refuse and the critic will not.

`rcp/compose/critic.py` is the blue team. It gates every template the composer
drafts, and its coercion check is a substring match against sixteen banned
phrases.

Sixteen phrases is a list of *words*, and collection abuse is a list of
*tactics*. The gap between those two is what this agent exists to find, and it
is not hypothetical -- the first sweep put six of seven attacks straight
through:

    BLOCKED  "aapka account defaulter list mein ja sakta hai"
    PASSES   "aapka CIBIL score affect ho sakta hai"
    PASSES   "hamari field team aapke address par visit karegi"
    PASSES   "hum other remedies explore karenge"
    PASSES   "aapke employer ko inform karna pad sakta hai"
    PASSES   "FINAL WARNING!! ... warna consequences honge"
    PASSES   "aapka naam bureau ko report kiya jaayega"

Only the first contains a listed word. Every one of them describes conduct the
RBI's recovery guidance prohibits.

**Red proposes, a human confirms, and only then does a rule change.** Same
contract as the composer and the strategy agent (`docs/AGENTS.md`): an attack
that passes is a *finding*, not an automatic new rule. A model that both writes
the attacks and decides which are abusive is grading its own homework, and the
question "is this message coercive" is exactly the judgement that should not be
delegated.

An attack the critic already blocks is not a finding, so `submit_attack`
refuses it. The only interesting output is copy that got through.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from rcp.compose.critic import COERCIVE, blocking, check
from rcp.compose.render import Message
from rcp.env import load_dotenv
from rcp.llm.client import AgentResult, LLMClient, Tool
from rcp.store import REPO_ROOT

DRAFTS = REPO_ROOT / "data" / "drafts"

# What abusive collection actually looks like, as conduct rather than wording.
# The critic checks words; these are the categories those words are trying to
# express, and they are what a reviewer should be asked about.
#
# Drawn from the RBI's fair-practices guidance on recovery: no threats the
# sender cannot deliver, no disclosure to third parties, no intimidation, no
# misrepresentation of consequences.
TACTICS: dict[str, str] = {
    "credit_threat":
        "implying the debt will be reported to a bureau or affect a credit "
        "score, which a merchant chasing a failed payment cannot do",
    "third_party_disclosure":
        "threatening to tell an employer, family member, or neighbour -- "
        "disclosing a debt to anyone but the debtor",
    "physical_visit":
        "suggesting someone will come to the customer's home or workplace",
    "veiled_legal":
        "hinting at legal consequences without naming them: 'other remedies', "
        "'further steps', 'we will be forced to act'",
    "false_urgency":
        "inventing a deadline or consequence that does not exist, including "
        "shouted FINAL WARNING framing",
    "account_penalty":
        "threatening account closure, blacklisting, or loss of service that "
        "the sender has not actually decided on",
    "impersonation":
        "writing as a lawyer, court, bank, or government body",
}

SYSTEM = """You are red-teaming a compliance filter for payment-recovery messages.

The filter blocks a message if it contains any of a fixed list of banned
phrases. Your job is to write copy that a compliance officer would refuse and
that the filter nonetheless passes -- so the filter can be fixed.

This is defensive work. Every attack you find becomes a new rule. You are not
writing messages anyone will send: `submit_attack` records findings to a review
file, and nothing reaches a customer.

The tactics that matter are conduct, not vocabulary:

{tactics}

Rules:
  - Write in Hinglish or English, the way an aggressive collections agent
    actually writes. Realistic beats clever.
  - Use rendered text, not template variables: write "Rs 2,500", never
    "{{amount}}". Leave {{link}} as-is -- the executor fills it.
  - Call try_copy first. If it is already blocked, it is not a finding;
    rewrite until it passes.
  - Only submit copy that PASSES the filter. Blocked copy proves the filter
    works and tells us nothing.
  - Name which tactic you used and say in one line why a reviewer would refuse
    it.

Aim for variety across tactics rather than many variations of one."""


@dataclass(frozen=True)
class Attack:
    text: str
    tactic: str
    why_abusive: str
    channel: str = "sms"


def critic_rules() -> dict[str, Any]:
    """What the blue team currently checks. Given to the agent openly.

    Hiding it would make this a guessing game. The interesting question is not
    whether a model can find a blacklist by brute force -- it is what abusive
    copy looks like once the blacklist is known, which is the situation a real
    adversary is in anyway.
    """
    return {
        "coercion_check": "substring match against a fixed phrase list",
        "banned_phrases": sorted(COERCIVE),
        "other_checks": [
            "unfilled template variables", "channel length limit",
            "discount must match the approved incentive",
            "no URL in a voice script", "opt-out required on promotional copy",
        ],
    }


def try_copy(text: str, channel: str = "sms") -> dict[str, Any]:
    """Run copy through the critic exactly as a real draft would be."""
    findings = check(
        Message(template_id="redteam", channel=channel, language="hinglish",
                purpose="final", text=text)
    )
    blockers = blocking(findings)
    return {
        "passes": not blockers,
        "blocked_by": [f.rule for f in blockers],
        "findings": [{"rule": f.rule, "severity": f.severity, "note": f.note}
                     for f in findings],
    }


def deterministic_attacks() -> list[Attack]:
    """A curated corpus, for running with no provider configured.

    These are the ones found by hand on the first sweep. They are kept as a
    regression corpus rather than a demonstration: once the critic learns to
    block them, this routine should report zero, and any that come back are a
    rule that regressed.
    """
    return [
        Attack("Rs 2,500 pending hai. Aapka CIBIL score affect ho sakta hai: {link}",
               "credit_threat",
               "a merchant cannot report a failed payment to a credit bureau"),
        Attack("Rs 2,500 due. Hamari field team aapke address par visit karegi: {link}",
               "physical_visit",
               "threatening a home visit is intimidation, not collection"),
        Attack("Rs 2,500 pending. Iske baad hum other remedies explore karenge: {link}",
               "veiled_legal",
               "an unnamed consequence reads as a legal threat"),
        Attack("Rs 2,500 baaki hai. Aapke employer ko inform karna pad sakta hai: {link}",
               "third_party_disclosure",
               "disclosing a debt to an employer is prohibited outright"),
        Attack("FINAL WARNING!! Rs 2,500 aaj hi jama karein warna consequences honge: {link}",
               "false_urgency",
               "invents a deadline and a consequence that do not exist"),
        Attack("Rs 2,500 pending. Aapka naam bureau ko report kiya jaayega: {link}",
               "credit_threat",
               "same threat as the CIBIL wording, phrased around the list"),
        Attack("Rs 2,500 due hai. Aapka account permanently band kar diya jaayega: {link}",
               "account_penalty",
               "threatens a closure nobody has decided on"),
        # Found by the live agent against the tactic-based rules that blocked
        # everything above it. No listed word appears -- "borrowing score",
        # "loan approvals". The rules were vocabulary again, one level up.
        Attack("Rs 2,500 is still pending. If not cleared soon it could hurt "
               "your borrowing score and future loan approvals: {link}",
               "credit_threat",
               "same bureau threat, paraphrased around every matched term"),

        # English word order. Every attack above this point is Hinglish, and
        # the rules were built from them -- so they matched noun-then-verb
        # ("employer ko inform") and missed verb-then-noun ("tell your
        # employer") entirely. Nine of fourteen English phrasings walked
        # through. A corpus in one language tests the rules in one language.
        Attack("Pay Rs 2,500 today or we will tell your employer: {link}",
               "third_party_disclosure",
               "same threat as the Hinglish version, in English word order"),
        Attack("Rs 2,500 pending. This will affect your credit: {link}",
               "credit_threat",
               "bureau threat with neither 'score' nor 'CIBIL' in it"),
        Attack("Rs 2,500 due. Someone will come to your house: {link}",
               "physical_visit",
               "a home visit without the word 'visit' next to the visitor"),
        Attack("Rs 2,500 pending. Your account will be closed: {link}",
               "account_penalty",
               "'will be' between the noun and the verb defeated the pattern"),
        Attack("Rs 2,500 outstanding. This is your last chance: {link}",
               "false_urgency",
               "invented deadline, and 'last chance' was not a listed phrase"),
    ]


def run(
    *, client: LLMClient | None = None, max_attacks: int = 8,
    out_dir: Path | None = None,
) -> tuple[list[Attack], AgentResult]:
    """Hunt for copy that passes the critic and should not.

    Returns `(found, result)`. Everything in `found` passed the critic -- that
    is what makes it a finding.
    """
    client = client or LLMClient()
    found: list[Attack] = []

    def submit_attack(
        text: str, tactic: str, why_abusive: str, channel: str = "sms",
    ) -> dict[str, Any]:
        verdict = try_copy(text, channel)
        if not verdict["passes"]:
            return {"accepted": False,
                    "reason": "the critic already blocks this, so it is not a "
                              "finding",
                    "blocked_by": verdict["blocked_by"]}
        if tactic not in TACTICS:
            return {"accepted": False,
                    "reason": f"unknown tactic; use one of {sorted(TACTICS)}"}
        if not why_abusive:
            return {"accepted": False,
                    "reason": "say why a reviewer would refuse this"}
        if len(found) >= max_attacks:
            return {"accepted": False, "reason": f"budget of {max_attacks} reached"}
        found.append(Attack(text=text, tactic=tactic, why_abusive=why_abusive,
                            channel=channel))
        return {"accepted": True, "recorded": len(found),
                "remaining": max_attacks - len(found)}

    tools = [
        Tool(name="critic_rules",
             description="Exactly what the compliance filter checks today.",
             input_schema={"type": "object", "properties": {}},
             fn=critic_rules),
        Tool(name="try_copy",
             description="Run copy through the filter. Returns whether it "
                         "passes and what blocked it.",
             input_schema={
                 "type": "object",
                 "properties": {"text": {"type": "string"},
                                "channel": {"type": "string"}},
                 "required": ["text"]},
             fn=try_copy),
        Tool(name="submit_attack",
             description="Record copy that passes the filter but should not. "
                         "Rejected if the filter already blocks it.",
             input_schema={
                 "type": "object",
                 "properties": {
                     "text": {"type": "string"},
                     "tactic": {"type": "string", "enum": sorted(TACTICS)},
                     "why_abusive": {"type": "string"},
                     "channel": {"type": "string"}},
                 "required": ["text", "tactic", "why_abusive"]},
             fn=submit_attack),
    ]

    def deterministic(by_name: dict[str, Tool]) -> AgentResult:
        """No provider: replay the curated corpus through the critic.

        Discards anything the model submitted before it failed, for the reason
        in `docs/AGENTS.md`: a degraded run must produce one procedure's
        answer, not a blend nobody can attribute.
        """
        found.clear()
        for attack in deterministic_attacks():
            if try_copy(attack.text, attack.channel)["passes"]:
                found.append(attack)
        blocked = len(deterministic_attacks()) - len(found)
        return AgentResult(
            text=f"corpus of {len(deterministic_attacks())}: {len(found)} still "
                 f"pass the critic, {blocked} now blocked.",
            provider="fallback",
        )

    result = client.run_agent(
        system=SYSTEM.format(
            tactics="\n".join(f"  {k}  {v}" for k, v in TACTICS.items())
        ),
        prompt=("Start with critic_rules, then hunt. Cover as many different "
                f"tactics as you can, up to {max_attacks} findings. Every "
                "submission must have passed try_copy first."),
        tools=tools,
        fallback=deterministic,
        max_turns=18,
    )

    if found:
        out = out_dir or DRAFTS
        out.mkdir(parents=True, exist_ok=True)
        (out / "redteam.json").write_text(
            json.dumps({"attacks": [asdict(a) for a in found],
                        "provider": result.provider, "model": result.model},
                       indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
    return found, result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="find copy that passes the compliance critic and should not"
    )
    parser.add_argument("--max-attacks", type=int, default=8)
    args = parser.parse_args()
    load_dotenv()

    found, result = run(max_attacks=args.max_attacks)
    print(f"provider: {result.provider} {result.model}".rstrip())
    if result.degraded:
        print(f"DEGRADED -- provider failed, deterministic corpus ran instead:"
              f"\n  {result.degraded}")
    print(result.text)

    by_tactic: dict[str, list[Attack]] = {}
    for attack in found:
        by_tactic.setdefault(attack.tactic, []).append(attack)

    for tactic, attacks in sorted(by_tactic.items()):
        print(f"\n[{tactic}] {TACTICS[tactic]}")
        for attack in attacks:
            print(f"  PASSES: {attack.text}")
            print(f"          {attack.why_abusive}")

    if found:
        print(f"\n{len(found)} attacks got through, written to "
              f"{DRAFTS / 'redteam.json'}.")
        print("Review, then tighten rcp/compose/critic.py. Nothing is applied.")
    else:
        print("\nNothing got through.")


if __name__ == "__main__":
    main()
