"""Phase 1D (): per-Body context-reads tracker routing.

context-reads.py routes its dedup tracker to
sessions/<unitKey>/body-context-reads.txt when the session names a Body whose
forked body-WM-file exists (the same reducer-aware activation signal wm.py and
AgentPaths.wm_path use), else the agent-wide session/context-reads.txt. With one
Body (no forked WM file) this collapses to today's agent-wide tracker, so
concurrent Bodies stop clobbering each other's session-scoped dedup state -- the
cross-contamination the move fixes.

Subprocess tests exercise the REAL record code path (incl. _paths resolution)
under a throwaway agent created beneath the real PROJECT_ROOT, torn down in
finally (mirrors test_pre_edit_context_gate.py's throwaway-agent pattern).

Daemon-safe (no daemon_integration marker -- no subprocess daemon, no
mind_api/state; pure file routing).

Run:
  python -m pytest core/scripts/tests/test_context_reads_body_routing.py -q
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent                  # repo root
CR_PY = CORE_SCRIPTS / "context-reads.py"
# A real in-scope file (TRACKED_PREFIXES includes .claude/skills) so `record`
# actually tracks it -- out-of-scope paths are silently ignored by design.
IN_SCOPE_FILE = PROJECT_ROOT / ".claude" / "skills" / "respond" / "SKILL.md"

THROWAWAY = "_cr_body_test_throwaway_agent_"
SID_A = "aaaa1111-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
SID_B = "bbbb2222-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


# : was a hand-rolled resolve-then-replace copy of
# context-reads.normalize_path — the ordering  proved wrong.
# Use the real function; never re-implement it here.
from _context_reads_helper import norm_path as _norm  # noqa: E402


@contextmanager
def _agent(fork_sids=()):
    """Create the throwaway agent under the real PROJECT_ROOT/agents/.

    For each sid in fork_sids, fork a Body by creating
    sessions/<sid>/working-memory.yaml (the activation signal). Yields (env, adir).
    Always torn down -- only ever removes the distinctive throwaway dir.
    """
    adir = PROJECT_ROOT / "agents" / THROWAWAY
    (adir / "session").mkdir(parents=True, exist_ok=True)
    # newline="" disables CRLF translation on Windows (guard-1688) — this conf
    # is sourced by _paths.sh, which would otherwise read a trailing \r.
    (adir / "local-paths.conf").write_text(
        "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline=""
    )
    for sid in fork_sids:
        bsd = adir / "sessions" / sid
        bsd.mkdir(parents=True, exist_ok=True)
        (bsd / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    try:
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY
        yield env, adir
    finally:
        if adir.name == THROWAWAY and adir.is_dir():
            shutil.rmtree(adir, ignore_errors=True)


def _record(env, sid, file_path):
    # sys.executable is the real (pytest) python -> avoids the Windows MS-Store
    # python stub the hook wrappers defend against.
    return subprocess.run(
        [sys.executable, str(CR_PY), "record", "--session-id", sid, str(file_path)],
        capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT), timeout=30)


def test_record_without_fork_writes_agent_wide():
    with _agent() as (env, adir):  # no Body forked
        r = _record(env, SID_A, IN_SCOPE_FILE)
        assert r.returncode == 0, r.stderr
        agent_wide = adir / "session" / "context-reads.txt"
        body = adir / "sessions" / SID_A / "body-context-reads.txt"
        assert agent_wide.is_file(), "agent-wide tracker expected without a forked Body"
        assert _norm(IN_SCOPE_FILE) in agent_wide.read_text(encoding="utf-8")
        assert not body.exists(), "no body tracker without a forked WM file"


def test_record_with_fork_writes_body_tracker():
    with _agent(fork_sids=[SID_A]) as (env, adir):
        r = _record(env, SID_A, IN_SCOPE_FILE)
        assert r.returncode == 0, r.stderr
        body = adir / "sessions" / SID_A / "body-context-reads.txt"
        agent_wide = adir / "session" / "context-reads.txt"
        assert body.is_file(), f"forked Body must route to body tracker; stderr={r.stderr}"
        assert _norm(IN_SCOPE_FILE) in body.read_text(encoding="utf-8")
        assert not agent_wide.exists(), "forked Body must NOT touch the agent-wide tracker"


def test_two_forked_bodies_do_not_cross_contaminate():
    # The regression this whole move fixes: with the agent-wide singleton, the
    # second Body's record would DELETE the first's tracker (session-header
    # mismatch). Per-Body trackers keep each Body's dedup state isolated.
    with _agent(fork_sids=[SID_A, SID_B]) as (env, adir):
        assert _record(env, SID_A, IN_SCOPE_FILE).returncode == 0
        assert _record(env, SID_B, IN_SCOPE_FILE).returncode == 0
        ta = adir / "sessions" / SID_A / "body-context-reads.txt"
        tb = adir / "sessions" / SID_B / "body-context-reads.txt"
        assert ta.is_file() and tb.is_file(), "each Body keeps its own tracker"
        # Each carries its OWN session header -> neither clobbered the other.
        assert f"#session:{SID_A}" in ta.read_text(encoding="utf-8")
        assert f"#session:{SID_B}" in tb.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# : `clear` must route on --session-id like every other subcommand.
#
# It did not. cmd_clear called tracker_path(None) unconditionally, so on a
# forked Body it unlinked the agent-wide singleton -- a file the Body's gates
# never read -- exited 0, and left the stale per-Body tracker in place. That
# matters because the clear is now wired into sessionstart-orchestrator.sh's
# source=compact block: autocompact KEEPS the SID, so the #session header
# mismatch (the tracker's only other reset) never fires, and without a
# correctly-routed clear the manifest keeps asserting "already in context"
# about content the compaction evicted.
#
# Measured on a live worker Body before the fix: agent-wide tracker EMPTY,
# per-Body tracker 49 full + 152 partial. The old clear had nothing to delete
# and looked entirely successful doing it.
# ---------------------------------------------------------------------------

def _clear(env, sid=None):
    cmd = [sys.executable, str(CR_PY), "clear"]
    if sid is not None:
        cmd += ["--session-id", sid]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=30)


# NOTE (2026-08-22): the two clear-routing tests that stood here --
# `test_clear_with_session_id_clears_the_body_tracker` and
# `test_clear_without_session_id_stays_agent_wide` -- were removed as exact
# duplicates of `test_session_scoped_clear_removes_the_body_tracker` and
# `test_bare_clear_does_NOT_touch_a_body_tracker` in
# test_context_reads_clear_wiring.py, which landed independently for the same
# goal and covers the clear path more thoroughly (peer-Body isolation, reducer
# SID resolution, the wrapper's "$@" forwarding, the precompact call site).
# What survives HERE is what that file does NOT cover: the RECORD (write) path
# above, and the agent-resolution requirement below.


def test_clear_reports_which_tracker_on_both_branches():
    # guard-4157: this runs from a hook whose only durable record is its stdout,
    # so a silent no-op and a silent success must not be the same output.
    with _agent(fork_sids=[SID_A]) as (env, adir):
        assert _record(env, SID_A, IN_SCOPE_FILE).returncode == 0

        cleared = _clear(env, SID_A)
        assert cleared.returncode == 0, cleared.stderr
        assert "cleared" in cleared.stdout, (
            f"cleared branch must say so; got {cleared.stdout!r}")
        assert SID_A in cleared.stdout, "cleared branch must name the scope"

        again = _clear(env, SID_A)
        assert again.returncode == 0, again.stderr
        assert "no tracker to clear" in again.stdout, (
            f"already-absent branch must say so; got {again.stdout!r}")
        assert cleared.stdout != again.stdout, (
            "the two branches must be distinguishable from stdout alone")


def test_orchestrator_compact_branch_supplies_an_AGENT_to_the_clear():
    # NOT a duplicate of test_sessionstart_clear_{is_INSIDE_the_source_compact_gate,
    # passes_a_session_id} in test_context_reads_clear_wiring.py. Those two pin
    # the gate placement and --session-id; NEITHER pins that an AGENT reaches the
    # clear, and --session-id ALONE IS INERT without one.
    #
    # A SessionStart hook gets NO MIND_AGENT (bash-agent-inject.py prepends the
    # binding env to Bash-TOOL commands only), and context-reads.py derives
    # SESSION_DIR/AGENT_NAME from exactly that var -> tracker_path() is None and
    # the clear no-ops on EVERY compaction, printing a cheerful rc=0. Measured
    # live on cc-07 2026-08-22, AFTER the --session-id wiring was already
    # committed and its own tests were green -- which is precisely why this
    # assertion has to exist separately from theirs.
    #
    # Pins the BEHAVIOUR (an agent is resolved and exported), never the resolver's
    # FILENAME: two implementations of this resolution have already shipped
    # (session-binding-read.sh and _resolve_agent_from_sid.py) and a name-pinned
    # test would be a rename-detector rather than a defect-detector.
    orch = (CORE_SCRIPTS / "sessionstart-orchestrator.sh").read_text(encoding="utf-8")
    assert "context-reads-clear.sh" in orch, (
        "sessionstart-orchestrator.sh must invoke the clear wrapper")
    guard = orch.index('if [ "$SOURCE" = "compact" ]')
    clear_at = orch.index("context-reads-clear.sh")
    assert clear_at > guard, (
        "the clear must sit INSIDE the source=compact branch -- clearing on every "
        "SessionStart would discard a fresh session's legitimate dedup state")
    # Bound the slice at the block's closing `fi`, NOT at end-of-file: an
    # `orch[guard:]` slice is the rest of the FILE, so an MIND_AGENT= added
    # anywhere below the block would satisfy the assertion without the clear
    # ever seeing an agent -- green-by-default, the exact shape guard-2903 warns
    # about and the one this test exists to detect.
    block = orch[guard:orch.index("\nfi\n", guard)]
    assert "context-reads-clear.sh" in block, (
        "the clear escaped the source=compact block")
    assert "--session-id" in block, (
        "the orchestrator must pass --session-id through, else the clear resolves "
        "the agent-wide tracker and is inert on every worker Body")
    agent_at = block.find("MIND_AGENT=")
    assert agent_at != -1, (
        "the compact branch must resolve the agent and export it to the clear -- "
        "passing --session-id alone is inert in a hook with no MIND_AGENT, which "
        "is the exact no-op this whole goal exists to remove")
    assert agent_at < block.index("context-reads-clear.sh"), (
        "MIND_AGENT must be set BEFORE/ON the clear invocation, not after it")


def test_clear_cannot_resolve_a_tracker_without_an_agent_in_env():
    # The negative control for the orchestrator assertion above: this is the
    # EXACT env shape a SessionStart hook runs under, and it is why the binding
    # lookup exists. Without it the clear reports "no tracker path resolved" and
    # exits 0 -- indistinguishable from success to anything but a reader.
    with _agent(fork_sids=[SID_A]) as (env, adir):
        assert _record(env, SID_A, IN_SCOPE_FILE).returncode == 0
        body = adir / "sessions" / SID_A / "body-context-reads.txt"
        assert body.is_file(), "precondition: record must create the body tracker"

        hook_env = {k: v for k, v in env.items() if k != "MIND_AGENT"}
        r = _clear(hook_env, SID_A)
        assert r.returncode == 0, r.stderr
        assert body.is_file(), (
            "agentless clear must NOT delete anything -- if this ever starts "
            "clearing, it resolved SOME agent's tracker without being told which")
        # guard-4157: the agentless branch must be DISTINGUISHABLE from a real
        # clear on stdout alone -- a hook's stdout is its only durable record.
        # `tp` is None when nothing resolved, which is exactly the tell.
        assert "no tracker to clear" in r.stdout and "None" in r.stdout, (
            f"the agentless branch must SAY it resolved nothing; got {r.stdout!r}")

        # ...and with the agent supplied (what the orchestrator now does) it works.
        assert _clear(env, SID_A).returncode == 0
        assert not body.exists(), "agent-supplied clear must delete the body tracker"


# ---------------------------------------------------------------------------
# : `invalidate` must route on --session-id like every other subcommand.
#
# It did not. cmd_invalidate called remove_from_tracker(normalized) with no
# session_id, and its subparser defined no --session-id at all -- it was the only
# subcommand in that group without one. So invalidation ALWAYS targeted the
# agent-wide session/context-reads.txt while READS were routed per-Body (Phase 1D
# above). In a forked Body, editing a tracked file therefore did NOT clear it: the
# stale entry survived the edit, `gate` kept BLOCKING re-reads of a file that HAD
# CHANGED, and `check-file` stayed silent for it. That is the false-all-clear
# direction -- invalidation exists precisely so a mid-session edit re-arms the
# advisory.
#
# Structurally identical to the `clear` defect above (): the same
# tracker_path(None) shape, one subcommand over. Latent until a 2nd Body forks
# (with one Body the routing collapses), and silent when it arms.
#
# TARGET CHOICE IS LOAD-BEARING: these use a TRACKED_FILES path
# (aspirations-compact.json), not the in-scope SKILL.md the record tests use.
# cmd_invalidate acts ONLY on TRACKED_FILES and tree nodes, so invalidating
# SKILL.md returns 0 without touching anything -- a test built on it would pass
# while exercising nothing.
# ---------------------------------------------------------------------------

def _tracked_target(adir):
    """A TRACKED_FILES path that cmd_invalidate actually acts on."""
    p = adir / "session" / "aspirations-compact.json"
    p.write_text("{}\n", encoding="utf-8")
    return p


def _invalidate(env, file_path, sid=None):
    cmd = [sys.executable, str(CR_PY), "invalidate"]
    if sid is not None:
        cmd += ["--session-id", sid]
    cmd += [str(file_path)]
    return subprocess.run(cmd, capture_output=True, text=True, env=env,
                          cwd=str(PROJECT_ROOT), timeout=30)


def test_invalidate_with_session_id_clears_the_body_tracker():
    with _agent(fork_sids=[SID_A]) as (env, adir):
        target = _tracked_target(adir)
        assert _record(env, SID_A, target).returncode == 0
        body = adir / "sessions" / SID_A / "body-context-reads.txt"
        assert _norm(target) in body.read_text(encoding="utf-8"), (
            "precondition: the record must land in the BODY tracker")

        r = _invalidate(env, target, SID_A)
        assert r.returncode == 0, r.stderr
        assert _norm(target) not in body.read_text(encoding="utf-8"), (
            "a session-scoped invalidate must clear the BODY tracker")


def test_invalidate_without_session_id_leaves_the_body_tracker_stale():
    # The defect's exact shape, pinned so the flag cannot be quietly dropped
    # again. With no --session-id the invalidation resolves to the agent-wide
    # file -- which a forked Body's gate never reads -- and exits 0 looking
    # entirely successful. That asymmetry is why the flag is load-bearing.
    with _agent(fork_sids=[SID_A]) as (env, adir):
        target = _tracked_target(adir)
        assert _record(env, SID_A, target).returncode == 0
        body = adir / "sessions" / SID_A / "body-context-reads.txt"

        r = _invalidate(env, target)          # no --session-id
        assert r.returncode == 0, r.stderr
        assert _norm(target) in body.read_text(encoding="utf-8"), (
            "an unrouted invalidate must not reach a Body tracker; if this ever "
            "fails, routing has silently become unconditional")


def test_invalidate_without_a_fork_still_clears_agent_wide():
    # The collapsed-routing path (one Body), pinned so the fix cannot regress the
    # single-Body case -- which is every session until a 2nd Body forks, i.e. the
    # only case anyone would notice breaking.
    with _agent() as (env, adir):
        target = _tracked_target(adir)
        assert _record(env, SID_A, target).returncode == 0
        agent_wide = adir / "session" / "context-reads.txt"
        assert _norm(target) in agent_wide.read_text(encoding="utf-8"), "precondition"

        assert _invalidate(env, target, SID_A).returncode == 0
        assert _norm(target) not in agent_wide.read_text(encoding="utf-8"), (
            "with no forked Body, --session-id must collapse to the agent-wide tracker")


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
