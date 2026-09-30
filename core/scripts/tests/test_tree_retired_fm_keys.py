"""Retiring the node front-matter `parent:` key ().

Covers _tree_fm_retired.py and its four callers:
  - the helper itself: byte-minimal, top-level only, idempotent, refuses what
    it cannot verify;
  - T21 (tree-front-matter-sync.py) strips it on a node write;
  - tree-retired-fm-keys.py: --check census, --dry-run writes nothing, --apply
    is clock-neutral (last_updated and _tree.yaml untouched) and idempotent;
  - coordination_merge: a stripped side and a stale side still carrying the
    key converge, commutatively, instead of freezing;
  - tree.py --validate warns on a carrier.

Subprocess tests pin STORAGE_BACKEND=local and MIND_WORLD=<tmp>, so no write
can reach a shared store (guard-955).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))
import coordination_merge as cm  # noqa: E402
from _tree_fm_retired import (  # noqa: E402
    RetiredKeyStripError, find_retired_fm_keys, strip_retired_fm_keys)

PYTHON = sys.executable

NODE = (
    "---\n"
    "topic: T\n"
    "parent: old-parent\n"
    "last_updated: '2026-01-01'\n"
    "last_update_trigger:\n"
    "  type: goal_execution\n"
    "  source: g-1\n"
    "---\n\n## Alpha\nbody\n"
)


# ------------------------------------------------------------------ helper --
def test_strip_removes_exactly_the_parent_line():
    out, removed = strip_retired_fm_keys(NODE)
    assert removed == ["parent"]
    assert out == NODE.replace("parent: old-parent\n", "")


def test_strip_keeps_crlf_endings_byte_for_byte():
    crlf = NODE.replace("\n", "\r\n")
    out, removed = strip_retired_fm_keys(crlf)
    assert removed == ["parent"]
    assert out == crlf.replace("parent: old-parent\r\n", "")


def test_strip_is_idempotent():
    once, _ = strip_retired_fm_keys(NODE)
    twice, removed = strip_retired_fm_keys(once)
    assert twice == once and removed == []


def test_last_updated_is_never_touched():
    out, _ = strip_retired_fm_keys(NODE)
    assert "last_updated: '2026-01-01'\n" in out


@pytest.mark.parametrize("text", [
    "---\ntopic: T\nlast_update_trigger:\n  parent: x\n---\nb\n",   # nested
    "---\nparentage: x\nparent_key: y\n---\nb\n",                   # other keys
    "---\ntopic: T\n---\nparent: in the body\n",                    # body text
    "parent: x\n",                                                  # no front matter
])
def test_only_a_top_level_front_matter_key_is_retired(text):
    assert find_retired_fm_keys(text) == []
    assert strip_retired_fm_keys(text) == (text, [])


def test_block_value_continuation_goes_with_the_key():
    text = "---\ntopic: T\nparent:\n  - a\n  - b\n\nnext: 1\n---\nb\n"
    out, removed = strip_retired_fm_keys(text)
    assert removed == ["parent"]
    assert out == "---\ntopic: T\n\nnext: 1\n---\nb\n"


def test_refuses_to_empty_the_front_matter():
    with pytest.raises(RetiredKeyStripError):
        strip_retired_fm_keys("---\nparent: x\n---\nbody\n")


def test_refuses_unparseable_front_matter():
    with pytest.raises(RetiredKeyStripError):
        strip_retired_fm_keys("---\ntopic: [unclosed\nparent: x\n---\nbody\n")


# ------------------------------------------------------------ temp world --
def _world(tmp_path, nodes):
    """nodes: {key: text}. Registers each in _tree.yaml under the root."""
    world = tmp_path / "world"
    tree = world / "knowledge" / "tree"
    tree.mkdir(parents=True)
    index = {"root": {"file": None, "depth": 0, "children": list(nodes),
                      "child_count": len(nodes)}}
    for key, text in nodes.items():
        (tree / f"{key}.md").write_bytes(text.encode("utf-8"))
        index[key] = {"file": f"world/knowledge/tree/{key}.md", "depth": 1,
                      "parent": "root", "children": [], "child_count": 0,
                      "last_updated": "2026-01-01"}
    (tree / "_tree.yaml").write_text(yaml.safe_dump({"nodes": index}),
                                     encoding="utf-8")
    return world


def _env(world):
    env = dict(os.environ)
    env.update(MIND_WORLD=str(world), STORAGE_BACKEND="local")
    env.pop("MIND_AGENT", None)
    return env


def _cli(world, *args):
    return subprocess.run(
        [PYTHON, str(CORE_SCRIPTS / "tree-retired-fm-keys.py"), *args],
        capture_output=True, text=True, env=_env(world), timeout=60)


# --------------------------------------------------------------------- T21 --
def test_t21_strips_parent_on_a_node_write(tmp_path):
    world = _world(tmp_path, {"n": NODE.replace("\n", "\r\n")})
    md = world / "knowledge" / "tree" / "n.md"
    r = subprocess.run(
        [PYTHON, str(CORE_SCRIPTS / "tree-front-matter-sync.py"),
         "--file", str(md), "--virtual-path", "world/knowledge/tree/n.md"],
        capture_output=True, text=True, env=_env(world), timeout=60)
    assert r.returncode == 0, r.stderr
    assert "retired key removed: parent" in r.stderr
    out = md.read_bytes()
    assert b"parent:" not in out
    assert b"\r\n" in out and b"\n" not in out.replace(b"\r\n", b"")


# --------------------------------------------------------------------- CLI --
def test_check_counts_carriers_and_skips_archive_and_index(tmp_path):
    world = _world(tmp_path, {"a": NODE, "b": NODE.replace("parent: old-parent\n", "")})
    tree = world / "knowledge" / "tree"
    (tree / ".archive").mkdir()
    (tree / ".archive" / "old.md").write_text(NODE, encoding="utf-8")
    (tree / "_notes.md").write_text(NODE, encoding="utf-8")
    r = _cli(world, "--check")
    assert r.returncode == 1, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["scanned"] == 2 and d["carriers"] == 1
    assert d["files"][0]["path"] == "world/knowledge/tree/a.md"


def test_dry_run_lists_the_line_and_writes_nothing(tmp_path):
    world = _world(tmp_path, {"a": NODE})
    md = world / "knowledge" / "tree" / "a.md"
    before = md.read_bytes()
    r = _cli(world, "--dry-run")
    assert r.returncode == 0, r.stderr
    rec = json.loads(r.stdout)["files"][0]
    assert rec["status"] == "would-strip"
    assert rec["removed_lines"] == ["parent: old-parent"]
    assert md.read_bytes() == before


def test_apply_is_clock_neutral_byte_minimal_and_idempotent(tmp_path):
    world = _world(tmp_path, {"a": NODE, "b": NODE.replace("\n", "\r\n")})
    tree = world / "knowledge" / "tree"
    index_before = (tree / "_tree.yaml").read_bytes()
    r = _cli(world, "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    d = json.loads(r.stdout)
    assert d["by_status"] == {"stripped": 2} and d["remaining"] == 0
    assert (tree / "a.md").read_text(encoding="utf-8") == \
        NODE.replace("parent: old-parent\n", "")
    assert (tree / "b.md").read_bytes() == \
        NODE.replace("\n", "\r\n").replace("parent: old-parent\r\n", "").encode()
    assert (tree / "_tree.yaml").read_bytes() == index_before   # no index bump
    again = _cli(world, "--apply")
    assert again.returncode == 0 and json.loads(again.stdout)["carriers"] == 0
    assert _cli(world, "--check").returncode == 0


# ------------------------------------------------------------ merge handler --
def _side(last_updated, source, parent=True):
    text = NODE.replace("'2026-01-01'", f"'{last_updated}'").replace("g-1", source)
    return (text if parent else text.replace("parent: old-parent\n", "")).encode()


def test_stripped_and_stale_sides_converge_commutatively():
    migrated = _side("2026-09-29", "migration", parent=False)
    stale = _side("2026-09-30", "edit on a stale mirror", parent=True)
    ab = cm.merge_tree_node_md(migrated, stale)
    ba = cm.merge_tree_node_md(stale, migrated)
    assert ab is not None and ba is not None, "a parent-only difference must not freeze"
    assert ab == ba
    assert b"parent:" not in ab


def test_the_convergence_depends_on_the_strip(monkeypatch):
    # POSITIVE CONTROL: with the helper unavailable the same pair refuses,
    # which is the freeze the strip exists to prevent.
    import types
    broken = types.ModuleType("_tree_fm_retired")

    def _raise(_text):
        raise RetiredKeyStripError("disabled")
    broken.strip_retired_fm_keys = _raise
    monkeypatch.setitem(sys.modules, "_tree_fm_retired", broken)
    migrated = _side("2026-09-29", "migration", parent=False)
    stale = _side("2026-09-30", "edit on a stale mirror", parent=True)
    assert cm.merge_tree_node_md(migrated, stale) is None


def test_identical_sides_both_carrying_parent_merge_without_it():
    side = _side("2026-09-29", "same", parent=True)
    out = cm.merge_tree_node_md(side, side)
    assert out is not None and b"parent:" not in out


def test_a_real_difference_outside_the_provenance_keys_still_refuses():
    a = _side("2026-09-29", "s", parent=False)
    b = _side("2026-09-30", "s", parent=True).replace(b"topic: T", b"topic: U")
    assert cm.merge_tree_node_md(a, b) is None


# ---------------------------------------------------------------- validate --
def test_validate_warns_on_a_carrier(tmp_path):
    world = _world(tmp_path, {"a": NODE, "b": NODE.replace("parent: old-parent\n", "")})
    r = subprocess.run([PYTHON, str(CORE_SCRIPTS / "tree.py"), "read", "--validate"],
                       capture_output=True, text=True, env=_env(world), timeout=60)
    assert r.returncode == 0, r.stderr
    warnings = [w for w in json.loads(r.stdout)["warnings"] if "retired key" in w]
    assert warnings == ["Node 'a' front matter carries retired key(s) parent (g-115-11490)"]
