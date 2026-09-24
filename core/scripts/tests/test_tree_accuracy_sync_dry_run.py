"""tree-accuracy-sync.py --dry-run must not persist the category->node
bindings cache (g-001-02, 2026-09-24).

A dry-run pinned hypothesis-pipeline -> `pip` (an unrelated product-spec node,
fuzzy-matched on the `pip` prefix of `pipeline`) into the shared bindings
file, so the next real run would have written accuracy onto that node.
These tests drive BOTH the function and the production call site (main with
--dry-run), and carry a positive control that a real run still persists.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent
MODULE_PATH = SCRIPT_DIR / "tree-accuracy-sync.py"

GROUPS = {"cat-new": {"confirmed": 1, "total": 1}}


def _load():
    if str(SCRIPT_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPT_DIR))
    spec = importlib.util.spec_from_file_location("_tas_under_test", str(MODULE_PATH))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tree():
    return {"nodes": {"node-a": {}}, "entity_index": {}}


@pytest.fixture
def tas(tmp_path, monkeypatch):
    mod = _load()
    # Never let a test touch the real shared cache (guard-6402).
    monkeypatch.setattr(mod, "BINDINGS_PATH", tmp_path / "bindings.json")
    monkeypatch.setattr(
        mod,
        "find_nodes",
        lambda category, nodes, entity_index, top=1, leaf_only=True: [
            {"key": "node-a", "file": "", "depth": 1, "summary": "", "node_type": "leaf"}
        ],
    )
    return mod


def _run_main(tas, monkeypatch, argv):
    records = [{"category": "cat-new", "outcome": "CONFIRMED", "stage": "resolved"}]
    monkeypatch.setattr(tas, "load_pipeline_records", lambda: records)
    monkeypatch.setattr(tas, "read_tree", _tree)
    writes = []
    monkeypatch.setattr(tas, "write_tree", lambda tree: writes.append(tree))
    monkeypatch.setattr(sys, "argv", ["tree-accuracy-sync.py", *argv])
    assert tas.main() == 0
    return writes


def test_plan_updates_without_persist_leaves_cache_absent(tas):
    plan, summary = tas.plan_updates(GROUPS, _tree(), persist_bindings=False)
    assert not tas.BINDINGS_PATH.exists()
    # The preview still REPORTS the binding it would make.
    assert summary["new_bindings"] == [{"category": "cat-new", "node_key": "node-a"}]
    assert plan and plan[0][0] == "node-a"


def test_plan_updates_default_still_persists(tas):
    # Positive control: a save_bindings that never ran would pass the test above.
    tas.plan_updates(GROUPS, _tree())
    assert json.loads(tas.BINDINGS_PATH.read_text(encoding="utf-8")) == {"cat-new": "node-a"}


def test_main_dry_run_writes_neither_cache_nor_tree(tas, monkeypatch):
    writes = _run_main(tas, monkeypatch, ["--dry-run"])
    assert not tas.BINDINGS_PATH.exists()
    assert writes == []


def test_main_real_run_persists_cache_and_tree(tas, monkeypatch):
    writes = _run_main(tas, monkeypatch, [])
    assert json.loads(tas.BINDINGS_PATH.read_text(encoding="utf-8")) == {"cat-new": "node-a"}
    assert len(writes) == 1
