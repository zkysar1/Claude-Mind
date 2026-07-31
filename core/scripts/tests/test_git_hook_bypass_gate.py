"""Pin : the Layer-B gate for hook-bypassing git commits.

guard-901 is a Layer-A rule and rb-5390 records it failing by enumeration
leak (--no-verify named; `-c core.hooksPath=/dev/null` used all session).
These tests exercise the gate in the LITERAL production shape (guard-920):
the hook receives Claude Code's PreToolUse stdin JSON and answers with empty
stdout (approve) or a deny payload on stdout — exit code 0 either way.

Every bypass form the goal names is pinned, plus the false-positive surface
that matters most (guard-958): prose inside a quoted -m message that MENTIONS
the forbidden flags must not trip a token-anchored predicate.

The override ledger is redirected to a tmp path via GIT_HOOK_BYPASS_LEDGER —
same-session fresh-eyes lesson: a test that appends to the real store leaves
residue every suite run.
"""
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[1] / "git-hook-bypass-gate.py"


def _run(command: str, tool_name: str = "Bash", ledger: Path | None = None):
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    env = os.environ.copy()
    if ledger is not None:
        env["GIT_HOOK_BYPASS_LEDGER"] = str(ledger)
    proc = subprocess.run(
        [sys.executable, str(GATE)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr  # hook contract: never non-zero
    return proc.stdout.strip()


def _is_deny(stdout: str) -> bool:
    if not stdout:
        return False
    d = json.loads(stdout)
    return d["hookSpecificOutput"]["permissionDecision"] == "deny"


def _reason(stdout: str) -> str:
    return json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]


# ── Form A: --no-verify / short clusters ──────────────────────────────────

def test_no_verify_denied():
    out = _run('git commit --no-verify -m "msg"')
    assert _is_deny(out)
    assert "guard-901" in _reason(out)


def test_short_n_after_commit_denied():
    out = _run('git commit -n -m "msg"')
    assert _is_deny(out)


def test_short_cluster_with_n_denied():
    out = _run('git commit -anm "msg"')
    assert _is_deny(out)
    assert "no-verify-short" in _reason(out)


def test_git_log_dash_n_approved():
    # -n belongs to `git log`, no commit token — must not fire.
    assert _run("git log -n 5") == ""


def test_short_cluster_without_n_approved():
    assert _run('git commit -am "msg"') == ""


# ── Form B: core.hooksPath via -c / --config-env ──────────────────────────

def test_hookspath_dev_null_denied():
    # The literal rb-5390 incident form.
    out = _run('git -c core.hooksPath=/dev/null commit -m "msg"')
    assert _is_deny(out)
    assert "hookspath-c" in _reason(out)


def test_hookspath_glued_denied():
    out = _run('git -ccore.hooksPath=/tmp/x commit -m "msg"')
    assert _is_deny(out)


def test_hookspath_case_insensitive_denied():
    out = _run('git -c core.hookspath=/dev/null commit -m "msg"')
    assert _is_deny(out)


def test_hookspath_canonical_value_approved():
    # Explicitly re-stating the repo's correct chain is repair, not bypass.
    assert _run('git -c core.hooksPath=core/githooks commit -m "msg"') == ""


def test_config_env_denied():
    out = _run('git --config-env=core.hooksPath=HP commit -m "msg"')
    assert _is_deny(out)


# ── Form C: env-var equivalents ───────────────────────────────────────────

def test_git_config_parameters_env_denied():
    out = _run("GIT_CONFIG_PARAMETERS=\"'core.hookspath'='/dev/null'\" git commit -m x")
    assert _is_deny(out)


def test_git_config_key_env_denied():
    out = _run(
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath "
        "GIT_CONFIG_VALUE_0=/dev/null git commit -m x"
    )
    assert _is_deny(out)


# ── Form D: persistent config writes ──────────────────────────────────────

def test_config_write_denied():
    out = _run("git config core.hooksPath /dev/null")
    assert _is_deny(out)
    assert "config-write" in _reason(out)


def test_config_unset_denied():
    out = _run("git config --unset core.hooksPath")
    assert _is_deny(out)
    assert "config-unset" in _reason(out)


def test_config_read_approved():
    assert _run("git config core.hooksPath") == ""


def test_config_get_approved():
    assert _run("git config --get core.hooksPath") == ""


def test_config_restore_canonical_approved():
    assert _run("git config core.hooksPath core/githooks") == ""


# ── False-positive surface (guard-958: token-anchored, prose-safe) ─────────

def test_legitimate_commit_approved():
    assert _run('git add -A && git commit -m "fix(tests): normal commit"') == ""


def test_prose_mention_in_message_approved():
    # This very goal's commits mention the flags in the -m payload.
    out = _run(
        'git commit -m "feat(gate): deny --no-verify and core.hooksPath bypass forms"'
    )
    assert out == ""


def test_non_git_command_approved():
    assert _run("echo core.hooksPath --no-verify") == ""


def test_unbalanced_quotes_approved():
    assert _run('git commit -m "unterminated --no-verify') == ""


def test_non_bash_tool_approved():
    assert _run('git commit --no-verify -m x', tool_name="Edit") == ""


# ── Override hatch + ledger ───────────────────────────────────────────────

def test_override_with_justification_approved_and_logged(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="repairing wedged pre-commit gate, chain cannot run" '
        'git commit --no-verify -m "hook repair"',
        ledger=ledger,
    )
    assert out == ""
    rows = [json.loads(l) for l in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert "repairing wedged" in rows[0]["justification"]
    assert "command_head" in rows[0] and "ts" in rows[0]


def test_override_empty_justification_denied(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="" git commit --no-verify -m x',
        ledger=ledger,
    )
    assert _is_deny(out)
    assert "override-rejected" in _reason(out)
    assert not ledger.exists()  # nothing logged on a rejected override


def test_override_on_clean_command_writes_nothing(tmp_path):
    ledger = tmp_path / "overrides.jsonl"
    out = _run(
        'GIT_HOOK_BYPASS_OVERRIDE="not needed" git commit -m "clean"',
        ledger=ledger,
    )
    assert out == ""
    assert not ledger.exists()  # no findings → override path never reached


# ── Predicate unit surface (import via file location — hyphenated name) ────

def _load_gate_module():
    spec = importlib.util.spec_from_file_location("_ghbg", GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_command_no_git_short_circuits():
    mod = _load_gate_module()
    assert mod.scan_command("echo --no-verify") == []
    assert mod.scan_command("") == []


def test_scan_command_multiple_findings():
    mod = _load_gate_module()
    forms = [f for f, _ in mod.scan_command(
        'git -c core.hooksPath=/dev/null commit --no-verify -m x'
    )]
    assert "no-verify" in forms and "hookspath-c" in forms
