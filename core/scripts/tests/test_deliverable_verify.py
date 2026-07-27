# Tests for deliverable-verify.py ().
#
# The helper is pure + fail-open; every case here is hermetic (tmp files only,
# no world/agent state, no S3-backed store). It verifies a recurring goal's
# `deliverable_file` was regenerated since its prior close, so recurring-close.sh
# can FLAG the rb-428 LLM-abbreviation drift (close advances lastAchievedAt
# without the deliverable-writing step having run).
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "deliverable-verify.py"


def _import():
    spec = importlib.util.spec_from_file_location("deliverable_verify", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["deliverable_verify"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _import()

LAST = "2026-07-11T00:00:00"


def _write_queue(tmp_path, goal):
    """Write a one-aspiration aspirations.jsonl containing `goal`. Return its path."""
    sf = tmp_path / "aspirations.jsonl"
    asp = {"id": "asp-001", "goals": [goal]}
    sf.write_text(json.dumps(asp) + "\n", encoding="utf-8")
    return sf


def _touch(path, iso):
    """Create `path` and set its mtime to the given ISO timestamp."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")
    ts = datetime.datetime.fromisoformat(iso).timestamp()
    os.utime(path, (ts, ts))


def test_no_field_skips(tmp_path):
    # Outcome 3: a goal WITHOUT deliverable_file closes exactly as before (skip).
    sf = _write_queue(tmp_path, {"id": "g-1", "recurring": True, "lastAchievedAt": LAST})
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "skip"


def test_null_lastachieved_skips(tmp_path):
    # First close (no baseline) → cannot compare → skip.
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": None,
        "deliverable_file": "report.md",
    })
    _touch(tmp_path / "report.md", "2026-07-11T12:00:00")
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "skip"


def test_goal_not_found_skips(tmp_path):
    sf = _write_queue(tmp_path, {"id": "g-1", "recurring": True, "lastAchievedAt": LAST})
    assert mod.verdict("g-DOES-NOT-EXIST", str(sf), "alpha", str(tmp_path)) == "skip"


def test_advanced(tmp_path):
    # Deliverable mtime AFTER lastAchievedAt → regenerated since prior close.
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": LAST,
        "deliverable_file": "report.md",
    })
    _touch(tmp_path / "report.md", "2026-07-11T12:00:00")
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "advanced"


def test_stale(tmp_path):
    # Deliverable mtime BEFORE lastAchievedAt → NOT regenerated (the drift).
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": LAST,
        "deliverable_file": "report.md",
    })
    _touch(tmp_path / "report.md", "2026-07-10T00:00:00")
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "stale"


def test_missing(tmp_path):
    # deliverable_file names a path that does not exist on disk.
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": LAST,
        "deliverable_file": "never-written.md",
    })
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "missing"


def test_agent_placeholder_expands(tmp_path):
    # {agent} expands so a shared recurring goal names a per-agent deliverable (rb-1556).
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": LAST,
        "deliverable_file": "agents/{agent}/COMPLETION-REPORT.md",
    })
    _touch(tmp_path / "agents" / "zeta" / "COMPLETION-REPORT.md", "2026-07-11T12:00:00")
    # alpha's report is absent → missing for alpha, advanced for zeta.
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "missing"
    assert mod.verdict("g-1", str(sf), "zeta", str(tmp_path)) == "advanced"


def test_absolute_deliverable_path(tmp_path):
    # An absolute deliverable_file is honored as-is (project_root not prepended).
    target = tmp_path / "out" / "abs-report.md"
    _touch(target, "2026-07-11T12:00:00")
    sf = _write_queue(tmp_path, {
        "id": "g-1", "recurring": True, "lastAchievedAt": LAST,
        "deliverable_file": str(target),
    })
    assert mod.verdict("g-1", str(sf), "alpha", "/nonexistent-root") == "advanced"


def test_malformed_jsonl_skips(tmp_path):
    # Fail-open: unparseable source file → skip (never flag falsely, never block).
    sf = tmp_path / "aspirations.jsonl"
    sf.write_text("{not valid json\n", encoding="utf-8")
    assert mod.verdict("g-1", str(sf), "alpha", str(tmp_path)) == "skip"


def test_exit_code_always_zero(tmp_path, capfd):
    # The CLI entrypoint prints the verdict and always exits 0 (flag, not gate).
    import subprocess
    sf = _write_queue(tmp_path, {"id": "g-1", "recurring": True, "lastAchievedAt": LAST})
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--goal-id", "g-1", "--source-file", str(sf),
         "--agent", "alpha", "--project-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert r.stdout.strip() == "skip"
