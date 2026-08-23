""": the context-reads clear must land on the tracker the session USES.

WHAT WAS ACTUALLY BROKEN, because the goal that filed this got it half wrong and
the correction is the whole lesson. The filing evidence said "No caller of
context-reads.py clear / context-reads-clear.sh exists anywhere in core/ or
.claude/" — a grep keyed on the WRAPPER name, run against the .sh files. But a
clear DID exist and DID run: `precompact-checkpoint.py` carried a hand-rolled
inline `unlink()` of AGENT_DIR/session/context-reads.txt, a second implementation
of `cmd_clear` that no wrapper-name grep could see. The caller finding was right;
the behaviour finding was wrong.

The real defect was one path. That unlink (and `cmd_clear` itself) targeted the
AGENT-WIDE tracker, while a forked worker Body's tracker is
sessions/<SID>/body-context-reads.txt. Measured 2026-08-22 on cc-08 (alpha worker
Body, uname -r 6.8.0-137-generic, own-cloud): PreCompact ran AND completed — it
wrote a body-keyed compact-checkpoint.yaml at 12:55 — while the Body's
body-context-reads.txt still held 135 entries at mtime 11:25, untouched. There
was no agent-wide tracker for ANY agent on the box, so the clear succeeded
against nothing on every live manifest there.

The consequence is not cosmetic: with the stale manifest in place, the
PreToolUse[Skill] gate refused `/reflect` with "already in context — follow them
from earlier in this conversation", pointing at content the compaction had just
evicted. Cleared, the same invocation returned rc=0.

WHAT THESE TESTS PIN, and the split is deliberate:

  BEHAVIOURAL (hermetic, real scripts, canonical invocation shape) — the clear's
  routing semantics. This is where the defect lived and where a regression would
  land, so it gets real execution against real trackers.

  STATIC — that both hook call sites exist, pass --session-id, and that the
  SessionStart one sits INSIDE the source=compact gate. Source-gating is a
  structural property of the file (guard-404: compact != startup != resume) and
  is correctly pinned by reading the file. It is NOT pinned by running the
  orchestrator: that would spawn the runtime daemon via mind-api-start.sh and
  hijack the live daemon.port out from under the running fleet — the exact
  daemon-storm hazard run-full-suite-after-deep-code.md documents. The
  orchestrator-level end-to-end (source=startup leaves the tracker at 140 lines;
  source=compact removes it; a peer Body's tracker untouched) was verified BY
  HAND on the live session, not here. Saying which half is automated matters:
  a static pin cannot catch the g-115-7146 failure mode where every unit test
  passes against an inert mechanism.

Hermetic: every test builds a throwaway agent dir under the real PROJECT_ROOT and
removes it, so the live session manifest is never touched.

Run: py -3 -m pytest core/scripts/tests/test_context_reads_clear_wiring.py -v
"""
import os
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPT_DIR = TESTS_DIR.parent            # core/scripts
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # repo root

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402

THROWAWAY_AGENT = "_clear_wiring_test_throwaway_agent_"
BODY_SID = "clear-wiring-body-sid-001"
PEER_SID = "clear-wiring-peer-sid-002"

ORCHESTRATOR = SCRIPT_DIR / "sessionstart-orchestrator.sh"
PRECOMPACT_SH = SCRIPT_DIR / "precompact-checkpoint.sh"
PRECOMPACT_PY = SCRIPT_DIR / "precompact-checkpoint.py"
CLEAR_SH = SCRIPT_DIR / "context-reads-clear.sh"


@contextmanager
def _throwaway_agent(*, body_sids=(), agent_wide=True):
    """Throwaway agent with an agent-wide tracker and zero or more Body trackers.

    A Body is discriminated by the presence of sessions/<sid>/working-memory.yaml
    — that is exactly what context_reads.tracker_path() tests, so the fixture has
    to create the fork WM file, not merely the directory.
    """
    agent_dir = PROJECT_ROOT / "agents" / THROWAWAY_AGENT
    try:
        (agent_dir / "session").mkdir(parents=True, exist_ok=True)
        (agent_dir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline="")
        if agent_wide:
            _seed(agent_dir / "session" / "context-reads.txt", "agent-wide")
        for sid in body_sids:
            bd = agent_dir / "sessions" / sid
            bd.mkdir(parents=True, exist_ok=True)
            (bd / "working-memory.yaml").write_text("slots: {}\n",
                                                    encoding="utf-8", newline="")
            _seed(bd / "body-context-reads.txt", sid)
        env = dict(os.environ)
        env["MIND_AGENT"] = THROWAWAY_AGENT
        yield env, agent_dir
    finally:
        if agent_dir.name == THROWAWAY_AGENT and agent_dir.is_dir():
            shutil.rmtree(agent_dir, ignore_errors=True)


def _seed(path, session_id):
    path.write_text(
        f"#session:{session_id}\n"
        f"{PROJECT_ROOT}/core/config/aspirations.yaml\n",
        encoding="utf-8", newline="")


def _clear(env, *args, timeout=60):
    r = subprocess.run([BASH, "core/scripts/context-reads-clear.sh", *args],
                       capture_output=True, text=True, env=env,
                       timeout=timeout, cwd=str(PROJECT_ROOT))
    return r.returncode, r.stdout, r.stderr


def _agent_wide(agent_dir):
    return agent_dir / "session" / "context-reads.txt"


def _body(agent_dir, sid):
    return agent_dir / "sessions" / sid / "body-context-reads.txt"


# ---------------------------------------------------------------------------
# Behavioural: routing
# ---------------------------------------------------------------------------

def test_session_scoped_clear_removes_the_body_tracker():
    """The defect, inverted. This is the assertion that was false before the fix."""
    with _throwaway_agent(body_sids=[BODY_SID]) as (env, ad):
        assert _body(ad, BODY_SID).exists()
        rc, out, _ = _clear(env, "--session-id", BODY_SID)
        assert rc == 0, out
        assert not _body(ad, BODY_SID).exists(), \
            "--session-id must clear the tracker the Body actually writes to"


def test_bare_clear_does_NOT_touch_a_body_tracker():
    """--session-id is load-bearing, not decorative.

    This is the mutant that must stay killed: drop the flag at either call site
    and the clear silently reverts to the agent-wide no-op that shipped the bug.
    """
    with _throwaway_agent(body_sids=[BODY_SID]) as (env, ad):
        rc, out, _ = _clear(env)
        assert rc == 0, out
        assert _body(ad, BODY_SID).exists(), \
            "a bare clear must leave a Body's tracker alone — it is not that session's"
        assert not _agent_wide(ad).exists(), "a bare clear still clears agent-wide"


def test_session_scoped_clear_leaves_the_agent_wide_tracker():
    """Scoping cuts both ways: a Body must not wipe the reducer's manifest."""
    with _throwaway_agent(body_sids=[BODY_SID]) as (env, ad):
        _clear(env, "--session-id", BODY_SID)
        assert _agent_wide(ad).exists(), \
            "clearing a Body's tracker must not touch the agent-wide one (guard-404)"


def test_one_body_clear_never_reaches_a_PEER_body(_=None):
    """guard-404, the cross-session half.

    Two Bodies of one agent co-reside on a box. If a compaction in one wiped the
    other's manifest, the peer would re-read everything it had legitimately
    loaded — a shared-state mutation crossing a session boundary.
    """
    with _throwaway_agent(body_sids=[BODY_SID, PEER_SID]) as (env, ad):
        _clear(env, "--session-id", BODY_SID)
        assert not _body(ad, BODY_SID).exists()
        assert _body(ad, PEER_SID).exists(), \
            "clearing one Body's tracker must never reach a peer Body's"


def test_reducer_session_id_resolves_to_the_agent_wide_tracker():
    """A SID with no forked WM is a reducer/observer: --session-id must still work.

    tracker_path() discriminates on the fork-WM file, so passing --session-id on a
    reducer is correct and lands agent-wide. Without this the fix would only help
    workers and would silently no-op on the reducer that PreCompact was written for.
    """
    with _throwaway_agent(body_sids=[]) as (env, ad):
        rc, out, _ = _clear(env, "--session-id", "a-sid-with-no-forked-wm")
        assert rc == 0, out
        assert not _agent_wide(ad).exists()


def test_clear_on_an_absent_tracker_is_a_quiet_success():
    """Idempotent. Both hook call sites are fail-open; a second clear must not error."""
    with _throwaway_agent(body_sids=[BODY_SID]) as (env, ad):
        assert _clear(env, "--session-id", BODY_SID)[0] == 0
        rc, out, _ = _clear(env, "--session-id", BODY_SID)
        assert rc == 0, out
        assert "no tracker to clear" in out


def test_the_wrapper_forwards_arguments():
    """context-reads-clear.sh execs with "$@". Without it --session-id never arrives.

    Pinned behaviourally rather than by grepping for "$@": a wrapper can forward
    and still route wrongly, and the observable difference is which file survives.
    """
    with _throwaway_agent(body_sids=[BODY_SID]) as (env, ad):
        _clear(env, "--session-id", BODY_SID)
        assert not _body(ad, BODY_SID).exists(), \
            "the flag did not survive the wrapper"


# ---------------------------------------------------------------------------
# Static: the two hook call sites
# ---------------------------------------------------------------------------

def _compact_block(text):
    """The body of `if [ "$SOURCE" = "compact" ]; then ... fi` in the orchestrator."""
    marker = 'if [ "$SOURCE" = "compact" ]; then'
    assert marker in text, "the source=compact gate itself is gone — read the file"
    after = text.split(marker, 1)[1]
    end = after.index("\nfi\n")
    return after[:end]


def test_sessionstart_clear_is_INSIDE_the_source_compact_gate():
    """guard-404. A clear reachable from source=startup would wipe a live manifest
    whose content genuinely IS in context — strictly worse than the bug it fixes."""
    text = ORCHESTRATOR.read_text(encoding="utf-8")
    assert "context-reads-clear.sh" in text, \
        "SessionStart no longer clears the manifest after a compaction"
    assert "context-reads-clear.sh" in _compact_block(text), \
        "the clear escaped the source=compact gate (guard-404)"


def test_sessionstart_clear_passes_a_session_id():
    assert "--session-id" in _compact_block(
        ORCHESTRATOR.read_text(encoding="utf-8")), \
        "without --session-id the SessionStart clear is the agent-wide no-op again"


def test_precompact_wrapper_clears_with_a_session_id():
    """The pre-hoc belt. It lives in the .sh, which already resolves $SID."""
    text = PRECOMPACT_SH.read_text(encoding="utf-8")
    assert "context-reads-clear.sh" in text
    line = next(l for l in text.splitlines() if "context-reads-clear.sh" in l)
    assert "--session-id" in line, line


def test_precompact_py_no_longer_hand_rolls_its_own_unlink():
    """The removed duplicate.

    A grep for the wrapper name could not see this implementation, which is why
    the defect survived a correct-looking audit. One implementation, two callers:
    a third copy is how the first one drifted out of correctness unnoticed.
    """
    text = PRECOMPACT_PY.read_text(encoding="utf-8")
    code = [l for l in text.splitlines()
            if "context" in l and "unlink()" in l and not l.strip().startswith("#")]
    assert not code, f"inline tracker unlink is back in precompact-checkpoint.py: {code}"


if __name__ == "__main__":
    sys.exit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-v"]))
