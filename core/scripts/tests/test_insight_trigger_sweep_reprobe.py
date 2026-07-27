""" / rb-1150: insight-trigger-sweep apply-time re-probe regression.

Verifies the sweeper's audit-time -> apply-time staleness gap closure:
when an insight_trigger carries `affects:<goal-id>` and the target goal's
current status is terminal at filing time, the sweep skips the Apply spawn
and emits an audit-stale note instead. Three cases pin the contract:

  1. target pending      -> file as-is (no audit-stale skip)
  2. target completed    -> skip Apply + emit note (audit_stale += 1)
  3. target missing      -> file as-is with warning (affects_missing += 1)

Canonical incident: zeta's 06:37 audit spawned a supersession-Apply at
18:11 via insight-trigger-sweep; the target g-115-922 had already closed
at 15:32. The re-probe at filing time catches that drift.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_sweep_reprobe.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent

# Load insight-trigger-sweep.py as a module — script name has hyphens so
# we cannot `import insight-trigger-sweep` directly.
SWEEP_PATH = CORE_SCRIPTS / "insight-trigger-sweep.py"
_spec = importlib.util.spec_from_file_location("its_under_test", SWEEP_PATH)
its = importlib.util.module_from_spec(_spec)
sys.modules["its_under_test"] = its
_spec.loader.exec_module(its)


def _findings_line(msg_id, *, author, target, action, severity,
                   affects_goal, timestamp):
    tags = [
        f"requires_action_by:{target}",
        f"action_type:{action}",
        f"severity:{severity}",
    ]
    if affects_goal:
        tags.append(f"affects:{affects_goal}")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "channel": "findings",
        "type": "finding",
        "text": f"test trigger {msg_id}",
        "tags": tags,
        "timestamp": timestamp,
    }) + "\n"


def _aspiration_record(asp_id, goals):
    return json.dumps({
        "id": asp_id,
        "status": "active",
        "goals": goals,
    }) + "\n"


@pytest.fixture
def sandbox(monkeypatch, tmp_path: Path):
    """Sandbox WORLD_ASPS + a fake agents_root() with one agent dir.

    Patches:
      - its.WORLD_ASPS: tmp world's aspirations.jsonl
      - its.FINDINGS: tmp world's findings.jsonl
      - its._agents_root: lambda returning an agents/ dir with one agent
      - its.file_goal: lambda recording calls (assertable)
      - its._emit_audit_stale_note: lambda recording calls (assertable)

    Capture lists are attached to the returned namespace so tests can
    assert what got filed and what got noted.
    """
    world = tmp_path / "world"
    world.mkdir()
    findings = world / "board" / "findings.jsonl"
    findings.parent.mkdir(parents=True)
    asp_jsonl = world / "aspirations.jsonl"
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    test_agent = agents_dir / "zeta-test"
    test_agent.mkdir()
    (test_agent / "local-paths.conf").write_text(
        f'WORLD_PATH="{world}"\nMETA_PATH="{tmp_path / "meta"}"\n',
        encoding="utf-8",
    )
    # Empty agent-side aspirations.jsonl so probe_goal_status walks it
    # cleanly (the iterator should not stumble on a missing file).
    (test_agent / "aspirations.jsonl").write_text("", encoding="utf-8")

    monkeypatch.setattr(its, "WORLD_ASPS", asp_jsonl)
    monkeypatch.setattr(its, "FINDINGS", findings)
    monkeypatch.setattr(its, "_agents_root", lambda: agents_dir)

    filed_calls = []

    def fake_file_goal(trigger, *, dry_run=False):
        filed_calls.append({"trigger": trigger, "dry_run": dry_run})
        return {"would_file": dry_run, "rc": 0, "stdout": "", "stderr": ""}

    note_calls = []

    def fake_note(trigger, target_status):
        note_calls.append({"trigger": trigger, "target_status": target_status})
        return {"posted": True, "msg_id": f"fake-note-{trigger['msg_id']}"}

    monkeypatch.setattr(its, "file_goal", fake_file_goal)
    monkeypatch.setattr(its, "_emit_audit_stale_note", fake_note)

    return {
        "world": world,
        "findings": findings,
        "asp_jsonl": asp_jsonl,
        "agents_dir": agents_dir,
        "filed_calls": filed_calls,
        "note_calls": note_calls,
    }


def _run_main(argv):
    """Run its.main() under a controlled sys.argv; capture exit code."""
    saved = sys.argv
    sys.argv = ["insight-trigger-sweep.py"] + argv
    try:
        rc = its.main()
    finally:
        sys.argv = saved
    return rc


def _trigger_timestamp(hours_ago=2.0):
    """Build a timestamp inside the (GRACE_HOURS, WINDOW_HOURS) window so
    load_triggers admits it. Default 2h ago — past 1h grace, well inside
    24h window."""
    return (datetime.now() - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def test_case1_affects_pending_files_as_is(sandbox, capsys):
    """target pending -> file as-is; no audit-stale skip."""
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line(
            "msg-test-1-pending", author="charlie",
            target="zeta", action="extend-filter", severity="constrains",
            affects_goal="g-100-001", timestamp=ts,
        ),
        encoding="utf-8",
    )
    # Target goal exists with status="pending"
    sandbox["asp_jsonl"].write_text(
        _aspiration_record("asp-test", [
            {"id": "g-100-001", "status": "pending", "title": "test goal"},
        ]),
        encoding="utf-8",
    )

    rc = _run_main(["--dry-run", "--json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["scanned"] == 1
    assert summary["audit_stale"] == 0, "pending target must NOT trigger audit-stale"
    assert summary["affects_missing"] == 0, "pending target is found — no missing"
    assert summary["filed"] == 1, "trigger must be filed as-is"
    assert len(sandbox["filed_calls"]) == 1
    assert len(sandbox["note_calls"]) == 0


def test_case2_affects_completed_skips_apply_emits_note(sandbox, capsys):
    """target completed -> skip Apply + emit audit-stale note."""
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line(
            "msg-test-2-completed", author="charlie",
            target="zeta", action="supersession-decision", severity="constrains",
            affects_goal="g-100-002", timestamp=ts,
        ),
        encoding="utf-8",
    )
    # Target goal exists with status="completed" — the canonical
    # rb-1150 staleness shape (zeta 06:37 audit -> charlie 18:11 spawn
    # after target closed at 15:32).
    sandbox["asp_jsonl"].write_text(
        _aspiration_record("asp-test", [
            {"id": "g-100-002", "status": "completed", "title": "already done"},
        ]),
        encoding="utf-8",
    )

    rc = _run_main(["--json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["scanned"] == 1
    assert summary["audit_stale"] == 1, "completed target MUST trigger audit-stale"
    assert summary["affects_missing"] == 0
    assert summary["filed"] == 0, "Apply must NOT spawn for terminal-status target"
    assert len(sandbox["filed_calls"]) == 0, "file_goal must NOT be called"
    assert len(sandbox["note_calls"]) == 1, "audit-stale note MUST be emitted"
    # Note payload carries the right fields
    note = sandbox["note_calls"][0]
    assert note["target_status"] == "completed"
    assert note["trigger"]["affects_goal"] == "g-100-002"
    # Summary details record the audit-stale entry
    assert len(summary["audit_stale_details"]) == 1
    assert summary["audit_stale_details"][0]["affects_goal"] == "g-100-002"
    assert summary["audit_stale_details"][0]["target_status"] == "completed"


def test_case3_affects_missing_files_with_warning(sandbox, capsys):
    """target missing -> file as-is, affects_missing flag set."""
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line(
            "msg-test-3-missing", author="charlie",
            target="zeta", action="extend-filter", severity="constrains",
            affects_goal="g-100-999", timestamp=ts,
        ),
        encoding="utf-8",
    )
    # Empty aspirations — target goal NOT present in any queue
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")

    rc = _run_main(["--dry-run", "--json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["scanned"] == 1
    assert summary["audit_stale"] == 0, "missing target is NOT audit-stale"
    assert summary["affects_missing"] == 1, "missing target must increment affects_missing"
    assert summary["filed"] == 1, "missing target should still file as-is with warning"
    assert len(sandbox["filed_calls"]) == 1
    assert len(sandbox["note_calls"]) == 0
    # Warning detail surfaced in summary
    assert len(summary["affects_missing_details"]) == 1
    assert summary["affects_missing_details"][0]["affects_goal"] == "g-100-999"


def test_case4_no_affects_tag_files_unchanged(sandbox, capsys):
    """Sanity: trigger without affects:<id> tag must NOT trigger probe path.

    This is the pre-g-115-1076 baseline — old triggers without affects
    tags must still file normally (backward compatibility).
    """
    ts = _trigger_timestamp()
    sandbox["findings"].write_text(
        _findings_line(
            "msg-test-4-no-affects", author="charlie",
            target="zeta", action="extend-filter", severity="constrains",
            affects_goal=None, timestamp=ts,
        ),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")

    rc = _run_main(["--dry-run", "--json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["scanned"] == 1
    assert summary["audit_stale"] == 0
    assert summary["affects_missing"] == 0
    assert summary["filed"] == 1
    assert len(sandbox["filed_calls"]) == 1
    # Pending payload should still record affects_goal field (None)
    assert summary["pending"][0]["affects_goal"] is None
