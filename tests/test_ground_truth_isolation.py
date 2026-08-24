"""rcp/ must never reach ground truth, and must never be non-deterministic.

Both checks parse the AST rather than grepping text. Comments do not appear in
the AST at all, and docstrings are excluded explicitly, so prose that merely
*mentions* truth.db (as several modules do, to explain the boundary) does not
trip the check while real usage does.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RCP_FILES = sorted(p for p in (REPO_ROOT / "rcp").rglob("*.py"))

# Anything that would let the control plane peek at the outcome model.
FORBIDDEN_IMPORTS = ("sim", "sim.truth_store", "sim.outcomes", "eval")
FORBIDDEN_STRINGS = ("truth.db", "truth_params", "customer_latent", "counterfactuals")

# Anything that would make two runs of the same seed diverge.
NONDETERMINISTIC_CALLS = ("random", "uuid")
# Wall-clock readers. rcp/ may use `datetime` for calendar arithmetic (see
# rcp/timeutil.py) but must never ask what time it is now -- time arrives as an
# explicit now_ms argument.
WALL_CLOCK_ATTRS = ("now", "utcnow", "today", "monotonic", "perf_counter")
NONDETERMINISTIC_SQL = ("random()", "datetime('now')", "current_timestamp", "randomblob")


def _literals(tree: ast.AST) -> list[str]:
    """String constants that are not docstrings."""
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                if isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
    return [
        n.value for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
    ]


def _imports(tree: ast.AST) -> list[str]:
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("path", RCP_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_rcp_never_imports_ground_truth(path: Path):
    tree = ast.parse(path.read_text())
    for name in _imports(tree):
        root = name.split(".")[0]
        assert root not in FORBIDDEN_IMPORTS, (
            f"{path.relative_to(REPO_ROOT)} imports {name}; rcp/ must not reach "
            f"the simulator or the evaluator"
        )


@pytest.mark.parametrize("path", RCP_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_rcp_never_names_the_truth_database(path: Path):
    tree = ast.parse(path.read_text())
    for literal in _literals(tree):
        for needle in FORBIDDEN_STRINGS:
            assert needle not in literal, (
                f"{path.relative_to(REPO_ROOT)} contains the literal {needle!r} "
                f"outside a docstring"
            )


@pytest.mark.parametrize("path", RCP_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_rcp_decision_path_is_deterministic(path: Path):
    """No randomness and no wall clock anywhere under rcp/.

    Time enters the system as an explicit `now_ms` argument threaded down from
    the caller, never read from the environment.
    """
    tree = ast.parse(path.read_text())

    for name in _imports(tree):
        root = name.split(".")[0]
        assert root not in NONDETERMINISTIC_CALLS, (
            f"{path.relative_to(REPO_ROOT)} imports {name}; seed a generator in "
            f"sim/ instead, or derive from content (see store.content_id)"
        )

    for literal in _literals(tree):
        lowered = literal.lower()
        for needle in NONDETERMINISTIC_SQL:
            assert needle not in lowered, (
                f"{path.relative_to(REPO_ROOT)} has SQL containing {needle!r}; "
                f"a decision query must be reproducible"
            )

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in WALL_CLOCK_ATTRS, (
                f"{path.relative_to(REPO_ROOT)} calls .{node.func.attr}(); rcp/ "
                f"must take time as an explicit now_ms argument"
            )


def test_control_plane_runs_against_a_read_only_database(db_path):
    """The strongest form of the guarantee: open rcp.db read-only with
    query_only on, and confirm the read side of the control plane works without
    any access to truth data."""
    from rcp.migrations import migrate
    from rcp.store import claim_pending, close, connect, contact_count
    from tests.conftest import make_chain

    rw = connect(db_path)
    migrate(rw)
    make_chain(rw)
    close(rw)

    ro = connect(db_path, read_only=True)
    try:
        assert ro.execute("PRAGMA query_only").fetchone()[0] == 1
        assert contact_count(ro, "cust_1", 0) == 0
        assert claim_pending(ro, now_ms=9999) == []
        tables = {r[0] for r in ro.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        assert not tables & {"truth_params", "customer_latent", "counterfactuals"}
    finally:
        ro.close()
