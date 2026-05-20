"""Equivalence + behavior tests for stale_read gate (PR 7c/1).

Decision branches:
  Skip:    no parent_goal → pass
  Skip:    parent not found in live queues → fail-open (exit 2)
  Skip:    parent has no last_modified → pass (legacy goal)
  Block:   agent never read parent
  Block:   parent modified after agent's most-recent read
  Pass:    read is fresh (last_read >= parent_last_modified)
  Override on block: pass + audit ledger write

CLI subprocess and direct module call must agree on every payload shape.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "core" / "scripts"
CLI = SCRIPTS_DIR / "stale-read-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


# ---------------------------------------------------------------------------
# Fixtures — tmp world+agent layout with a parent goal we can edit
# ---------------------------------------------------------------------------

@pytest.fixture
def stale_read_env(tmp_path: Path):
    """Construct a self-contained env: world/, agent/, with one parent goal
    in world/aspirations.jsonl. Returns (world_dir, agent_dir, agent_name)
    plus helpers (write_parent, write_read_log)."""
    world = tmp_path / "world"
    world.mkdir()
    agent = tmp_path / "test-alpha-zzz"
    agent.mkdir()
    (agent / "session").mkdir()

    parent = {
        "id": "asp-001",
        "title": "Test asp",
        "status": "active",
        "priority": "LOW",
        "archived": False,
        "goals": [
            {"id": "g-001-01", "title": "parent goal",
             "status": "pending", "last_modified": "2026-05-12T10:00:00"},
        ],
        "progress": {"completed_goals": 0, "total_goals": 1},
    }
    (world / "aspirations.jsonl").write_text(
        json.dumps(parent) + "\n", encoding="utf-8")

    def write_parent(last_modified: str | None):
        p = dict(parent)
        goal = dict(p["goals"][0])
        if last_modified is None:
            goal.pop("last_modified", None)
        else:
            goal["last_modified"] = last_modified
        p["goals"] = [goal]
        (world / "aspirations.jsonl").write_text(
            json.dumps(p) + "\n", encoding="utf-8")

    def write_read_log(entries: list[dict]):
        log = agent / "session" / "goal-reads.jsonl"
        log.write_text(
            "\n".join(json.dumps(e) for e in entries) + ("\n" if entries else ""),
            encoding="utf-8")

    return {
        "world": world, "agent_dir": agent, "agent": "test-alpha-zzz",
        "write_parent": write_parent, "write_read_log": write_read_log,
    }


def _run_cli(env, payload: dict, *, override: str | None = None,
             output: str = "json") -> tuple[int, dict | str, str]:
    """Invoke the CLI subprocess pointed at the tmp env."""
    args = [sys.executable, str(CLI), "--output", output]
    if override is not None:
        args.extend(["--override-stale-read", override])
    proc_env = os.environ.copy()
    proc_env["MIND_AGENT"] = env["agent"]
    # _paths.py reads conf if MIND_AGENT is set AND <agent>/local-paths.conf
    # exists. Easier: MIND_WORLD + MIND_AGENT_DIR env overrides.
    proc_env["MIND_WORLD"] = str(env["world"])
    proc_env["MIND_AGENT_DIR"] = str(env["agent_dir"])
    proc = subprocess.run(
        args, input=json.dumps(payload), env=proc_env,
        capture_output=True, text=True, check=False,
    )
    if proc.stdout.strip():
        try:
            return proc.returncode, json.loads(proc.stdout), proc.stderr
        except json.JSONDecodeError:
            return proc.returncode, proc.stdout, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def _call_module(env, payload: dict, *, override: str | None = None) -> dict:
    from gates.stale_read import evaluate
    return evaluate(
        payload,
        override=override,
        agent_name=env["agent"],
        world_dir=env["world"],
        agent_dir=env["agent_dir"],
    )


# ---------------------------------------------------------------------------
# Skip paths (gate does not fire)
# ---------------------------------------------------------------------------

def test_no_parent_goal_skips(stale_read_env):
    out = _call_module(stale_read_env, {"agent": stale_read_env["agent"]})
    assert out["would_block"] is False
    assert out["parent_goal"] is None
    assert "gate does not apply" in out["reason"]


def test_no_parent_goal_cli_equivalent(stale_read_env):
    rc, cli_out, _ = _run_cli(stale_read_env, {"agent": stale_read_env["agent"]})
    assert rc == 0
    mod_out = _call_module(stale_read_env, {"agent": stale_read_env["agent"]})
    # CLI strips _fail_open. Module retains it. Compare ignoring the daemon-
    # only field.
    mod_out.pop("_fail_open", None)
    assert cli_out == mod_out


def test_parent_not_found_fail_open(stale_read_env):
    out = _call_module(stale_read_env, {"parent_goal": "g-999-99"})
    assert out["would_block"] is False
    assert out["_fail_open"] is True
    assert "not found" in out["reason"]


def test_parent_not_found_cli_returns_exit_2(stale_read_env):
    rc, _, _ = _run_cli(stale_read_env, {"parent_goal": "g-999-99"})
    assert rc == 2, "fail-open framework error → exit 2"


def test_parent_no_last_modified_passes(stale_read_env):
    """Legacy goal lacking last_modified field — fail-open via 'pass'."""
    stale_read_env["write_parent"](last_modified=None)
    out = _call_module(stale_read_env, {"parent_goal": "g-001-01"})
    assert out["would_block"] is False
    assert "legacy goal" in out["reason"]
    assert out["_fail_open"] is False  # legacy pass is exit 0, not exit 2


# ---------------------------------------------------------------------------
# Block paths
# ---------------------------------------------------------------------------

def test_agent_never_read_blocks(stale_read_env):
    """Parent has last_modified but agent has no read entry → block."""
    out = _call_module(stale_read_env, {"parent_goal": "g-001-01"})
    assert out["would_block"] is True
    assert "no read entry" in out["reason"]
    assert out["agent_last_read"] is None
    assert out["parent_last_modified"] == "2026-05-12T10:00:00"


def test_agent_never_read_cli_returns_exit_1(stale_read_env):
    rc, _, _ = _run_cli(stale_read_env, {"parent_goal": "g-001-01"})
    assert rc == 1


def test_stale_read_blocks(stale_read_env):
    """Agent read parent BEFORE parent was modified → stale → block."""
    stale_read_env["write_read_log"]([
        {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
         "read_at": "2026-05-12T09:00:00"},
    ])
    # parent.last_modified = 2026-05-12T10:00:00 (from fixture default)
    out = _call_module(stale_read_env, {"parent_goal": "g-001-01"})
    assert out["would_block"] is True
    assert "stale read" in out["reason"]
    assert out["agent_last_read"] == "2026-05-12T09:00:00"
    assert out["parent_last_modified"] == "2026-05-12T10:00:00"


def test_fresh_read_passes(stale_read_env):
    """Agent read parent AFTER parent's last_modified → fresh → pass."""
    stale_read_env["write_read_log"]([
        {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
         "read_at": "2026-05-12T11:00:00"},
    ])
    out = _call_module(stale_read_env, {"parent_goal": "g-001-01"})
    assert out["would_block"] is False
    assert out["reason"] == "fresh read"
    assert out["agent_last_read"] == "2026-05-12T11:00:00"


def test_read_at_exact_modification_time_passes(stale_read_env):
    """Boundary: read_at == last_modified → pass (NOT stale).

    String comparison: '10:00:00' > '10:00:00' is False, so the >
    check in the gate correctly treats equality as fresh."""
    stale_read_env["write_read_log"]([
        {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
         "read_at": "2026-05-12T10:00:00"},  # exact match
    ])
    out = _call_module(stale_read_env, {"parent_goal": "g-001-01"})
    assert out["would_block"] is False
    assert out["reason"] == "fresh read"


# ---------------------------------------------------------------------------
# Override
# ---------------------------------------------------------------------------

def test_override_never_read(stale_read_env):
    out = _call_module(
        stale_read_env, {"parent_goal": "g-001-01"},
        override="emergency unblock",
    )
    assert out["would_block"] is False
    assert out["override_applied"] == "emergency unblock"
    assert "agent never read" in out["reason"]
    # Audit ledger was written
    ledger = stale_read_env["world"] / "stale-read-overrides.jsonl"
    assert ledger.exists()
    entries = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l]
    assert len(entries) == 1
    assert entries[0]["justification"] == "emergency unblock"
    assert entries[0]["parent_goal"] == "g-001-01"


def test_override_stale_read(stale_read_env):
    stale_read_env["write_read_log"]([
        {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
         "read_at": "2026-05-12T09:00:00"},
    ])
    out = _call_module(
        stale_read_env, {"parent_goal": "g-001-01"},
        override="modification irrelevant to child",
    )
    assert out["would_block"] is False
    assert out["override_applied"] == "modification irrelevant to child"
    ledger = stale_read_env["world"] / "stale-read-overrides.jsonl"
    assert ledger.exists()


def test_override_when_not_blocking_does_nothing(stale_read_env):
    """Fresh-read goals + override flag → pass without audit write.

    Legacy contract: ledger only records overrides that WOULD have blocked.
    """
    stale_read_env["write_read_log"]([
        {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
         "read_at": "2026-05-12T11:00:00"},
    ])
    out = _call_module(
        stale_read_env, {"parent_goal": "g-001-01"},
        override="unnecessary override",
    )
    assert out["would_block"] is False
    # override_applied should be None — gate didn't actually use it
    assert out["override_applied"] is None
    assert out["reason"] == "fresh read"
    ledger = stale_read_env["world"] / "stale-read-overrides.jsonl"
    assert not ledger.exists()


# ---------------------------------------------------------------------------
# Equivalence — CLI subprocess vs direct module call
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", ["never_read", "stale", "fresh"])
def test_cli_module_equivalent(stale_read_env, scenario):
    if scenario == "stale":
        stale_read_env["write_read_log"]([
            {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
             "read_at": "2026-05-12T09:00:00"},
        ])
    elif scenario == "fresh":
        stale_read_env["write_read_log"]([
            {"goal_id": "g-001-01", "agent": stale_read_env["agent"],
             "read_at": "2026-05-12T11:00:00"},
        ])
    # never_read: no log written

    payload = {"parent_goal": "g-001-01"}
    rc, cli_out, _ = _run_cli(stale_read_env, payload)
    mod_out = _call_module(stale_read_env, payload)
    mod_out.pop("_fail_open", None)

    # CLI exit code must match decision
    expected_rc = 1 if mod_out["would_block"] else 0
    assert rc == expected_rc
    assert cli_out == mod_out


def test_cli_module_equivalent_override(stale_read_env):
    """Override path equivalence — both should write audit AND return
    identical JSON. Audit ledger should have ONE entry (only the CLI run
    writes; the module call sees the file already exists and appends a
    second entry — verify both wrote)."""
    rc, cli_out, _ = _run_cli(
        stale_read_env, {"parent_goal": "g-001-01"},
        override="cli justification",
    )
    assert rc == 0  # override → pass
    cli_audit = (stale_read_env["world"] / "stale-read-overrides.jsonl").read_text(encoding="utf-8")
    assert "cli justification" in cli_audit

    mod_out = _call_module(
        stale_read_env, {"parent_goal": "g-001-01"},
        override="module justification",
    )
    mod_out.pop("_fail_open", None)
    # Verdict + reason should match (justification text differs by design)
    assert cli_out["would_block"] == mod_out["would_block"]
    assert cli_out["reason"] == mod_out["reason"]
    final_audit = (stale_read_env["world"] / "stale-read-overrides.jsonl").read_text(encoding="utf-8")
    assert "cli justification" in final_audit
    assert "module justification" in final_audit
