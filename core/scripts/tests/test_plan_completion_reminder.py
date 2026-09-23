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

import pytest

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


# ── a plan TOOL's results are not this hook's business (branch retired 2026-09-21) ──
#
# Until 2026-09-21 the hook also matched a task-network plan tool and parsed its rendered
# result for "every step done". The harness changed what that tool returns five days after
# the branch shipped, and the branch went dead with every test here still green, because the
# fixtures carried the OLD render (guard-920: a fixture must carry the production shape). The
# branch is gone: the harness owns the moment its own plan completes (the why, with the
# measurements, is in the hook's docstring). What is pinned now is BEHAVIOUR (guard-6333):
# whatever a plan tool's result says, under either tool name, the hook is silent.

def _plan_tool_payload(tool_name: str, output: str) -> str:
    # Key for key what a task-network harness hands a PostToolUse shell hook (captured from
    # Zak-Code's own wire builder, 2026-09-21): the Claude Code contract plus its native keys.
    return json.dumps({
        "event": "PostToolUse", "hook_event_name": "PostToolUse", "tool_name": tool_name,
        "tool_input": {"tasks": [{"title": "a", "status": "done"}]},
        "session_id": "s1", "cwd": "/ws", "transcript_path": "",
        "output": output, "tool_response": output, "is_error": None,
    })


_PLAN_TOOL_RESULTS = [
    "Plan updated: 3/3 steps done — complete.",       # a finished plan's receipt, as sent today
    "Plan updated: 1/3 steps done · current: t2 b",   # a plan in progress, as sent today
    "Plan unchanged: 3/3 steps done. Nothing was updated.",
    "Plan cleared.",
    # The full render the harness returned until 2026-09-10: the shape the retired branch fired on.
    "Current plan (3/3 steps done):\n  [x] t1 a\n  [x] t2 b\n  [-] t3 c — dropped",
]


@pytest.mark.parametrize("tool_name", ["TodoWrite", "update_plan"])
@pytest.mark.parametrize("output", _PLAN_TOOL_RESULTS)
def test_a_plan_tools_result_is_silent_whatever_it_says(tool_name, output):
    r = _run(_plan_tool_payload(tool_name, output))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_the_silence_check_can_see_an_emission():
    # The control for the tests above: the SAME payload under the one tool this hook serves
    # does emit, so an empty stdout up there is the hook's decision and not a dead harness.
    r = _run(_plan_tool_payload("ExitPlanMode", _PLAN_TOOL_RESULTS[0]))
    assert r.returncode == 0
    assert "Plan APPROVED" in _emitted_context(r)


def test_todowrite_without_rendered_output_is_silent():
    # A Claude Code TodoWrite payload carries tool_response and no `output` at all.
    r = _run(json.dumps({"tool_name": "TodoWrite",
                         "tool_input": {"todos": [{"content": "x", "status": "completed"}]},
                         "tool_response": {"oldTodos": [], "newTodos": []}}))
    assert r.returncode == 0
    assert r.stdout.strip() == ""


def test_settings_json_routes_no_other_tool_to_this_script():
    # Reads the SHIPPED settings: re-adding a plan-tool matcher for this script turns this red.
    s = json.loads(SETTINGS.read_text())
    matchers = [
        e.get("matcher")
        for entries in s["hooks"].values()
        for e in entries
        if any("plan-completion-reminder.sh" in h.get("command", "") for h in e.get("hooks", []))
    ]
    assert matchers == ["ExitPlanMode"], matchers


def test_rule_file_carries_anchor_clear_and_answer_clauses():
    rule = RULE.read_text()
    assert "## Original request" in rule            # the durable anchor (rule 1)
    assert "clear the plan" in rule.lower()         # rule 2
    assert "answer the original request" in rule.lower()  # rule 3
    assert "plan finished" in rule.lower()          # rule 4 anti-pattern
