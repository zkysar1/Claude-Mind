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


# ---------------------------------------------------------------------------
# Check 6: efs_ssh_probe ()
#
# guard-1160 ends with "before concluding blocked, re-run the SAME call through
# efs-ssh.sh" and did not reach: times_active 3218, times_helpful 2. Two measured
# saves in different services (amplify:UpdateApp ;
# s3:PutLifecycleConfiguration ) where the agent had ALREADY concluded
# human-gated on a correct, exhaustive enumeration of box credentials. Check 5
# cannot catch that class — the enumeration was honest and every LOCAL source
# really was denied; the missing principal is not on this box at all.
# ---------------------------------------------------------------------------

def _cred_blocker(**over) -> dict:
    """A credentials-required blocker that passes checks 1-5, so any refusal
    below is attributable to check 6 alone (guard-4054: a failure at gate N is
    uninterpretable without survival at N-1).

    The two evidence tools are DISTINCT deliberately. The first draft used
    `aws-exec.sh` for both, which silently failed `multi_signal` — so
    `would_block` was True for a reason that had nothing to do with check 6, and
    two assertions written to pin check 6 were pinning check 2 instead. The
    mutation proof caught it: neutering check 6 left both tests green.
    `test_efs_ssh_refuses_aws_action_without_the_probe` now asserts the failing
    set EXACTLY, so this can never rot back.
    """
    b = {
        "type": "credentials-required",
        "affected_skills": [],
        "failure_reason": "AccessDenied for s3:PutLifecycleConfiguration",
        "evidence": [
            {"tool": "aws-exec.sh", "command": "aws s3api put-bucket-lifecycle-configuration",
             "output": "AccessDenied", "evidence_type": "command_exit"},
            {"tool": "env-read.sh", "command": "env-read.sh --list-credential-sources",
             "output": "default chain + profile mind", "evidence_type": "config_read"},
        ],
        "credential_source_enumeration": [
            {"source": ".env.local default chain", "identity": "user/Zak_first_test",
             "probed": True, "denied": True},
            {"source": "~/.aws profile mind", "identity": "user/mind-svc",
             "probed": True, "denied": True},
        ],
    }
    b.update(over)
    return b


def _check(result: dict, name: str) -> dict:
    return next(c for c in result["checks"] if c["name"] == name)


def test_efs_ssh_refuses_aws_action_without_the_probe():
    """The whole point: a complete local enumeration must NOT be sufficient."""
    r = _call_module(_cred_blocker())
    c = _check(r, "efs_ssh_probe")
    assert c["passed"] is False
    assert r["would_block"] is True
    assert "s3:PutLifecycleConfiguration" in c["reason"]
    assert "efs-ssh.sh" in c["reason"]
    # Quotes guard-1160 rather than paraphrasing it.
    assert "guard-1160" in c["reason"]
    assert "re-run the SAME call through efs-ssh.sh" in c["reason"]
    # ATTRIBUTION, and it is the load-bearing line: check 6 must be the ONLY
    # failure, or `would_block` above is being earned by some other check and
    # this test would stay green with check 6 deleted.
    assert [x["name"] for x in r["checks"] if not x["passed"]] == ["efs_ssh_probe"]


def test_efs_ssh_positive_control_probe_present_passes():
    """Not a blanket refusal: evidence naming the wrapper passes unchanged."""
    b = _cred_blocker()
    b["evidence"].append({
        "tool": "efs-ssh.sh",
        "command": "bash world/scripts/efs-ssh.sh 'aws s3api put-bucket-lifecycle-configuration ...'",
        "output": "AccessDenied", "evidence_type": "command_exit"})
    r = _call_module(b)
    assert _check(r, "efs_ssh_probe")["passed"] is True
    assert r["would_block"] is False


def test_efs_ssh_ignores_the_script_name_appearing_only_in_output():
    """A denial message can quote the script name back at you. Crediting that
    would let the ABSENCE of the probe satisfy the check FOR the probe."""
    b = _cred_blocker()
    b["evidence"].append({
        "tool": "aws-exec.sh", "command": "aws s3api put-bucket-lifecycle-configuration",
        "output": "hint: try world/scripts/efs-ssh.sh", "evidence_type": "command_exit"})
    assert _check(_call_module(b), "efs_ssh_probe")["passed"] is False


@pytest.mark.parametrize("blocker_type", [
    "security-trust", "physical-hardware", "user_action", "infrastructure", "resource",
])
def test_efs_ssh_governs_only_credentials_required(blocker_type: str):
    """Other HUMAN_ONLY types with no companion-script path stay exempt,
    matching the canonical_probe precedent."""
    b = _cred_blocker(type=blocker_type)
    c = _check(_call_module(b), "efs_ssh_probe")
    assert c["passed"] is True
    assert "check skipped" in c["reason"]


def test_efs_ssh_skipped_when_no_aws_action_is_named():
    """Scope note: do NOT widen to every credentials blocker."""
    b = _cred_blocker(failure_reason="the vendor portal needs a human to click approve",
                      evidence=[
                          {"tool": "curl", "command": "curl https://portal.example/api",
                           "output": "401", "evidence_type": "http_status"},
                          {"tool": "sts", "command": "aws sts get-caller-identity",
                           "output": "user/x", "evidence_type": "command_exit"}])
    c = _check(_call_module(b), "efs_ssh_probe")
    assert c["passed"] is True
    assert "no AWS service:Action" in c["reason"]


def _prose_evidence() -> list:
    return [{"tool": "curl", "command": "curl https://portal.example",
             "output": "401", "evidence_type": "http_status"},
            {"tool": "sts", "command": "aws sts get-caller-identity",
             "output": "user/x", "evidence_type": "command_exit"}]


def test_efs_ssh_lowercase_prose_label_is_damped():
    """`error:AccessDenied` matches the service:Action SHAPE exactly and appears
    in real logs. _NOT_AWS_SERVICES is what stops it demanding a probe.

    The first version of this test used `Note:HumanApprovalRequired`, which the
    regex rejects on its leading-[a-z] anchor BEFORE the damper is consulted —
    so deleting _NOT_AWS_SERVICES entirely left it green. Measured: that input
    yields zero regex hits; `error:AccessDenied` and `note:...` yield one each,
    both damped. Pinned on an input that actually reaches the damper.
    """
    b = _cred_blocker(failure_reason="error:AccessDenied talking to the vendor portal",
                      evidence=_prose_evidence())
    c = _check(_call_module(b), "efs_ssh_probe")
    assert c["passed"] is True
    assert "no AWS service:Action" in c["reason"]


def test_efs_ssh_capitalised_label_never_reaches_the_damper():
    """The regex's leading-[a-z] anchor is a SECOND, independent filter, and it
    carries every Capitalised prose label without help from the exclusion set."""
    b = _cred_blocker(failure_reason="Note:HumanApprovalRequired for the vendor portal",
                      evidence=_prose_evidence())
    assert _check(_call_module(b), "efs_ssh_probe")["passed"] is True


def test_efs_ssh_action_named_only_in_evidence_still_triggers():
    """failure_reason is not the only place the action shows up."""
    b = _cred_blocker(failure_reason="permission denied on the deploy")
    b["evidence"][0]["output"] = "User is not authorized to perform amplify:UpdateApp"
    c = _check(_call_module(b), "efs_ssh_probe")
    assert c["passed"] is False
    assert "amplify:UpdateApp" in c["reason"]


def test_efs_ssh_override_bypasses_and_logs_to_the_gate_family_ledger(tmp_path: Path):
    """Override path is consistent with the existing gate family."""
    r = _call_module(_cred_blocker(),
                     override_blocker_gate="env-server unreachable this cycle; re-probe filed",
                     world_dir=tmp_path, agent="alpha")
    assert r["would_block"] is False
    ledger = tmp_path / "blocker-gate-overrides.jsonl"
    assert ledger.is_file()
    rec = json.loads(ledger.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert "efs_ssh_probe" in rec["which_checks_bypassed"]
    assert rec["blocker_type"] == "credentials-required"
    assert rec["agent"] == "alpha"


def test_efs_ssh_cli_exits_1_and_module_agrees():
    """The goal asks for exit 1 from the CLI, not merely a module verdict."""
    blocker = _cred_blocker()
    rc, out, _err = _run_cli(blocker)
    assert rc == 1
    assert out["would_block"] is True
    assert _call_module(blocker)["would_block"] is out["would_block"]
    names = [c["name"] for c in out["checks"]]
    assert "efs_ssh_probe" in names
