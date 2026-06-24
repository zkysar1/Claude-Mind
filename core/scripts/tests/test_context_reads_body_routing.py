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


def _norm(p) -> str:
    """Match context-reads.py normalize_path: resolve + forward slashes."""
    return str(Path(p).resolve()).replace("\\", "/")


@contextmanager
def _agent(fork_sids=()):
    """Create the throwaway agent under the real PROJECT_ROOT/agents/.

    For each sid in fork_sids, fork a Body by creating
    sessions/<sid>/working-memory.yaml (the activation signal). Yields (env, adir).
    Always torn down -- only ever removes the distinctive throwaway dir.
    """
    adir = PROJECT_ROOT / "agents" / THROWAWAY
    (adir / "session").mkdir(parents=True, exist_ok=True)
    (adir / "local-paths.conf").write_text("WORLD_PATH=\nMETA_PATH=\n")
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


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
