"""Meta-test: every daemon tree write-op in VALID_OPS has a paired byte-compat
runtime test (g-115-1497).

Background (rb-1901 / g-115-1493): the daemon write helpers in
``mind_api/src/world/tree_write.py`` hand-mirror ``core/scripts/tree.py`` CLI
logic. Two test layers exist:

  1. AST field-SET parity (``test_daemon_cli_mirror_parity.py``) -- asserts the
     daemon SETS the same fields as the CLI. It inspects field-sets, not runtime
     behavior, so it CANNOT catch behavioral-derivation divergence: node_type
     flips, field injection, count-driven state transitions.
  2. byte-compat RUNTIME tests (``mind_api/tests/test_runtime_tree_write.py``
     ``test_byte_compat_*`` functions) -- seed both worlds, run the REAL CLI +
     the daemon handler, and assert the on-disk ``_tree.yaml`` is byte-identical.
     This is the layer that catches behavioral divergence.

g-115-1493 found that ``_apply_remove_child`` updated ``child_count`` but never
flipped a now-childless parent ``node_type`` interior->leaf -- invisible to the
AST parity layer AND, crucially, to the byte-compat suite, because remove-child
was the ONLY dispatched op with no byte-compat test. That silent coverage gap
hid for ~24 days. A new op can be added to the dispatch (``VALID_OPS``) without
anyone adding its paired byte-compat test, reopening the same gap class.

This meta-test makes that gap class STRUCTURALLY impossible to reintroduce: it
introspects the op dispatch (``VALID_OPS``) and the byte-compat suite's test
names, then asserts EVERY dispatched op key has a paired ``test_byte_compat_<op>``
function -- failing loudly the moment a newly-dispatched op lacks byte-compat
coverage. It is the proactive structural sibling of ``test_daemon_cli_mirror_
parity.py`` (which guards the field-SET layer); together they ratchet both test
layers against silent drift.

Pure-ast by design (mirrors ``test_daemon_cli_mirror_parity.py``): it reads both
source files by path via the ``ast`` module -- no daemon spawn, no package
import, no CLI subprocess -- so it runs identically from ``core/scripts/tests``
and ``mind_api/tests`` and is daemon-safe (no ``daemon_integration`` marker; runs
under ``pytest core/scripts/tests -q -m "not daemon_integration"``, guard-672).
"""
from __future__ import annotations

import ast
from pathlib import Path


def _find_repo_root() -> Path:
    """Walk upward for the dir holding BOTH the daemon dispatch source and the
    byte-compat runtime suite, so this test is independent of which suite
    directory it lives in (same idiom as test_daemon_cli_mirror_parity.py)."""
    here = Path(__file__).resolve()
    for anc in [here] + list(here.parents):
        if ((anc / "mind_api" / "src" / "world" / "tree_write.py").exists()
                and (anc / "mind_api" / "tests"
                     / "test_runtime_tree_write.py").exists()):
            return anc
    raise RuntimeError(
        "repo root not found (need mind_api/src/world/tree_write.py + "
        "mind_api/tests/test_runtime_tree_write.py)"
    )


REPO_ROOT = _find_repo_root()
DAEMON_TREE_WRITE = REPO_ROOT / "mind_api" / "src" / "world" / "tree_write.py"
RUNTIME_SUITE = REPO_ROOT / "mind_api" / "tests" / "test_runtime_tree_write.py"

# The byte-compat runtime tests follow the convention `test_byte_compat_<op>`
# (op token = op key with '-' -> '_'). Variants append a `_<suffix>` (e.g.
# test_byte_compat_remove_child_flips_interior_parent) and still count as
# coverage for their base op.
BYTE_COMPAT_PREFIX = "test_byte_compat_"


# --- ast helpers (no import of either module) -------------------------------

def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_assign_literal(mod: ast.Module, name: str, where: str):
    """ast.literal_eval the value of a module-level ``name = <literal>``."""
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found in {where}")


def _valid_ops() -> set:
    """The authoritative dispatch op-key registry (the `if op == ...` chain in
    handle_tree_write is gated by membership in this set)."""
    return set(_module_assign_literal(
        _module(DAEMON_TREE_WRITE), "VALID_OPS", "tree_write.py"))


def _byte_compat_test_names() -> list:
    """Collected test-function names from the byte-compat runtime suite that
    follow the `test_byte_compat_*` convention."""
    mod = _module(RUNTIME_SUITE)
    return sorted(
        n.name for n in ast.walk(mod)
        if isinstance(n, ast.FunctionDef) and n.name.startswith(BYTE_COMPAT_PREFIX)
    )


def _op_token(op: str) -> str:
    return op.replace("-", "_")


def _uncovered_ops(ops, test_names) -> list:
    """Return the sorted list of ops with NO paired byte-compat test.

    An op is covered iff some test name equals ``test_byte_compat_<token>`` OR
    starts with ``test_byte_compat_<token>_``. The trailing ``_`` boundary
    prevents a short op token from matching a longer op's test (e.g. an op
    ``set`` must not be considered covered by a hypothetical
    ``test_byte_compat_setx`` belonging to an op ``setx``)."""
    uncovered = []
    for op in ops:
        tok = _op_token(op)
        exact = f"{BYTE_COMPAT_PREFIX}{tok}"
        prefix = f"{exact}_"
        if not any(n == exact or n.startswith(prefix) for n in test_names):
            uncovered.append(op)
    return sorted(uncovered)


# --- tests ------------------------------------------------------------------

def test_valid_ops_introspectable():
    ops = _valid_ops()
    assert ops, "VALID_OPS introspected empty -- dispatch registry not found"
    # Anchor the introspection against the two ops whose gap motivated this
    # meta-test (add-child / remove-child node_type flips, rb-1901).
    assert "add-child" in ops, ops
    assert "remove-child" in ops, ops


def test_byte_compat_suite_introspectable():
    names = _byte_compat_test_names()
    assert names, (
        "no test_byte_compat_* functions found in "
        f"{RUNTIME_SUITE} -- the byte-compat runtime layer is missing or was "
        "renamed away from the test_byte_compat_<op> convention")


def test_every_dispatched_op_has_byte_compat_test():
    ops = _valid_ops()
    names = _byte_compat_test_names()
    uncovered = _uncovered_ops(ops, names)
    assert not uncovered, (
        "daemon tree write-op(s) with NO paired byte-compat runtime test: "
        f"{uncovered}. Each op in VALID_OPS (mind_api/src/world/tree_write.py) "
        "MUST have a test_byte_compat_<op> function in "
        "mind_api/tests/test_runtime_tree_write.py that seeds both worlds, runs "
        "the real CLI + daemon handler, and asserts byte-identical _tree.yaml. "
        "A field-set (AST) parity test is NOT sufficient -- it cannot catch "
        "behavioral divergence (node_type flips, field injection, count-driven "
        "transitions). See rb-1901 / g-115-1493 for the gap class this guards.")


def test_synthetic_uncovered_op_is_flagged():
    """Non-vacuity guard (rb-1901 strategy 4): the coverage assertion above must
    FAIL LOUDLY for a newly-dispatched op that has no byte-compat test. Without
    this, a bug in _uncovered_ops that returns [] for everything would make the
    real assertion silently pass forever."""
    names = _byte_compat_test_names()
    augmented = _valid_ops() | {"synthetic-uncovered-op"}
    uncovered = _uncovered_ops(augmented, names)
    assert "synthetic-uncovered-op" in uncovered, (
        "meta-check failed to flag a synthetic uncovered op -- the coverage "
        "assertion would be vacuous")
    # Symmetric positive control: a synthetic op WITH a (synthetic) byte-compat
    # test must NOT be flagged -- proves the matcher recognizes real coverage,
    # not just absence.
    augmented2 = _valid_ops() | {"synthetic-covered-op"}
    names2 = names + ["test_byte_compat_synthetic_covered_op"]
    assert "synthetic-covered-op" not in _uncovered_ops(augmented2, names2)


def test_op_token_boundary_prevents_prefix_collision():
    """A short op token must not be considered covered by a longer op's test
    name. Guards the `_` boundary in _uncovered_ops against a future op pair
    like ('set', 'setx') where test_byte_compat_setx would otherwise spuriously
    cover 'set'."""
    # 'set' is genuinely covered (test_byte_compat_set_field); but if its only
    # candidate were a longer-op test, the boundary must reject it.
    assert _uncovered_ops({"set"}, ["test_byte_compat_setx_roundtrip"]) == ["set"]
    assert _uncovered_ops({"set"}, ["test_byte_compat_set_field"]) == []
