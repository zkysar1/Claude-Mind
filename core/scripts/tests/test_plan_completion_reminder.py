"""Tests for the PostToolUse[ExitPlanMode] plan-completion verdict reminder.

Covers core/scripts/plan-completion-reminder.{sh,py}, its settings.json wiring,
and the always-loaded rule it boosts (.claude/rules/plan-completion-verdict.md).
The hook plants the completion obligation at plan-approval time so delivering
the verdict never depends on the model remembering a rule under context
pressure (2026-09-05: agents ended plan execution on "plan finished" with the
plan left in place and the user's original question unanswered).
"""
import json
import subprocess
from pathlib import Path

from _bash_helpers import BASH  # portable bash argv[0] (guard-580: bare "bash" -> WSL hang on win32)

ROOT = Path(__file__).resolve().parents[3]
HOOK = ROOT / "core" / "scripts" / "plan-completion-reminder.sh"
RULE = ROOT / ".claude" / "rules" / "plan-completion-verdict.md"
SETTINGS = ROOT / ".claude" / "settings.json"


def _run(stdin_text: str) -> subprocess.CompletedProcess:
    # .as_posix(), never str(Path): bash strips the backslashes of a str(WindowsPath) (guard-581)
    return subprocess.run(
        [BASH, HOOK.as_posix()], input=stdin_text, capture_output=True, text=True, timeout=30
    )


def _emitted_context(r: subprocess.CompletedProcess) -> str:
    d = json.loads(r.stdout)
    h = d["hookSpecificOutput"]
    assert h["hookEventName"] == "PostToolUse"
    return h["additionalContext"]


def test_exit_plan_mode_emits_all_four_obligations():
    r = _run(json.dumps({"tool_name": "ExitPlanMode", "tool_input": {}}))
    assert r.returncode == 0
    ac = _emitted_context(r)
    assert ac.startswith("<system-reminder>") and ac.endswith("</system-reminder>")
    # 1 clear, 2 anchor, 3 answer, 4 never-bare-status
    for must in ("CLEAR the plan file", "## Original request",
                 "ANSWER that original request", "plan finished"):
        assert must in ac, must
    assert "plan-completion-verdict.md" in ac


def test_other_tool_is_silent():
    # settings.json matcher is the real gate; this is the defensive no-op guard.
    r = _run(json.dumps({"tool_name": "Bash", "tool_input": {}}))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_garbage_stdin_fails_open_with_valid_json_or_silence():
    r = _run("this is not json")
    assert r.returncode == 0
    if r.stdout.strip():
        _emitted_context(r)  # if it emitted, the payload must be well-formed


def test_empty_stdin_fails_open():
    r = _run("")
    assert r.returncode == 0
    if r.stdout.strip():
        _emitted_context(r)


def test_settings_json_wires_hook_exactly_once_on_exit_plan_mode():
    s = json.loads(SETTINGS.read_text())
    hits = [e for e in s["hooks"]["PostToolUse"] if e.get("matcher") == "ExitPlanMode"]
    assert len(hits) == 1, f"expected exactly one ExitPlanMode PostToolUse entry, got {len(hits)}"
    cmds = [h["command"] for h in hits[0]["hooks"]]
    assert any("plan-completion-reminder.sh" in c for c in cmds), cmds


# ── task-network harness (plan tool on the wire as TodoWrite, rendered `output`) ──

def _plan_tool_payload(output: str) -> str:
    # The wire shape a task-network harness hands a PostToolUse shell hook: the Claude Code
    # contract (tool_name = the Claude Code counterpart, tool_input) plus the tool's rendered
    # `output`. No `tool_response`/`data` — completion must be read from the render.
    return json.dumps({
        "event": "PostToolUse", "tool_name": "TodoWrite", "session_id": "s1", "cwd": "/ws",
        "tool_input": {"tasks": [{"title": "a"}, {"title": "b"}, {"title": "c"}]},
        "output": output, "is_error": False,
    })


def test_plan_tool_completion_emits_verdict_obligations_without_a_clear():
    r = _run(_plan_tool_payload(
        "Current plan (3/3 steps done):\n  [x] t1 a\n  [x] t2 b\n  [-] t3 c — dropped"))
    assert r.returncode == 0
    ac = _emitted_context(r)
    for must in ("Plan COMPLETE", "do not call update_plan", "ORIGINAL request",
                 "plan finished", "plan-completion-verdict.md"):
        assert must in ac, must
    # The harness retires a complete plan itself (Zak-Code ADR-0108); asking the model to
    # clear it would only spend a tool call on a no-op.
    assert "tasks: []" not in ac


def test_plan_tool_in_progress_is_silent():
    r = _run(_plan_tool_payload(
        "Current plan (1/3 steps done):\n  [x] t1 a\n  [~] t2 b  <- current\n  [ ] t3 c"))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_plan_tool_cleared_is_silent_so_the_asked_for_clear_cannot_refire():
    r = _run(_plan_tool_payload("Plan cleared."))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_plan_tool_zero_steps_is_silent():
    r = _run(_plan_tool_payload("Current plan (0/0 steps done):"))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_todowrite_without_rendered_output_is_silent():
    # A Claude Code TodoWrite payload carries tool_response, never `output` — silent by construction.
    r = _run(json.dumps({"tool_name": "TodoWrite",
                         "tool_input": {"todos": [{"content": "x", "status": "completed"}]},
                         "tool_response": {"oldTodos": [], "newTodos": []}}))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_settings_json_routes_the_plan_tool_matcher_to_the_same_script():
    s = json.loads(SETTINGS.read_text())
    hits = [e for e in s["hooks"]["PostToolUse"] if e.get("matcher") == "update_plan"]
    assert len(hits) == 1, f"expected exactly one update_plan PostToolUse entry, got {len(hits)}"
    cmds = [h["command"] for h in hits[0]["hooks"]]
    assert any("plan-completion-reminder.sh" in c for c in cmds), cmds


def test_rule_file_carries_anchor_clear_and_answer_clauses():
    rule = RULE.read_text()
    assert "## Original request" in rule            # the durable anchor (rule 1)
    assert "clear the plan" in rule.lower()         # rule 2
    assert "answer the original request" in rule.lower()  # rule 3
    assert "plan finished" in rule.lower()          # rule 4 anti-pattern
