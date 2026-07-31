"""test_tree_body_presence_warn.py —  (asp-115).

Pins the body-presence advisory on the tree registration/enrichment surface.
Before the fix, cmd_add_child / cmd_set / cmd_batch (and the daemon mirrors in
mind_api/src/world/tree_write.py) recorded or enriched a node's `file:` path
without ever checking a body exists there — so a node could be registered,
enriched, and retrieved indefinitely with no retrievable content (measured
2026-07-30: 3 of 1298 nodes desync, 36 retrievals against absent bodies, the
producer still active).

The fix is a LOUD-WARN, never a refusal (guard-1562 enumeration: the canonical
/tree add flow authors the body BEFORE registering — silent there; 8+
register-then-author callers would break under fail-closed). Contract pinned
here:

  * CLI: one stderr line containing "body-presence g-115-4140" when the
    touched node's body is absent from the LOCAL mirror; exit code and stdout
    JSON are UNCHANGED in both directions.
  * Daemon: additive `body_presence_warning` response key (list form
    `body_presence_warnings` for batch); absent when the body exists or when
    the same request wrote the body (`body` field → md_written).

Modeled on test_tree_add_child_node_type_derive.py (CLI subprocess against a
tmp world) + test_l1_pick.py (DaemonFixture round-trip). sys.executable, not
ambient python3 (the test_run_full_suite_wrapper hermeticity lesson).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable

WARN_TOKEN = "body-presence g-115-4140"


def _seed_tree(world: Path, extra_nodes=None, parent_body=True):
    """Seed _tree.yaml with a `parent` node (EXPLOIT keeps the child-limit gate
    clear). parent_body=True also authors the parent's .md so parent-side
    bookkeeping never trips the advisory being tested on the child."""
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    nodes = {
        "parent": {
            "file": "world/knowledge/tree/parent.md", "depth": 0,
            "children": [], "child_count": 0,
            "capability_level": "EXPLOIT", "node_type": "leaf",
        },
    }
    if extra_nodes:
        nodes.update(extra_nodes)
    (tree_dir / "_tree.yaml").write_text(
        yaml.safe_dump({"nodes": nodes}), encoding="utf-8")
    if parent_body:
        (tree_dir / "parent.md").write_text("# parent\n", encoding="utf-8")
    return tree_dir


def _run_tree(args, stdin_text, world: Path, meta: Path):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True, env=env, timeout=30,
    )


# --- CLI: add-child ------------------------------------------------------------

def test_add_child_bodiless_warns_but_registers(tmp_path):
    """Register-without-body (the defect class) → ONE stderr advisory; the
    write still succeeds and stdout JSON is intact (advisory, never refusal)."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)
    meta.mkdir()

    r = _run_tree(["update", "--add-child", "parent", "--no-dedup"],
                  json.dumps({"key": "child-a", "summary": "bodiless probe"}),
                  world, meta)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["key"] == "child-a"
    assert WARN_TOKEN in r.stderr, "absent-body registration did not warn"
    assert "child-a" in r.stderr, "warning does not name the node"
    tree = yaml.safe_load((world / "knowledge" / "tree" / "_tree.yaml").read_text())
    assert "child-a" in tree["nodes"], "advisory must never block the write"


def test_add_child_body_first_is_silent(tmp_path):
    """The canonical /tree add flow (body authored BEFORE registration) must
    see no advisory — the warn is high-precision by construction."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_dir = _seed_tree(world)
    meta.mkdir()
    # computed child path: parent.md → parent/child-b.md
    (tree_dir / "parent").mkdir()
    (tree_dir / "parent" / "child-b.md").write_text("# child-b\n", encoding="utf-8")

    r = _run_tree(["update", "--add-child", "parent", "--no-dedup"],
                  json.dumps({"key": "child-b", "summary": "body-first probe"}),
                  world, meta)
    assert r.returncode == 0, r.stderr
    assert WARN_TOKEN not in r.stderr, (
        "body-first add must not warn: " + r.stderr)


# --- CLI: set ------------------------------------------------------------------

def test_set_on_bodiless_node_warns(tmp_path):
    """Enriching a bodiless node is the desync signature (registered, enriched,
    retrieved, no content) — must warn."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world, extra_nodes={
        "ghost": {"file": "world/knowledge/tree/ghost.md", "depth": 0,
                  "children": [], "child_count": 0,
                  "capability_level": "EXPLOIT", "node_type": "leaf"},
    })
    meta.mkdir()

    r = _run_tree(["update", "--set", "ghost", "summary", "enriched while bodiless"],
                  None, world, meta)
    assert r.returncode == 0, r.stderr
    assert WARN_TOKEN in r.stderr and "ghost" in r.stderr
    out = json.loads(r.stdout)
    assert out["summary"] == "enriched while bodiless"


def test_set_on_bodied_node_is_silent(tmp_path):
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)  # parent.md exists
    meta.mkdir()

    r = _run_tree(["update", "--set", "parent", "summary", "normal enrichment"],
                  None, world, meta)
    assert r.returncode == 0, r.stderr
    assert WARN_TOKEN not in r.stderr, r.stderr


# --- CLI: batch ----------------------------------------------------------------

def test_batch_warns_only_for_bodiless_targets(tmp_path):
    """Batch add-child (bodiless) + set on a bodied node → exactly one advisory,
    naming the bodiless child; the bodied set target stays silent."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    _seed_tree(world)
    meta.mkdir()

    ops = {"operations": [
        {"op": "add-child", "key": "parent",
         "child": {"key": "child-c", "summary": "batch bodiless"}},
        {"op": "set", "key": "parent", "field": "summary",
         "value": "batch enrichment"},
    ]}
    r = _run_tree(["update", "--batch", "--no-dedup"], json.dumps(ops), world, meta)
    assert r.returncode == 0, r.stderr
    warn_lines = [ln for ln in r.stderr.splitlines() if WARN_TOKEN in ln]
    assert len(warn_lines) == 1, r.stderr
    assert "child-c" in warn_lines[0]
    assert "'parent'" not in warn_lines[0]


# --- daemon mirror -------------------------------------------------------------

def test_daemon_add_child_carries_warning_key(tmp_path):
    """POST /v1/tree/write op=add-child without `body` → 200 + additive
    `body_presence_warning` key; with `body` → md written, no key."""
    import urllib.request
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump({
        "nodes": {
            "root": {"depth": 0, "file": "world/knowledge/tree/root.md",
                     "children": [], "child_count": 0,
                     "capability_level": "EXPLOIT"},
        },
    }, sort_keys=False), encoding="utf-8")
    (tree_dir / "root.md").write_text("# root\n", encoding="utf-8")

    def _post(df, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{df.port}/v1/tree/write",
            data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"X-Mind-Agent": "alpha",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            return json.loads(resp.read().decode("utf-8"))

    with DaemonFixture(world, agent="alpha") as df:
        out = _post(df, {"op": "add-child", "parent": "root",
                         "child": {"key": "bodiless-child",
                                   "summary": "daemon probe"},
                         "no_dedup": True})
        assert out["ok"] is True
        assert WARN_TOKEN in out.get("body_presence_warning", ""), out

        out2 = _post(df, {"op": "add-child", "parent": "root",
                          "child": {"key": "bodied-child",
                                    "summary": "daemon probe 2"},
                          "body": "# bodied-child\n",
                          "no_dedup": True})
        assert out2["ok"] is True and out2["md_written"] is True
        assert "body_presence_warning" not in out2, out2
