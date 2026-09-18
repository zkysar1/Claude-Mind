"""test_iteration_close_reminder.py —  +  regression tests.

Pins TWO independent decisions in iteration-close-reminder.py. They are separate
questions and have opposite defaults; conflating them produced the g-115-4179
false-fire class.

WHETHER to inject at all (the outcome gate, g-115-4179):

  stdout carries "[<script>-close] ... ITERATION COMPLETE"     -> inject
  --help probe / usage error / invalid outcome-class or
    --source / truncated mid-phase output / no tool_response   -> inject NOTHING

WHICH reminder text, once a close is proven (g-115-1138):

  recurring-close.sh stdout contains
    "[recurring-close] OUTCOME=deep -- NEXT ACTION REQUIRED"   -> DEEP_RECURRING
  iteration-close.sh productivity-check (no OUTCOME marker)    -> GENERIC
  recurring-close.sh OUTCOME=routine (different marker text)   -> GENERIC
  unexpected tool_response shape                               -> GENERIC (fail-open)

g-115-4179 origin (discovered during g-001-10, 2026-07-31): the hook decided
whether to fire from the COMMAND TEXT alone and never checked whether the close
succeeded. `recurring-close.sh --help` was rejected, closed nothing, printed no
ITERATION COMPLETE — and the hook injected the full deadman-pair directive
asserting an iteration had closed. The directive is maximally imperative
("your terminal response MUST be EXACTLY these TWO batched tool calls"), so an
agent obeying it mid-goal abandons its in-flight work and re-enters the loop on
a false premise. guard-1118 / guard-1162 are the Layer-A compensations this gate
makes unnecessary.

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


# ── Case 4 + malformed shapes: no close evidence → inject NOTHING ───────
#
# CONTRACT CHANGE,  (2026-07-31). These three cases previously
# asserted GENERIC_MARKER — i.e. "no evidence of a close → fire anyway". That
# was the  fail-open, and it was written to answer a DIFFERENT
# question than the one it ended up governing:
#
#   _is_deep_recurring_close  answers WHICH reminder (deep vs generic).
#     Fail-open there is right, and is UNCHANGED — an unknown shape still
#     degrades to the generic text rather than crashing.
#   _close_actually_completed answers WHETHER to emit one at all.
#     Fail-open there is WRONG: it injects a maximally-imperative "an
#     iteration just closed" directive on no evidence that one did.
#
# The three cases below exercise the second question, so their assertion
# flips. Their original intent — the hook must not crash on a weird payload —
# is preserved verbatim in the rc == 0 assertions. Direction is deliberate and
# specified by the goal: a FALSE fire tells the agent to abandon live work; a
# MISSED fire is caught by the Stop hook, return-protocol discipline, and the
# pending_phase_6_spark sentinel.


def test_missing_tool_response_injects_nothing(tmp_path):
    """Older Claude Code versions / non-standard payloads may omit
    tool_response entirely. The hook must NOT crash (rc=0) AND must inject
    nothing — with no tool_response there is no evidence any iteration
    closed."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    # tool_response key entirely absent from the payload.
    payload = _make_payload(COMMAND_RECURRING, tool_response=None)
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, (
        f"hook must fail-open (rc=0) when tool_response missing; "
        f"got rc={rc}, stderr={stderr[-300:]}"
    )
    assert stdout.strip() == "", (
        f"missing tool_response is NO evidence of a close — the hook must "
        f"inject nothing; got stdout: {stdout[:400]}"
    )


def test_non_dict_tool_response_injects_nothing(tmp_path):
    """tool_response is a string instead of a dict — no crash, no injection."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(COMMAND_RECURRING, tool_response="not-a-dict")
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    assert stdout.strip() == "", (
        f"non-dict tool_response carries no close marker — inject nothing; "
        f"got stdout: {stdout[:400]}"
    )


def test_tool_response_missing_stdout_injects_nothing(tmp_path):
    """tool_response present but stdout key missing — no crash, no injection."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(
        COMMAND_RECURRING, tool_response={"stderr": "x", "interrupted": False}
    )
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    assert stdout.strip() == "", (
        f"tool_response without stdout carries no close marker — inject "
        f"nothing; got stdout: {stdout[:400]}"
    )


# ── : the false-fire class the outcome gate closes ────────────
#
# Every case below invokes a close script by command text (so the pre-fix
# predicate matched and fired) while closing NOTHING. Each is reachable from a
# normal agent turn: a --help probe, and the three early-exit rejections in
# recurring-close.sh (usage L135, invalid outcome-class L145, invalid --source
# L164). Per guard-1943, a suite that only feeds a SUCCESSFUL close cannot
# distinguish the pre-fix hook from the fixed one — these are the cases that
# can.


def test_help_probe_that_closed_nothing_injects_nothing(tmp_path):
    """The observed incident (, 2026-07-31): `recurring-close.sh
    --help` is rejected by the script, prints no ITERATION COMPLETE, and
    closes nothing — yet the command text matched and the full deadman-pair
    directive fired. Obeying it mid-goal abandons the in-flight goal."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(
        "bash core/scripts/recurring-close.sh --help",
        {
            "stdout": "",
            "stderr": "recurring-close: unknown flag --help\n",
            "interrupted": False,
        },
    )
    rc, stdout, stderr = _invoke_hook(payload, proj)
    assert rc == 0, f"hook exit non-zero: rc={rc}, stderr={stderr[-300:]}"
    assert stdout.strip() == "", (
        f"a rejected --help probe closed no iteration — the hook must inject "
        f"nothing; got stdout: {stdout[:400]}"
    )


@pytest.mark.parametrize(
    "label,command,resp_stdout,resp_stderr",
    [
        (
            "usage-error",
            "bash core/scripts/recurring-close.sh",
            "",
            "recurring-close: usage: recurring-close.sh <goal-id> "
            "<outcome-class> [--source <world|agent>]\n",
        ),
        (
            "invalid-outcome-class",
            "bash core/scripts/recurring-close.sh g-001-05 banana",
            "",
            "recurring-close: invalid outcome class 'banana' "
            "(expected: routine|deep)\n",
        ),
        (
            "invalid-source",
            "bash core/scripts/recurring-close.sh g-001-05 routine --source moon",
            "",
            "recurring-close: invalid --source 'moon' (expected: world|agent)\n",
        ),
        (
            "iteration-close-phase-rejected",
            "bash core/scripts/iteration-close.sh --phase productivity-check",
            "[iteration-close] missing required flag(s): --source\n",
            "",
        ),
    ],
)
def test_rejected_close_invocations_inject_nothing(
    tmp_path, label, command, resp_stdout, resp_stderr
):
    """Each early-exit rejection path matched the command-text predicate and
    fired the reminder pre-fix. None of them printed ITERATION COMPLETE,
    because none of them closed anything."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(
        command,
        {"stdout": resp_stdout, "stderr": resp_stderr, "interrupted": False},
    )
    rc, stdout, hook_stderr = _invoke_hook(payload, proj)
    assert rc == 0, f"[{label}] hook exit non-zero: rc={rc}, {hook_stderr[-300:]}"
    assert stdout.strip() == "", (
        f"[{label}] closed no iteration — the hook must inject nothing; "
        f"got stdout: {stdout[:400]}"
    )


def test_partial_output_without_the_marker_injects_nothing(tmp_path):
    """A close that ran real work but died before its terminal marker (a
    mid-phase crash, a truncated stream) is NOT a completed iteration. The
    gate keys on the marker, not on 'stdout looks close-ish'."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    truncated = (
        "[iteration-close] retrieval gate (state-update): inferred utilization\n"
        "[loop-state-bump-counters] outcome=deep goals_completed=162\n"
        "[iteration-close] iteration-commit: {\"commit_sha\": \"abc123\"}\n"
    )
    payload = _make_payload(
        COMMAND_ITERATION_CLOSE,
        {"stdout": truncated, "stderr": "", "interrupted": True},
    )
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    assert stdout.strip() == "", (
        f"close output without the terminal marker is not a completed "
        f"iteration; got stdout: {stdout[:400]}"
    )


def test_marker_survives_rule_character_drift(tmp_path):
    """The gate anchors on the two STABLE literals — the bracketed producer
    tag and the phrase — so a cosmetic change to the U+2550 rule characters
    cannot silently re-open the false-fire (the same tolerance
    _DEEP_RECURRING_RE has for dash drift). Mirrors the producer-side pin in
    /verify-learning sq-018."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    for variant in (
        "[iteration-close] ═══ ITERATION COMPLETE ═══",   # production literal
        "[iteration-close] === ITERATION COMPLETE ===",   # ascii drift
        "[iteration-close] --- ITERATION COMPLETE ---",   # hyphen drift
        "[iteration-close] ITERATION COMPLETE",           # rule removed
    ):
        payload = _make_payload(
            COMMAND_ITERATION_CLOSE,
            {"stdout": variant + "\n", "stderr": "", "interrupted": False},
        )
        rc, stdout, _ = _invoke_hook(payload, proj)
        assert rc == 0
        ctx = _additional_context(stdout)
        assert GENERIC_MARKER in ctx, (
            f"marker variant {variant!r} should still be recognised as a "
            f"completed close; got: {ctx[:200]}"
        )


def test_tag_and_phrase_on_separate_lines_do_not_pair(tmp_path):
    """The gate's inter-literal run excludes newlines, so a producer tag on
    one line cannot pair with the phrase on another to manufacture a false
    positive."""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    payload = _make_payload(
        COMMAND_RECURRING,
        {
            "stdout": "[recurring-close] starting\nsome ITERATION COMPLETE text\n",
            "stderr": "",
            "interrupted": False,
        },
    )
    rc, stdout, _ = _invoke_hook(payload, proj)
    assert rc == 0
    assert stdout.strip() == "", (
        f"tag and phrase on separate lines must not pair; got: {stdout[:400]}"
    )


# ── Orphaned-stdin regression (): hook must NOT hang forever ──
# Before the guard-664 daemon-thread+join(timeout) fix, json.load(sys.stdin)
# blocked INDEFINITELY when the stdin pipe's write-end was held open by an
# inherited/orphaned handle — pid-28404 ran 343h (stale-jobs-scan flagged it,
# stale-scanner overage-deadlock). The fix makes the hook self-exit at the
# timeout instead. This test holds the pipe open and asserts the process exits.


def test_orphaned_stdin_does_not_hang_forever(tmp_path):
    """Hold the hook's stdin pipe open (never write, never close) to simulate
    the orphaned/inherited pipe that produced the 343h pid-28404 orphan. With
    ITERATION_CLOSE_REMINDER_STDIN_TIMEOUT_S=2, the hook MUST self-exit (rc 0)
    via the guard-664 daemon-thread + join() deadline rather than blocking on
    json.load(sys.stdin). A regression to a bare blocking read makes p.wait
    raise TimeoutExpired -> test fails. (g-115-1382)"""
    proj = _build_fake_project(tmp_path, AGENT, SID)
    env = os.environ.copy()
    for k in list(env):
        if k.startswith("MIND_") or k == "PROJECT_ROOT":
            env.pop(k, None)
    env["PROJECT_ROOT"] = str(proj).replace("\\", "/")
    env["PYTHONPATH"] = str(CORE_SCRIPTS)
    env["ITERATION_CLOSE_REMINDER_STDIN_TIMEOUT_S"] = "2"

    proc = subprocess.Popen(
        [sys.executable, str(HOOK_PY)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(proj),
    )
    try:
        # Never write to or close proc.stdin: the reader thread blocks on
        # read(), the 2s join() deadline must fire, and the process exits.
        # 8s is a generous margin over the 2s stdin timeout.
        rc = proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        pytest.fail(
            "hook hung > 8s on an orphaned stdin pipe — the guard-664 "
            "daemon-thread+join(timeout) read regressed to a blocking "
            "json.load(sys.stdin) (g-115-1382 343h-orphan class)"
        )
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
    assert rc == 0, f"hook should fail-open rc=0 on stdin timeout; got rc={rc}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
