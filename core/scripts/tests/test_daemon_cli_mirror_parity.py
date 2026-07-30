"""Daemon-CLI mirror field-set parity ().

Daemon endpoints under ``mind_api/src/world/`` hand-maintain mirror constants
and functions that reimplement ``core/scripts`` CLI logic. These mirrors drift
silently when the CLI gains a field, surfacing later as cryptic byte-compat
test diffs (recurring class -- see guard-742 / guard-547 and Bug B of
g-115-1459). guard-742 / guard-547 are REACTIVE (they remind the editor after
the fact). This module is the PROACTIVE structural catch: it asserts each
daemon mirror's field-set EQUALS its CLI counterpart so drift fails a fast,
targeted test instead of a downstream byte-compat diff.

Pure-ast by design: it reads both source files by path and extracts the
field-sets via the ``ast`` module. No daemon spawn, no package import, no CLI
subprocess -- so it runs identically from ``core/scripts/tests`` and
``mind_api/tests`` and cannot be defeated by import/relative-import friction.
The contract it enforces is a SOURCE-level contract (a key added to one side's
constant/function but not the other), which is exactly the drift class.

Mirror pairs covered:
  A. team_state._EMPTY_STATE_DEFAULTS  <-> team-state.py EMPTY_STATE
  B. tree_write._apply_defaults        <-> tree.py apply_defaults
  C. tree_write._CHILD_COPY_FIELDS     <-> tree.py cmd_add_child inline copy tuple
  D. tree_write._UTILITY_RATIO_FIELDS  <-> tree.py _UTILITY_RATIO_FIELDS

Ordered equality is asserted (strictly stronger than set equality): the daemon
writes _tree.yaml / team-state.yaml with sort_keys=False, so insertion ORDER is
part of the byte-compat contract, not just field presence.
"""
from __future__ import annotations

import ast
from pathlib import Path


def _find_repo_root() -> Path:
    """Walk upward for the dir holding BOTH the CLI and daemon mirror sources,
    so this test is independent of which suite directory it lives in."""
    here = Path(__file__).resolve()
    for anc in [here] + list(here.parents):
        if ((anc / "core" / "scripts" / "tree.py").exists()
                and (anc / "mind_api" / "src" / "world" / "tree_write.py").exists()):
            return anc
    raise RuntimeError(
        "repo root not found (need core/scripts/tree.py + "
        "mind_api/src/world/tree_write.py)"
    )


REPO_ROOT = _find_repo_root()
CLI_TREE = REPO_ROOT / "core" / "scripts" / "tree.py"
CLI_TEAM_STATE = REPO_ROOT / "core" / "scripts" / "team-state.py"
DAEMON_TREE_WRITE = REPO_ROOT / "mind_api" / "src" / "world" / "tree_write.py"
DAEMON_TEAM_STATE = REPO_ROOT / "mind_api" / "src" / "world" / "team_state.py"

_RESYNC = ("Re-sync the daemon mirror to the CLI (byte-compat drift class; "
           "g-115-1459, guard-742 / guard-547).")


# --- ast helpers (no import of either module) -------------------------------

def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _module_assign_literal(mod: ast.Module, name: str):
    """ast.literal_eval the value of a module-level ``name = <literal>``."""
    for node in mod.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"module-level assignment {name!r} not found")


def _func(mod: ast.Module, name: str) -> ast.AST:
    """Find a FunctionDef named ``name`` anywhere in the module (incl. nested)."""
    for node in ast.walk(mod):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found")


def _const_str(slice_node: ast.AST):
    """Return the str value of a subscript slice (`out["x"]`), or None.
    Handles both the py>=3.9 (Constant) and py<3.9 (Index) shapes."""
    node = slice_node
    if node.__class__.__name__ == "Index":  # pragma: no cover - py<3.9
        node = node.value  # type: ignore[attr-defined]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ordered_out_keys(func_node: ast.AST, var: str = "out") -> list:
    """Collect, in source order, the string keys assigned via ``out["KEY"] = ...``
    inside a function body. In-order DFS so the list reflects insertion order."""
    keys: list = []

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Assign):
                for tgt in child.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == var):
                        key = _const_str(tgt.slice)
                        if key is not None and key not in keys:
                            keys.append(key)
            visit(child)

    visit(func_node)
    return keys


def _child_copy_inline_tuple(mod: ast.Module) -> list:
    """Extract the CLI inline child-copy field list from cmd_add_child: the
    for-loop whose iterable is a tuple/list of string literals containing the
    stable signature fields ('summary', 'domain_confidence')."""
    fn = _func(mod, "cmd_add_child")
    for node in ast.walk(fn):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            elts = node.iter.elts
            vals = [e.value for e in elts
                    if isinstance(e, ast.Constant) and isinstance(e.value, str)]
            if (len(vals) == len(elts) and len(vals) >= 3
                    and "summary" in vals and "domain_confidence" in vals):
                return vals
    raise AssertionError(
        "CLI child-copy inline tuple not found in cmd_add_child (expected a "
        "for-loop over a string tuple containing 'summary' + 'domain_confidence')"
    )


# --- A. team_state EMPTY_STATE ----------------------------------------------

def test_team_state_empty_state_field_set_parity():
    cli = _module_assign_literal(_module(CLI_TEAM_STATE), "EMPTY_STATE")
    daemon = _module_assign_literal(_module(DAEMON_TEAM_STATE), "_EMPTY_STATE_DEFAULTS")
    cli_keys, daemon_keys = list(cli.keys()), list(daemon.keys())
    assert cli_keys == daemon_keys, (
        "team-state EMPTY_STATE field-set drift: CLI keys "
        f"{cli_keys} != daemon _EMPTY_STATE_DEFAULTS keys {daemon_keys}. " + _RESYNC
    )


# --- B. apply_defaults -------------------------------------------------------

def test_tree_apply_defaults_field_set_parity():
    cli_keys = _ordered_out_keys(_func(_module(CLI_TREE), "apply_defaults"))
    daemon_keys = _ordered_out_keys(_func(_module(DAEMON_TREE_WRITE), "_apply_defaults"))
    assert cli_keys == daemon_keys, (
        "tree apply_defaults default-field drift: CLI keys "
        f"{cli_keys} != daemon _apply_defaults keys {daemon_keys}. " + _RESYNC
    )


# --- C. child-copy field list -----------------------------------------------

def test_child_copy_fields_parity():
    cli_fields = _child_copy_inline_tuple(_module(CLI_TREE))
    daemon_fields = list(
        _module_assign_literal(_module(DAEMON_TREE_WRITE), "_CHILD_COPY_FIELDS")
    )
    assert cli_fields == daemon_fields, (
        "child-copy field-set drift: CLI cmd_add_child copies "
        f"{cli_fields} but daemon _CHILD_COPY_FIELDS is {daemon_fields}. "
        "A field the CLI copies onto a new child node but the daemon does not "
        "is silently dropped from daemon-created nodes. " + _RESYNC
    )


# --- D. utility-ratio field tuple -------------------------------------------

def test_utility_ratio_fields_parity():
    cli_fields = list(
        _module_assign_literal(_module(CLI_TREE), "_UTILITY_RATIO_FIELDS")
    )
    daemon_fields = list(
        _module_assign_literal(_module(DAEMON_TREE_WRITE), "_UTILITY_RATIO_FIELDS")
    )
    assert cli_fields == daemon_fields, (
        "utility-ratio field-set drift: CLI _UTILITY_RATIO_FIELDS "
        f"{cli_fields} != daemon {daemon_fields}. " + _RESYNC
    )


# --- E. distill detector: DELEGATION, not a mirror ---------------------------
# Pairs A-D above are FIELD-SET checks, and that is exactly why this one is
# shaped differently. tree_write._get_distill_candidates was a hand-maintained
# mirror of the whole DETECTOR ALGORITHM, and it drifted four fixes behind while
# every field-set test above stayed green: crit3 ( oversized read-cap)
# absent, interior nodes skipped by an early `continue` (/rb-4648), the
#  sparse-feedback + stale-signal bars missing, and
# `maintain_exempt: distill` (/guard-896) unhonoured. None of those is
# a field, so none was visible to a field-set comparison. Measured cost (foxtrot,
# 2026-07-30, cc-04/Linux, one 1297-node tree, one process): daemon 809 vs CLI
# 566 — a ~40% disagreement between the WRITE path (which feeds post_run_debt and
# gates backlog-mode escalation fleet-wide) and the READ path (`tree-read.sh
# --distill-candidates`, which tree_read.py lists as NOT daemon-served and so
# falls through to the CLI).
#
#  fixed it by DELEGATION rather than by re-syncing the copy: the two
# ctx-dependent seams (config dir, node-.md resolver) are injected into the CLI
# function. So the invariant worth pinning is not "the field-sets match" but
# "there is no second implementation to drift" — assert the daemon function's
# body still delegates. A re-fork fails here immediately instead of surfacing
# months later as a debt figure nobody can reconcile.
def test_distill_candidates_daemon_delegates_to_cli():
    fn = _func(_module(DAEMON_TREE_WRITE), "_get_distill_candidates")
    calls = [n.func.id for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "_cli_get_distill_candidates" in calls, (
        "tree_write._get_distill_candidates no longer delegates to the CLI "
        "detector (core/scripts/tree.py::get_distill_candidates). It was "
        "re-forked into a second implementation, which is the exact drift that "
        "made the daemon report 809 distill candidates while the CLI reported "
        "566 on the same tree (g-115-4062). If the daemon needs caller-specific "
        "behaviour, add a keyword-only seam to the CLI function and inject it "
        "here — do not re-implement the detector. " + _RESYNC
    )
    # The delegation must pass BOTH ctx seams. Dropping either silently
    # re-points the daemon at the CLI module's own PROJECT_ROOT / WORLD_DIR
    # globals, which are wrong for a daemon serving a non-bound agent — a
    # tenant-correctness bug that no count comparison on THIS box would reveal.
    kwargs = {kw.arg for n in ast.walk(fn) if isinstance(n, ast.Call)
              for kw in n.keywords if kw.arg}
    for seam in ("config_dir", "resolve_path"):
        assert seam in kwargs, (
            f"delegation drops the {seam!r} seam: the daemon must resolve this "
            "through its per-request ctx, not the CLI module globals. "
            "A daemon serving a project root other than the CLI module's would "
            "read the wrong path with no local symptom. " + _RESYNC
        )
