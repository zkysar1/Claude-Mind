"""test_iteration_close_reminder.py — 8 regression test.

Pins outcome-aware reminder text selection in iteration-close-reminder.py:

  recurring-close.sh stdout contains
    "[recurring-close] OUTCOME=deep -- NEXT ACTION REQUIRED"   -> DEEP_RECURRING
  iteration-close.sh productivity-check (no OUTCOME marker)    -> GENERIC
  recurring-close.sh OUTCOME=routine (different marker text)   -> GENERIC
  tool_response missing / empty / wrong shape                  -> GENERIC (fail-open)

Origin (zeta investigation g-115-1121, 2026-05-22): the hook predated the
g-115-977 outcome-aware imperative split in recurring-close.sh by 27 days.
PostToolUse system-reminder wins over plain stdout in LLM priority, so the
hook's generic Skill(aspirations) imperative silently overrode
recurring-close.sh's stdout directing Skill(aspirations-spark) first. Phase 6
spark was silently skipped on every deep recurring close (witnessed: charlie
session 67/68 iter 22, exp-g-115-22-2026-05-22-iter22-exemption-applied.md
line 34). Fix design: make the hook outcome-aware by reading tool_response
and detecting the OUTCOME=deep marker.

Test strategy: tmp_path-mocked PROJECT_ROOT with synthetic
agents/<agent>/session state (agent-state=RUNNING, agent-mode=autonomous,
running-session-id=<sid>, plus .active-agent-<sid> binding) so the hook's
mode gate passes deterministically. The 4 cases vary only tool_response —
session state is identical across cases.

Refs:
  - g-115-1138 (this Apply)
  - g-115-1121 (zeta investigation)
  - agents/zeta/reports/g-115-1121-posttool-hook-overrides-recurring-imperative-analysis.md
  - core/scripts/iteration-close-reminder.py (the consumer being pinned)
  - core/scripts/recurring-close.sh:691-697 (the producer side of the marker)
  - .claude/skills/aspirations/SKILL.md "Recurring-goal shortcut"
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
PROJECT_ROOT_REAL = CORE_SCRIPTS.parent.parent
HOOK_PY = CORE_SCRIPTS / "iteration-close-reminder.py"


def _build_fake_project(tmp_path: Path, agent: str, sid: str) -> Path:
    """Construct a minimal PROJECT_ROOT under tmp_path with agent session
    state files arranged so the hook's mode gate (state=RUNNING +
    mode=autonomous + running_sid=sid) passes.

    Returns the tmp PROJECT_ROOT path.
    """
    proj = tmp_path / "fake-mind"
    session_dir = proj / "agents" / agent / "session"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "agent-state").write_text("RUNNING\n", encoding="utf-8")
    (session_dir / "agent-mode").write_text("autonomous\n", encoding="utf-8")
    (session_dir / "running-session-id").write_text(sid + "\n", encoding="utf-8")
    # Legacy binding file (Phase 2.6 binding YAML is the preferred path but
    # the legacy fallback is the more portable mock for tests).
    (proj / f".active-agent-{sid}").write_text(agent + "\n", encoding="utf-8")
    return proj


def _invoke_hook(payload: dict, project_root: Path) -> tuple[int, str, str]:
    """Run iteration-close-reminder.py as a PostToolUse hook with the given
    payload as stdin JSON. Returns (rc, stdout, stderr).

    Each helper that needs to import _stdio (iteration-close-reminder.py's
    sibling) requires core/scripts/ on sys.path. The hook handles that itself
    via its own location — we just point cwd at PROJECT_ROOT.
    """
    env = os.environ.copy()
    # Strip session-binding env so the host's real session doesn't leak in.
    for k in list(env):
        if k.startswith("MIND_") or k == "PROJECT_ROOT":
            env.pop(k, None)
    env["PROJECT_ROOT"] = str(project_root).replace("\\", "/")
    # Ensure core/scripts is importable for the _stdio sibling. The hook
    # itself lives in the real repo, so its dirname IS core/scripts (the
    # path we point sys.executable at).
    env["PYTHONPATH"] = str(CORE_SCRIPTS)

    result = subprocess.run(
        [sys.executable, str(HOOK_PY)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=str(project_root),
        timeout=15,
    )
    return result.returncode, result.stdout, result.stderr


def _additional_context(stdout: str) -> str:
    """Extract hookSpecificOutput.additionalContext from hook stdout JSON.

    Returns empty string when stdout is empty (hook's silent fail-open path)
    or the JSON shape is unexpected. The caller's assertions distinguish
    silence-fail-open from genuine selection.
    """
    if not stdout.strip():
        return ""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return ""
    return data.get("hookSpecificOutput", {}).get("additionalContext", "")


# Markers to assert. The DEEP marker is the unique phrase only the
# DEEP_RECURRING text contains; the GENERIC marker is the unique phrase
# only the GENERIC text contains. They are NON-overlapping by design —
# the goal of the fix is to make them distinguishable from the LLM's POV.
DEEP_MARKER = "Skill(aspirations-spark)"
GENERIC_MARKER = "Skill(aspirations) with args='loop'"
SID = "test-session-g-115-1138-zzz"
AGENT = "alpha"
COMMAND_RECURRING = "bash core/scripts/recurring-close.sh g-115-817 routine"
COMMAND_ITERATION_CLOSE = "bash core/scripts/iteration-close.sh --phase productivity-check"


def _make_payload(command: str, tool_response) -> dict:
    """Construct a PostToolUse payload for the hook to ingest.

    `tool_response` may be a dict (normal shape), None (missing key), or
    any other value — the test cases exercise different shapes.
    """
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "session_id": SID,
    }
    if tool_response is not None:
        payload["tool_response"] = tool_response
    return payload


# ── Case 1: deep recurring close → DEEP_RECURRING reminder ──────────────


def test_deep_recurring_close_emits_outcome_aware_reminder(tmp_path):
    """recurring-close.sh stdout containing OUTCOME=deep marker should
    produce the spark-first reminder. Uses the literal em-dash (U+2014)
    that the production script emits (recurring-close.sh:694)."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    deep_stdout = (
        "[recurring-close] ═══ ITERATION COMPLETE ═══\n"
        "[recurring-close] OUTCOME=deep — NEXT ACTION REQUIRED: Call "
        "Skill(aspirations-spark) FIRST (Phase 6 fires on deep; NOT wrapped "
        "by recurring-close.sh), THEN Skill(aspirations) with args='loop'."
    )
    payload = _make_payload(
        COMMAND_RECURRING, {"stdout": deep_stdout, "stderr": "", "interrupted": False}
    )
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, f"hook exit non-zero: rc={rc}, stderr={stderr[-300:]}"
    ctx = _additional_context(stdout)
    assert DEEP_MARKER in ctx, (
        f"deep recurring close should emit Skill(aspirations-spark) reminder; "
        f"got additionalContext: {ctx[:400]}"
    )
    # The deep reminder MUST NOT direct generic args='loop' as the
    # very-next action — that would re-introduce the spark-skip bug.
    assert "VERY NEXT tool call MUST be Skill(aspirations-spark)" in ctx, (
        f"deep reminder should pin spark as the VERY NEXT call; got: {ctx[:400]}"
    )


# ── Case 2: routine recurring close → GENERIC reminder ──────────────────


def test_routine_recurring_close_emits_generic_reminder(tmp_path):
    """recurring-close.sh stdout for OUTCOME=routine should NOT match the
    deep marker (different literal string) and should produce the generic
    reminder directing Skill(aspirations) loop directly. Uses the literal
    em-dash that the production script emits."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    routine_stdout = (
        "[recurring-close] ═══ ITERATION COMPLETE ═══\n"
        "[recurring-close] OUTCOME=routine — NEXT ACTION REQUIRED: Call "
        "Skill(aspirations) with args='loop' as your VERY NEXT tool call."
    )
    payload = _make_payload(
        COMMAND_RECURRING,
        {"stdout": routine_stdout, "stderr": "", "interrupted": False},
    )
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, f"hook exit non-zero: rc={rc}, stderr={stderr[-300:]}"
    ctx = _additional_context(stdout)
    assert DEEP_MARKER not in ctx, (
        f"routine recurring close should NOT emit spark reminder; got: {ctx[:400]}"
    )
    assert GENERIC_MARKER in ctx, (
        f"routine recurring close should emit generic Skill(aspirations) reminder; "
        f"got: {ctx[:400]}"
    )


# ── Case 3: iteration-close productivity-check (non-recurring) → GENERIC


def test_iteration_close_productivity_check_emits_generic_reminder(tmp_path):
    """iteration-close.sh --phase productivity-check has no OUTCOME marker
    in its stdout. The generic reminder should fire."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    productivity_stdout = (
        "[iteration-close] ═══ ITERATION COMPLETE ═══\n"
        "[iteration-close] NEXT ACTION REQUIRED: Call Skill(aspirations) "
        "with args='loop' as your VERY NEXT tool call."
    )
    payload = _make_payload(
        COMMAND_ITERATION_CLOSE,
        {"stdout": productivity_stdout, "stderr": "", "interrupted": False},
    )
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, f"hook exit non-zero: rc={rc}, stderr={stderr[-300:]}"
    ctx = _additional_context(stdout)
    assert DEEP_MARKER not in ctx, (
        f"productivity-check should NOT emit spark reminder; got: {ctx[:400]}"
    )
    assert GENERIC_MARKER in ctx, (
        f"productivity-check should emit generic reminder; got: {ctx[:400]}"
    )


# ── Case 4: missing tool_response → fail-open (GENERIC, no crash) ───────


def test_missing_tool_response_fails_open_to_generic(tmp_path):
    """Older Claude Code versions / non-standard payloads may omit
    tool_response entirely. The hook must NOT crash — it must fall through
    to the generic reminder (preserves the original session-58 alpha-stopping
    safety-net behavior)."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    # tool_response key entirely absent from the payload.
    payload = _make_payload(COMMAND_RECURRING, tool_response=None)
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, (
        f"hook must fail-open (rc=0) when tool_response missing; "
        f"got rc={rc}, stderr={stderr[-300:]}"
    )
    ctx = _additional_context(stdout)
    assert GENERIC_MARKER in ctx, (
        f"missing tool_response should fall through to generic reminder; "
        f"got: {ctx[:400]}"
    )
    assert DEEP_MARKER not in ctx, (
        f"missing tool_response must NOT emit spark reminder (no evidence "
        f"the close was deep); got: {ctx[:400]}"
    )


# ── Bonus: malformed tool_response shapes also fail-open ────────────────
# Not part of the 4-case acceptance matrix from 8, but the
# fail-open contract in _is_deep_recurring_close warrants explicit pins.


def test_non_dict_tool_response_fails_open_to_generic(tmp_path):
    """tool_response is a string instead of a dict — fail-open path."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(COMMAND_RECURRING, tool_response="not-a-dict")
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    ctx = _additional_context(stdout)
    assert GENERIC_MARKER in ctx and DEEP_MARKER not in ctx


def test_tool_response_missing_stdout_fails_open_to_generic(tmp_path):
    """tool_response present but stdout key missing — fail-open path."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(
        COMMAND_RECURRING, tool_response={"stderr": "x", "interrupted": False}
    )
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    ctx = _additional_context(stdout)
    assert GENERIC_MARKER in ctx and DEEP_MARKER not in ctx


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
