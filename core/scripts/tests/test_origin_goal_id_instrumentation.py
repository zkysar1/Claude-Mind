"""test_origin_goal_id_instrumentation.py —  (asp-325, Gate D prereq).

Pins the origin_goal_id instrumentation added to the reasoning-bank and
tree-node write paths for the Gate D SPILL-1 spillover analysis: every encoding
write records the EXECUTING goal id (team-state in_flight.goal_id), distinct
from source_goal's SEMANTIC-source meaning (a caller may override source_goal
to a different goal). SPILL-1 needs to know which experiment-arm goal produced
a given piece of knowledge — that is origin_goal_id.

Two write paths, two verification styles:
  - reasoning-bank: store_registry._rb_inject_source_goal (the daemon rb
    StoreSpec prepare hook) — unit-tested in-process with a mock ctx + a
    seeded team-state.yaml. The rb validator has no unknown-field gate, so the
    additive default field needs only RB_DEFAULT_FIELDS + the prepare inject.
  - tree-node: tree.py cmd_add_child + _read_in_flight_goal_id — end-to-end via
    subprocess (real argparse + stdin + locked YAML write), the path the
    LLM/daemon actually exercises. Modeled on test_tree_node_field_prevention.py.

Verify criterion (verbatim from the goal): "one tree add + one rb add carry the
field; existing readers parse unchanged."
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

from mind_api.src.store_registry import (  # noqa: E402
    RB_DEFAULT_FIELDS,
    _rb_inject_source_goal,
    validate_rb_record,
)

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable


# ===========================================================================
# Helpers
# ===========================================================================

def _seed_team_state(world: Path, agent: str, goal_id):
    """Write team-state.yaml with agent_status.<agent>.in_flight[.goal_id].
    goal_id=None writes an in_flight block WITHOUT a goal_id (absent-goal case)."""
    world.mkdir(parents=True, exist_ok=True)
    in_flight = {} if goal_id is None else {"goal_id": goal_id}
    ts = {"agent_status": {agent: {"in_flight": in_flight}}}
    (world / "team-state.yaml").write_text(yaml.safe_dump(ts), encoding="utf-8")


class _MockPaths:
    def __init__(self, agent_name, world):
        self.agent_name = agent_name
        self.world = world


class _MockCtx:
    """Minimal ctx exposing the two attrs _rb_inject_source_goal reads:
    ctx.paths.agent_name and ctx.paths.world (a Path)."""
    def __init__(self, agent_name, world):
        self.paths = _MockPaths(agent_name, world)


def _minimal_rb(**overrides):
    rec = {
        "id": "rb-999",
        "type": "success",
        "status": "active",
        "category": "test",
        "title": "test",
        "content": "test content",
        "created": "2026-06-01T00:00:00",
        "applies_to": "framework",
        "tags": [],
    }
    rec.update(overrides)
    return rec


# ===========================================================================
# reasoning-bank path — _rb_inject_source_goal prepare hook (in-process)
# ===========================================================================

def test_rb_default_field_is_null():
    """RB_DEFAULT_FIELDS carries origin_goal_id defaulting to None, so a record
    written with no executing goal serializes the field as null (not absent)."""
    assert "origin_goal_id" in RB_DEFAULT_FIELDS
    assert RB_DEFAULT_FIELDS["origin_goal_id"] is None


def test_rb_prepare_injects_origin_from_in_flight(tmp_path):
    """One rb add carries the field: the prepare hook auto-populates
    origin_goal_id (and source_goal) from team-state in_flight.goal_id."""
    _seed_team_state(tmp_path, "zeta", "g-325-06")
    rec = {}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)
    assert rec["origin_goal_id"] == "g-325-06"
    assert rec["source_goal"] == "g-325-06"


def test_rb_prepare_origin_caller_wins(tmp_path):
    """An explicit origin_goal_id is preserved (caller-wins), even over a
    different in_flight goal."""
    _seed_team_state(tmp_path, "zeta", "g-325-06")
    rec = {"origin_goal_id": "g-caller-11"}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)
    assert rec["origin_goal_id"] == "g-caller-11"
    # source_goal was NOT caller-set, so it still auto-populates independently
    assert rec["source_goal"] == "g-325-06"


def test_rb_prepare_source_and_origin_independent(tmp_path):
    """source_goal and origin_goal_id are each caller-wins INDEPENDENTLY: a
    caller overriding source_goal (semantic source) still gets origin_goal_id
    (executing goal) auto-injected — they are not the same field."""
    _seed_team_state(tmp_path, "zeta", "g-325-06")
    rec = {"source_goal": "g-semantic-source-99"}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)
    assert rec["source_goal"] == "g-semantic-source-99"
    assert rec["origin_goal_id"] == "g-325-06"


def test_rb_prepare_both_caller_set_no_read(tmp_path):
    """When the caller set BOTH fields (including explicit null), the hook
    early-returns without consulting team-state — explicit null is preserved."""
    _seed_team_state(tmp_path, "zeta", "g-325-06")
    rec = {"source_goal": "g-a", "origin_goal_id": None}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)
    assert rec["source_goal"] == "g-a"
    assert rec["origin_goal_id"] is None


def test_rb_prepare_fail_open_no_team_state(tmp_path):
    """Fail-open: no team-state.yaml -> no injection, no error; the field is
    left for apply_defaults to default to null."""
    rec = {}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)  # no team-state written
    assert "origin_goal_id" not in rec
    assert "source_goal" not in rec


def test_rb_prepare_no_in_flight_goal(tmp_path):
    """in_flight present but no goal_id -> no injection (absent-goal case)."""
    _seed_team_state(tmp_path, "zeta", None)
    rec = {}
    _rb_inject_source_goal(_MockCtx("zeta", tmp_path), rec)
    assert "origin_goal_id" not in rec


def test_rb_validator_accepts_origin_goal_id():
    """Existing readers parse unchanged: the rb validator accepts a record
    carrying origin_goal_id (additive field, no unknown-field rejection), AND a
    record without it still validates (the field is optional)."""
    validate_rb_record(None, _minimal_rb(origin_goal_id="g-325-06"), skip_id=False)
    validate_rb_record(None, _minimal_rb(), skip_id=False)


# ===========================================================================
# tree-node path — cmd_add_child + _read_in_flight_goal_id (subprocess)
# ===========================================================================

def _seed_tree(world: Path):
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {"nodes": {
        "root": {"file": "world/knowledge/tree/root.md", "depth": 0,
                 "children": [], "child_count": 0, "capability_level": "EXPLOIT"},
    }}
    (tree_dir / "_tree.yaml").write_text(yaml.safe_dump(tree), encoding="utf-8")
    return tree_dir / "_tree.yaml"


def _run_tree(args, stdin_text, world: Path, meta: Path, agent=None):
    """Invoke tree.py with WORLD_DIR/META isolated to tmp. Setting MIND_AGENT
    is what makes _read_in_flight_goal_id resolve our seeded in_flight (the
    sibling field-prevention test POPS it because it needs no agent context)."""
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    if agent is None:
        env.pop("MIND_AGENT", None)
    else:
        env["MIND_AGENT"] = agent
    return subprocess.run(
        [PYTHON, str(TREE_PY)] + args,
        input=stdin_text, text=True, capture_output=True, env=env, timeout=30,
    )


def test_tree_add_child_injects_origin_from_in_flight(tmp_path):
    """One tree add carries the field: cmd_add_child auto-injects origin_goal_id
    from team-state in_flight via _read_in_flight_goal_id."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", "g-325-06")
    payload = json.dumps({"key": "child-a", "summary": "a test child node"})
    r = _run_tree(["update", "--add-child", "root"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["child-a"]["origin_goal_id"] == "g-325-06"


def test_tree_add_child_caller_origin_wins(tmp_path):
    """Explicit origin_goal_id in the child JSON wins over in_flight."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", "g-325-06")
    payload = json.dumps({"key": "child-b", "summary": "child",
                          "origin_goal_id": "g-explicit-11"})
    r = _run_tree(["update", "--add-child", "root"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["child-b"]["origin_goal_id"] == "g-explicit-11"


def test_tree_add_child_absent_when_no_in_flight(tmp_path):
    """Existing readers parse unchanged: with no in_flight goal, the new node
    carries no origin_goal_id (ABSENT, not null) — the manual-add case. tree
    nodes default-absent (no RB_DEFAULT_FIELDS analog), so consumers that never
    knew the field see structurally-identical nodes."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", None)  # in_flight present, no goal_id
    payload = json.dumps({"key": "child-c", "summary": "manual child"})
    r = _run_tree(["update", "--add-child", "root"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert "origin_goal_id" not in tree["nodes"]["child-c"]


# ── : batch add-child must mirror the single-path origin_goal_id ──
# instrumentation. The  fix landed in cmd_add_child only; cmd_batch
# add-child silently dropped origin_goal_id (both explicit AND auto-inject) for
# every batch-created node — invisible to Gate D SPILL-1 attribution — until
#  restored parity. These three pin the batch path against the same
# three cases as the single-path tests above (duplicated-path divergence,
# rb-1776 — enumerate ALL sibling paths; the coverage gap here is what let the
# omission ship undetected: run-full-suite-after-deep-code.md).

def test_tree_batch_add_child_injects_origin_from_in_flight(tmp_path):
    """cmd_batch add-child auto-injects origin_goal_id from team-state in_flight,
    mirroring cmd_add_child."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", "g-325-06")
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "root",
         "child": {"key": "bchild-a", "summary": "a batch child node"}}]})
    r = _run_tree(["update", "--batch"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["bchild-a"]["origin_goal_id"] == "g-325-06"


def test_tree_batch_add_child_caller_origin_wins(tmp_path):
    """Explicit origin_goal_id in the batch child JSON wins over in_flight."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", "g-325-06")
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "root",
         "child": {"key": "bchild-b", "summary": "child",
                   "origin_goal_id": "g-explicit-11"}}]})
    r = _run_tree(["update", "--batch"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert tree["nodes"]["bchild-b"]["origin_goal_id"] == "g-explicit-11"


def test_tree_batch_add_child_absent_when_no_in_flight(tmp_path):
    """With no in_flight goal, a batch-created node carries no origin_goal_id
    (ABSENT, not null) — the manual-add case, mirroring the single path."""
    world = tmp_path / "world"
    meta = tmp_path / "meta"
    tree_path = _seed_tree(world)
    _seed_team_state(world, "zeta", None)
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "root",
         "child": {"key": "bchild-c", "summary": "manual batch child"}}]})
    r = _run_tree(["update", "--batch"], payload, world, meta, agent="zeta")
    assert r.returncode == 0, f"stderr={r.stderr}"
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    assert "origin_goal_id" not in tree["nodes"]["bchild-c"]
