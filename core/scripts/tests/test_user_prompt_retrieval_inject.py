"""test_user_prompt_retrieval_inject.py — skip-path pins for the
UserPromptSubmit auto-retrieval hook (2026-08-21).

The hook's danger surface is its EXIT CODE: on UserPromptSubmit, exit 2
BLOCKS AND ERASES the user's prompt. Every pin below therefore asserts
rc in (0, 1) — never 2 — alongside the skip behavior. The happy path
(binding + live daemon + index) is exercised manually and by real use;
these pins cover the hermetic skip lattice that must stay silent."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _runtime_bash import bash_cmd  # noqa: E402  guard-580: never bare "bash"

SCRIPT = Path(__file__).resolve().parent.parent / "user-prompt-retrieval-inject.sh"


def _run(payload):
    r = subprocess.run(bash_cmd(SCRIPT),
                       input=json.dumps(payload) if isinstance(payload, dict) else payload,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode != 2, "exit 2 on UserPromptSubmit erases the user's prompt"
    return r


def test_short_prompt_is_silent():
    r = _run({"session_id": "s1", "user_input": "ok thanks"})
    assert r.stdout.strip() == ""
    assert r.returncode == 0


def test_ack_prompt_is_silent():
    r = _run({"session_id": "s1", "user_input": "sounds good, go ahead and do it!"})
    assert r.stdout.strip() == ""


def test_slash_command_is_silent():
    r = _run({"session_id": "s1", "user_input": "/encode-session please run this now"})
    assert r.stdout.strip() == ""


def test_expansion_typed_prompt_is_silent():
    r = _run({"session_id": "s1", "expansion_type": "slash_command",
              "command_name": "start",
              "user_input": "a long enough prompt that would otherwise pass the length gate"})
    assert r.stdout.strip() == ""


def test_unresolvable_binding_is_silent():
    r = _run({"session_id": "no-such-sid-anywhere-000",
              "user_input": "a substantive question about the architecture of the retrieval system"})
    assert r.stdout.strip() == ""
    assert r.returncode == 0


def test_garbage_payload_never_blocks():
    r = _run("this is not json at all {{{")
    assert r.stdout.strip() == ""
    assert r.returncode in (0, 1)


def test_empty_stdin_never_blocks():
    r = _run("")
    assert r.returncode in (0, 1)


def test_wakeup_sentinel_is_silent():
    """Loop-continuation sentinels are machine re-entry prompts, not questions
    (schedule-wakeup-correctness.md). Without this skip every deadman wakeup
    burned a junk retrieval (measured 2026-08-21)."""
    r = _run({"session_id": "s1",
              "user_input": "<<autonomous-loop-dynamic>> resume the loop iteration"})
    assert r.stdout.strip() == ""


def _binding_tree(tmp_path, sid, binding_mode, file_mode):
    """Resolvable Phase-2.6 tree: the resolver cross-checks session_id AND
    agent fields against the on-disk layout AND requires local-paths.conf —
    a fixture missing any of the three resolves to None and a gate test
    passes at the wrong exit (vacuous; caught live 2026-08-21)."""
    adir = tmp_path / "agents" / "tau"
    (adir / "sessions" / sid).mkdir(parents=True)
    (adir / "session").mkdir()
    (adir / "local-paths.conf").write_text("# test\n", encoding="utf-8")
    (adir / "sessions" / sid / "binding.yaml").write_text(
        "agent: tau\nsession_id: %s\nmode: %s\n" % (sid, binding_mode),
        encoding="utf-8")
    (adir / "session" / "agent-mode").write_text(file_mode + "\n",
                                                encoding="utf-8")
    env = dict(os.environ)
    env["MIND_PROMPT_HOOK_ROOT"] = str(tmp_path)
    env["MIND_PROMPT_HOOK_DRYRUN"] = "1"
    return env


def _run_env(sid, env):
    r = subprocess.run(
        bash_cmd(SCRIPT),
        input=json.dumps({"session_id": sid,
                          "user_input": "a substantive domain question long "
                                        "enough to pass every text gate"}),
        capture_output=True, text=True, timeout=60, env=env)
    assert r.returncode != 2, "exit 2 on UserPromptSubmit erases the user's prompt"
    return r


def test_autonomous_mode_session_is_silent(tmp_path):
    """Binding mode autonomous → no injection: the loop retrieves for itself.
    DRYRUN seam makes the negative non-vacuous — were the gate absent, the
    dryrun marker would print."""
    env = _binding_tree(tmp_path, "hook-test-sid-0001", "autonomous", "autonomous")
    r = _run_env("hook-test-sid-0001", env)
    assert r.stdout.strip() == ""


def test_observer_binding_mode_overrides_agent_file(tmp_path):
    """Session-first ordering: an observer session (binding mode assistant)
    beside a RUNNING loop (agent-wide file autonomous) MUST still get the
    pre-pass — the agent file is only the binding-less fallback."""
    env = _binding_tree(tmp_path, "hook-test-sid-0002", "assistant", "autonomous")
    r = _run_env("hook-test-sid-0002", env)
    out = json.loads(r.stdout)
    assert out == {"dryrun": True, "agent": "tau", "mode": "assistant"}


def test_agent_file_fallback_gates_bindingless_autonomous(tmp_path):
    """A binding that carries no mode field falls back to the agent-wide
    file: file=autonomous → silent. (The memo-resolved binding-less path
    shares this fallback leg — same read, no per-session mode available.)"""
    sid = "hook-test-sid-0003"
    env = _binding_tree(tmp_path, sid, "autonomous", "autonomous")
    # strip the mode line from the binding so the fallback leg is exercised
    b = tmp_path / "agents" / "tau" / "sessions" / sid / "binding.yaml"
    b.write_text("agent: tau\nsession_id: %s\n" % sid, encoding="utf-8")
    r = _run_env(sid, env)
    assert r.stdout.strip() == ""
