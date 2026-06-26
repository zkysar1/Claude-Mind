"""7 / rb-1150: insight-trigger-gate apply-time re-probe regression.

Mirrors the contract pinned by test_insight_trigger_sweep_reprobe.py
(g-115-1076) onto the sibling event-driven gate path. Three cases match
the acceptance criteria of g-115-1077:

  1. target pending      -> file Investigate normally (no audit-stale)
  2. target completed    -> skip Investigate, emit audit-stale note
  3. target missing      -> file Investigate with affects_missing warning

Canonical incident lineage (rb-1150): audit-time -> apply-time staleness
gap. The sweep path was closed by g-115-1076; the gate path (this one)
still spawned duplicate Investigate goals when the target had already
closed between the finding's authoring and the gate's invocation. The
re-probe at filing time closes the second half of the gap.

Run: py -3 -m pytest core/scripts/tests/test_insight_trigger_gate_reprobe.py
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

# Load insight-trigger-gate.py as a module - script name has hyphens so
# we cannot `import insight-trigger-gate` directly.
GATE_PATH = CORE_SCRIPTS / "insight-trigger-gate.py"
_spec = importlib.util.spec_from_file_location("itg_under_test", GATE_PATH)
itg = importlib.util.module_from_spec(_spec)
sys.modules["itg_under_test"] = itg
_spec.loader.exec_module(itg)


def _finding_record(msg_id, *, author, requires_by, severity, affects,
                    text=None):
    """Build a findings.jsonl record exercising the gate's tag schema.

    The gate filters on `insight_trigger` tag + `finding_kind:coordination`
    + `requires_action_by:<self>` + severity in {invalidates, constrains}.
    """
    tags = [
        "insight_trigger",
        "finding_kind:coordination",
        f"requires_action_by:{requires_by}",
        f"severity:{severity}",
    ]
    for aff in affects:
        tags.append(f"affects:{aff}")
    return json.dumps({
        "id": msg_id,
        "author": author,
        "channel": "findings",
        "type": "finding",
        "text": text or f"test finding {msg_id}",
        "tags": tags,
        # guard-566: a hardcoded past timestamp ages out of the gate's 24h
        # scan window (_load_findings cutoff = now - since_hours), silently
        # emptying `findings` and zeroing every summary counter. Compute it
        # relative to now, mirroring sibling _trigger_timestamp in
        # test_insight_trigger_sweep_reprobe.py (rb-1318: suspect the fixture
        # before blaming just-changed production code).
        "timestamp": (datetime.now() - timedelta(hours=2)).isoformat(
            timespec="seconds"),
    }) + "\n"


def _aspiration_record(asp_id, goals):
    return json.dumps({
        "id": asp_id,
        "status": "active",
        "goals": goals,
    }) + "\n"


@pytest.fixture
def sandbox(monkeypatch, tmp_path: Path):
    """Sandbox the gate's IO so probe_goal_status reads tmp_path JSONL and
    none of the goal-filing / board-posting actually escapes the test.

    Patches:
      - MIND_AGENT env to "zeta-test" (matches requires_action_by tag)
      - itg._world_dir to return tmp world
      - itg._enumerate_agent_confs to yield the sandbox agent's conf
      - itg._rt.aspirations_add_goal to record calls
      - itg._emit_audit_stale_note to record calls
      - itg._post_reply / itg._log_action to no-ops
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
    conf = test_agent / "local-paths.conf"
    conf.write_text(
        f'WORLD_PATH="{world}"\nMETA_PATH="{tmp_path / "meta"}"\n',
        encoding="utf-8",
    )
    # Empty agent aspirations so probe_goal_status walks it cleanly.
    (test_agent / "aspirations.jsonl").write_text("", encoding="utf-8")
    # Per-agent session dir for action log (kept under sandbox tmp).
    (test_agent / "session").mkdir()

    monkeypatch.setenv("MIND_AGENT", "zeta-test")
    monkeypatch.setattr(itg, "_world_dir", lambda: world)
    monkeypatch.setattr(itg, "_enumerate_agent_confs", lambda: [conf])

    add_goal_calls = []

    def fake_add_goal(asp_id, payload, source="agent"):
        add_goal_calls.append(
            {"asp_id": asp_id, "payload": payload, "source": source})
        return None

    note_calls = []

    def fake_note(finding, affected, target_status, severity):
        note_calls.append({
            "finding_id": finding.get("id"),
            "affected": affected,
            "target_status": target_status,
            "severity": severity,
        })
        return {"posted": True, "msg_id": f"fake-note-{finding.get('id')}"}

    reply_calls = []

    def fake_reply(finding_id, message, tags):
        reply_calls.append(
            {"finding_id": finding_id, "message": message, "tags": tags})
        return True, ""

    log_calls = []

    def fake_log(record):
        log_calls.append(record)
        return True

    monkeypatch.setattr(itg._rt, "aspirations_add_goal", fake_add_goal)
    monkeypatch.setattr(itg, "_emit_audit_stale_note", fake_note)
    monkeypatch.setattr(itg, "_post_reply", fake_reply)
    monkeypatch.setattr(itg, "_log_action", fake_log)

    return {
        "world": world,
        "findings": findings,
        "asp_jsonl": asp_jsonl,
        "conf": conf,
        "agents_dir": agents_dir,
        "add_goal_calls": add_goal_calls,
        "note_calls": note_calls,
        "reply_calls": reply_calls,
        "log_calls": log_calls,
    }


def _run_main(argv):
    """Run itg.main() under a controlled argv; capture exit code."""
    return itg.main(argv)


def test_case1_affects_pending_files_normally(sandbox, capsys):
    """target pending -> file Investigate normally; no audit-stale skip."""
    sandbox["findings"].write_text(
        _finding_record(
            "msg-test-1-pending", author="charlie",
            requires_by="zeta-test", severity="constrains",
            affects=["g-100-001"],
        ),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text(
        _aspiration_record("asp-test", [
            {"id": "g-100-001", "status": "pending", "title": "test goal"},
        ]),
        encoding="utf-8",
    )

    rc = _run_main(["--output", "json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["triggers_acted_on"] == 1
    assert summary["audit_stale"] == 0, (
        "pending target must NOT trigger audit-stale")
    assert summary["affects_missing"] == 0, (
        "pending target is found - no missing warning")
    assert len(sandbox["add_goal_calls"]) == 1, (
        "Investigate goal MUST be filed for pending target")
    assert len(sandbox["note_calls"]) == 0


def test_case2_affects_completed_skips_and_emits_note(sandbox, capsys):
    """target completed -> skip Investigate + emit audit-stale note."""
    sandbox["findings"].write_text(
        _finding_record(
            "msg-test-2-completed", author="charlie",
            requires_by="zeta-test", severity="constrains",
            affects=["g-100-002"],
        ),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text(
        _aspiration_record("asp-test", [
            {"id": "g-100-002", "status": "completed", "title": "already done"},
        ]),
        encoding="utf-8",
    )

    rc = _run_main(["--output", "json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["triggers_acted_on"] == 1
    assert summary["audit_stale"] == 1, (
        "completed target MUST trigger audit-stale")
    assert summary["affects_missing"] == 0
    assert len(sandbox["add_goal_calls"]) == 0, (
        "Investigate MUST NOT be filed for terminal-status target")
    assert len(sandbox["note_calls"]) == 1, (
        "audit-stale note MUST be emitted")
    note = sandbox["note_calls"][0]
    assert note["target_status"] == "completed"
    assert note["affected"] == "g-100-002"
    assert len(summary["audit_stale_details"]) == 1
    assert summary["audit_stale_details"][0]["target"] == "g-100-002"
    assert summary["audit_stale_details"][0]["target_status"] == "completed"


def test_case3_affects_missing_files_with_warning(sandbox, capsys):
    """target missing -> file Investigate WITH affects_missing warning."""
    sandbox["findings"].write_text(
        _finding_record(
            "msg-test-3-missing", author="charlie",
            requires_by="zeta-test", severity="constrains",
            affects=["g-100-999"],
        ),
        encoding="utf-8",
    )
    # Empty aspirations - target goal NOT present anywhere
    sandbox["asp_jsonl"].write_text("", encoding="utf-8")

    rc = _run_main(["--output", "json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["triggers_acted_on"] == 1
    assert summary["audit_stale"] == 0, (
        "missing target is NOT audit-stale")
    assert summary["affects_missing"] == 1, (
        "missing target MUST increment affects_missing")
    assert len(sandbox["add_goal_calls"]) == 1, (
        "missing target should still file Investigate with warning")
    assert len(sandbox["note_calls"]) == 0
    assert len(summary["affects_missing_details"]) == 1
    assert summary["affects_missing_details"][0]["target"] == "g-100-999"
    # The action record carries the warning text so callers can surface it.
    actions = summary["actions"][0]["actions"]
    assert any(a.get("affects_missing_warning") for a in actions)


def test_case4_terminal_status_set_covers_all_terminal_states(sandbox, capsys):
    """Sanity: every SSOT terminal goal status counts as terminal here.

    Integration guard that all five SSOT terminal statuses (completed,
    skipped, superseded, expired, decomposed) skip-with-note and never spawn
    an Investigate. g-303-21 (zeta audit D1) replaced the fictional 'archived'
    case (never a valid goal status) with the real expired/decomposed cases
    the fix added. The constant's exact contents are pinned against the SSOT
    by test_terminal_goal_states_parity.py.
    """
    # Build a single finding with FIVE affects entries, one per terminal
    # state. All five MUST skip-with-note.
    sandbox["findings"].write_text(
        _finding_record(
            "msg-test-5-all-terminal", author="charlie",
            requires_by="zeta-test", severity="invalidates",
            affects=["g-100-c", "g-100-s", "g-100-u", "g-100-e", "g-100-d"],
        ),
        encoding="utf-8",
    )
    sandbox["asp_jsonl"].write_text(
        _aspiration_record("asp-test", [
            {"id": "g-100-c", "status": "completed", "title": "c"},
            {"id": "g-100-s", "status": "skipped", "title": "s"},
            {"id": "g-100-u", "status": "superseded", "title": "u"},
            {"id": "g-100-e", "status": "expired", "title": "e"},
            {"id": "g-100-d", "status": "decomposed", "title": "d"},
        ]),
        encoding="utf-8",
    )

    rc = _run_main(["--output", "json"])
    out = capsys.readouterr().out
    summary = json.loads(out)

    assert rc == 0
    assert summary["audit_stale"] == 5, (
        "all five SSOT terminal statuses must trigger audit-stale")
    assert summary["affects_missing"] == 0
    assert len(sandbox["add_goal_calls"]) == 0, (
        "no Investigate spawn for any terminal target")
    assert len(sandbox["note_calls"]) == 5
    statuses_seen = {n["target_status"] for n in sandbox["note_calls"]}
    assert statuses_seen == {"completed", "skipped", "superseded", "expired", "decomposed"}
