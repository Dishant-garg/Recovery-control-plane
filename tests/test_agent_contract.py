"""The agent contract, enforced rather than documented.

`docs/AGENTS.md` states four rules every agent in this project follows. A rule
that lives only in prose is a rule the next agent breaks, so these tests parse
the agent modules and check them -- the same approach
`tests/test_ground_truth_isolation.py` takes to the `rcp/` ↔ `sim/` boundary and
`tests/test_pipeline.py` takes to determinism.

The list of agents is discovered from the filesystem, so a new agent module is
subject to all of this the moment it exists, without anyone remembering to add
it here.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from rcp.store import REPO_ROOT

DOC = REPO_ROOT / "docs" / "AGENTS.md"

# Agents live in one of two places: rcp/agents/ for anything the control plane
# runs, and eval/ for anything that may read ground truth (ADR-002).
AGENT_FILES = sorted(
    p for p in [
        *(REPO_ROOT / "rcp" / "agents").glob("*.py"),
        REPO_ROOT / "eval" / "analyst.py",
    ]
    if p.name != "__init__.py"
)

SOURCE_TREES = ("rcp", "eval", "sim", "config", "app")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


@pytest.fixture(scope="module")
def agent_files() -> list[Path]:
    assert AGENT_FILES, "no agent modules found; the discovery glob is wrong"
    return AGENT_FILES


# ---- rule 1: a prompt AND a deterministic routine --------------------------

@pytest.mark.parametrize(
    "path", AGENT_FILES, ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_every_agent_defines_a_system_prompt(path: Path):
    """ADR-007: an agent ships the prompt next to the tools it describes.

    Not in a separate markdown file. The prompt and the deterministic routine
    are two halves of one contract, and separating them is how they drift.
    """
    names = {
        target.id
        for node in _tree(path).body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "SYSTEM" in names, (
        f"{path.name} has no module-level SYSTEM prompt"
    )


@pytest.mark.parametrize(
    "path", AGENT_FILES, ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_every_run_agent_call_supplies_a_fallback(path: Path):
    """The deterministic routine is what makes `make eval` free and offline,
    and what a provider failure falls back to (`AgentResult.degraded`).

    `LLMClient.run_agent` raises without one when the fallback provider is
    selected, so omitting it turns "no API key" into a crash.
    """
    calls = [
        node for node in ast.walk(_tree(path))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_agent"
    ]
    assert calls, f"{path.name} defines an agent but never calls run_agent"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "fallback" in keywords, (
            f"{path.name}:{call.lineno} calls run_agent without a fallback= "
            f"deterministic routine"
        )


# ---- rule 2: propose, never apply -----------------------------------------

def _snapshot() -> dict[Path, int]:
    return {
        path: path.stat().st_mtime_ns
        for tree in SOURCE_TREES
        for path in (REPO_ROOT / tree).rglob("*")
        if path.is_file() and path.suffix in (".py", ".yaml", ".yml")
    }


def test_authoring_agents_do_not_touch_the_source_tree(tmp_path):
    """A prior that rewrites itself from its own outcomes is a feedback loop.

    The composer drafts templates and the strategy agent proposes new
    probability tables; both write to a review file a human reads. Neither may
    edit `rcp/proposers/` or `rcp/compose/templates.py` -- and for the composer
    that is not only good sense: an unregistered template is undeliverable, so
    the human step is a legal requirement rather than a workflow preference.
    """
    from rcp.agents import composer, strategy
    from rcp.llm.client import LLMClient
    from rcp.llm.providers.fallback import FallbackAdapter
    from rcp.store import DATA_DIR, connect

    before = _snapshot()
    offline = LLMClient(adapter=FallbackAdapter(), cache=None)

    composer.draft_templates(client=offline, out_dir=tmp_path)

    arm = next(DATA_DIR.glob("seed_*/rcp_baseline.db"), None)
    if arm is not None:
        conn = connect(arm, read_only=True)
        try:
            strategy.revise_tables(conn, client=offline, out_dir=tmp_path)
        finally:
            conn.close()

    changed = [p for p, mtime in _snapshot().items() if before.get(p) != mtime]
    assert not changed, f"an agent modified source files: {changed}"


# ---- rule 3: the docs name every agent ------------------------------------

def test_the_contract_doc_names_every_agent(agent_files):
    """Keeps `docs/AGENTS.md` from going stale the way the README's Status
    section did -- it claimed the recovery agent and the Razorpay executor were
    unbuilt for days after both shipped."""
    assert DOC.exists(), "docs/AGENTS.md is missing"
    text = DOC.read_text(encoding="utf-8")
    for path in agent_files:
        relative = path.relative_to(REPO_ROOT).as_posix()
        assert relative in text, f"docs/AGENTS.md does not mention {relative}"


def test_the_contract_doc_states_the_rules_it_enforces(agent_files):
    """If a rule is removed from the doc, the test enforcing it is orphaned."""
    text = DOC.read_text(encoding="utf-8").lower()
    for phrase in ("fallback", "degraded", "review file", "system"):
        assert phrase in text, f"docs/AGENTS.md does not mention `{phrase}`"
