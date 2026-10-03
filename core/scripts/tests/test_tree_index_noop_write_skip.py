""": a tree-index update that changes nothing must not rewrite _tree.yaml.

Under own-cloud every rewrite of the ~2 MB knowledge-tree index is a new object
version, and 160 of 687 consecutive version pairs in one 24 h window carried the
same content hash (byte-identical, measured by g-358-229). The dominant source is
the PostToolUse front-matter sync, which assigns today's date to the edited node
and to the index on EVERY node edit, so the second edit of a node on the same day
rewrites exactly what it just read. The other source is the drift-check backfill,
which rewrites the index even when no node drifted.

Both call sites now pass `skip_if_unchanged=True` to `locked_modify_yaml`
(g-115-11231). The tests count calls to the atomic writer (the effect), not file
contents, because an identical rewrite leaves identical bytes (guard-1965), and
every skip case has a positive control that goes through the same call and DOES
write. The counter-bump writers (`retrieve.py`, `utilization-feedback.py`) change
a counter whenever they find a node, so they are deliberately not covered here.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import yaml  # noqa: E402

import _fileops  # noqa: E402

TODAY = "2026-10-02"
YESTERDAY = "2026-10-01"


def _load(name: str, filename: str):
    """Import a hyphen-named script by path; it is not importable by name."""
    spec = importlib.util.spec_from_file_location(name, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sync = _load("tree_front_matter_sync_under_test", "tree-front-matter-sync.py")
drift = _load("tree_last_updated_drift_check_under_test", "tree-last-updated-drift-check.py")


@pytest.fixture
def writes(monkeypatch):
    calls = []
    real = _fileops._atomic_write_with_fallback

    def _counting(path, write_fn, **kw):
        calls.append(Path(path))
        return real(path, write_fn, **kw)

    monkeypatch.setattr(_fileops, "_atomic_write_with_fallback", _counting)
    return calls


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A tmp world both scripts resolve their index and node files against."""
    root = tmp_path / "world"
    (root / "knowledge" / "tree").mkdir(parents=True)
    monkeypatch.setattr(sync, "WORLD_DIR", root)
    monkeypatch.setattr(drift, "WORLD_DIR", root)
    monkeypatch.setattr(drift, "TREE_PATH", str(root / "knowledge" / "tree" / "_tree.yaml"))
    return root


def _seed(world: Path, dates: dict, top: str) -> Path:
    """Write the index and one node .md per key; return the index path.

    dates: {key: (index last_updated, node front-matter last_updated)}. The index is
    dumped the way `locked_modify_yaml` dumps it, so a date-like string round-trips
    as a string, and each node carries the other fields a real one has.
    """
    nodes = {}
    for key, (idx_date, fm_date) in dates.items():
        rel = f"knowledge/tree/{key}.md"
        nodes[key] = {"file": f"world/{rel}", "depth": 1, "summary": f"{key} summary",
                      "retrieval_count": 3, "last_updated": idx_date}
        (world / rel).write_text(f"---\nlast_updated: '{fm_date}'\n---\n# {key}\n",
                                 encoding="utf-8")
    index = world / "knowledge" / "tree" / "_tree.yaml"
    index.write_text(
        yaml.dump({"version": 1, "last_updated": top, "nodes": nodes},
                  Dumper=yaml.CSafeDumper, default_flow_style=False,
                  allow_unicode=True, sort_keys=False),
        encoding="utf-8")
    return index


def _node_dates(index: Path) -> dict:
    nodes = yaml.safe_load(index.read_text(encoding="utf-8"))["nodes"]
    return {key: node["last_updated"] for key, node in nodes.items()}


# --- tree-front-matter-sync.py: _bump_tree_yaml_last_updated -----------------------


def test_first_bump_of_the_day_writes_once(world, writes):
    # Positive control: the call the skip test repeats DOES write when the node
    # still carries yesterday's date.
    index = _seed(world, {"node-a": (YESTERDAY, YESTERDAY), "node-b": (YESTERDAY, YESTERDAY)},
                  top=YESTERDAY)

    assert sync._bump_tree_yaml_last_updated("node-a", TODAY) is True

    assert writes == [index]
    assert _node_dates(index) == {"node-a": TODAY, "node-b": YESTERDAY}
    assert yaml.safe_load(index.read_text(encoding="utf-8"))["last_updated"] == TODAY


def test_second_same_day_bump_of_a_node_writes_nothing(world, writes):
    index = _seed(world, {"node-a": (YESTERDAY, YESTERDAY), "node-b": (YESTERDAY, YESTERDAY)},
                  top=YESTERDAY)
    assert sync._bump_tree_yaml_last_updated("node-a", TODAY) is True
    assert writes == [index]  # the first bump of the day wrote once
    after_first = index.read_bytes()

    assert sync._bump_tree_yaml_last_updated("node-a", TODAY) is True

    assert writes == [index]  # still the one write: the second bump added none
    assert index.read_bytes() == after_first


def test_bump_of_another_node_the_same_day_is_written(world, writes):
    # node-a and the top-level date already read today; node-b does not, so the
    # result differs from what was read and must reach the index.
    index = _seed(world, {"node-a": (TODAY, TODAY), "node-b": (YESTERDAY, YESTERDAY)}, top=TODAY)

    assert sync._bump_tree_yaml_last_updated("node-b", TODAY) is True

    assert writes == [index]
    assert _node_dates(index) == {"node-a": TODAY, "node-b": TODAY}


# --- tree-last-updated-drift-check.py: cmd_apply ------------------------------------


def test_drift_apply_with_nothing_drifted_writes_nothing(world, writes, capsys):
    index = _seed(world, {"node-a": (TODAY, TODAY), "node-b": (YESTERDAY, YESTERDAY)}, top=TODAY)
    before = index.read_bytes()

    assert drift.cmd_apply() == 0

    assert writes == []
    assert index.read_bytes() == before
    assert '"backfilled": 0' in capsys.readouterr().out


def test_drift_apply_with_a_drifted_node_writes_once(world, writes, capsys):
    # Positive control: node-b's index date is behind its front matter, so the
    # backfill changes the result and the index is written once.
    index = _seed(world, {"node-a": (TODAY, TODAY), "node-b": (YESTERDAY, TODAY)}, top=TODAY)

    assert drift.cmd_apply() == 0

    assert writes == [index]
    assert _node_dates(index) == {"node-a": TODAY, "node-b": TODAY}
    assert '"backfilled": 1' in capsys.readouterr().out
