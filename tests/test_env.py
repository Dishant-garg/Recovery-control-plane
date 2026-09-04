"""`.env` loading.

The repo shipped `.env.example` for a while with nothing reading `.env`, so a
filled-in file did nothing and the failure looked like a bad API key. These
tests pin the behaviour that fixes it -- especially the precedence rule, which
is the part that silently bites.
"""

from __future__ import annotations

import os

import pytest

from rcp.env import load_dotenv


@pytest.fixture
def clean_env(monkeypatch):
    """Snapshot and restore the WHOLE environment.

    `load_dotenv` writes `os.environ` directly, which monkeypatch cannot undo --
    so these tests leaked `RCP_LLM=groq` into the rest of the suite and broke
    every later test that built an adapter. Restoring wholesale is the only
    version of this fixture that is actually safe.
    """
    snapshot = dict(os.environ)
    for key in ("RCP_LLM", "GROQ_API_KEY", "RCP_LLM_MODEL", "QUOTED", "EXPORTED"):
        monkeypatch.delenv(key, raising=False)
    yield monkeypatch
    os.environ.clear()
    os.environ.update(snapshot)


def write(tmp_path, body: str):
    path = tmp_path / ".env"
    path.write_text(body)
    return path


def test_missing_file_is_fine(tmp_path, clean_env):
    """Everything in this project runs without a .env."""
    assert load_dotenv(tmp_path / "nope") == {}


def test_loads_key_value_pairs(tmp_path, clean_env):
    load_dotenv(write(tmp_path, "RCP_LLM=groq\nGROQ_API_KEY=gsk_abc\n"))
    assert os.environ["RCP_LLM"] == "groq"
    assert os.environ["GROQ_API_KEY"] == "gsk_abc"


def test_a_real_env_var_beats_the_file(tmp_path, clean_env):
    """`RCP_LLM=groq make analyze` must not be overridden by a stale .env."""
    clean_env.setenv("RCP_LLM", "anthropic")
    applied = load_dotenv(write(tmp_path, "RCP_LLM=groq\n"))

    assert os.environ["RCP_LLM"] == "anthropic"
    assert "RCP_LLM" not in applied


def test_override_is_available_when_asked(tmp_path, clean_env):
    clean_env.setenv("RCP_LLM", "anthropic")
    load_dotenv(write(tmp_path, "RCP_LLM=groq\n"), override=True)
    assert os.environ["RCP_LLM"] == "groq"


def test_blank_values_do_not_configure_anything(tmp_path, clean_env):
    """.env.example ships empty keys meaning 'not configured'. Setting them to
    an empty string would turn a clear 'key missing' error into a 401."""
    load_dotenv(write(tmp_path, "GROQ_API_KEY=\nRCP_LLM=groq\n"))
    assert "GROQ_API_KEY" not in os.environ
    assert os.environ["RCP_LLM"] == "groq"


def test_comments_and_blank_lines_are_skipped(tmp_path, clean_env):
    applied = load_dotenv(write(tmp_path, """
# a comment
RCP_LLM=groq

  # indented comment
not_a_pair
"""))
    assert applied == {"RCP_LLM": "groq"}


def test_quotes_and_export_prefix_are_stripped(tmp_path, clean_env):
    load_dotenv(write(tmp_path, 'QUOTED="gsk_abc"\nexport EXPORTED=yes\n'))
    assert os.environ["QUOTED"] == "gsk_abc"
    assert os.environ["EXPORTED"] == "yes"


def test_values_containing_equals_survive(tmp_path, clean_env):
    """Base64-ish secrets routinely contain '='."""
    load_dotenv(write(tmp_path, "GROQ_API_KEY=abc=def==\n"))
    assert os.environ["GROQ_API_KEY"] == "abc=def=="


def test_env_example_parses(clean_env):
    """The shipped template must actually be loadable."""
    from rcp.store import REPO_ROOT
    applied = load_dotenv(REPO_ROOT / ".env.example")
    # Every key in it is blank, so nothing should be configured by it.
    assert applied.get("GROQ_API_KEY") is None
    assert applied.get("RCP_LLM") == "fallback"


def test_every_cli_entry_point_loads_dotenv():
    """A missed import here is invisible until someone runs that command --
    `eval/sensitivity.py` shipped broken for exactly this reason, because no
    test imported its main()."""
    import ast
    from pathlib import Path

    from rcp.store import REPO_ROOT

    entries = ["eval/run.py", "eval/sensitivity.py", "eval/analyst.py",
               "sim/generate.py", "rcp/llm/check.py"]
    for rel in entries:
        tree = ast.parse((REPO_ROOT / rel).read_text())
        names = {n.name for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) for n in node.names}
        assert "load_dotenv" in names, f"{rel} never imports load_dotenv"

        main = next((n for n in tree.body
                     if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
        assert main is not None, f"{rel} has no main()"
        calls = {n.func.id for n in ast.walk(main)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "load_dotenv" in calls, f"{rel}::main does not call load_dotenv()"
