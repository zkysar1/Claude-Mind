"""Regression tests for the argv truncation gate in bash-agent-inject.

Context (g-115-9314, 2026-09-07): on Windows a native parent spawning MSYS2
bash with the command as one `-c` argument hits a fixed buffer in the MSYS2
runtime that SILENTLY truncates at 8186 bytes. The truncation misattributes --
the harness wraps the command in `eval '...'`, so a cut inside it surfaces as
`unexpected EOF while looking for matching '` pointing at a line in the
caller's own correct content. The gate converts that into an explicit refusal.

These tests are hermetic: pure-function checks plus one subprocess round trip
against the hook with a synthetic envelope. No daemon, no world writes.
"""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = next(q for q in Path(__file__).resolve().parents
            if (q / "CLAUDE.md").exists())
HOOK = ROOT / "core" / "scripts" / "bash-agent-inject.py"


def _load():
    spec = importlib.util.spec_from_file_location("bai_argv_gate", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load()


def test_constants_are_coherent(mod):
    """The derived threshold must leave real room for a command."""
    assert mod.ARGV_CAP_BYTES == 8186
    assert mod._ARGV_MAX_COMMAND == (
        mod._ARGV_CAP - mod.ARGV_WRAPPER_BYTES - mod.ARGV_SAFETY_MARGIN)
    assert mod._ARGV_MAX_COMMAND > 7000, "threshold must not starve normal work"


def test_silent_below_threshold(mod):
    if not mod._ARGV_GATE_ON:
        pytest.skip("gate is platform-gated off here")
    assert mod._argv_gate_reason("echo hi", 319) == ""
    assert mod._argv_gate_reason("x" * mod._ARGV_MAX_COMMAND, 319) == ""


def test_fires_one_byte_over(mod):
    """Boundary is exact -- off-by-one here silently re-opens the defect."""
    if not mod._ARGV_GATE_ON:
        pytest.skip("gate is platform-gated off here")
    assert mod._argv_gate_reason("x" * (mod._ARGV_MAX_COMMAND + 1), 319) != ""


def test_measures_bytes_not_characters(mod):
    """A non-ASCII payload is larger on the wire than len() suggests."""
    if not mod._ARGV_GATE_ON:
        pytest.skip("gate is platform-gated off here")
    # 3 bytes per char in UTF-8, so this is under the char threshold but over
    # the byte threshold. Measuring characters would let it through.
    payload = "中" * (mod._ARGV_MAX_COMMAND // 2)
    assert len(payload) < mod._ARGV_MAX_COMMAND
    assert len(payload.encode("utf-8")) > mod._ARGV_MAX_COMMAND
    assert mod._argv_gate_reason(payload, 319) != ""


def test_no_op_when_platform_gate_off(mod, monkeypatch):
    """POSIX has no MSYS argv layer -- a Linux agent must not inherit this."""
    monkeypatch.setattr(mod, "_ARGV_GATE_ON", False)
    assert mod._argv_gate_reason("x" * 100000, 319) == ""


def test_reason_is_actionable(mod):
    """The message must name the cause and every escape route."""
    if not mod._ARGV_GATE_ON:
        pytest.skip("gate is platform-gated off here")
    reason = mod._argv_gate_reason("x" * (mod._ARGV_MAX_COMMAND + 500), 319)
    for token in ("SILENTLY", "Write tool", "stdin", "env var",
                  "MIND_ARGV_CAP", str(mod._ARGV_CAP)):
        assert token in reason, "deny reason missing %r" % token


def _run_hook(command, sid="00000000-aaaa-bbbb-cccc-000000000000"):
    envelope = json.dumps({
        "session_id": sid,
        "cwd": str(ROOT),
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command, "description": "argv gate test"},
    })
    r = subprocess.run([sys.executable, str(HOOK)], input=envelope,
                       capture_output=True, text=True, timeout=90,
                       cwd=str(ROOT))
    out = (r.stdout or "").strip()
    if not out:
        return "approve-no-mutation", ""
    payload = json.loads(out)["hookSpecificOutput"]
    return (payload.get("permissionDecision", ""),
            payload.get("permissionDecisionReason", ""))


def test_end_to_end_denies_oversize(mod):
    """The whole hook path refuses, not just the pure predicate."""
    if not mod._ARGV_GATE_ON:
        pytest.skip("gate is platform-gated off here")
    decision, reason = _run_hook("echo start\n# " + "A" * 20000)
    assert decision == "deny"
    assert "argv gate" in reason


def test_end_to_end_allows_normal(mod):
    """A normal command must be unaffected -- this is every Bash call."""
    decision, _ = _run_hook("echo hello")
    assert decision in ("allow", "approve-no-mutation")
