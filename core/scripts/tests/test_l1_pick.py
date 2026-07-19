"""Tests for _l1_pick.py — the S9 L1-pick-log SSOT (3).

Covers the L1-resolution walk, the fail-open append contract, the CLI/daemon
delegation wiring (split-brain regression guard, sibling to
test_competence.py's parity checks), and a DaemonFixture round-trip proving a
POST /v1/tree/write op=add-child appends a pick-log entry — the exact
telemetry the tree daemonization deferred 2026-05-28→2026-07-12.
"""
import json
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _l1_pick  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CLI_TREE = PROJECT_ROOT / "core" / "scripts" / "tree.py"
DAEMON_TREE_WRITE = PROJECT_ROOT / "mind_api" / "src" / "world" / "tree_write.py"
WRAPPER = PROJECT_ROOT / "core" / "scripts" / "tree-update.sh"


def _nodes_chain():
    """root(0) -> l1(1) -> mid(2) -> leaf(3)"""
    return {
        "root": {"depth": 0, "children": ["l1"]},
        "l1": {"depth": 1, "parent": "root", "children": ["mid"]},
        "mid": {"depth": 2, "parent": "l1", "children": ["leaf"]},
        "leaf": {"depth": 3, "parent": "mid", "children": []},
    }


# --- get_l1_for_node ---------------------------------------------------------

def test_get_l1_walks_chain_to_depth_one():
    assert _l1_pick.get_l1_for_node(_nodes_chain(), "leaf") == "l1"
    assert _l1_pick.get_l1_for_node(_nodes_chain(), "mid") == "l1"


def test_get_l1_of_l1_returns_itself():
    assert _l1_pick.get_l1_for_node(_nodes_chain(), "l1") == "l1"


def test_get_l1_of_root_returns_none():
    assert _l1_pick.get_l1_for_node(_nodes_chain(), "root") is None


def test_get_l1_broken_chain_returns_none():
    nodes = _nodes_chain()
    del nodes["mid"]  # leaf's parent vanishes mid-walk
    assert _l1_pick.get_l1_for_node(nodes, "leaf") is None
    assert _l1_pick.get_l1_for_node(nodes, "no-such-key") is None


def test_get_l1_cycle_guard_terminates():
    nodes = {
        "a": {"depth": 5, "parent": "b"},
        "b": {"depth": 5, "parent": "a"},
    }
    assert _l1_pick.get_l1_for_node(nodes, "a") is None


# --- append_l1_pick_log ------------------------------------------------------

def test_append_writes_full_schema_and_appends(tmp_path):
    _l1_pick.append_l1_pick_log(tmp_path, "system/foo", "system", "add-child",
                                source="goal-execution", reason="test",
                                agent="zeta", session_id="sid-1")
    _l1_pick.append_l1_pick_log(tmp_path, "product/bar", "product", "reparent")
    lines = (tmp_path / "l1-pick-log.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1["target_node"] == "system/foo"
    assert e1["l1"] == "system"
    assert e1["decision_type"] == "add-child"
    assert e1["source"] == "goal-execution"
    assert e1["reason"] == "test"
    assert e1["agent"] == "zeta"
    assert e1["session_id"] == "sid-1"
    assert e1["ts"]
    e2 = json.loads(lines[1])
    assert e2["l1"] == "product"
    assert e2["source"] is None and e2["agent"] is None


def test_append_creates_meta_dir(tmp_path):
    missing = tmp_path / "not-yet"
    _l1_pick.append_l1_pick_log(missing, "n", "l1x", "batch-add-child")
    assert (missing / "l1-pick-log.jsonl").exists()


# --- log_l1_pick (resolve + append, fail-open) --------------------------------

def test_log_l1_pick_resolves_and_appends(tmp_path):
    _l1_pick.log_l1_pick(_nodes_chain(), tmp_path, "leaf", "add-child",
                         agent="zeta")
    entry = json.loads((tmp_path / "l1-pick-log.jsonl").read_text(encoding="utf-8"))
    assert entry["l1"] == "l1"
    assert entry["target_node"] == "leaf"


def test_log_l1_pick_orphan_fallback(tmp_path):
    nodes = {"stray": {"depth": 4, "parent": "gone"}}
    _l1_pick.log_l1_pick(nodes, tmp_path, "stray", "reparent")
    entry = json.loads((tmp_path / "l1-pick-log.jsonl").read_text(encoding="utf-8"))
    assert entry["l1"] == "_orphan"


def test_log_l1_pick_fail_open_never_raises(tmp_path, capsys):
    # meta_dir is a FILE → parent-mkdir raises inside append → swallowed.
    blocker = tmp_path / "meta-as-file"
    blocker.write_text("x", encoding="utf-8")
    _l1_pick.log_l1_pick(_nodes_chain(), blocker, "leaf", "add-child")
    # nodes=None → attribute error in the walk → swallowed.
    _l1_pick.log_l1_pick(None, tmp_path, "leaf", "add-child")
    err = capsys.readouterr().err
    assert "[l1-pick-log] WARN" in err


# --- delegation wiring (split-brain regression guards) -------------------------

def test_cli_tree_delegates_to_ssot():
    src = CLI_TREE.read_text(encoding="utf-8")
    assert "from _l1_pick import" in src, "tree.py lost the SSOT import"
    # The old inline bodies must not survive alongside the delegation.
    assert "def get_l1_for_node(" not in src
    assert src.count("def _append_l1_pick_log(") <= 1  # thin env-wrapper only


def test_daemon_tree_write_carries_all_three_call_sites():
    src = DAEMON_TREE_WRITE.read_text(encoding="utf-8")
    assert "from _l1_pick import log_l1_pick" in src, "daemon lost the SSOT import"
    for decision in ('"add-child"', '"batch-add-child"', '"reparent"'):
        assert any(decision in line for line in src.splitlines()
                   if "log_l1_pick(" in line or decision in line), \
            f"daemon lost the {decision} pick-log call"
    assert src.count("log_l1_pick(") >= 3, "fewer than 3 daemon call sites"


def test_wrapper_forwards_encoding_flags():
    src = WRAPPER.read_text(encoding="utf-8")
    assert 'ENC_SOURCE="${2-}"' in src, "wrapper dropped --encoding-source capture"
    assert 'ENC_REASON="${2-}"' in src, "wrapper dropped --encoding-reason capture"
    assert '"encoding_source"' in src and '"encoding_reason"' in src, \
        "wrapper no longer forwards encoding fields in the POST body"
    # The stale claims (pre-3) must be gone; the historical note
    # "They were no-ops 2026-05-28→..." is allowed to remain.
    assert "ACCEPTED but no-ops" not in src, "stale no-op header claim survived"
    assert "daemon defers the L1-pick-log" not in src, \
        "stale deferral comment survived"


# --- daemon round-trip ---------------------------------------------------------

def test_daemon_roundtrip_add_child_appends_pick_log(tmp_path):
    """POST /v1/tree/write op=add-child → meta/l1-pick-log.jsonl gains an entry
    resolving the fresh child to its L1, carrying the forwarded
    encoding_source/encoding_reason and the X-Mind-Agent identity. This is the
    signal that was silent for ~6 weeks (g-115-1943)."""
    import urllib.request
    from _daemon_fixture import DaemonFixture

    world = tmp_path / "world"
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True)
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump({
        "nodes": {
            "root": {"depth": 0, "file": "root.md",
                     "children": ["system"], "child_count": 1},
            "system": {"depth": 1, "parent": "root", "file": "system/_index.md",
                       "children": [], "child_count": 0},
        },
    }, sort_keys=False), encoding="utf-8")

    with DaemonFixture(world, agent="alpha") as df:
        pick_log = df.project_root / "meta" / "l1-pick-log.jsonl"
        assert not pick_log.exists()

        body = json.dumps({
            "op": "add-child",
            "parent": "system",
            "child": {"key": "system/fresh-node", "summary": "round-trip probe"},
            "no_dedup": True,
            "encoding_source": "goal-execution",
            "encoding_reason": "g-115-1943 round-trip",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"http://127.0.0.1:{df.port}/v1/tree/write",
            data=body, method="POST",
            headers={"X-Mind-Agent": "alpha",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            assert resp.status == 200
            out = json.loads(resp.read().decode("utf-8"))
        assert out["ok"] is True and out["key"] == "system/fresh-node"

        assert pick_log.exists(), "daemon write did not append the pick log"
        entry = json.loads(pick_log.read_text(encoding="utf-8").splitlines()[-1])
        assert entry["target_node"] == "system/fresh-node"
        assert entry["l1"] == "system"
        assert entry["decision_type"] == "add-child"
        assert entry["source"] == "goal-execution"
        assert entry["reason"] == "g-115-1943 round-trip"
        assert entry["agent"] == "alpha"
