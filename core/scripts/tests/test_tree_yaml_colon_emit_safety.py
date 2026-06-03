"""test_tree_yaml_colon_emit_safety.py — regression for 7 / 8.

Canonical incident (2026-05-21): node cold-start-handshake.summary contained an
unquoted multi-line scalar with 'Option D: client_type...'. PyYAML's default
plain-scalar emitter folded the long value across lines at width=200; the colon
ended up at the start of a continuation line; YAML scanner re-interpreted it as
a mapping-key separator on next read; tree-read.sh failed with ScannerError;
all tree retrieval silently disabled until the node was hand-quoted.

write_tree() in tree.py uses yaml.dump(..., width=200) without a representer
that forces quoted style on strings containing YAML structural markers. The
fix registers a representer that single-quotes any string containing ':' or
'#' so a wrap-then-break shape cannot reach disk.

Test strategy: seed a minimal tree, write a summary long enough to trigger
PyYAML line-wrap AND containing a colon. Re-read with yaml.safe_load and
verify (a) parse succeeds, (b) summary round-trips byte-for-byte. Without
the fix, step (a) raises ScannerError.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable


def _new_tmpworld():
    tmpdir = Path(tempfile.mkdtemp(prefix="tree-colon-emit-"))
    world = tmpdir / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    return tmpdir, world, tree_dir / "_tree.yaml"


def _seed_tree(tree_path, nodes_dict):
    import yaml
    with open(tree_path, "w", encoding="utf-8") as f:
        yaml.dump({"nodes": nodes_dict}, f)


def _run_tree(args, world, stdin_text=None):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True,
        env=env, timeout=30,
    )


def test_summary_with_colon_round_trips_through_emit_and_parse():
    """The canonical reproduction: long summary + colon -> emit -> parse -> match."""
    _, world, tree_path = _new_tmpworld()
    _seed_tree(tree_path, {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": ["child"], "child_count": 1},
        "child": {"file": "world/knowledge/tree/root/child.md",
                  "depth": 1, "parent": "root", "children": [],
                  "capability_level": "EXPLORE"},
    })

    # Mirror the cold-start-handshake shape: a sentence containing "Option X: <something>"
    # long enough to wrap at width=200. Indent + key + value combined exceeds 200
    # cols when the value is ~190+ chars, so this triggers PyYAML's line-wrap.
    bug_summary = (
        "ARC cold-start handshake regression brief covering the the framework Environment "
        "Server gap that was resolved 2026-05-21 via Option D: client_type dispatch "
        "in CollectAyoEnvironmentInBatchesOnStartUp; SAES Lambda reverted as the "
        "wrong path forward."
    )
    # Sanity: ensure the test input would actually hit the wrap boundary
    assert len(bug_summary) > 190, "test fixture too short to trigger wrap"
    assert ":" in bug_summary, "test fixture missing the colon trigger"

    r = _run_tree(["update", "--set", "child", "summary", bug_summary], world)
    assert r.returncode == 0, f"update failed: rc={r.returncode} stderr={r.stderr}"

    # Critical assertion: parse must succeed. Without the fix, this raises
    # yaml.scanner.ScannerError("mapping values are not allowed here") because
    # the wrap put "Option D:" at the start of a continuation line.
    import yaml
    with open(tree_path, "r", encoding="utf-8") as f:
        parsed = yaml.safe_load(f)

    assert parsed is not None, "tree YAML parsed to None"
    assert "nodes" in parsed, f"missing nodes key in parsed: {list(parsed)}"
    assert "child" in parsed["nodes"], f"child node missing: {list(parsed['nodes'])}"

    actual = parsed["nodes"]["child"].get("summary")
    assert actual == bug_summary, (
        f"summary round-trip mismatch.\n  written: {bug_summary!r}\n"
        f"  read   : {actual!r}"
    )


def test_summary_with_hash_round_trips():
    """Hash (#) is another YAML structural marker — comment starter. A summary
    containing '# something' at the start of a continuation line would be
    silently truncated by the parser. Same fix class as the colon case."""
    _, world, tree_path = _new_tmpworld()
    _seed_tree(tree_path, {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": ["child"], "child_count": 1},
        "child": {"file": "world/knowledge/tree/root/child.md",
                  "depth": 1, "parent": "root", "children": [],
                  "capability_level": "EXPLORE"},
    })

    hash_summary = (
        "Audit summary covering the seven framework areas reviewed in this sprint "
        "including knowledge-tree taxonomy review, recurring-goal cadence analysis, "
        "and the pattern-signatures audit (# tagged as inferred-helpful) "
        "with downstream follow-up filings."
    )
    assert len(hash_summary) > 190
    assert "#" in hash_summary

    r = _run_tree(["update", "--set", "child", "summary", hash_summary], world)
    assert r.returncode == 0, f"update failed: rc={r.returncode} stderr={r.stderr}"

    import yaml
    with open(tree_path, "r", encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    actual = parsed["nodes"]["child"].get("summary")
    assert actual == hash_summary, (
        f"summary with '#' round-trip mismatch.\n  written: {hash_summary!r}\n"
        f"  read   : {actual!r}"
    )


def test_short_safe_summary_unchanged():
    """Sanity check: a short summary with no structural markers should still
    emit as a plain (unquoted) scalar — the fix must not over-quote."""
    _, world, tree_path = _new_tmpworld()
    _seed_tree(tree_path, {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": ["child"], "child_count": 1},
        "child": {"file": "world/knowledge/tree/root/child.md",
                  "depth": 1, "parent": "root", "children": [],
                  "capability_level": "EXPLORE"},
    })

    safe_summary = "Short benign summary without any structural markers."
    r = _run_tree(["update", "--set", "child", "summary", safe_summary], world)
    assert r.returncode == 0

    import yaml
    with open(tree_path, "r", encoding="utf-8") as f:
        parsed = yaml.safe_load(f)
    assert parsed["nodes"]["child"]["summary"] == safe_summary
