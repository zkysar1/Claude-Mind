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
import re
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
    (adir / "session").mkdir(exist_ok=True)  # reentrant: the per-session dedup pin builds two SIDs under ONE agent tree (sharing the root is the whole point — separate roots would make that test vacuous)
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


# --- session dedup + latency budget ( outcomes 2 and 3) -------------
#
# These use MIND_PROMPT_HOOK_FAKE_RETRIEVAL, a seam in the same family as
# MIND_PROMPT_HOOK_DRYRUN: it supplies the retrieval JSON directly. Without
# it a dedup pin needs a live daemon AND a stable corpus, which makes it an
# integration test that cannot actually pin the branch.

FAKE = json.dumps({
    "meta": {"embedding_channel": "test"},
    "tree_nodes": [{"key": "system/alpha", "summary": "first node"},
                   {"key": "system/beta", "summary": "second node"}],
    "reasoning_bank": [{"id": "rb-1", "title": "a lesson"}],
    "guardrails": [],
    "framework_rules": [],
})


def _assistant_tree(tmp_path, sid):
    env = _binding_tree(tmp_path, sid, "assistant", "assistant")
    env.pop("MIND_PROMPT_HOOK_DRYRUN", None)   # we want the real inject path
    env["MIND_PROMPT_HOOK_FAKE_RETRIEVAL"] = FAKE
    return env


def _ctx(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_first_injection_carries_the_index(tmp_path):
    """Positive control for every dedup pin below. Without it a suppression
    test cannot distinguish 'deduped' from 'never injected anything'."""
    env = _assistant_tree(tmp_path, "dedup-sid-0001")
    ctx = _ctx(_run_env("dedup-sid-0001", env))
    assert "system/alpha" in ctx and "system/beta" in ctx and "rb-1" in ctx


def test_identical_hits_are_not_re_injected_in_the_same_session(tmp_path):
    """Outcome 3. The second identical message must not repeat the index."""
    sid = "dedup-sid-0002"
    env = _assistant_tree(tmp_path, sid)
    first = _ctx(_run_env(sid, env))
    second = _ctx(_run_env(sid, env))
    assert first != second
    assert "already injected earlier this session" in second
    # The bulk is gone: no summaries/titles are repeated...
    assert "first node" not in second and "a lesson" not in second
    # ...but the POINTER survives. Silence here would read to the model as
    # "nothing matched", which is the opposite of the truth.
    assert "system/alpha" in second and "rb-1" in second


def test_dedup_is_per_session_not_global(tmp_path):
    """A different SID must get the full index — the state is session-scoped,
    and a global suppression would blind every later session on this box."""
    env_a = _assistant_tree(tmp_path, "dedup-sid-0003")
    _run_env("dedup-sid-0003", env_a)
    env_b = _assistant_tree(tmp_path, "dedup-sid-0004")
    ctx = _ctx(_run_env("dedup-sid-0004", env_b))
    assert "first node" in ctx


def test_partially_seen_hits_inject_only_the_new_ones(tmp_path):
    """Filter BEFORE cap: a session that saw some hits gets the rest, plus a
    count of what was withheld."""
    sid = "dedup-sid-0005"
    env = _assistant_tree(tmp_path, sid)
    seen = tmp_path / "agents" / "tau" / "sessions" / sid / "prompt-injected-hits.txt"
    seen.write_text("t:system/alpha\n", encoding="utf-8")
    ctx = _ctx(_run_env(sid, env))
    assert "second node" in ctx          # the unseen one is injected
    assert "first node" not in ctx       # the seen one is not
    assert "not repeated" in ctx or "already injected" in ctx


def test_dedup_failure_fails_OPEN_toward_injecting(tmp_path):
    """The load-bearing asymmetry: a broken state file must never SUPPRESS.
    Under-injecting silently disables the whole hook and is indistinguishable
    from 'no matches'; over-injecting only costs tokens."""
    sid = "dedup-sid-0006"
    env = _assistant_tree(tmp_path, sid)
    p = tmp_path / "agents" / "tau" / "sessions" / sid / "prompt-injected-hits.txt"
    p.mkdir(parents=True)     # a DIRECTORY where a file belongs -> read raises
    ctx = _ctx(_run_env(sid, env))
    assert "first node" in ctx and "rb-1" in ctx


def test_dedup_never_exits_2(tmp_path):
    """The hook's danger surface is unchanged by dedup: exit 2 erases the
    user's prompt, so the new branches must not introduce one."""
    sid = "dedup-sid-0007"
    env = _assistant_tree(tmp_path, sid)
    for _ in range(3):
        assert _run_env(sid, env).returncode != 2


def test_latency_budget_is_bounded_in_source():
    """Outcome 2's untested half.

    SOURCE PIN, AND WEAKER THAN A TIMING ASSERTION — labelled so rather than
    reported as if it measured anything. It pins the declared ceiling, not
    observed latency; a real timing test would need a deliberately-slow
    daemon and would be flaky on a loaded box. Measured on cc-08 2026-09-03,
    this script's exact invocation ran 957/1037/1575 ms, so 5s is ~3.2x the
    observed max while capping user-visible dead air.

    The regression this blocks is the one that was live until 2026-09-03:
    the ceiling silently sitting at 18s, where a pathological retrieval
    stalls the user's prompt for 18 seconds before the model even starts.
    """
    src = SCRIPT.read_text(encoding="utf-8")
    m = re.search(r"^RETRIEVE_TIMEOUT_S\s*=\s*(\d+)", src, re.M)
    assert m, "the latency budget must be a named constant, not an inline literal"
    assert 2 <= int(m.group(1)) <= 8, "budget outside the measured-safe band"
    assert "timeout=RETRIEVE_TIMEOUT_S" in src, "the constant must be the one in force"
