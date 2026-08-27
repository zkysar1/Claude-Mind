"""POST /v1/tree/write — daemon tree writer tests.

Two layers:
  1. HTTP round-trip (running_daemon, conftest world): the endpoint is wired
     and add-child / set / increment / remove-child + error cases work
     end-to-end through the real server.
  2. Byte-compat (direct handler vs the REAL CLI tree.py): the on-disk
     _tree.yaml the daemon writes is byte-identical to what the CLI writes.
     This guards the helpers copied verbatim into tree_write.py and the
     CSafeDumper params against drift from core/scripts/tree.py.

The CLI subprocess is driven with MIND_WORLD / MIND_META env overrides
(core/scripts/_paths.py honours MIND_WORLD > WORLD_PATH) and sys.executable
(bypasses the Windows python3 Microsoft-Store stub), so it writes to a temp
world and never touches the real tree.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
TREE_PY = REPO_ROOT / "core" / "scripts" / "tree.py"

_HAS_LIBYAML = bool(yaml) and hasattr(yaml, "CSafeDumper")

# Baseline tree shared by both the CLI and daemon sides of the byte-compat
# test. An L1 parent WITH a real `file` (so compute_child_path doesn't hit a
# null), capability CALIBRATE (child-limit 4, so a single add passes the CLI
# gate without --no-dedup), zero children (under the limit).
_BASELINE_TREE = (
    "nodes:\n"
    "  root:\n"
    "    file: null\n"
    "    summary: Root node\n"
    "    depth: 0\n"
    "    children:\n"
    "    - intelligence\n"
    "    child_count: 1\n"
    "  intelligence:\n"
    "    file: world/knowledge/tree/intelligence.md\n"
    "    summary: Intelligence L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    capability_level: CALIBRATE\n"
    "    confidence: 0.5\n"
    "    retrieval_count: 3\n"
    "    times_helpful: 1\n"
    "last_updated: '2026-01-01'\n"
    "entity_index: {}\n"
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(port: int, path: str, body: dict, *, agent: str = "alpha"):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if agent:
        req.add_header("X-Mind-Agent", agent)
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status, resp.read().decode("utf-8")


def _read_tree(world: Path) -> dict:
    p = world / "knowledge" / "tree" / "_tree.yaml"
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# HTTP round-trip tests (conftest world)
# ---------------------------------------------------------------------------

def test_add_child_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "add-child",
        "parent": "beta-other-node",
        "child": {"key": "gamma-child", "summary": "a gamma child node"},
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["key"] == "gamma-child"
    assert data["node"]["depth"] == 2          # beta-other-node is depth 1
    assert data["node"]["parent"] == "beta-other-node"
    # Default fields applied.
    assert data["node"]["capability_level"] == "EXPLORE" or \
        data["node"]["capability_level"]      # inherited or default
    # On disk: child present, parent's children list updated.
    tree = _read_tree(world)
    assert "gamma-child" in tree["nodes"]
    assert "gamma-child" in tree["nodes"]["beta-other-node"]["children"]


def test_add_child_with_body_writes_md(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    md_body = "---\nkey: doc-child\n---\n\n## Decision Rules\n- do the thing\n"
    status, body = _post(port, "/v1/tree/write", {
        "op": "add-child",
        "parent": "alpha-test-node",
        "child": {"key": "doc-child", "summary": "node with a body"},
        "body": md_body,
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["md_written"] is True
    md_path = world / "knowledge" / "tree" / "alpha-test-node" / "doc-child.md"
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8") == md_body


def test_add_child_dedup_reject_409(running_daemon):
    """Two siblings with identical summaries → the second is rejected by the
    dedup gate. alpha-test-node is depth 1, so children land at depth 2 — at
    the default enforce_from_depth=2, the gate is active."""
    _, port = running_daemon
    summary = "machine learning neural network training data"
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "dedup-a", "summary": summary}})
    try:
        _post(port, "/v1/tree/write", {
            "op": "add-child", "parent": "alpha-test-node",
            "child": {"key": "dedup-b", "summary": summary}})
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert json.loads(e.read())["error"] == "dedup_reject"
    else:
        raise AssertionError("expected 409 for a near-duplicate sibling summary")


def test_add_child_dedup_bypass(running_daemon):
    """`no_dedup` lets an overlapping sibling through (mirrors --no-dedup)."""
    _, port = running_daemon
    summary = "reinforcement policy gradient optimization method"
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "dedup-c", "summary": summary}})
    status, body = _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "dedup-d", "summary": summary}, "no_dedup": True})
    assert status == 200, body


def test_add_child_child_limit_409(running_daemon):
    """alpha-test-node has no capability_level → defaults to CALIBRATE
    (limit 4). 2-token summaries fall below min_tokens_for_overlap=3 so dedup
    accepts each (summary_too_short), isolating the child-limit gate: the 5th
    add hits the cap and is rejected."""
    _, port = running_daemon
    for i in range(4):
        status, body = _post(port, "/v1/tree/write", {
            "op": "add-child", "parent": "alpha-test-node",
            "child": {"key": f"cl-{i}", "summary": f"node{i} item{i}"}})
        assert status == 200, body
    try:
        _post(port, "/v1/tree/write", {
            "op": "add-child", "parent": "alpha-test-node",
            "child": {"key": "cl-overflow", "summary": "node9 item9"}})
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert json.loads(e.read())["error"] == "child_limit_reject"
    else:
        raise AssertionError("expected 409 when parent hits its child cap")


def test_add_child_accept_overflow(running_daemon):
    """`accept_overflow` writes a tree-debt entry and allows an over-cap add."""
    project_root, port = running_daemon
    for i in range(4):
        _post(port, "/v1/tree/write", {
            "op": "add-child", "parent": "alpha-test-node",
            "child": {"key": f"ao-{i}", "summary": f"alpha{i} bravo{i}"}})
    status, body = _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "ao-overflow", "summary": "alpha9 bravo9"},
        "accept_overflow": "consolidation deferred to a dedicated goal"})
    assert status == 200, body
    debt = project_root / "world" / "tree-debt.jsonl"
    assert debt.exists()
    text = debt.read_text(encoding="utf-8")
    assert "alpha-test-node" in text
    assert "consolidation deferred" in text


def test_set_field_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "set", "key": "alpha-test-node",
        "field": "summary", "value": "updated summary text",
    })
    assert status == 200, body
    tree = _read_tree(world)
    assert tree["nodes"]["alpha-test-node"]["summary"] == "updated summary text"
    #  Option B (ported to the daemon in ): a metadata set
    # must NOT auto-bump per-node last_updated — node .md front matter is the
    # source of truth and the index syncs to it only via
    # tree-front-matter-sync.py. The baseline node carries no last_updated, so
    # after a summary set it must still be absent. (This assertion previously
    # pinned the retired auto-bump behavior.)
    assert "last_updated" not in tree["nodes"]["alpha-test-node"]
    # The index-level stamp IS retained.
    assert tree.get("last_updated")


def test_set_confidence_propagates(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "set", "key": "alpha-test-node",
        "field": "confidence", "value": 0.9,
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["key"] == "alpha-test-node"
    tree = _read_tree(world)
    assert tree["nodes"]["alpha-test-node"]["confidence"] == 0.9
    # Self-graduation ran: response carries the propagation result arrays.
    assert "ancestors_updated" in data["node"]
    assert "capability_changes" in data["node"]


def test_propagate_roundtrip(running_daemon):
    project_root, port = running_daemon
    status, body = _post(port, "/v1/tree/write", {
        "op": "propagate", "key": "alpha-test-node",
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["source_node"] == "alpha-test-node"
    assert "ancestors_updated" in data and "capability_changes" in data


def test_propagate_missing_key_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {"op": "propagate"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 for propagate without 'key'")


def test_reconcile_capabilities_roundtrip(running_daemon):
    project_root, port = running_daemon
    status, body = _post(port, "/v1/tree/write", {
        "op": "reconcile-capabilities",
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["op"] == "reconcile-capabilities"
    assert data["total_nodes"] >= 2
    assert isinstance(data["changes"], list)


def test_reparent_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    # Build a self-contained subtree via add-child (which sets parent pointers):
    #   alpha-test-node ─┬─ rp-src-parent ── rp-movable
    #                    └─ rp-dst-parent
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "rp-src-parent", "summary": "src parent"}})
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "rp-dst-parent", "summary": "dst parent"}})
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "rp-src-parent",
        "child": {"key": "rp-movable", "summary": "the node that moves"}})

    status, body = _post(port, "/v1/tree/write", {
        "op": "reparent", "key": "rp-movable", "new_parent": "rp-dst-parent"})
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["reparented"] == "rp-movable"
    assert data["old_parent"] == "rp-src-parent"
    assert data["new_parent"] == "rp-dst-parent"
    # On disk: parent pointer + both children lists updated.
    tree = _read_tree(world)
    assert tree["nodes"]["rp-movable"]["parent"] == "rp-dst-parent"
    assert "rp-movable" not in tree["nodes"]["rp-src-parent"]["children"]
    assert "rp-movable" in tree["nodes"]["rp-dst-parent"]["children"]
    # Subtree file path recomputed under the new parent.
    assert tree["nodes"]["rp-movable"]["file"].endswith(
        "rp-dst-parent/rp-movable.md")


def test_reparent_circular_409(running_daemon):
    _, port = running_daemon
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "circ-top", "summary": "top"}})
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "circ-top",
        "child": {"key": "circ-mid", "summary": "mid"}})
    # Reparent circ-top under its own descendant circ-mid → circular.
    try:
        _post(port, "/v1/tree/write", {
            "op": "reparent", "key": "circ-top", "new_parent": "circ-mid"})
    except urllib.error.HTTPError as e:
        assert e.code == 409
        assert json.loads(e.read())["error"] == "circular_reparent"
    else:
        raise AssertionError("expected 409 for a circular reparent")


def test_reparent_self_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {
            "op": "reparent", "key": "alpha-test-node",
            "new_parent": "alpha-test-node"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_reparent"
    else:
        raise AssertionError("expected 400 for reparent-to-self")


def test_reparent_missing_param_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {
            "op": "reparent", "key": "alpha-test-node"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 for reparent without 'new_parent'")


def test_reparent_node_not_found_404(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {
            "op": "reparent", "key": "no-such-node-xyz",
            "new_parent": "alpha-test-node"})
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read())["error"] == "node_not_found"
    else:
        raise AssertionError("expected 404 for reparent of a missing node")


def test_batch_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "batch",
        "operations": [
            {"op": "add-child", "key": "alpha-test-node",
             "child": {"key": "batch-k1", "summary": "alpha one"}},
            {"op": "set", "key": "alpha-test-node", "field": "summary",
             "value": "batched summary update"},
            {"op": "increment", "key": "alpha-test-node",
             "field": "retrieval_count"},
        ]})
    assert status == 200, body
    data = json.loads(body)
    assert data["ok"] and data["op"] == "batch"
    tree = _read_tree(world)
    assert "batch-k1" in tree["nodes"]
    assert tree["nodes"]["alpha-test-node"]["summary"] == "batched summary update"
    assert tree["nodes"]["alpha-test-node"]["retrieval_count"] == 1


def test_batch_propagate_runs_last(running_daemon):
    """A propagate op listed BEFORE a set still sees the set's effect, because
    propagate ops are deferred to phase 2."""
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "batch",
        "operations": [
            {"op": "propagate", "key": "alpha-test-node"},
            {"op": "set", "key": "alpha-test-node", "field": "confidence",
             "value": 0.9},
        ]})
    assert status == 200, body
    data = json.loads(body)
    # The propagate result reflects confidence 0.9 (set applied first despite
    # being listed second).
    assert data["propagate"], "propagate results should be present"
    tree = _read_tree(world)
    assert tree["nodes"]["alpha-test-node"]["confidence"] == 0.9


def test_batch_invalid_op_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {
            "op": "batch",
            "operations": [{"op": "frobnicate", "key": "alpha-test-node"}]})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "invalid_operation"
    else:
        raise AssertionError("expected 400 for an invalid batch op")


def test_batch_nonexistent_node_404(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {
            "op": "batch",
            "operations": [{"op": "set", "key": "ghost-node",
                            "field": "summary", "value": "x"}]})
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read())["error"] == "node_not_found"
    else:
        raise AssertionError("expected 404 for a non-existent node in batch")


def test_batch_empty_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {"op": "batch", "operations": []})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        assert json.loads(e.read())["error"] == "missing_param"
    else:
        raise AssertionError("expected 400 for an empty batch")


def test_batch_atomicity_on_failure(running_daemon):
    """A phase-1 failure (duplicate child key, AFTER an earlier set mutated the
    in-memory tree) must write NOTHING — the whole batch is unwritten."""
    project_root, port = running_daemon
    world = project_root / "world"
    before = _read_tree(world)["nodes"]["alpha-test-node"].get("summary")
    try:
        _post(port, "/v1/tree/write", {
            "op": "batch",
            "operations": [
                {"op": "set", "key": "alpha-test-node", "field": "summary",
                 "value": "SHOULD NOT PERSIST"},
                {"op": "add-child", "key": "alpha-test-node",
                 "child": {"key": "beta-other-node", "summary": "dup key"}},
            ]})
    except urllib.error.HTTPError as e:
        assert e.code == 409
    else:
        raise AssertionError("expected 409 — duplicate child key in batch")
    after = _read_tree(world)["nodes"]["alpha-test-node"].get("summary")
    assert after == before, "batch must be atomic — phase-1 failure wrote nothing"


def test_increment_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    status, body = _post(port, "/v1/tree/write", {
        "op": "increment", "key": "alpha-test-node", "field": "retrieval_count",
    })
    assert status == 200, body
    tree = _read_tree(world)
    node = tree["nodes"]["alpha-test-node"]
    assert node["retrieval_count"] == 1
    # utility_ratio recomputed because retrieval_count is a utility field.
    assert "utility_ratio" in node and "utility_ratio_v2" in node


def test_remove_child_roundtrip(running_daemon):
    project_root, port = running_daemon
    world = project_root / "world"
    # First add a leaf, then remove it (removing a node with descendants is
    # refused; a fresh leaf is safe).
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "temp-leaf", "summary": "temp"},
    })
    status, body = _post(port, "/v1/tree/write", {
        "op": "remove-child", "parent": "alpha-test-node", "child_key": "temp-leaf",
    })
    assert status == 200, body
    tree = _read_tree(world)
    assert "temp-leaf" not in tree["nodes"]
    assert "temp-leaf" not in tree["nodes"]["alpha-test-node"]["children"]


def test_remove_child_refuses_orphaning(running_daemon):
    _, port = running_daemon
    # beta-other-node has child alpha-test-node — removing it from root would
    # orphan the subtree. (root lists beta-other-node? conftest: beta lists
    # alpha as a child; we attempt to remove beta from a parent that lists it.)
    # Build the situation: add parent->child->grandchild, then try removing child.
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "mid-node", "summary": "mid"},
    })
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "mid-node",
        "child": {"key": "leaf-node", "summary": "leaf"},
    })
    try:
        _post(port, "/v1/tree/write", {
            "op": "remove-child", "parent": "alpha-test-node", "child_key": "mid-node",
        })
    except urllib.error.HTTPError as e:
        assert e.code == 409
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "would_orphan_subtree"
    else:
        raise AssertionError("expected 409 when removal would orphan a subtree")


def test_invalid_op_400(running_daemon):
    _, port = running_daemon
    try:
        _post(port, "/v1/tree/write", {"op": "frobnicate", "key": "x"})
    except urllib.error.HTTPError as e:
        assert e.code == 400
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "invalid_op"
    else:
        raise AssertionError("expected 400 for an unknown op")


def test_add_child_duplicate_key_409(running_daemon):
    _, port = running_daemon
    # beta-other-node exists (parent check passes); alpha-test-node already
    # exists as a node (duplicate-key check fires → 409).
    try:
        _post(port, "/v1/tree/write", {
            "op": "add-child", "parent": "beta-other-node",
            "child": {"key": "alpha-test-node", "summary": "dup"},
        })
    except urllib.error.HTTPError as e:
        assert e.code == 409
    else:
        raise AssertionError("expected 409 for a duplicate node key")


# ---------------------------------------------------------------------------
# Byte-compat: daemon handler output == real CLI output
# ---------------------------------------------------------------------------

class _FakePaths:
    def __init__(self, world: Path, project_root: Path = REPO_ROOT,
                 meta: Path | None = None):
        self.world = world
        # project_root drives _load_competence_config (reads
        # core/config/tree.yaml). Pointing it at the real repo makes the
        # daemon read the SAME competence_mapping the CLI subprocess reads.
        self.project_root = project_root
        # meta drives _merged_config's config-overrides overlay (reparent's
        # D_max gate). Point it at an EMPTY dir so the overlay no-ops exactly
        # as the CLI does when MIND_META has no config-overrides.yaml — both
        # sides then read D_max from the same real core/config/tree.yaml.
        self.meta = meta


class _FakeCtx:
    def __init__(self, world: Path, body: dict, agent: str = "alpha",
                 project_root: Path = REPO_ROOT, meta: Path | None = None):
        self.paths = _FakePaths(world, project_root, meta)
        self.body = json.dumps(body).encode("utf-8")
        self.headers = {"x-mind-agent": agent}
        self.query = {}


def _seed_world(base: Path, name: str) -> Path:
    world = base / name
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        _BASELINE_TREE, encoding="utf-8")
    return world


def _run_cli(world: Path, meta: Path, args: list, stdin_text: str | None):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["MIND_AGENT"] = "alpha"
    meta.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(TREE_PY), "update", *args],
        input=stdin_text, text=True, env=env, cwd=str(REPO_ROOT),
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"CLI tree.py failed (rc={proc.returncode}):\n"
        f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


_STAMP_SENTINEL = "<AMEND-STAMP>"


def _tree_bytes(world: Path) -> bytes:
    """Raw _tree.yaml bytes, with per-field amendment stamp VALUES neutralized.

    THIS IS NOT A WEAKENING OF BYTE-COMPAT, and the distinction is the whole
    reason it is scoped this narrowly. The contract these tests enforce is that
    the CLI and daemon hand-mirrors emit the SAME BYTES for the same operation. A
    wall-clock stamp is the one thing they legitimately cannot agree on: the CLI
    runs as a subprocess and the daemon writes in-process a fraction of a second
    later, so g-115-5411's `amended_fields` stamp differs whenever the two writes
    straddle a second boundary. That stamp is SECOND-granular by necessity — a
    date-granular one would reproduce the very same-day merge tie it exists to
    break — unlike the deliberately date-granular progression/calibration stamps.

    MEASURED before this normalization existed (cc-07, 2026-08-21):
    ``test_byte_compat_set_field`` failed 1 run in 15 (~7%), the observed diff
    being a single seconds digit. That test sets ``summary``, a BASE field, so
    every future BASE-field byte-compat case inherits the same flake. One green
    run does not detect this; it took a repeat loop.

    THE KEY SET IS STILL COMPARED, which is the half that catches real drift.
    Only the VALUE of each stamp is replaced, and the replacement is keyed on the
    exact ``<field>: <value>`` pair read back from the parsed document — so a
    writer that stops stamping, or stamps a DIFFERENT set of fields, still
    produces a different key set and still fails. That is precisely the
    g-115-2422 shape (the CLI dropped a stamp, the daemon kept it, 19 days).
    Formatting, ordering, and every other field remain compared byte-for-byte.
    """
    raw = (world / "knowledge" / "tree" / "_tree.yaml").read_bytes()
    if yaml is None:
        return raw
    try:
        doc = yaml.safe_load(raw) or {}
    except Exception:  # pragma: no cover - a malformed dump must still diff
        return raw
    for node in (doc.get("nodes") or {}).values():
        if not isinstance(node, dict):
            continue
        stamps = node.get("amended_fields")
        if not isinstance(stamps, dict):
            continue
        for fld, val in stamps.items():
            if not isinstance(val, str):
                continue
            # Both quoting styles the dumper may choose, plus bare. Scoped to the
            # field NAME as well as the value so an unrelated field that happens
            # to carry the same timestamp string is not touched.
            for form in (f"{fld}: '{val}'", f'{fld}: "{val}"', f"{fld}: {val}"):
                raw = raw.replace(
                    form.encode("utf-8"),
                    f"{fld}: {_STAMP_SENTINEL}".encode("utf-8"))
    return raw


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_add_child(tmp_path):
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    child = {"key": "test-child", "summary": "a freshly added child node"}
    # CLI: tree.py update --add-child intelligence  (child JSON on stdin)
    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--add-child", "intelligence"], json.dumps(child))
    # Daemon: direct handler call against an identical baseline.
    tree_write.write(_FakeCtx(dae_world, {
        "op": "add-child", "parent": "intelligence", "child": child}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


# Baseline with an EXPLICIT node_type=leaf parent so add-child exercises the
#  leaf->interior flip (the _BASELINE_TREE parent has no node_type,
# so .get("node_type") != "leaf" and the flip is a no-op there).
_LEAF_PARENT_TREE = (
    "nodes:\n"
    "  root:\n"
    "    file: null\n"
    "    summary: Root node\n"
    "    depth: 0\n"
    "    children:\n"
    "    - intelligence\n"
    "    child_count: 1\n"
    "  intelligence:\n"
    "    file: world/knowledge/tree/intelligence.md\n"
    "    summary: Intelligence L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    node_type: leaf\n"
    "    capability_level: CALIBRATE\n"
    "    confidence: 0.5\n"
    "    retrieval_count: 3\n"
    "    times_helpful: 1\n"
    "last_updated: '2026-01-01'\n"
    "entity_index: {}\n"
)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_add_child_flips_leaf_parent(tmp_path):
    """: add-child onto an EXPLICIT node_type=leaf parent must flip the
    parent leaf->interior (g-115-1437) in the daemon exactly as the CLI does. The
    pre-fix daemon _apply_add_child left the parent node_type stale; this
    byte-compat add proves the flip lands identically, and the explicit assertion
    pins the parent is 'interior' on disk."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world_text(tmp_path, "cli", _LEAF_PARENT_TREE)
    dae_world = _seed_world_text(tmp_path, "dae", _LEAF_PARENT_TREE)

    child = {"key": "test-child", "summary": "a freshly added child node"}
    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--add-child", "intelligence"], json.dumps(child))
    tree_write.write(_FakeCtx(dae_world, {
        "op": "add-child", "parent": "intelligence", "child": child}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)
    # Explicit: the parent flipped leaf -> interior on the daemon side.
    assert _read_tree(dae_world)["nodes"]["intelligence"]["node_type"] == "interior"


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_add_child_injects_origin_goal_id(tmp_path):
    """: add-child with NO explicit origin_goal_id auto-injects the
    EXECUTING goal id from world/team-state.yaml in_flight (g-325-06 / g-115-1463)
    in the daemon exactly as the CLI does. The pre-fix daemon dropped the inject;
    seeding BOTH worlds with an identical in_flight goal proves the daemon now
    records the same origin signal, and the explicit assertion pins origin_goal_id
    on the child."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")
    # Identical in_flight goal for agent 'alpha' in BOTH worlds. The CLI reads
    # WORLD_DIR/team-state.yaml (MIND_WORLD=cli_world, MIND_AGENT=alpha); the
    # daemon reads world_path/team-state.yaml with the per-request agent. The
    # file lives at the world ROOT, outside knowledge/tree, so _tree_bytes is
    # unaffected by its presence — it is purely an inject INPUT.
    team_state = ("agent_status:\n"
                  "  alpha:\n"
                  "    in_flight:\n"
                  "      goal_id: g-999-42\n")
    (cli_world / "team-state.yaml").write_text(team_state, encoding="utf-8")
    (dae_world / "team-state.yaml").write_text(team_state, encoding="utf-8")

    child = {"key": "test-child", "summary": "a freshly added child node"}
    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--add-child", "intelligence"], json.dumps(child))
    tree_write.write(_FakeCtx(dae_world, {
        "op": "add-child", "parent": "intelligence", "child": child},
        agent="alpha"))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)
    # Explicit: the child carries the in_flight goal id as origin_goal_id.
    assert _read_tree(dae_world)["nodes"]["test-child"]["origin_goal_id"] == "g-999-42"


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_set_field(tmp_path):
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--set", "intelligence", "summary", "Reworded summary"], None)
    tree_write.write(_FakeCtx(dae_world, {
        "op": "set", "key": "intelligence",
        "field": "summary", "value": "Reworded summary"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_increment(tmp_path):
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--increment", "intelligence", "retrieval_count"], None)
    tree_write.write(_FakeCtx(dae_world, {
        "op": "increment", "key": "intelligence", "field": "retrieval_count"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


# Baseline with a deliberately-stale capability_level so reconcile-capabilities
# exercises a real change (confidence 0.6 vs the CLI's competence thresholds).
_STALE_CAP_TREE = (
    "nodes:\n"
    "  root:\n"
    "    file: null\n"
    "    summary: Root node\n"
    "    depth: 0\n"
    "    children:\n"
    "    - intelligence\n"
    "    child_count: 1\n"
    "  intelligence:\n"
    "    file: world/knowledge/tree/intelligence.md\n"
    "    summary: Intelligence L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.6\n"
    "last_updated: '2026-01-01'\n"
    "entity_index: {}\n"
)


def _seed_world_text(base: Path, name: str, tree_text: str) -> Path:
    world = base / name
    (world / "knowledge" / "tree").mkdir(parents=True)
    (world / "knowledge" / "tree" / "_tree.yaml").write_text(
        tree_text, encoding="utf-8")
    return world


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_set_confidence(tmp_path):
    """The headline Batch-3 path: set field=confidence runs _propagate_in_memory
    up the parent chain + self-graduation. Both sides read the SAME real
    core/config/tree.yaml competence_mapping, so the capability_level strings
    and propagated confidences are identical."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--set", "intelligence", "confidence", "0.9"], None)
    tree_write.write(_FakeCtx(dae_world, {
        "op": "set", "key": "intelligence", "field": "confidence", "value": 0.9}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_propagate(tmp_path):
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    _run_cli(cli_world, tmp_path / "cli-meta", ["--propagate", "intelligence"], None)
    tree_write.write(_FakeCtx(dae_world, {"op": "propagate", "key": "intelligence"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_reconcile_capabilities(tmp_path):
    from mind_api.src.world import tree_write

    cli_world = _seed_world_text(tmp_path, "cli", _STALE_CAP_TREE)
    dae_world = _seed_world_text(tmp_path, "dae", _STALE_CAP_TREE)

    _run_cli(cli_world, tmp_path / "cli-meta", ["--reconcile-capabilities"], None)
    tree_write.write(_FakeCtx(dae_world, {"op": "reconcile-capabilities"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


# Multi-level tree for reparent byte-compat: alpha (with a child) and beta are
# both children of root. Reparenting alpha → beta exercises every reparent
# code path that touches _tree.yaml — old/new parent children-list edits,
# subtree file-path + depth recompute (alpha AND alpha-child move), the
# leaf→interior flip on beta, and dual-chain confidence propagation. Depths
# stay ≤3, far under D_max=20.
_REPARENT_TREE = (
    "nodes:\n"
    "  root:\n"
    "    file: null\n"
    "    summary: Root node\n"
    "    depth: 0\n"
    "    children:\n"
    "    - alpha\n"
    "    - beta\n"
    "    child_count: 2\n"
    "  alpha:\n"
    "    file: world/knowledge/tree/alpha.md\n"
    "    summary: Alpha L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children:\n"
    "    - alpha-child\n"
    "    child_count: 1\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.4\n"
    "  alpha-child:\n"
    "    file: world/knowledge/tree/alpha/alpha-child.md\n"
    "    summary: Alpha child\n"
    "    depth: 2\n"
    "    parent: alpha\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.8\n"
    "  beta:\n"
    "    file: world/knowledge/tree/beta.md\n"
    "    summary: Beta L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    node_type: leaf\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.5\n"
    "last_updated: '2026-01-01'\n"
    "entity_index: {}\n"
)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_reparent(tmp_path):
    """Reparent alpha → beta: full subtree recompute + dual-chain propagation.
    Both sides read the SAME real core/config/tree.yaml (D_max + competence)
    with NO config-overrides overlay (empty meta dirs), so the on-disk tree is
    byte-identical."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world_text(tmp_path, "cli", _REPARENT_TREE)
    dae_world = _seed_world_text(tmp_path, "dae", _REPARENT_TREE)

    _run_cli(cli_world, tmp_path / "cli-meta", ["--reparent", "alpha", "beta"], None)
    tree_write.write(_FakeCtx(dae_world,
                              {"op": "reparent", "key": "alpha",
                               "new_parent": "beta"},
                              meta=tmp_path / "dae-meta"))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


# Baseline with TWO interior parents: 'intelligence' (2 children) exercises the
# NON-flip path (remove one of two -> stays interior); 'spatial' (1 child)
# exercises the  last-child-removal flip (interior -> leaf), the
# inverse of the add-child leaf->interior flip and the gap _apply_remove_child
# carried until  (it updated child_count but not node_type).
_REMOVE_CHILD_TREE = (
    "nodes:\n"
    "  root:\n"
    "    file: null\n"
    "    summary: Root node\n"
    "    depth: 0\n"
    "    children:\n"
    "    - intelligence\n"
    "    - spatial\n"
    "    child_count: 2\n"
    "  intelligence:\n"
    "    file: world/knowledge/tree/intelligence.md\n"
    "    summary: Intelligence L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children:\n"
    "    - keep-me\n"
    "    - remove-me\n"
    "    child_count: 2\n"
    "    node_type: interior\n"
    "    capability_level: CALIBRATE\n"
    "    confidence: 0.5\n"
    "  keep-me:\n"
    "    file: world/knowledge/tree/keep-me.md\n"
    "    summary: Sibling that remains\n"
    "    depth: 2\n"
    "    parent: intelligence\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    node_type: leaf\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.3\n"
    "  remove-me:\n"
    "    file: world/knowledge/tree/remove-me.md\n"
    "    summary: Child to be removed\n"
    "    depth: 2\n"
    "    parent: intelligence\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    node_type: leaf\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.3\n"
    "  spatial:\n"
    "    file: world/knowledge/tree/spatial.md\n"
    "    summary: Spatial L1\n"
    "    depth: 1\n"
    "    parent: root\n"
    "    children:\n"
    "    - lone-child\n"
    "    child_count: 1\n"
    "    node_type: interior\n"
    "    capability_level: CALIBRATE\n"
    "    confidence: 0.5\n"
    "  lone-child:\n"
    "    file: world/knowledge/tree/lone-child.md\n"
    "    summary: Only child of spatial\n"
    "    depth: 2\n"
    "    parent: spatial\n"
    "    children: []\n"
    "    child_count: 0\n"
    "    node_type: leaf\n"
    "    capability_level: EXPLORE\n"
    "    confidence: 0.3\n"
    "last_updated: '2026-01-01'\n"
    "entity_index: {}\n"
)


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_remove_child(tmp_path):
    """Removing one of two children: parent keeps the sibling, child_count
    decrements, node_type stays 'interior'. The daemon _tree.yaml is
    byte-identical to the CLI's. No prior byte-compat coverage existed for
    remove-child -- every other write op had one; g-115-1493 closes that gap."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world_text(tmp_path, "cli", _REMOVE_CHILD_TREE)
    dae_world = _seed_world_text(tmp_path, "dae", _REMOVE_CHILD_TREE)

    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--remove-child", "intelligence", "remove-me"], None)
    tree_write.write(_FakeCtx(dae_world, {
        "op": "remove-child", "parent": "intelligence", "child_key": "remove-me"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)
    # Parent still interior (one child remains); count decremented.
    intel = _read_tree(dae_world)["nodes"]["intelligence"]
    assert intel["node_type"] == "interior"
    assert intel["child_count"] == 1


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_remove_child_flips_interior_parent(tmp_path):
    """: removing the LAST child of an interior parent flips it
    interior->leaf in the daemon exactly as cmd_remove_child does (tree.py
    g-115-1445) -- the inverse of the add-child leaf->interior flip. Pre-fix
    _apply_remove_child updated child_count but left node_type stale at
    'interior' on a now-childless parent, a byte-compat parity gap invisible to
    BOTH the AST field-set test and the (previously absent) byte-compat suite."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world_text(tmp_path, "cli", _REMOVE_CHILD_TREE)
    dae_world = _seed_world_text(tmp_path, "dae", _REMOVE_CHILD_TREE)

    _run_cli(cli_world, tmp_path / "cli-meta",
             ["--remove-child", "spatial", "lone-child"], None)
    tree_write.write(_FakeCtx(dae_world, {
        "op": "remove-child", "parent": "spatial", "child_key": "lone-child"}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)
    # Parent flipped interior -> leaf (last child removed); count zero.
    spatial = _read_tree(dae_world)["nodes"]["spatial"]
    assert spatial["node_type"] == "leaf"
    assert spatial["child_count"] == 0


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_batch(tmp_path):
    """A mixed batch (add-child + set + increment + propagate) writes a
    _tree.yaml byte-identical to the CLI's. Exercises the full batch path —
    per-op mutations reusing the same helpers, gates, and the phase-2
    propagate-last ordering — against the real CLI."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    operations = [
        {"op": "add-child", "key": "intelligence",
         "child": {"key": "batch-kid", "summary": "first brand new child node"}},
        {"op": "set", "key": "intelligence", "field": "summary",
         "value": "Batch reworded summary"},
        {"op": "increment", "key": "intelligence", "field": "retrieval_count"},
        {"op": "propagate", "key": "intelligence"},
    ]

    _run_cli(cli_world, tmp_path / "cli-meta", ["--batch"],
             json.dumps({"operations": operations}))
    tree_write.write(_FakeCtx(dae_world, {"op": "batch", "operations": operations}))

    assert _tree_bytes(dae_world) == _tree_bytes(cli_world)


# ---------------------------------------------------------------------------
# record-maintenance byte-compat
# ---------------------------------------------------------------------------
# The maintenance block stamps wall-clock timestamps (datetime.now()), so the
# CLI subprocess and the daemon handler — which run a few ms apart — never
# produce byte-identical clocks. _normalize_ts collapses every ISO datetime to
# <TS> and every bare date to <DATE>, so the comparison proves the DUMP FORMAT,
# KEY ORDERING, and every-field-except-the-clock are byte-identical (the clock
# values are equal-by-construction modulo skew). Both sides read the SAME real
# core/config/tree.yaml (debt_threshold + decompose_threshold + pruning), and
# the baseline worlds carry no node .md files, so distill/decompose counts are
# 0 on both → last_backlog_clear_at lands on both.

def _normalize_ts(b: bytes) -> bytes:
    s = b.decode("utf-8")
    s = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "<TS>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<DATE>", s)
    return s.encode("utf-8")


def _daemon_result(resp):
    """Extract the `result` payload from a record-maintenance Response."""
    body = json.loads(resp.body.decode("utf-8"))
    assert body.get("ok") is True, body
    assert body.get("op") == "record-maintenance", body
    return body["result"]


def _normalize_result(obj):
    """Recursively replace ISO datetimes/dates in a parsed result dict so the
    daemon result and the CLI stdout JSON compare equal modulo clock skew."""
    return json.loads(_normalize_ts(json.dumps(obj).encode("utf-8")).decode("utf-8"))


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_record_maintenance(tmp_path):
    """Plain --record-maintenance: maintenance block (last_maintain_at +
    last_backlog_clear_at, since post-run debt is 0) lands byte-identically,
    and the daemon result dict matches the CLI stdout JSON."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    cli_proc = _run_cli(cli_world, tmp_path / "cli-meta",
                        ["--record-maintenance"], None)
    resp = tree_write.write(_FakeCtx(dae_world, {"op": "record-maintenance"},
                                     meta=tmp_path / "dae-meta"))

    # 1. _tree.yaml byte-compat (timestamps normalized).
    assert _normalize_ts(_tree_bytes(dae_world)) == _normalize_ts(_tree_bytes(cli_world))
    # 2. stdout/result-dict compat.
    cli_result = json.loads(cli_proc.stdout)
    dae_result = _daemon_result(resp)
    assert _normalize_result(dae_result) == _normalize_result(cli_result)
    # 3. the debt math actually fired (0 candidates → cleared).
    assert dae_result["post_run_debt"]["cleared"] is True
    assert dae_result["post_run_debt"]["total"] == 0


@pytest.mark.skipif(not _HAS_LIBYAML,
                    reason="libyaml (CSafeDumper) required for byte-compat")
@pytest.mark.skipif(not TREE_PY.exists(), reason="core/scripts/tree.py missing")
def test_byte_compat_record_maintenance_with_run_record(tmp_path):
    """--record-maintenance --backlog-mode --stop-mode --with-run-record:
    exercises every maintenance-block key (last_maintain_at, last_backlog_mode_at,
    last_stop_mode_at, last_backlog_clear_at — insertion order is byte-compat
    significant) AND the run-record JSONL append. started_at is FIXED in the
    input blob so run_id is deterministic across both sides; only ended_at is
    clock-skewed (normalized)."""
    from mind_api.src.world import tree_write

    cli_world = _seed_world(tmp_path, "cli")
    dae_world = _seed_world(tmp_path, "dae")

    run_input = {
        "mode": "backlog",
        "started_at": "2026-05-01T12:00:00",
        "decompose": {"actioned": 2, "deferred": 0},
        "distill": {"actioned": 1},
        "redistribute": {"actioned": 0},
    }

    cli_proc = _run_cli(
        cli_world, tmp_path / "cli-meta",
        ["--record-maintenance", "--backlog-mode", "--stop-mode", "--with-run-record"],
        json.dumps(run_input))
    resp = tree_write.write(_FakeCtx(dae_world, {
        "op": "record-maintenance",
        "backlog_mode": True, "stop_mode": True, "with_run_record": True,
        "run_record_input": run_input,
    }, meta=tmp_path / "dae-meta"))

    # 1. _tree.yaml byte-compat (all four maintenance keys, normalized clocks).
    assert _normalize_ts(_tree_bytes(dae_world)) == _normalize_ts(_tree_bytes(cli_world))

    # 2. result-dict compat (includes run_record block; log_path differs by
    #    world root, so compare only run_id + appended).
    cli_result = json.loads(cli_proc.stdout)
    dae_result = _daemon_result(resp)
    assert dae_result["run_record"]["run_id"] == cli_result["run_record"]["run_id"]
    assert dae_result["run_record"]["appended"] is True
    assert (_normalize_result({k: v for k, v in dae_result.items() if k != "run_record"})
            == _normalize_result({k: v for k, v in cli_result.items() if k != "run_record"}))

    # 3. the run-record JSONL append is byte-identical (only ended_at skews).
    cli_log = (cli_world / "tree-maintenance-log.jsonl").read_bytes()
    dae_log = (dae_world / "tree-maintenance-log.jsonl").read_bytes()
    assert _normalize_ts(dae_log) == _normalize_ts(cli_log)

    # 4. all four maintenance cadence keys present.
    maint = dae_result["maintenance"]
    for k in ("last_maintain_at", "last_backlog_mode_at",
              "last_stop_mode_at", "last_backlog_clear_at"):
        assert k in maint, f"{k} missing from {maint}"


# ---------------------------------------------------------------------------
#  — value-integrity (silent partial write detection)
#
# WHAT THESE PIN, and why the obvious test would be worthless. On 2026-08-19 a
# `tree-update.sh --set` of a 17,708-byte value returned rc=0, echoed a
# complete-looking node record, and stored 8,186 bytes cut mid-word, destroying
# four catalogue entries of a live node. Nothing in the tree write path compared
# stored bytes against sent bytes, so the loss was invisible by construction.
#
# The goal that filed this work states the trap explicitly: "the regression test
# MUST assert stored LENGTH equals intended length; a test asserting only rc=0
# passes against the exact failure being prevented." Every assertion below is
# therefore on the LENGTH ON DISK or on the write NOT HAVING HAPPENED — never on
# the status code alone, and never on the echoed record (which is rendered from
# the in-memory dict and would look correct even for a short write).
#
# The check compares ACROSS THE WIRE (client-declared vs daemon-received). The
# in-daemon comparison one reaches for first — len(req["value"]) against
# len(node[field]) after _apply_set — is vacuous: _apply_set does
# node[field] = the parsed request value, so it compares a string to itself and
# passes 100% of the time while looking exactly like protection.
# ---------------------------------------------------------------------------

def test_set_reports_stored_value_bytes(running_daemon):
    """The success response carries the stored byte length (guard-1661), and it
    equals the length actually on disk — not the length we hoped for."""
    project_root, port = running_daemon
    world = project_root / "world"
    value = "x" * 17708          # the exact size the incident truncated FROM
    status, body = _post(port, "/v1/tree/write", {
        "op": "set", "key": "alpha-test-node", "field": "summary",
        "value": value, "value_bytes": len(value.encode("utf-8")),
    })
    assert status == 200, body
    data = json.loads(body)
    assert data["value_bytes"] == 17708
    # The assertion that matters: LENGTH ON DISK, not the echoed record.
    tree = _read_tree(world)
    stored = tree["nodes"]["alpha-test-node"]["summary"]
    assert len(stored.encode("utf-8")) == 17708
    assert stored == value


def test_set_refuses_value_that_lost_bytes_in_transit(running_daemon):
    """A value shorter than the client declared is REFUSED, and nothing is
    written. This reproduces the incident's shape: the daemon receives 8,186
    bytes for a value the caller built at 17,708."""
    project_root, port = running_daemon
    world = project_root / "world"

    before = _read_tree(world)["nodes"]["alpha-test-node"].get("summary")

    truncated = "y" * 8186       # what arrived
    status = None
    try:
        status, body = _post(port, "/v1/tree/write", {
            "op": "set", "key": "alpha-test-node", "field": "summary",
            "value": truncated,
            "value_bytes": 17708,          # what the caller says it sent
        })
    except urllib.error.HTTPError as e:
        status = e.code
        body = e.read().decode("utf-8")
    assert status == 500, f"expected refusal, got {status}: {body}"
    assert "value_truncated_in_transit" in body
    assert "9522" in body or "17708" in body, body

    # THE LOAD-BEARING ASSERTION: the write did not happen. guard-3150 — a check
    # that fires after the bytes are on disk cannot prevent anything, and on an
    # S3-authoritative store they are already the shared truth for every box.
    after = _read_tree(world)["nodes"]["alpha-test-node"].get("summary")
    assert after == before, "refused write must leave the tree untouched"
    assert after != truncated


def test_set_without_declared_bytes_still_writes(running_daemon):
    """Fail-open for callers that do not declare value_bytes. Every client
    predates this field; refusing them would take the tree write path down
    fleet-wide. This is a deliberate coverage gap, pinned so that a later change
    to fail-closed is a conscious edit and not an accident."""
    project_root, port = running_daemon
    world = project_root / "world"
    value = "z" * 4096
    status, body = _post(port, "/v1/tree/write", {
        "op": "set", "key": "alpha-test-node", "field": "summary",
        "value": value,                      # no value_bytes
    })
    assert status == 200, body
    tree = _read_tree(world)
    assert len(tree["nodes"]["alpha-test-node"]["summary"].encode("utf-8")) == 4096


def test_set_value_bytes_counts_utf8_bytes_not_characters(running_daemon):
    """Both sides must count UTF-8 BYTES. A multibyte value where
    len(str) != len(bytes) would false-alarm on every write if either side
    counted characters."""
    project_root, port = running_daemon
    world = project_root / "world"
    value = "éèê" * 200        # 600 chars, 1200 UTF-8 bytes
    assert len(value) != len(value.encode("utf-8"))
    status, body = _post(port, "/v1/tree/write", {
        "op": "set", "key": "alpha-test-node", "field": "summary",
        "value": value, "value_bytes": len(value.encode("utf-8")),
    })
    assert status == 200, body
    assert json.loads(body)["value_bytes"] == 1200
    tree = _read_tree(world)
    assert tree["nodes"]["alpha-test-node"]["summary"] == value


# --- : structural post-condition for non-set tree ops -------------
# The shipped value_bytes check () compares a VALUE LENGTH, so it
# covers `set` only -- --remove-child carries no value. These two tests pin the
# structural equivalent. They assert the STRUCTURE ON DISK, never rc/status
# alone: a status-only assertion passes against the exact failure being
# prevented (rc=0 was what the 2026-08-20 reproduction returned).

def test_remove_child_response_carries_stored_children_not_request_echo(
        running_daemon):
    """guard-1661: the success payload must carry caller-verifiable evidence.

    Pre-fix the response was {"removed": child_key, "parent": parent_key} --
    both echoed verbatim from the REQUEST, so it was true by construction and
    could not report a failed removal. The new parent_children field is read
    from the mutated tree, so it can DISAGREE with the request; this test pins
    that it matches what actually landed on disk.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "evidence-leaf", "summary": "temp"},
    })
    status, body = _post(port, "/v1/tree/write", {
        "op": "remove-child", "parent": "alpha-test-node",
        "child_key": "evidence-leaf",
    })
    assert status == 200, body
    resp = json.loads(body) if isinstance(body, str) else body

    # The evidence field exists and is a real list (not the request echo).
    assert "parent_children" in resp, (
        "response must carry parent_children so the caller can verify the "
        "removal without a second read (guard-1661)")
    assert isinstance(resp["parent_children"], list)
    assert resp["parent_child_count"] == len(resp["parent_children"])

    # And it AGREES with disk -- this is the half that makes it evidence.
    tree = _read_tree(world)
    on_disk = tree["nodes"]["alpha-test-node"]["children"]
    assert resp["parent_children"] == on_disk
    assert "evidence-leaf" not in on_disk
    assert "evidence-leaf" not in tree["nodes"]


def test_remove_child_refuses_when_duplicate_entry_survives_removal(
        running_daemon):
    """The non-tautological catch: list.remove() drops only the FIRST match.

    A duplicated child entry -- the shape an own-cloud union merge produces
    when a peer write resurrects a node (rb-2859 class) -- leaves the child
    STILL PRESENT after _apply_remove_child returns success. Pre-fix that
    persisted, and the response echoed the request, so rc=0 with the node still
    on disk was exactly what the caller saw (measured 2026-08-20, cc-08).

    Asserts BOTH halves: the write is refused (guard-3150 -- before the write,
    so nothing is persisted) AND the on-disk structure is byte-unchanged.
    """
    project_root, port = running_daemon
    world = project_root / "world"
    _post(port, "/v1/tree/write", {
        "op": "add-child", "parent": "alpha-test-node",
        "child": {"key": "dup-leaf", "summary": "temp"},
    })

    # Inject the duplicate directly -- no supported op creates one, which is
    # precisely why it goes undetected in the wild.
    tree_path = world / "knowledge" / "tree" / "_tree.yaml"
    raw = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    kids = raw["nodes"]["alpha-test-node"]["children"]
    assert kids.count("dup-leaf") == 1
    kids.insert(kids.index("dup-leaf"), "dup-leaf")
    raw["nodes"]["alpha-test-node"]["children"] = kids
    raw["nodes"]["alpha-test-node"]["child_count"] = len(kids)
    tree_path.write_text(yaml.safe_dump(raw, sort_keys=False,
                                        default_flow_style=None, width=200),
                         encoding="utf-8")
    before = tree_path.read_text(encoding="utf-8")

    try:
        _post(port, "/v1/tree/write", {
            "op": "remove-child", "parent": "alpha-test-node",
            "child_key": "dup-leaf",
        })
    except urllib.error.HTTPError as e:
        assert e.code == 500
        err = json.loads(e.read().decode("utf-8"))
        assert err["error"] == "remove_child_post_condition_failed", err
    else:
        raise AssertionError(
            "expected a refusal: one 'dup-leaf' entry survives list.remove(), "
            "so the removal did not take effect and must not be persisted")

    # STRUCTURE ON DISK, not status alone -- nothing may have been written.
    assert tree_path.read_text(encoding="utf-8") == before, (
        "the refusal must happen BEFORE the write (guard-3150); on an "
        "S3-authoritative store a post-write verify has already published the "
        "bad state to every other box")
    after = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert after["nodes"]["alpha-test-node"]["children"].count("dup-leaf") == 2
    assert "dup-leaf" in after["nodes"]


# ---- : durability post-condition -----------------------------------
# These pin the TWO properties that make _durability_witness worth having, both
# of which a naive implementation gets wrong in the dangerous direction.

def test_durability_witness_fails_open_on_unreadable_store(tmp_path):
    """A probe fault must NEVER be reported as a lost write (guard-1562 class).

    The write has already succeeded by the time this runs, so a false
    'write_not_durable' is strictly worse than silence: it manufactures a data-
    loss alarm out of a plumbing error.
    """
    from mind_api.src.world import tree_write
    out = tree_write._durability_witness(
        tmp_path / "does-not-exist" / "_tree.yaml", "some-key",
        {"confidence": 0.9})
    assert out is None


def test_durability_witness_rejects_derived_fields_as_witnesses(tmp_path, monkeypatch):
    """guard-5212: a witness must be a merge INPUT, never an OUTPUT.

    merge_tree's _rebuild_tree_structure recomputes children/child_count/
    node_type/depth from each node's `parent` on EVERY merge. So a read-back
    assertion on one of those passes regardless of what happened to the write —
    it witnesses that the rebuild ran, not that the write survived.

    This test pins the DISTINCTION the helper is built on: an INPUT field that
    diverges is reported, and it is reported by VALUE comparison rather than by
    byte identity (which would alarm forever on any multi-writer store).
    """
    from mind_api.src.world import tree_write

    authoritative = (
        "nodes:\n"
        "  n1:\n"
        "    parent: root\n"
        "    confidence: 0.4\n"
        "    children: [c1, c2]\n"
    ).encode("utf-8")

    class _FakeBackend:
        def read_authoritative_bytes(self, _p):
            return authoritative

    monkeypatch.setitem(
        sys.modules, "storage_backend",
        type("m", (), {"get_backend": staticmethod(lambda: _FakeBackend())}))

    p = tmp_path / "_tree.yaml"
    p.write_text("nodes: {}\n", encoding="utf-8")

    # INPUT field that diverged -> reported.
    bad = tree_write._durability_witness(p, "n1", {"confidence": 0.9})
    assert bad is not None and bad["verdict"] == "write_not_durable"
    assert bad["mismatched"]["confidence"]["authoritative"] == 0.4

    # INPUT field that matches -> silent, even though `children` differs from
    # anything a caller might have expected. Peer-tolerance: only the asserted
    # field is compared, never the whole node.
    assert tree_write._durability_witness(p, "n1", {"confidence": 0.4}) is None
    assert tree_write._durability_witness(p, "n1", {"parent": "root"}) is None
