""": one context-reads tracker per SESSION, not per agent.

THE DEFECT. tracker_path() gave a per-session tracker only to a worker Body with a
forked working-memory.yaml. Every other session of an agent (the reducer, and every
reader/assistant/observer session) shared agents/<agent>/session/context-reads.txt,
and _read_tracker_split() UNLINKS that file whenever the calling session id differs
from its #session header. So two live same-agent sessions erased each other's read
records on every hook call. Measured 2026-09-27 with three alpha sessions on one
box: pre-edit-context-gate advised "has not been Read" after real Reads, and
load-conventions re-offered conventions already in context.

THE FIX. Any session with a per-session dir (agents/<agent>/sessions/<sid>/, which
the Phase 2.6 binding creates) gets sessions/<sid>/body-context-reads.txt. The
agent-wide file remains only for a session with no such dir, and for a caller
with no session id at all.

RECONCILED WITH g-115-8976. A caller that omits --session-id now falls back to
MIND_SID (resolved once, at dispatch). The two changes are only safe together:
- Routing alone would split each session in two. Hooks pass --session-id and would
  write the per-session file, while the flagless loaders (load-conventions.sh and
  the digest loaders) would read the agent-wide one.
- The fallback alone would put a session id on every flagless call, and so would
  multiply the mismatch unlinks on the shared file.

Subprocess tests drive the real code paths under a throwaway agent beneath the
real PROJECT_ROOT, torn down in finally. Each fixture scrubs MIND_SID so the
launching shell's session cannot steer a flagless call (guard-1515: the env is
an input). Daemon-safe: pure file routing, no daemon, no world store.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_context_reads_session_routing.py -q
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent                      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent            # repo root
CR_PY = CORE_SCRIPTS / "context-reads.py"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402
# : the REAL normalizer, never a hand-rolled copy.
from _context_reads_helper import norm_path as _norm  # noqa: E402

THROWAWAY = "_cr_session_routing_test_agent_"
SID_A = "a1a1a1a1-0000-4000-8000-00000000000a"
SID_B = "b2b2b2b2-0000-4000-8000-00000000000b"
SID_W = "c3c3c3c3-0000-4000-8000-00000000000c"   # a forked worker Body

# Real in-scope files (recorder scope includes .claude/skills and core/config).
F1 = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"
F2 = PROJECT_ROOT / "core" / "config" / "conventions" / "session-state.md"
F3 = PROJECT_ROOT / "core" / "config" / "conventions" / "board.md"


@contextmanager
def _agent(bound=(), forked=()):
    """Throwaway agent. `bound` sids get a per-session dir holding only a Phase 2.6
    binding.yaml (the reducer / assistant / reader shape). `forked` sids also get a
    forked working-memory.yaml (the worker-Body shape). Yields (env, agent_dir)."""
    adir = PROJECT_ROOT / "agents" / THROWAWAY
    try:
        (adir / "session").mkdir(parents=True, exist_ok=True)
        # newline="" (guard-1688): _paths.sh sources this conf.
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline="")
        for sid in list(bound) + list(forked):
            sd = adir / "sessions" / sid
            sd.mkdir(parents=True, exist_ok=True)
            (sd / "binding.yaml").write_text(
                f"session_id: {sid}\nagent: {THROWAWAY}\nmode: assistant\n"
                "started_at: '2026-01-01T00:00:00'\nstarted_by: test\n",
                encoding="utf-8", newline="")
        for sid in forked:
            (adir / "sessions" / sid / "working-memory.yaml").write_text(
                "slots: {}\n", encoding="utf-8", newline="")
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY
        env["STORAGE_BACKEND"] = "local"   # guard-955
        env.pop("MIND_SID", None)         # guard-1515: no inherited session id
        yield env, adir
    finally:
        if adir.name == THROWAWAY and adir.is_dir():
            shutil.rmtree(adir, ignore_errors=True)


def _cr(env, *args):
    return subprocess.run([sys.executable, str(CR_PY), *map(str, args)],
                          capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=60)


def _ok(result):
    assert result.returncode == 0, f"rc={result.returncode} stderr={result.stderr[:400]}"
    return result


def _session_tracker(adir, sid):
    return adir / "sessions" / sid / "body-context-reads.txt"


def _agent_wide(adir):
    return adir / "session" / "context-reads.txt"


def _untracked(env, sid, *paths):
    """check-file goes through _read_tracker_split, the path that holds the
    session-mismatch unlink. Returns the set of paths it reports NOT read."""
    r = _ok(_cr(env, "check-file", "--session-id", sid, *paths))
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def _interleave(env):
    """A and B alternate. Every call reads the tracker first (cmd_record runs
    _read_tracker_split before it appends), so on a SHARED tracker each call
    meets the other session's header and unlinks the file."""
    _ok(_cr(env, "record", "--session-id", SID_A, F1))
    _ok(_cr(env, "record", "--session-id", SID_B, F2))
    _ok(_cr(env, "record", "--session-id", SID_A, "--partial", F3))
    _ok(_cr(env, "record-prov", "--session-id", SID_B, "--kind", "url",
            "https://example.invalid/b-only"))
    # Queries through the mismatch path, peer first, then owner.
    _untracked(env, SID_B, F1, F2, F3)
    _untracked(env, SID_A, F1, F2, F3)


# ---------------------------------------------------------------------------
# Outcome 1: two sessions without forked WMs keep separate read records
# ---------------------------------------------------------------------------

def test_two_unforked_sessions_keep_separate_records():
    with _agent(bound=[SID_A, SID_B]) as (env, adir):
        _interleave(env)

        ta, tb = _session_tracker(adir, SID_A), _session_tracker(adir, SID_B)
        assert ta.is_file() and tb.is_file(), "each bound session keeps its own tracker"
        a_text, b_text = ta.read_text(encoding="utf-8"), tb.read_text(encoding="utf-8")
        assert a_text.startswith(f"#session:{SID_A}\n"), a_text[:120]
        assert b_text.startswith(f"#session:{SID_B}\n"), b_text[:120]
        assert not _agent_wide(adir).exists(), (
            "a session with a per-session dir must never write the agent-wide file")

        # Each session's reads survived the other's writes and queries.
        assert _untracked(env, SID_A, F1, F2, F3) == {_norm(F2), _norm(F3)}, (
            "A read F1 in full; B's calls must not have erased it")
        assert _untracked(env, SID_B, F1, F2, F3) == {_norm(F1), _norm(F3)}, (
            "B read F2 in full; A's calls must not have erased it")
        assert f"#partial:{_norm(F3)}" in a_text, "A's ranged read survived"
        assert "b-only" in b_text and "b-only" not in a_text, (
            "provenance stays with the session that retrieved it")


def test_control_sessions_without_a_dir_still_share_the_singleton_and_erase():
    """POSITIVE CONTROL for the test above. The same interleave, run by two
    sessions WITHOUT per-session dirs, lands on the shared agent-wide file.
    There A's full read of F1 is erased, which proves that _interleave() really
    drives _read_tracker_split's mismatch unlink. Before g-115-11179 every
    unforked session had this shape. After it, only a session with no
    per-session dir does (a legacy .active-agent-<SID> binding, say).

    If the agent-wide fallback ever stops thrashing, replace this control with
    one that pins the pre-fix routing directly. Do not delete it: without a
    control, the test above could pass on an interleave that never reaches the
    unlink."""
    with _agent() as (env, adir):   # no per-session dirs
        _ok(_cr(env, "record", "--session-id", SID_A, F1))
        assert _norm(F1) in _agent_wide(adir).read_text(encoding="utf-8"), (
            "dirless sessions use the agent-wide file")
        _interleave(env)
        assert not _session_tracker(adir, SID_A).exists()
        # No existence assertion on the agent-wide file at the end: the last
        # mismatch in the interleave may legitimately have unlinked it.
        assert _norm(F1) in _untracked(env, SID_A, F1), (
            "control failed: the shared-file interleave no longer erases A's read, "
            "so it no longer proves the per-session test exercises the mismatch path")


# ---------------------------------------------------------------------------
# Outcome 3 / : an omitted --session-id falls back to MIND_SID
# ---------------------------------------------------------------------------

def test_flagless_callers_resolve_the_session_tracker_from_MIND_SID():
    """load-conventions.sh (`check`) and the digest loaders (`check-file`) pass no
    --session-id. They must still read the tracker the session's hooks write, or
    routing breaks their dedup."""
    with _agent(bound=[SID_A]) as (env, adir):
        _ok(_cr(env, "record", "--session-id", SID_A, F2))       # a hook-recorded Read
        env_a = dict(env, MIND_SID=SID_A)

        r = _ok(_cr(env_a, "check", "session-state", "board"))
        offered = {line.strip() for line in r.stdout.splitlines() if line.strip()}
        assert _norm(F2) not in offered, (
            "load-conventions re-offered a convention this session already read: the "
            "flagless `check` read some other tracker")
        assert _norm(F3) in offered, "an unread convention must still be offered"

        r = _ok(_cr(env_a, "check-file", F2))
        assert r.stdout.strip() == "", f"flagless check-file lost the read: {r.stdout!r}"

        _ok(_cr(env_a, "record-prov", "--kind", "node", "some/node-key"))
        assert "some/node-key" in _session_tracker(adir, SID_A).read_text(encoding="utf-8")

        r = _ok(_cr(env_a, "status"))
        assert "1 full" in r.stdout, f"status must show this session's tracker: {r.stdout!r}"
        assert not _agent_wide(adir).exists()


def test_flagless_record_on_a_forked_worker_body_reaches_the_body_tracker():
    """'s own acceptance shape: --session-id omitted, MIND_SID naming a
    forked Body's unitKey, and the per-Body tracker, not the agent-wide one."""
    with _agent(forked=[SID_W]) as (env, adir):
        _ok(_cr(dict(env, MIND_SID=SID_W), "record", F1))
        assert _norm(F1) in _session_tracker(adir, SID_W).read_text(encoding="utf-8")
        assert not _agent_wide(adir).exists()


def test_an_explicit_session_id_outranks_MIND_SID():
    with _agent(bound=[SID_A, SID_B]) as (env, adir):
        _ok(_cr(dict(env, MIND_SID=SID_A), "record", "--session-id", SID_B, F1))
        assert _session_tracker(adir, SID_B).is_file()
        assert not _session_tracker(adir, SID_A).exists(), (
            "the hook's --session-id is the authority; the env is only a fallback")


def test_with_no_session_id_anywhere_the_agent_wide_file_is_used():
    """An operator running the wrapper by hand. Neither the flag nor MIND_SID is
    set, so the agent-wide file is used, and a bare clear must not reach a
    session's own tracker."""
    with _agent(bound=[SID_A]) as (env, adir):
        _ok(_cr(env, "record", "--session-id", SID_A, F1))
        _ok(_cr(env, "record", F2))
        assert _norm(F2) in _agent_wide(adir).read_text(encoding="utf-8")
        _ok(_cr(env, "clear"))
        assert not _agent_wide(adir).exists()
        assert _session_tracker(adir, SID_A).is_file(), (
            "a bare clear must leave a session's own tracker alone (guard-404)")


# ---------------------------------------------------------------------------
# Outcome 2: the pre-edit advisory while another same-agent session is active
# ---------------------------------------------------------------------------

def _hook(env, script, payload):
    return subprocess.run([BASH, f"core/scripts/{script}"], input=json.dumps(payload),
                          capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=120)


def test_pre_edit_gate_stays_silent_for_a_file_this_session_read_while_a_peer_is_active():
    """PRODUCTION SHAPE (the  lesson): MIND_AGENT is scrubbed, as it is
    in a Read or Edit hook. The agent is resolved from each session's binding,
    and every Read goes through the real PostToolUse recorder hook."""
    with _agent(bound=[SID_A, SID_B]) as (env, adir):
        hook_env = {k: v for k, v in env.items() if k != "MIND_AGENT"}

        def read(sid, path):
            r = _hook(hook_env, "context-reads-record.sh",
                      {"tool_name": "Read", "session_id": sid,
                       "tool_input": {"file_path": str(path)}})
            assert r.returncode == 0, r.stderr[:400]

        def edit(sid, path):
            return _hook(hook_env, "pre-edit-context-gate.sh",
                         {"tool_name": "Edit", "session_id": sid,
                          "tool_input": {"file_path": str(path)}})

        read(SID_A, F1)       # A reads the file it is about to edit
        read(SID_B, F2)       # the peer session is active and reading
        read(SID_B, F3)

        mine = edit(SID_A, F1)
        assert mine.returncode == 0
        assert "has not been Read" not in mine.stderr and "ADVISORY" not in mine.stdout, (
            "the advisory fired for a file THIS session Read, because a peer "
            f"same-agent session erased the record. stderr={mine.stderr[:300]!r}")

        # Control: the gate is live in this shape. B never read F1.
        theirs = edit(SID_B, F1)
        assert "has not been Read" in theirs.stderr, (
            "control failed: the gate did not advise for a genuinely unread file, so "
            f"the silence above proves nothing. stderr={theirs.stderr[:300]!r}")


# ---------------------------------------------------------------------------
# guard-802: orchestrator re-entry must survive a per-session skill tracker
# ---------------------------------------------------------------------------

def test_orchestrator_skill_reentry_is_not_blocked_by_a_session_tracker():
    """guard-802: "Verify in an isolated MIND_AGENT_DIR test that re-invoking
    Skill(aspirations) twice does NOT return exit 2." The reducer is a bound
    session with no forked WM, which is exactly the session that now gets its
    own tracker. The respond control proves the gate ran against that tracker
    and is still deduping."""
    with _agent(bound=[SID_A]) as (env, adir):
        hook_env = {k: v for k, v in env.items() if k != "MIND_AGENT"}

        def skill(name):
            return _hook(hook_env, "context-reads-skill-gate.sh",
                         {"tool_name": "Skill", "session_id": SID_A,
                          "tool_input": {"skill": name}})

        first, second = skill("aspirations"), skill("aspirations")
        assert first.returncode != 2, first.stderr[:300]
        assert second.returncode != 2, (
            "Skill(aspirations) re-entry was BLOCKED under a per-session tracker: the "
            f"g-304-20 loop-death shape. stderr={second.stderr[:300]!r}")

        assert skill("respond").returncode == 0
        again = skill("respond")
        assert again.returncode == 2, (
            f"control failed: an ordinary skill was not deduped. rc={again.returncode}")
        tracker = _session_tracker(adir, SID_A)
        assert tracker.is_file() and "respond" in tracker.read_text(encoding="utf-8"), (
            "the skill gate must record into the session's own tracker")
        assert not _agent_wide(adir).exists()
