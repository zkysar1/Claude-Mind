"""test_goal_script_orphan_gate.py — .

Unit tests for goal-script-orphan-gate.py, the inverse-direction companion
to scripts-referenced-gate.py.

Background: scripts-referenced-gate catches `core/scripts/*.{sh,py}` files
orphan FROM references. This gate catches the OPPOSITE: goal description
+ skill fields that NAME `core/scripts/*.{sh,py}` paths NOT present on
disk. The originating incident (g-115-603) hid a deleted-script reference
for 1 week — the gate exists so that class of orphan never sits silent
again.

Cases covered:
  1. clean: goal references an existing script -> 0 orphans, exit 0.
  2. orphan: goal references a nonexistent script -> 1 orphan, exit 1.
  3. dedup: same goal references the same nonexistent script in both
     description AND skill -> still 1 orphan (de-duped).
  4. status filter: completed/skipped/superseded goals are skipped by
     default; --include-completed-goals widens the scan.
  5. multi-source: world + agent each contribute orphans.
  6. invoker prefix variants: matches bash/python3/py -3 prefixes AND
     bare `core/scripts/...` paths in narrative.
  7. parse-tolerant: bad JSONL lines are skipped, valid lines processed.

Pattern: tempdir world+agent + monkeypatch the script's WORLD_DIR /
AGENT_DIR / SCRIPTS_DIR module-globals to point at the temp tree, then
invoke main() in-process. No subprocess, no daemon — the gate is a pure
analysis script over disk state.

Run: py -3 -m pytest core/scripts/tests/test_goal_script_orphan_gate.py -v
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import importlib  # noqa: E402

# Import via importlib so the hyphenated filename resolves cleanly.
gsog = importlib.import_module("goal-script-orphan-gate")


@pytest.fixture
def world_tmp(tmp_path, monkeypatch):
    """Build a tempdir 'world' + 'scripts' tree and rewire module globals.

    Returns a helper namespace with handles for adding aspirations + scripts.
    """
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    world = tmp_path / "world"
    world.mkdir()
    agent = tmp_path / "agent"
    agent.mkdir()

    monkeypatch.setattr(gsog, "SCRIPTS_DIR", scripts_dir)
    monkeypatch.setattr(gsog, "WORLD_DIR", world)
    monkeypatch.setattr(gsog, "AGENT_DIR", agent)

    class Helper:
        def add_script(self, name: str):
            (scripts_dir / name).write_text("#!/bin/sh\necho hi\n",
                                            encoding="utf-8")

        def write_aspirations(self, source: str, asp_dict: dict):
            target = (world if source == "world" else agent) / "aspirations.jsonl"
            with open(target, "a", encoding="utf-8") as f:
                f.write(json.dumps(asp_dict, ensure_ascii=False) + "\n")

    return Helper()


def _run_gate(args: list = None):
    """Run gsog.main() with argv = ['gate.py', *args] and capture sys.exit.

    Returns (exit_code, stdout_json_or_None) by parsing the JSON the gate
    prints on stdout in non-text mode.
    """
    if args is None:
        args = []
    saved_argv = sys.argv
    sys.argv = ["goal-script-orphan-gate.py", *args]
    try:
        captured = []
        old_stdout = sys.stdout

        class Cap:
            def write(self, s):
                captured.append(s)
                old_stdout.write(s)

            def flush(self):
                old_stdout.flush()

        sys.stdout = Cap()
        try:
            try:
                gsog.main()
                rc = 0
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else 0
        finally:
            sys.stdout = old_stdout
        # Parse the last non-empty stdout line as JSON.
        out_text = "".join(captured).strip()
        last_line = out_text.splitlines()[-1] if out_text else ""
        try:
            parsed = json.loads(last_line)
        except (json.JSONDecodeError, IndexError):
            parsed = None
        return rc, parsed
    finally:
        sys.argv = saved_argv


# ---- Test cases ----------------------------------------------------------


def test_clean_existing_script_reference(world_tmp):
    """Goal references an existing script -> no orphans, exit 0."""
    world_tmp.add_script("existing-tool.sh")
    world_tmp.write_aspirations("world", {
        "id": "asp-test",
        "goals": [{
            "id": "g-test-001",
            "status": "pending",
            "description": "Run `bash core/scripts/existing-tool.sh --apply` daily.",
        }],
    })
    rc, j = _run_gate()
    assert rc == 0, f"clean run must exit 0; got rc={rc}, json={j}"
    assert j["orphan_count"] == 0
    assert j["would_block"] is False
    assert j["total_references"] == 1


def test_orphan_nonexistent_script_reference(world_tmp):
    """Goal references a missing script -> 1 orphan, exit 1."""
    world_tmp.add_script("present.sh")  # script that DOES exist
    world_tmp.write_aspirations("world", {
        "id": "asp-test",
        "goals": [{
            "id": "g-test-002",
            "status": "pending",
            "description": "Run `bash core/scripts/deleted-script.sh` weekly.",
        }],
    })
    rc, j = _run_gate()
    assert rc == 1, f"orphan must exit 1; got rc={rc}"
    assert j["orphan_count"] == 1
    assert j["would_block"] is True
    orphan = j["orphan_references"][0]
    assert orphan["goal_id"] == "g-test-002"
    assert orphan["script_name"] == "deleted-script.sh"
    assert orphan["source"] == "world"


def test_dedup_same_goal_both_fields(world_tmp):
    """Same goal naming the same missing script in description AND skill
    yields exactly 1 dedup'd orphan entry."""
    world_tmp.write_aspirations("world", {
        "id": "asp-test",
        "goals": [{
            "id": "g-test-003",
            "status": "pending",
            "description": "Sweep via core/scripts/missing-sweep.sh weekly.",
            "skill": "bash core/scripts/missing-sweep.sh",
        }],
    })
    rc, j = _run_gate()
    assert rc == 1
    assert j["orphan_count"] == 1, (
        f"dedup expected 1 orphan; got {j['orphan_references']}")
    assert j["total_references"] == 2, (
        "before dedup the gate sees 2 raw references")


def test_completed_goal_skipped_by_default(world_tmp):
    """Completed goals are historical — orphan refs in them are skipped
    by default. --include-completed-goals widens the scope."""
    world_tmp.write_aspirations("world", {
        "id": "asp-test",
        "goals": [{
            "id": "g-test-004",
            "status": "completed",
            "description": "Once ran `bash core/scripts/ancient-tool.sh`.",
        }],
    })
    # Default: completed goals not scanned -> 0 orphans, exit 0.
    rc, j = _run_gate()
    assert rc == 0
    assert j["orphan_count"] == 0
    # With --include-completed-goals: the orphan surfaces.
    rc, j = _run_gate(["--include-completed-goals"])
    assert rc == 1
    assert j["orphan_count"] == 1
    assert j["orphan_references"][0]["status"] == "completed"


def test_multi_source_world_and_agent(world_tmp):
    """Each source contributes its own orphans; both surface in one run."""
    world_tmp.write_aspirations("world", {
        "id": "asp-w",
        "goals": [{
            "id": "g-w-001",
            "status": "pending",
            "description": "Run `bash core/scripts/world-only-missing.sh`.",
        }],
    })
    world_tmp.write_aspirations("agent", {
        "id": "asp-a",
        "goals": [{
            "id": "g-a-001",
            "status": "in-progress",
            "description": "Probe `python3 core/scripts/agent-only-missing.py`.",
        }],
    })
    rc, j = _run_gate()
    assert rc == 1
    assert j["orphan_count"] == 2
    sources = {o["source"] for o in j["orphan_references"]}
    assert sources == {"world", "agent"}


def test_invoker_prefix_variants(world_tmp):
    """Match patterns under multiple invoker prefixes."""
    # Each goal names a unique missing script via a different invocation form.
    world_tmp.write_aspirations("world", {
        "id": "asp-test",
        "goals": [
            {"id": "g-prefix-1", "status": "pending",
             "description": "`bash core/scripts/missing-a.sh`"},
            {"id": "g-prefix-2", "status": "pending",
             "description": "`python3 core/scripts/missing-b.py`"},
            {"id": "g-prefix-3", "status": "pending",
             "description": "`py -3 core/scripts/missing-c.py --json`"},
            {"id": "g-prefix-4", "status": "pending",
             "description": "See core/scripts/missing-d.sh for details."},
        ],
    })
    rc, j = _run_gate()
    assert rc == 1
    assert j["orphan_count"] == 4, (
        f"all 4 invoker shapes must match; got {[o['script_name'] for o in j['orphan_references']]}")
    names = {o["script_name"] for o in j["orphan_references"]}
    assert names == {
        "missing-a.sh", "missing-b.py", "missing-c.py", "missing-d.sh",
    }


def test_malformed_jsonl_skipped(world_tmp):
    """Bad JSONL lines are tolerated — the gate continues with valid lines."""
    target = gsog.WORLD_DIR / "aspirations.jsonl"
    # Write one bad line then one good line with an orphan ref.
    target.write_text(
        "this is not json\n"
        + json.dumps({
            "id": "asp-test",
            "goals": [{
                "id": "g-malformed-tolerant",
                "status": "pending",
                "description": "Calls `bash core/scripts/missing-after-bad.sh`.",
            }],
        }) + "\n",
        encoding="utf-8",
    )
    rc, j = _run_gate()
    assert rc == 1
    assert j["orphan_count"] == 1
    assert j["orphan_references"][0]["goal_id"] == "g-malformed-tolerant"
