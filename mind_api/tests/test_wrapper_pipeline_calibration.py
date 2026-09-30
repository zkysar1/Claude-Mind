"""End-to-end: the calibration-ledger capture in pipeline-add.sh / pipeline-move.sh
(g-306-553), through the real wrappers and a running daemon.

The unit tests in core/scripts/tests/test_confidence_ledger_hypothesis.py pin the
verdict map and the row. These pin what only a live round trip can show:

  1. FORMATION: a record added with `tests_node` keeps it on disk. The daemon
     stores unknown keys verbatim, but "the field survives the write path" is a
     claim about the write path, so it is measured here, not assumed.
  2. RESOLUTION: after the daemon's 200 on a move INTO resolved, exactly one row
     lands, carrying the node's confidence from the tree index (0.7, a NON-NULL
     positive control), while the wrapper's stdout is still the record.
  3. What must NOT write a row: a formation add, a record with no link, and a
     second resolve of the same record (the daemon refuses it).

HERMETIC BY PRECONDITION: the capture runs CLIENT-side and resolves the world
through _paths, whose precedence is MIND_WORLD, then .mind-data/, then
local-paths.conf. The mind_api conftest POPS MIND_WORLD, and this repo HAS a
.mind-data/, so an unpinned run would append to the real ledger. Every wrapper
call pins MIND_WORLD + STORAGE_BACKEND=local, and the first test asserts that
the client resolves the tmp world before any write can happen (guard-7359: a
control is a precondition with its own branch).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

_INDEX = """\
last_updated: '2026-09-29'
nodes:
  known-node:
    confidence: 0.7
    file: world/knowledge/tree/x/known-node.md
"""


def _active(rec_id, **extra):
    rec = {
        "id": rec_id,
        "title": "Calibration capture wrapper test hypothesis",
        "stage": "active",
        "horizon": "session",
        "type": "calibration",
        "confidence": 0.6,
        "position": "YES this is a valid multi-word hypothesis position",
        "claim": "The linked node's claim holds under this wrapper test",
        "formed_date": "2026-09-29",
        "category": "test-cat",
        "reflected": False,
    }
    rec.update(extra)
    return rec


_LINK = {"key": "known-node", "stance": "supports"}
_RESOLVE = {"outcome": "CONFIRMED",
            "outcome_detail": "Settled by the g-306-553 wrapper test fixture."}


@pytest.fixture
def world(running_daemon):
    project_root, _port = running_daemon
    world = project_root / "world"
    (world / "pipeline.jsonl").write_text(
        json.dumps(_active("2026-09-29_linked", tests_node=_LINK)) + "\n"
        + json.dumps(_active("2026-09-29_unlinked")) + "\n", encoding="utf-8")
    (world / "pipeline-archive.jsonl").write_text("", encoding="utf-8")
    tree = world / "knowledge" / "tree"
    tree.mkdir(parents=True, exist_ok=True)
    (tree / "_tree.yaml").write_text(_INDEX, encoding="utf-8")
    return project_root, world


def _env(project_root, world):
    env = os.environ.copy()
    env["MSYS_NO_PATHCONV"] = "1"
    env["RT_DIR"] = str(project_root / "mind_api" / "state")
    env["MIND_AGENT"] = "alpha"
    env["MIND_RUNTIME_DISABLE_SPAWN"] = "1"
    env["MIND_WORLD"] = str(world)
    env["STORAGE_BACKEND"] = "local"
    return env


def _run(project_root, world, script, args, stdin=""):
    return subprocess.run(
        [shutil.which("bash") or "bash",
         (REPO_ROOT / "core" / "scripts" / script).as_posix()] + args,
        capture_output=True, text=True, timeout=30, input=stdin or None,
        env=_env(project_root, world))


def _ledger(world):
    led = world / "confidence-calibration-ledger.jsonl"
    if not led.exists():
        return []
    return [json.loads(x) for x in led.read_text(encoding="utf-8").splitlines()
            if x.strip()]


def _live(world):
    return {json.loads(x)["id"]: json.loads(x)
            for x in (world / "pipeline.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()}


def test_the_client_side_capture_resolves_the_tmp_world(world):
    """PRECONDITION for every other test in this file."""
    project_root, w = world
    out = subprocess.run(
        [shutil.which("python3") or "python3", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import _paths; "
         "print(_paths.WORLD_DIR)", str(REPO_ROOT / "core" / "scripts")],
        capture_output=True, text=True, timeout=30, env=_env(project_root, w))
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve() == w.resolve()


def test_a_formation_add_keeps_tests_node_and_writes_no_row(world):
    project_root, w = world
    rec = _active("2026-09-29_formed", stage="discovered",
                  tests_node={"key": "known-node", "stance": "challenges"})
    res = _run(project_root, w, "pipeline-add.sh", [], stdin=json.dumps(rec))
    assert res.returncode == 0, res.stderr
    assert json.loads(res.stdout)["tests_node"] == rec["tests_node"]
    assert _live(w)["2026-09-29_formed"]["tests_node"] == rec["tests_node"]
    assert _ledger(w) == []


def test_resolving_a_linked_record_writes_one_non_null_row(world):
    project_root, w = world
    res = _run(project_root, w, "pipeline-move.sh",
               ["2026-09-29_linked", "resolved"], stdin=json.dumps(_RESOLVE))
    assert res.returncode == 0, res.stderr
    printed = json.loads(res.stdout)
    assert (printed["id"], printed["stage"]) == ("2026-09-29_linked", "resolved")
    (row,) = _ledger(w)
    assert row["entry_id"] == "known-node"
    assert row["declared_confidence"] == 0.7
    assert row["verdict"] == "survived"
    assert row["evidence_ref"] == "2026-09-29_linked"
    assert row["source"] == "hypothesis-resolution"
    assert row["agent"] == "alpha"

    again = _run(project_root, w, "pipeline-move.sh",
                 ["2026-09-29_linked", "resolved"], stdin=json.dumps(_RESOLVE))
    assert again.returncode != 0
    assert len(_ledger(w)) == 1


def test_resolving_an_unlinked_record_writes_no_row(world):
    project_root, w = world
    res = _run(project_root, w, "pipeline-move.sh",
               ["2026-09-29_unlinked", "resolved"], stdin=json.dumps(_RESOLVE))
    assert res.returncode == 0, res.stderr
    assert _ledger(w) == []


def test_an_add_at_stage_resolved_is_a_resolution(world):
    project_root, w = world
    rec = _active("2026-09-29_added-resolved", stage="resolved",
                  tests_node={"key": "known-node", "stance": "challenges"},
                  **_RESOLVE)
    res = _run(project_root, w, "pipeline-add.sh", [], stdin=json.dumps(rec))
    assert res.returncode == 0, res.stderr
    (row,) = _ledger(w)
    assert (row["entry_id"], row["verdict"], row["declared_confidence"]) == (
        "known-node", "refuted", 0.7)
