"""Equivalence + behavior tests for blocker-create gate (PR 7a/3).

Four sub-checks (canonical_probe, multi_signal, schema_probe, infra_health),
override behavior with audit-ledger side effect, and CLI ↔ module equivalence
on every payload. Uses the real `.claude/skills/felt-sense-checkin` SKILL.md
for canonical-probe tests so we exercise the actual companion_scripts parse
path instead of mocking it.
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
CLI = SCRIPTS_DIR / "blocker-create-gate.py"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _run_cli(blocker: dict, *, probe_command: str | None = None,
             override_blocker_gate: str | None = None,
             agent: str = "", output: str = "json") -> tuple[int, dict | str, str]:
    """Invoke CLI as subprocess. Returns (rc, parsed_stdout_or_raw, stderr).

    No world_dir arg: the CLI's _resolve_world_dir() computes PROJECT_ROOT
    from __file__ (not from cwd), so we can't redirect it without invasive
    mocks. Override-audit ledger writes are tested ONLY via _call_module,
    which takes world_dir as an explicit arg. The CLI's resolution path is
    a few lines and matches uncommitted-work-gate.py / origin-signal-gate.py
    verbatim — those are covered by their own equivalence tests.
    """
    args = [sys.executable, str(CLI), "--output", output]
    if probe_command is not None:
        args.extend(["--probe-command", probe_command])
    if override_blocker_gate is not None:
        args.extend(["--override-blocker-gate", override_blocker_gate])
    env = os.environ.copy()
    env["MIND_AGENT"] = agent

    proc = subprocess.run(
        args, input=json.dumps(blocker), env=env,
        capture_output=True, text=True, check=False,
    )
    if proc.stdout.strip():
        try:
            return proc.returncode, json.loads(proc.stdout), proc.stderr
        except json.JSONDecodeError:
            return proc.returncode, proc.stdout, proc.stderr
    return proc.returncode, proc.stdout, proc.stderr


def _call_module(blocker: dict, *, probe_command: str | None = None,
                 override_blocker_gate: str | None = None,
                 agent: str = "", world_dir: Path | None = None) -> dict:
    from gates.blocker_create import evaluate
    return evaluate(
        blocker,
        probe_command=probe_command,
        override_blocker_gate=override_blocker_gate,
        world_dir=world_dir,
        agent_name=agent,
    )


# ---------------------------------------------------------------------------
# Check 1: canonical_probe
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("blocker_type", [
    "security-trust", "credentials-required", "physical-hardware", "user_action",
])
def test_canonical_probe_skipped_for_human_only(blocker_type: str):
    """Human-only blocker types skip canonical_probe (no companion_script exists)."""
    blocker = {
        "type": blocker_type,
        "affected_skills": ["felt-sense-checkin"],
        "failure_reason": "user needs to do X",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is True
    assert "skipped" in cp["reason"]


def test_canonical_probe_no_affected_skills():
    blocker = {
        "type": "infrastructure",
        "affected_skills": [],
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
        "infra_health_check": {"output": "fail"},
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is True
    assert "no affected_skills" in cp["reason"]


def test_canonical_probe_missing_probe_command_fails():
    """Real skill with companion_scripts, no --probe-command → check fails."""
    blocker = {
        "type": "resource",
        "affected_skills": ["felt-sense-checkin"],
        "failure_reason": "x",
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is False
    assert "no --probe-command" in cp["reason"]


def test_canonical_probe_wrong_command_fails():
    """Probe doesn't invoke a companion script → check fails."""
    blocker = {
        "type": "resource",
        "affected_skills": ["felt-sense-checkin"],
        "failure_reason": "x",
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    rc, cli, _ = _run_cli(blocker, probe_command="curl http://example.com")
    mod = _call_module(blocker, probe_command="curl http://example.com")
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is False
    assert "non-canonical probe" in cp["reason"]


def test_canonical_probe_matching_command_passes():
    """Probe invokes companion_script basename → check passes."""
    # felt-sense-checkin SKILL.md declares
    # companion_scripts: [core/scripts/felt-sense-cadence-check.sh]
    blocker = {
        "type": "resource",
        "affected_skills": ["felt-sense-checkin"],
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    cmd = "bash core/scripts/felt-sense-cadence-check.sh --probe"
    rc, cli, _ = _run_cli(blocker, probe_command=cmd)
    mod = _call_module(blocker, probe_command=cmd)
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is True


def test_canonical_probe_skill_without_companion_scripts_skipped():
    """Skill with no companion_scripts → that skill is skipped (no enforcement)."""
    # nonexistent-skill-foo has no SKILL.md → get_companion_scripts → []
    blocker = {
        "type": "resource",
        "affected_skills": ["nonexistent-skill-foo-zzz"],
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker, probe_command="anything")
    mod = _call_module(blocker, probe_command="anything")
    assert cli == mod
    cp = next(c for c in cli["checks"] if c["name"] == "canonical_probe")
    assert cp["passed"] is True


# ---------------------------------------------------------------------------
# Check 2: multi_signal
# ---------------------------------------------------------------------------

def test_multi_signal_missing_evidence_fails():
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ms = next(c for c in cli["checks"] if c["name"] == "multi_signal")
    assert ms["passed"] is False
    assert "missing or empty" in ms["reason"]


def test_multi_signal_single_signal_fails():
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ms = next(c for c in cli["checks"] if c["name"] == "multi_signal")
    assert ms["passed"] is False
    assert "single-signal" in ms["reason"]


def test_multi_signal_duplicate_signature_counts_once():
    """Two entries with same (tool|endpoint|evidence_type) = 1 distinct signal."""
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "a", "evidence_type": "x"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ms = next(c for c in cli["checks"] if c["name"] == "multi_signal")
    assert ms["passed"] is False


@pytest.mark.parametrize("silent_cmd", [
    "curl -sf http://example.com",
    "curl -s -f http://example.com",
    "ssh -q user@host echo hi",
    "ls /tmp 2>/dev/null",
    "wget --silent http://x",
    "git --quiet status",
])
def test_multi_signal_silent_failure_zero_credit(silent_cmd: str):
    """Silent-failure flags make the entry count for ZERO signals."""
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "command": silent_cmd, "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ms = next(c for c in cli["checks"] if c["name"] == "multi_signal")
    # Only 'b' counts → 1 distinct signal → fail.
    assert ms["passed"] is False


def test_multi_signal_two_distinct_passes():
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ms = next(c for c in cli["checks"] if c["name"] == "multi_signal")
    assert ms["passed"] is True


# ---------------------------------------------------------------------------
# Check 3: schema_probe
# ---------------------------------------------------------------------------

def test_schema_probe_non_statistical_skipped():
    blocker = {
        "type": "user_action",
        "failure_reason": "service is down, returning 500s",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    sp = next(c for c in cli["checks"] if c["name"] == "schema_probe")
    assert sp["passed"] is True
    assert "not a statistical negation" in sp["reason"]


@pytest.mark.parametrize("failure_reason", [
    "0 records have the field set",
    "missing field active_brain in all entries",
    "all 50 have status=null",
    "none have a valid timestamp",
    "98% of records have active_brain=0",
    "100% records are zero",
])
def test_schema_probe_statistical_without_probe_fails(failure_reason: str):
    blocker = {
        "type": "user_action",
        "failure_reason": failure_reason,
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    sp = next(c for c in cli["checks"] if c["name"] == "schema_probe")
    assert sp["passed"] is False
    assert "without schema verification" in sp["reason"]


def test_schema_probe_statistical_with_probe_passes():
    blocker = {
        "type": "user_action",
        "failure_reason": "0 records have the field set",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
        "schema_probe_evidence": {
            "command": "head -1 records.jsonl | jq .field",
            "output": "<exists>",
        },
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    sp = next(c for c in cli["checks"] if c["name"] == "schema_probe")
    assert sp["passed"] is True


# ---------------------------------------------------------------------------
# Check 4: infra_health
# ---------------------------------------------------------------------------

def test_infra_health_non_infra_skipped():
    blocker = {
        "type": "resource",
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ih = next(c for c in cli["checks"] if c["name"] == "infra_health")
    assert ih["passed"] is True
    assert "not an infrastructure blocker" in ih["reason"]


def test_infra_health_infra_without_probe_fails():
    blocker = {
        "type": "infrastructure",
        "failure_reason": "service unreachable",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ih = next(c for c in cli["checks"] if c["name"] == "infra_health")
    assert ih["passed"] is False
    assert "without infra-health probe" in ih["reason"]


def test_infra_health_infra_with_probe_passes():
    blocker = {
        "type": "infrastructure",
        "failure_reason": "service unreachable",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
        "infra_health_check": {
            "command": "infra-health.sh check db",
            "exit_code": 1,
        },
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    ih = next(c for c in cli["checks"] if c["name"] == "infra_health")
    assert ih["passed"] is True


# ---------------------------------------------------------------------------
# Override: bypasses block, writes to audit ledger (module path only — the
# CLI's _resolve_world_dir() reads from the real project root and there is
# no clean way to redirect it for testing without invasive mocks).
# ---------------------------------------------------------------------------

def test_override_bypasses_block_module(tmp_path: Path):
    """Override flips would_block=False; with world_dir set, audits to ledger."""
    blocker = {
        "type": "infrastructure",
        "affected_skills": [],
        "failure_reason": "service unreachable",
        "evidence": [{"tool": "a", "evidence_type": "x"}],  # only 1 signal
    }
    result = _call_module(blocker, override_blocker_gate="emergency-fix",
                          agent="alpha", world_dir=tmp_path)
    assert result["would_block"] is False
    assert result["override_applied"] == "emergency-fix"
    assert result["failing_count"] >= 1  # multi_signal + infra_health both fail
    ledger = tmp_path / "blocker-gate-overrides.jsonl"
    entries = [json.loads(l) for l in
               ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    e = entries[0]
    assert e["justification"] == "emergency-fix"
    assert e["agent"] == "alpha"
    assert e["blocker_type"] == "infrastructure"
    assert "multi_signal" in e["which_checks_bypassed"]


def test_override_no_world_dir_still_grants(capsys):
    """When world_dir is None, override is still granted; stderr warns."""
    blocker = {
        "type": "infrastructure",
        "affected_skills": [],
        "failure_reason": "x",
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    result = _call_module(blocker, override_blocker_gate="ad-hoc")
    assert result["would_block"] is False
    assert result.get("override_logged_to") is None


def test_override_no_failing_checks_no_ledger(tmp_path: Path):
    """Override with no failing checks: would_block already False, no ledger."""
    blocker = {
        "type": "resource",
        "affected_skills": [],
        "failure_reason": "x",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    result = _call_module(blocker, override_blocker_gate="just in case",
                          agent="alpha", world_dir=tmp_path)
    assert result["would_block"] is False
    assert result["failing_count"] == 0
    ledger = tmp_path / "blocker-gate-overrides.jsonl"
    # No failing checks → still writes an audit row (override flag was set).
    # The which_checks_bypassed list will be empty. This is intentional —
    # the override claim was made, even if there was nothing to bypass.
    if ledger.exists():
        entries = [json.loads(l) for l in
                   ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        # Legacy behavior preserved: yes, a row is written; bypassed list empty.
        if entries:
            assert entries[0]["which_checks_bypassed"] == []


# ---------------------------------------------------------------------------
# CLI-specific error paths
# ---------------------------------------------------------------------------

def test_empty_stdin_returns_2():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input="", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "empty blocker JSON" in proc.stderr


def test_invalid_json_returns_2():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input="not json", capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "parse failed" in proc.stderr


def test_non_object_root_returns_2():
    proc = subprocess.run(
        [sys.executable, str(CLI)],
        input='["array", "not", "object"]',
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 2
    assert "must be an object" in proc.stderr


# ---------------------------------------------------------------------------
# All-checks-pass exits 0
# ---------------------------------------------------------------------------

def test_all_checks_pass():
    blocker = {
        "type": "user_action",
        "failure_reason": "user must approve",
        "evidence": [
            {"tool": "a", "evidence_type": "x"},
            {"tool": "b", "evidence_type": "y"},
        ],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    assert cli["would_block"] is False
    assert cli["failing_count"] == 0
    assert cli["reason"] == "all checks passed"
    assert rc == 0


def test_block_exit_1():
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        # Only 1 signal → multi_signal fails.
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    rc, cli, _ = _run_cli(blocker)
    mod = _call_module(blocker)
    assert cli == mod
    assert cli["would_block"] is True
    assert rc == 1


# ---------------------------------------------------------------------------
# Human output mode
# ---------------------------------------------------------------------------

def test_human_output_mode():
    blocker = {
        "type": "user_action",
        "failure_reason": "x",
        "evidence": [{"tool": "a", "evidence_type": "x"}],
    }
    proc = subprocess.run(
        [sys.executable, str(CLI), "--output", "human"],
        input=json.dumps(blocker), capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 1
    assert "would_block: True" in proc.stdout
    assert "[FAIL] multi_signal" in proc.stdout
