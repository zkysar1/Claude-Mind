"""test_growth_log_reaches_disk.py —  (asp-115).

The END-TO-END half of the tree_growth_log coverage. `test_growth_log.py`
already pins the row SHAPES (unit), the CALL SITES (wiring, by grep), and the
ORDERING (the call precedes serialization). Every one of those can be green
while the row never reaches disk — a merge handler could drop it, a serializer
could filter unknown keys, or a lock path could write a stale copy of `tree`.

That is not a hypothetical failure mode for this particular log. The defect
g-115-3210 fixed was an append that silently never happened for 3.7 months, and
the thing that made it survive so long was that no test ever read `_tree.yaml`
back and looked. This file does exactly that: run a REAL batch decompose
through the real CLI, then re-open `_tree.yaml` from disk and find the row.

WHY SUBPROCESS AND NOT `cmd_batch(...)` IN-PROCESS: the production caller pipes
JSON to `tree.py update --batch` on stdin, and guard-920 says a regression test
must replicate the literal production arg shape, not the contract-ideal one. An
in-process call would also share this pytest process's already-imported
`_growth_log`, which is precisely the coupling the test is supposed to be blind
to.

STORAGE_BACKEND=local IS PINNED EXPLICITLY in `_run_tree`, on top of the
conftest autouse pin. Both are wanted: the conftest pin covers pytest-collected
tests, and the explicit env pin travels with the subprocess so this file stays
correct if it is ever run outside pytest. Under own-cloud,
`OwnCloudBackend._s3_key` derives from customer_prefix+env_id+filename and NOT
from the `MIND_WORLD` tmp override, so a tmp-world write collides on the
PRODUCTION key — that is the 2026-07-09 `aspirations.jsonl` truncation
(guard-955 / rb-2983). A test that seeds a world MUST NOT be the one that
rediscovers this.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_growth_log_reaches_disk.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
TREE_PY = CORE_SCRIPTS / "tree.py"
PYTHON = sys.executable

# The single line whose removal must make the E2E assertion fail. Asserted
# present before it is mutated, so a refactor that moves or rewrites it breaks
# this test LOUDLY instead of silently mutating nothing and still "passing".
GROWTH_CALL = (
    "_growth_record_batch(tree, mutation_ops, date.today().isoformat())"
)


def _seed_tree(world: Path) -> Path:
    """A root with one leaf `target`, and an EMPTY growth log.

    Empty rather than absent: `append_rows` seeds a missing key just fine, but
    starting empty means any row found afterwards was written by this batch and
    not inherited from the fixture.
    """
    tree_dir = world / "knowledge" / "tree"
    tree_dir.mkdir(parents=True, exist_ok=True)
    tree = {
        "last_updated": "2026-08-10",
        "tree_growth_log": [],
        "nodes": {
            "root": {
                "file": None, "depth": 0, "parent": None,
                "children": ["target"], "child_count": 1,
                "capability_level": "EXPLOIT", "node_type": "interior",
                "summary": "root",
            },
            "target": {
                "file": "world/knowledge/tree/target.md", "depth": 1,
                "parent": "root", "children": [], "child_count": 0,
                "capability_level": "EXPLOIT", "node_type": "leaf",
                "summary": "the node this batch decomposes",
            },
        },
    }
    path = tree_dir / "_tree.yaml"
    path.write_text(yaml.safe_dump(tree, sort_keys=False), encoding="utf-8")
    return path


def _run_tree(args, stdin_text, world: Path, meta: Path, script: Path = TREE_PY):
    env = dict(os.environ)
    env["MIND_WORLD"] = str(world)
    env["MIND_META"] = str(meta)
    env["STORAGE_BACKEND"] = "local"      # guard-955 — see module docstring
    env.pop("MIND_AGENT", None)
    return subprocess.run(
        [PYTHON, str(script)] + args,
        input=stdin_text, text=True, capture_output=True, env=env, timeout=60,
    )


def _decompose_batch_payload() -> str:
    """The operation signature `_growth_log.decompose_rows` recognizes: flip the
    parent to interior AND give it children, in ONE batch."""
    return json.dumps({"operations": [
        {"op": "set", "key": "target", "field": "node_type",
         "value": "interior"},
        {"op": "add-child", "key": "target",
         "child": {"key": "child-a", "summary": "first decomposed child"}},
        {"op": "add-child", "key": "target",
         "child": {"key": "child-b", "summary": "second decomposed child"}},
    ]})


def _rows_from_disk(tree_path: Path, op: str = "DECOMPOSE"):
    """Re-open the file. Never inspect the command's stdout — stdout proving a
    row was COMPUTED is the exact thing this file exists not to trust."""
    tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
    return [r for r in (tree.get("tree_growth_log") or [])
            if r.get("op") == op]


# --------------------------- the end-to-end assertion ---------------------------

def test_decompose_row_reaches_disk_after_a_real_batch_decompose(tmp_path):
    """The gap this goal closes: run the real thing, then read the file back."""
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)

    r = _run_tree(["update", "--batch"], _decompose_batch_payload(), world, meta)
    assert r.returncode == 0, f"batch failed: stderr={r.stderr}"

    rows = _rows_from_disk(tree_path)
    assert len(rows) == 1, (
        f"expected exactly one DECOMPOSE row on disk, got {rows!r}. "
        f"stderr={r.stderr}")
    row = rows[0]
    assert row["node"] == "target"
    assert sorted(row["children"]) == ["child-a", "child-b"]
    assert row["reason"] == "batch decompose: 2 children"
    assert row["date"], "row must carry a date"


def test_the_batch_actually_mutated_the_tree(tmp_path):
    """Positive control on the FIXTURE, not on the log.

    Without this, a batch that silently no-ops would leave an empty growth log
    and the mutation test below would pass for entirely the wrong reason.
    """
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    r = _run_tree(["update", "--batch"], _decompose_batch_payload(), world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"

    nodes = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]
    assert nodes["target"]["node_type"] == "interior"
    assert sorted(nodes["target"]["children"]) == ["child-a", "child-b"]
    assert "child-a" in nodes and "child-b" in nodes


def test_ordinary_add_child_writes_no_row_to_disk(tmp_path):
    """Discrimination: the log must stay empty for a NON-decompose batch.

    `decompose_rows` deliberately logs nothing for a plain add-child (g-115-3210
    item 3 — logging every add would bury the structural signal). Asserted
    against disk so a serializer that invented rows would be caught too.
    """
    world, meta = tmp_path / "world", tmp_path / "meta"
    tree_path = _seed_tree(world)
    payload = json.dumps({"operations": [
        {"op": "add-child", "key": "target",
         "child": {"key": "child-a", "summary": "an ordinary child"}},
    ]})
    r = _run_tree(["update", "--batch"], payload, world, meta)
    assert r.returncode == 0, f"stderr={r.stderr}"
    assert _rows_from_disk(tree_path) == []


# ------------------------------ the mutation proof ------------------------------

def test_removing_the_growth_call_makes_the_row_vanish(tmp_path):
    """Verification outcome 2: the test FAILS if the growth-log call is removed.

    Proven by actually removing it. A copy of tree.py with the single
    `_growth_record_batch(...)` line deleted is run against an identical batch;
    the DECOMPOSE row must be ABSENT. Without this, the E2E assertion above
    could be passing on something other than the call it means to pin.

    The copy lives in core/scripts/ rather than tmp because tree.py resolves
    sibling scripts via `Path(__file__).parent` (tree-dedup-check.py,
    learning-routing-repair.py) — running it from elsewhere would change what
    the module under test can find, and the mutant would then differ from the
    original in more than the one line.
    """
    src = TREE_PY.read_text(encoding="utf-8")
    assert src.count(GROWTH_CALL) == 1, (
        "the batch growth-log call is no longer a unique literal in tree.py; "
        "this mutation test can no longer target it — update GROWTH_CALL")

    mutant = CORE_SCRIPTS / "tree__growthlog_mutant_g115_3898.py"
    mutant.write_text(src.replace(GROWTH_CALL, "pass"), encoding="utf-8")
    try:
        world, meta = tmp_path / "world", tmp_path / "meta"
        tree_path = _seed_tree(world)
        r = _run_tree(["update", "--batch"], _decompose_batch_payload(),
                      world, meta, script=mutant)
        assert r.returncode == 0, f"mutant batch failed: stderr={r.stderr}"

        # The batch must still have done its real work — otherwise "no row"
        # would be explained by "no batch", and this would prove nothing.
        nodes = yaml.safe_load(tree_path.read_text(encoding="utf-8"))["nodes"]
        assert nodes["target"]["node_type"] == "interior", (
            "mutant did not perform the decompose; the absent row below would "
            "be meaningless")

        assert _rows_from_disk(tree_path) == [], (
            "a DECOMPOSE row reached disk with the growth-log call REMOVED — "
            "the E2E assertion is not pinning what it claims to pin")
    finally:
        mutant.unlink(missing_ok=True)
