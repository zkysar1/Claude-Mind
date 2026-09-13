""": /start must not delete a stop-requested the sidecar just raised.

/start Step 2.5 clears `stop-requested` unconditionally, on the premise that
"state is already IDLE, so no loop polling could be interrupted". That held while
/stop was the only writer. stop-hook-compliance.md now authorizes four
programmatic writers, and the newest — the vessel sidecar — raises the signal
from outside the session when a served run hits its duration cap. On a vessel
that raise can land while /start is still onboarding, and the unlink would then
delete the run's only ending.

Two properties carry the fix and every test pins one:
  REFUSE  a signal raised after this session started, with `stop-loop` absent,
          is never deleted;
  PERMIT  every legitimate clear still works — graceful-stop's D2/D3 ordering,
          /start's genuine stale-signal hygiene, --force, and other signals.

guard-4166: a guard that refuses EVERYTHING satisfies every REFUSE assertion and
a guard that refuses NOTHING satisfies every PERMIT assertion, so the two classes
are pinned in the same file and the REFUSE cases flip exactly one input against a
PERMIT control.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1]          # core/scripts
ROOT = Path(__file__).resolve().parents[3]          # project root (CORE is core/scripts)
sys.path.insert(0, str(CORE))

_spec = importlib.util.spec_from_file_location("session_mod", CORE / "session.py")
session_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(session_mod)

decide = session_mod.live_stop_decision
CLEAR = session_mod.CLEAR
REFUSE = session_mod.REFUSE
CLEAR_UNDETERMINED = session_mod.CLEAR_UNDETERMINED

T0 = 1_000_000.0          # session started_at
LATER = T0 + 60.0         # raised during this session  -> live
EARLIER = T0 - 60.0       # left over from a prior session -> stale


# ---------------------------------------------------------------- REFUSE ------

def test_live_unhandled_stop_is_refused():
    """The defect this fix exists to prevent: sidecar raised, nobody handled it."""
    assert decide(signal_exists=True, stop_loop_exists=False,
                  signal_mtime=LATER, started_at=T0, force=False) == REFUSE


def test_refusal_survives_a_mtime_only_one_second_past_start():
    """No grace window: 'after the session started' is the whole predicate."""
    assert decide(signal_exists=True, stop_loop_exists=False,
                  signal_mtime=T0 + 1, started_at=T0, force=False) == REFUSE


# ---------------------------------------------------------------- PERMIT ------

def test_graceful_stop_d3_is_permitted():
    """aspirations-graceful-stop sets stop-loop at D2, then clears at D3.

    The ordering is what makes the legitimate clear distinguishable from the
    destructive one, so this is the single most important PERMIT case: if it
    ever fails, /stop is broken.
    """
    assert decide(signal_exists=True, stop_loop_exists=True,
                  signal_mtime=LATER, started_at=T0, force=False) == CLEAR


def test_stale_signal_from_a_prior_session_is_permitted():
    """/start Step 2.5's genuine purpose — a partial /stop left this behind."""
    assert decide(signal_exists=True, stop_loop_exists=False,
                  signal_mtime=EARLIER, started_at=T0, force=False) == CLEAR


def test_absent_signal_is_permitted():
    assert decide(signal_exists=False, stop_loop_exists=False,
                  signal_mtime=None, started_at=T0, force=False) == CLEAR


def test_force_overrides_a_live_stop():
    assert decide(signal_exists=True, stop_loop_exists=False,
                  signal_mtime=LATER, started_at=T0, force=True) == CLEAR


# ------------------------------------------------- UNDETERMINED != all-clear --

def test_unknown_session_start_clears_but_names_itself():
    """Fails toward today's behaviour, but never silently.

    The verdict is its OWN value rather than CLEAR precisely so an
    un-evaluatable check cannot be read as a verdict that the signal was stale
    (guard-6178 shape). Asserting it is distinct from CLEAR is the point.
    """
    verdict = decide(signal_exists=True, stop_loop_exists=False,
                     signal_mtime=LATER, started_at=None, force=False)
    assert verdict == CLEAR_UNDETERMINED
    assert verdict != CLEAR


def test_unknown_start_with_stop_loop_set_is_a_plain_clear():
    """stop-loop short-circuits before the start time is ever consulted."""
    assert decide(signal_exists=True, stop_loop_exists=True,
                  signal_mtime=LATER, started_at=None, force=False) == CLEAR


# ------------------------------------------------------------ real wiring -----

@pytest.fixture()
def staged_agent(tmp_path):
    """A real agent dir + binding under the repo, so the CLI path is exercised.

    PROJECT_ROOT is derived from the script's own location and is not
    env-overridable, so a throwaway agent name under agents/ is the only way to
    reach the production invocation shape (a wrapper exec'ing session.py).
    """
    agent = "ztest-g37316-liveguard"
    sid = "00000000-0000-4000-8000-00000000g373"[:36]
    root = ROOT
    sess = root / "agents" / agent / "session"
    bind = root / "agents" / agent / "sessions" / sid
    sess.mkdir(parents=True, exist_ok=True)
    bind.mkdir(parents=True, exist_ok=True)
    src_conf = root / "agents" / "alpha" / "local-paths.conf"
    if src_conf.exists():
        (root / "agents" / agent / "local-paths.conf").write_text(
            src_conf.read_text(encoding="utf-8"), encoding="utf-8")
    (bind / "binding.yaml").write_text(
        f"session_id: {sid}\nagent: {agent}\nmode: autonomous\n"
        "started_at: '2026-09-13T12:00:00'\nstarted_by: pytest\n",
        encoding="utf-8")
    yield agent, sid, sess
    import shutil
    shutil.rmtree(root / "agents" / agent, ignore_errors=True)


def _clear(agent, sid, *extra):
    env = dict(os.environ, MIND_AGENT=agent, MIND_SID=sid)
    return subprocess.run(
        [sys.executable, str(CORE / "session.py"), "signal", "clear", "stop-requested", *extra],
        capture_output=True, text=True, env=env, cwd=str(ROOT))


def test_cli_refuses_a_live_stop_and_leaves_the_file(staged_agent):
    agent, sid, sess = staged_agent
    sig = sess / "stop-requested"
    sig.touch()                      # now == raised long after started_at
    r = _clear(agent, sid)
    assert r.returncode == 1, r.stderr
    assert "REJECTED" in r.stderr
    assert sig.exists(), "the live stop signal must survive a refused clear"


def test_cli_permits_the_graceful_stop_shape(staged_agent):
    agent, sid, sess = staged_agent
    sig = sess / "stop-requested"
    sig.touch()
    (sess / "stop-loop").touch()     # D2 ran before D3, as graceful-stop orders
    r = _clear(agent, sid)
    assert r.returncode == 0, r.stderr
    assert not sig.exists()


def test_cli_force_clears_a_live_stop(staged_agent):
    agent, sid, sess = staged_agent
    sig = sess / "stop-requested"
    sig.touch()
    r = _clear(agent, sid, "--force")
    assert r.returncode == 0, r.stderr
    assert not sig.exists()


def test_cli_leaves_other_signals_ungated(staged_agent):
    """The guard is scoped to stop-requested; loop-active must be unaffected."""
    agent, sid, sess = staged_agent
    (sess / "loop-active").touch()
    env = dict(os.environ, MIND_AGENT=agent, MIND_SID=sid)
    r = subprocess.run(
        [sys.executable, str(CORE / "session.py"), "signal", "clear", "loop-active"],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    assert r.returncode == 0, r.stderr
    assert not (sess / "loop-active").exists()
