"""Render the voice templates to audio, so they can be heard rather than read.

The brief names "Hinglish voice recovery" as a direction, and
`rcp/compose/templates.py` has carried spoken scripts since the composer was
built -- but nobody has ever heard one. A script reads fine on a page and is a
different artifact out loud: pacing, the way an amount lands, whether 40 seconds
is actually 40 seconds.

**macOS only, and the output is committed.** `say` and `afconvert` ship with
macOS and exist nowhere else, so this is a build step run on a Mac whose results
live in the repository. A Linux container, CI, and any reviewer without a Mac
serve `app/static/audio/` as static files and never run this.

`Aman` is an en_IN voice, which is the right one for Hinglish: the templates are
Latin script by design (GSM-7, see `rcp/compose/critic.py`), and an Indian
English voice reads "Aapke account par" the way a person would. A hi_IN voice
would be correct for Devanagari and wrong for this.

Every script is run through the critic before it is spoken. `url_in_voice` is
the rule that matters here -- nobody writes down a link from a phone call, and a
script containing one is a defect that should not reach audio.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from rcp.compose.critic import blocking, check
from rcp.compose.render import MONTHS, Message
from rcp.compose.templates import REGISTRY
from rcp.store import REPO_ROOT

OUT_DIR = REPO_ROOT / "app" / "static" / "audio"

# One voice for every script. Two voices would imply the channel picks one,
# which it does not -- the executor's telephony provider does.
VOICE = "Aman"

# Representative values, matching `rcp/agents/composer.check_draft` so what is
# heard is what the critic judged.
VALUES = {"amount": "2,500", "days": "12", "discount": "50",
          "due": f"14 {MONTHS[8]}"}


def render(text: str) -> str:
    for key, value in sorted(VALUES.items()):
        text = text.replace("{" + key + "}", value)
    return text


def require_macos() -> None:
    missing = [tool for tool in ("say", "afconvert") if shutil.which(tool) is None]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}: this is a macOS build step.\n"
            f"The generated audio is committed to {OUT_DIR.relative_to(REPO_ROOT)}, "
            f"so the dashboard works without it."
        )


def speak(text: str, destination: Path) -> int:
    """Returns the size of the written .m4a in bytes."""
    aiff = destination.with_suffix(".aiff")
    subprocess.run(["say", "-v", VOICE, "-o", str(aiff), text], check=True)
    subprocess.run(
        ["afconvert", str(aiff), str(destination), "-f", "m4af", "-d", "aac"],
        check=True,
    )
    aiff.unlink()
    return destination.stat().st_size


def main() -> None:
    require_macos()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    scripts = [t for t in REGISTRY if t.channel == "voice"]
    if not scripts:
        raise SystemExit("no voice templates in the registry")

    total = 0
    for template in sorted(scripts, key=lambda t: t.id):
        text = render(template.text)

        findings = blocking(check(Message(
            template_id=template.id, channel="voice",
            language=template.language, purpose=template.purpose, text=text,
        )))
        if findings:
            rules = ", ".join(f.rule for f in findings)
            print(f"  SKIP {template.id}: the critic blocks it ({rules})",
                  file=sys.stderr)
            continue

        size = speak(text, OUT_DIR / f"{template.id}.m4a")
        total += size
        print(f"  {template.id:32} {size / 1024:6.1f} KB  {len(text):3} chars")

    print(f"\n{total / 1024:.0f} KB written to "
          f"{OUT_DIR.relative_to(REPO_ROOT)}. Commit it -- `say` is macOS only.")


if __name__ == "__main__":
    main()
