"""Tests for parked-body-gate.{sh,py} (): a PARKED worker Body does no
work until its park check runs.

The positive control is the measured case (g-375-98): a parked Body, no park
check since the park, a work call opened by something other than the park
wakeup -> DENY. Everything else pins what must keep passing: the park path, an
open re-poll, an operator command, and every Body that is not parked.

PRODUCTION SHAPE THROUGHOUT (guard-1742). MIND_AGENT is scrubbed from the child
env, as the harness runs a Write/Edit hook, and a real sessions/<SID>/binding.yaml
is written for the gate to resolve; the real .sh wrapper is invoked from
PROJECT_ROOT. The park state is set through body-manifest.py's own CLI, so the
stamps the gate reads are the ones production writes.

Run: py -3 -m pytest core/scripts/tests/test_parked_body_gate.py -v
"""
import json
import os
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent   # repo root
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

# A throwaway agent no real session uses, created under the real PROJECT_ROOT so
# _paths resolves exactly as in production, and removed in `finally`.
AGENT = "_parked_gate_test_agent_"
SID = "parked-gate-test-sid-001"
WORK_CMD = "python3 scripts/next_plan_step.py --apply"   # work, not the park path
STAMP = "operator-input-at"


@contextmanager
def _body(role="worker", manifest_text=None):
    """A bound worker session with a fork WM and an ACTIVE manifest.

    Yields (session_dir, env). env has MIND_AGENT scrubbed (production shape).
    """
    agent_dir = PROJECT_ROOT / "agents" / AGENT
    sdir = agent_dir / "sessions" / SID
    try:
        sdir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "session").mkdir(parents=True, exist_ok=True)
        # newline="" keeps the fixture LF on Windows (guard-1688).
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline="")
        (sdir / "binding.yaml").write_text(
            f"session_id: {SID}\nagent: {AGENT}\nmode: autonomous\n"
            "started_at: '2026-01-01T00:00:00'\nstarted_by: test\n",
            encoding="utf-8", newline="")
        (sdir / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8", newline="")
        (sdir / "body-manifest.yaml").write_text(
            manifest_text if manifest_text is not None
            else f"role: '{role}'\nbody_state: 'active'\n",
            encoding="utf-8", newline="")
        env = dict(os.environ)
        env.pop("MIND_AGENT", None)
        yield sdir, env
    finally:
        if agent_dir.name == AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _manifest_cli(env, *args, extra_env=None):
    """body-manifest.py <verb> --sid SID --agent AGENT, the production writer."""
    e = dict(env)
    e.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "body-manifest.py"), *args,
         "--sid", SID, "--agent", AGENT],
        capture_output=True, text=True, env=e, cwd=str(PROJECT_ROOT), timeout=60)


def _hook(env, payload, *args):
    """Run the real wrapper as the harness does. Returns the CompletedProcess."""
    return subprocess.run(
        [BASH, "core/scripts/parked-body-gate.sh", *args],
        input=json.dumps(payload), capture_output=True, text=True,
        env=env, cwd=str(PROJECT_ROOT), timeout=60)


def _tool(env, tool, tool_input):
    return _hook(env, {"tool_name": tool, "tool_input": tool_input, "session_id": SID})


def _bash(env, command):
    return _tool(env, "Bash", {"command": command})


def _denied(result):
    return '"permissionDecision": "deny"' in result.stdout


def _approved(result):
    return result.returncode == 0 and result.stdout.strip() == ""


def _park(env, extra_env=None):
    r = _manifest_cli(env, "park", extra_env=extra_env)
    assert r.returncode == 0, r.stderr[-400:]
    return r.stdout.strip()


def _next_second():
    """Stamps have one-second resolution and the gate compares them strictly."""
    time.sleep(1.1)


# ── The measured case: parked, no park check since, a work call ─────────────

def test_parked_body_work_call_is_denied_positive_control():
    with _body() as (_sdir, env):
        assert "MIND_AGENT" not in env, "must run the production (binding) shape"
        assert _park(env) == "parked"
        r = _bash(env, WORK_CMD)
        assert _denied(r), f"a parked Body's work call must be denied: {r.stdout!r}"
        assert "Skill(worker-loop)" in r.stdout, "the deny must name the park check's entry"


def test_parked_body_file_writers_are_denied():
    with _body() as (sdir, env):
        _park(env)
        target = str(PROJECT_ROOT / "notes.md")
        assert _denied(_tool(env, "Write", {"file_path": target, "content": "x"}))
        assert _denied(_tool(env, "Edit", {"file_path": target, "old_string": "a",
                                           "new_string": "b"}))
        assert _denied(_tool(env, "MultiEdit", {"file_path": target, "edits": []}))


def test_reads_and_the_park_tools_are_never_gated():
    with _body() as (_sdir, env):
        _park(env)
        for tool in ("Read", "Grep", "Skill", "ScheduleWakeup", "TaskOutput"):
            assert _approved(_tool(env, tool, {})), tool


# ── The park path passes while parked ────────────────────────────────────────

PARK_PATH_COMMANDS = (
    # Phase -0-stop and Phase -0, as the worker loop prints them.
    'test -n "$MIND_SID" && test -f "agents/$MIND_AGENT/sessions/$MIND_SID/stop-requested"'
    ' && echo "user-stop" || echo "no-stop-or-unreadable"',
    'grep "^body_state:" "agents/$MIND_AGENT/sessions/$MIND_SID/body-manifest.yaml"',
    'py -3 core/scripts/body-manifest.py park-due --sid "$MIND_SID" --agent "$MIND_AGENT"',
    # The park sequence and the no-goal park.
    'echo "reducer stale, this Body PARKED" | bash core/scripts/board-post.sh'
    ' --channel coordination --type finding --tags reducer-stall,body-parked',
    'python3 core/scripts/stop-reason-record.py --path worker-body-parked --reason x --agent a',
    'py -3 core/scripts/recovery_yank.py check --agent "$MIND_AGENT"',
    'touch "agents/$MIND_AGENT/sessions/$MIND_SID/body-closing"',
    # The turn's terminal.
    'echo "parked; wakeup armed"',
)


def test_park_path_bash_calls_pass_while_parked():
    with _body() as (_sdir, env):
        _park(env)
        for command in PARK_PATH_COMMANDS:
            assert _approved(_bash(env, command)), command


def test_an_echo_that_does_more_than_echo_is_gated():
    with _body() as (_sdir, env):
        _park(env)
        assert _denied(_bash(env, 'echo done > notes.txt'))
        assert _denied(_bash(env, 'echo ok; ' + WORK_CMD))
        assert _denied(_bash(env, 'echo "$(' + WORK_CMD + ')"'))


# ── The park check opens a re-poll; the next park closes it ──────────────────

def test_due_park_check_opens_the_repoll_and_a_repark_closes_it():
    with _body() as (sdir, env):
        # A zero backoff makes the first full poll due at once.
        _park(env, extra_env={"PARK_BACKOFF_BASE_SECONDS": "0"})
        assert _denied(_bash(env, WORK_CMD)), "no park check yet"
        _next_second()
        due = _manifest_cli(env, "park-due")
        assert due.returncode == 0 and due.stdout.startswith("due"), due.stdout
        assert "repoll_opened_at:" in (sdir / "body-manifest.yaml").read_text(encoding="utf-8")
        assert _approved(_bash(env, WORK_CMD)), "a DUE park check opens the re-poll"
        _next_second()
        assert _park(env) == "already-parked"
        assert _denied(_bash(env, WORK_CMD)), "the re-park closes the re-poll it ended"
        r = _manifest_cli(env, "resume")
        assert r.stdout.strip() == "resumed", r.stdout
        assert _approved(_bash(env, WORK_CMD)), "a resumed Body is active again"
        text = (sdir / "body-manifest.yaml").read_text(encoding="utf-8")
        assert "last_parked_at" not in text and "repoll_opened_at" not in text


def test_a_not_due_park_check_does_not_open_the_gate():
    with _body() as (sdir, env):
        _park(env)                      # the default orbit: next full poll an hour out
        _next_second()
        r = _manifest_cli(env, "park-due")
        assert r.returncode == 1 and r.stdout.startswith("not-due"), r.stdout
        assert "repoll_opened_at" not in (sdir / "body-manifest.yaml").read_text(encoding="utf-8")
        assert _denied(_bash(env, WORK_CMD))


# ── An operator command opens it; a harness notification does not ───────────

OPERATOR_PROMPTS = (
    "/start alpha",
    "/stop",
    "<command-message>/stop is running</command-message>\n<command-name>/stop</command-name>",
)
NON_OPERATOR_PROMPTS = (
    "[harness] a background command you started has exited. This is a notification",
    "<task-notification>\n<task-id>b1</task-id>\n<status>killed</status>\n</task-notification>",
    "Parked worker Body: re-enter /worker-loop at Phase -0 (manifest: parked = RESUMABLE)",
    # A prompt that opens with a pasted path is not a slash command.
    "/opt/ayoai-mind/core/scripts/body-manifest.py exits 1 here",
    "/README.md",
)


def _submit(env, prompt):
    return _hook(env, {"prompt": prompt, "session_id": SID,
                       "hook_event_name": "UserPromptSubmit"}, "stamp")


def test_operator_command_opens_the_gate():
    for prompt in OPERATOR_PROMPTS:
        with _body() as (sdir, env):
            _park(env)
            _next_second()
            r = _submit(env, prompt)
            assert r.returncode == 0 and r.stdout == "", "stamp mode prints nothing"
            assert (sdir / STAMP).is_file(), prompt
            assert _approved(_bash(env, WORK_CMD)), prompt


def test_notifications_and_the_park_wakeup_do_not_stamp():
    with _body() as (sdir, env):
        _park(env)
        _next_second()
        for prompt in NON_OPERATOR_PROMPTS:
            r = _submit(env, prompt)
            assert r.returncode == 0 and r.stdout == "", prompt
        assert not (sdir / STAMP).exists()
        assert _denied(_bash(env, WORK_CMD))


def test_an_operator_command_before_the_park_does_not_open_it():
    with _body() as (sdir, env):
        _submit(env, "/start alpha")
        assert (sdir / STAMP).is_file()
        _next_second()
        _park(env)
        assert _denied(_bash(env, WORK_CMD))


# ── Everything that is not a parked worker passes ────────────────────────────

def test_active_body_is_not_gated():
    with _body() as (_sdir, env):
        assert _approved(_bash(env, WORK_CMD))


def test_a_non_worker_session_is_not_gated():
    with _body(role="reducer", manifest_text="role: 'reducer'\nbody_state: 'parked'\n"
               "parked_at: '2026-09-30T12:00:00'\npark_next_poll_at: '2099-01-01T00:00:00'\n"
               ) as (_sdir, env):
        assert _approved(_bash(env, WORK_CMD))


def test_an_unbound_session_is_not_gated():
    with _body() as (sdir, env):
        _park(env)
        (sdir / "binding.yaml").unlink()
        assert _approved(_bash(env, WORK_CMD))


def test_an_untimeable_park_fails_open_toward_polling():
    manifest = ("role: 'worker'\nbody_state: 'parked'\nparked_at: '2026-09-30T12:00:00'\n"
                "park_next_poll_at: 'soon'\n")
    with _body(manifest_text=manifest) as (_sdir, env):
        assert _approved(_bash(env, WORK_CMD))


def test_a_malformed_manifest_fails_open():
    with _body(manifest_text="role: [unclosed\n") as (_sdir, env):
        assert _approved(_bash(env, WORK_CMD))


def test_a_manifest_parked_before_this_change_uses_parked_at():
    """A Body parked by the previous body-manifest.py has no last_parked_at."""
    manifest = ("role: 'worker'\nbody_state: 'parked'\nparked_at: '2026-09-30T12:47:47'\n"
                "park_count: 1\npark_next_poll_at: '2099-01-01T00:00:00'\n")
    with _body(manifest_text=manifest) as (_sdir, env):
        assert _denied(_bash(env, WORK_CMD))


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
