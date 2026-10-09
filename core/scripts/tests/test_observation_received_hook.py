"""test_observation_received_hook.py — the vessel's early wake on a world change ().

Zak-Code's POST /observe runs core/scripts/observation-received-hook.sh once per accepted
CHANGE frame, the lifecycle payload on stdin, and reports the hook's exit code as that
frame's wake disposition: 0 delivered, anything else dropped. The hook raises the agent's
perception-received signal, which breaks a sleeping autonomous loop at once. The hook is
declared in the tracked .zakcode/settings.json, which Claude Code never reads.

Subprocess tests drive the real hook and the real session scripts under a throwaway agent
beneath the real PROJECT_ROOT, torn down in finally (the pattern of
test_context_reads_session_routing.py). Every case scrubs MIND_AGENT, MIND_AGENT and
MIND_SID, so the launching shell never chooses the agent (guard-1515: the env is an input).
Pure file routing: no daemon, no world store.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_observation_received_hook.py -q
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

TESTS_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = TESTS_DIR.parent
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
HOOK = CORE_SCRIPTS / "observation-received-hook.sh"
SETTINGS = PROJECT_ROOT / ".zakcode" / "settings.json"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
from _bash_helpers import BASH  # noqa: E402
from _frontier_world import requires_frontier_world  # noqa: E402

# Per process, so parallel workers (pytest-xdist) never share, or tear down, one agent dir.
THROWAWAY = f"_orh_test_agent_{os.getpid()}_"
SID = "d4d4d4d4-0000-4000-8000-0000000000d4"
UNBOUND_SID = "e5e5e5e5-0000-4000-8000-0000000000e5"


@contextmanager
def _agent(mode: str | None = "autonomous"):
    """Throwaway agent bound to SID, in `mode` (None = no mode file, which reads as
    reader). Yields its dir."""
    adir = PROJECT_ROOT / "agents" / THROWAWAY
    try:
        (adir / "session").mkdir(parents=True, exist_ok=True)
        # newline="" (guard-1688): _paths.sh sources this conf. The binding resolver
        # refuses an agent dir without one.
        (adir / "local-paths.conf").write_text(
            "WORLD_PATH=\nMETA_PATH=\n", encoding="utf-8", newline="")
        sd = adir / "sessions" / SID
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "binding.yaml").write_text(
            f"session_id: {SID}\nagent: {THROWAWAY}\nmode: autonomous\n"
            "started_at: '2026-01-01T00:00:00'\nstarted_by: test\n",
            encoding="utf-8", newline="")
        if mode is not None:
            (adir / "session" / "agent-mode").write_text(mode + "\n", encoding="utf-8")
        yield adir
    finally:
        if adir.name == THROWAWAY and adir.is_dir():
            shutil.rmtree(adir, ignore_errors=True)


def _marker(adir: Path) -> Path:
    return adir / "session" / "perception-received"


def _run_hook(session_id: str | None = SID, *, agent_env: str | None = None,
              stdin: str | None = None) -> int:
    """Run the hook the way the runtime does: payload on stdin, cwd = workspace root."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MIND_AGENT", "MIND_AGENT", "MIND_SID", "MIND_AGENT_DIR")}
    env["STORAGE_BACKEND"] = "local"   # guard-955
    if agent_env is not None:
        env["MIND_AGENT"] = agent_env
    if stdin is None:
        stdin = json.dumps({
            "hook_event_name": "ObservationReceived",
            "session_id": session_id,
            "cwd": str(PROJECT_ROOT),
            "data": {"kind": "change", "observation_path": str(PROJECT_ROOT / "frame.json")},
        })
    proc = subprocess.run([BASH, str(HOOK)], input=stdin, text=True, capture_output=True,
                          env=env, cwd=str(PROJECT_ROOT), timeout=60)
    return proc.returncode


def test_an_autonomous_agent_bound_to_the_session_is_woken():
    # The vessel's shape: nothing in the environment names the agent, the binding does.
    with _agent("autonomous") as adir:
        assert _run_hook(SID) == 0
        assert _marker(adir).is_file()


def test_the_environment_names_the_agent_when_the_payload_carries_no_session():
    # Before the run has a session, the payload's session_id is "".
    with _agent("autonomous") as adir:
        assert _run_hook("", agent_env=THROWAWAY) == 0
        assert _marker(adir).is_file()


@pytest.mark.parametrize("mode", ["assistant", "reader", None])
def test_a_loop_free_mode_is_delivered_without_a_marker(mode):
    # reader and assistant run no loop, so nothing would read the marker (guard-1806).
    # That is a healthy answer, so it reads as delivered, not dropped.
    with _agent(mode) as adir:
        assert _run_hook(SID) == 0
        assert not _marker(adir).exists()


def test_an_unknown_mode_is_dropped():
    # Not autonomous, so no wake; but a corrupt mode is a fault the runtime should count.
    with _agent("autonomus") as adir:
        assert _run_hook(SID) == 1
        assert not _marker(adir).exists()


@pytest.mark.parametrize("session_id", [UNBOUND_SID, ""])
def test_no_agent_bound_is_delivered_and_wakes_nobody(session_id):
    # NO_AGENT: /start has not run in this session, so no loop of ours is sleeping. The
    # throwaway agent is autonomous, and its binding is for another session: it must
    # not be woken by a frame that belongs to none.
    with _agent("autonomous") as adir:
        assert _run_hook(session_id) == 0
        assert not _marker(adir).exists()


def test_an_unreadable_payload_still_uses_the_environment():
    with _agent("autonomous") as adir:
        assert _run_hook(stdin="not json {", agent_env=THROWAWAY) == 0
        assert _marker(adir).is_file()
        _marker(adir).unlink()
        assert _run_hook(stdin="not json {") == 0   # and with no agent, nothing to wake
        assert not _marker(adir).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlink creation needs privileges on Windows")
def test_a_write_that_fails_is_dropped(tmp_path):
    # A marker path the writer cannot create: a dangling symlink into a missing dir.
    # session.py's touch() raises, so the setter exits non-zero and the hook says dropped.
    with _agent("autonomous") as adir:
        _marker(adir).symlink_to(tmp_path / "missing-dir" / "perception-received")
        assert _run_hook(SID) == 1
        assert not _marker(adir).exists()   # still dangling: nothing was written


def test_the_write_refreshes_a_marker_already_there():
    # A sleep consumes a marker older than its start as stale, without waking
    # (interruptible-sleep.sh, ). So a frame arriving while an old marker sits
    # there must refresh it, not skip the write because a marker exists.
    with _agent("autonomous") as adir:
        _marker(adir).touch()
        hour_ago = time.time() - 3600
        os.utime(_marker(adir), (hour_ago, hour_ago))
        started = time.time()
        assert _run_hook(SID) == 0
        assert _marker(adir).stat().st_mtime >= started - 1


def test_a_loop_that_consumes_the_marker_at_once_still_reads_delivered():
    # A sleeping loop deletes the marker as it wakes. Here a consumer deletes it the
    # moment it appears, far faster than the loop's one-second poll. A hook that read the
    # marker back after writing it would find it gone and report the wake it had just
    # delivered as dropped; the writer's exit status cannot be raced.
    with _agent("autonomous") as adir:
        marker = _marker(adir)
        consumed = []
        stop = threading.Event()

        def consume():
            while not stop.is_set():
                try:
                    marker.unlink()
                    consumed.append(True)
                except OSError:   # absent, or (Windows) still open in the writer
                    pass

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        try:
            rc = _run_hook(SID)
        finally:
            stop.set()
            consumer.join(timeout=10)
        assert consumed, "the consumer never saw the marker, so the hook never wrote it"
        assert rc == 0


def test_the_tracked_settings_declare_this_hook():
    # The runtime reads .zakcode/settings.json; Claude Code never does, so a Claude Code
    # session in this workspace never meets an event it does not know.
    hooks = json.loads(SETTINGS.read_text(encoding="utf-8"))["hooks"]
    assert list(hooks) == ["ObservationReceived"]
    commands = [h["command"] for entry in hooks["ObservationReceived"] for h in entry["hooks"]]
    assert commands == ["bash $CLAUDE_PROJECT_DIR/core/scripts/observation-received-hook.sh"]
    assert HOOK.is_file()


@requires_frontier_world(
    "asserts the checkout's own .gitignore, which promotion-preflight lists as deployment-local")
def test_git_tracks_the_settings_and_ignores_the_runtime_files():
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not installed")

    def ignored(rel: str) -> bool:
        proc = subprocess.run([git, "-C", str(PROJECT_ROOT), "check-ignore", "--no-index", "-q", rel],
                              capture_output=True, timeout=30)
        assert proc.returncode in (0, 1), proc.stderr
        return proc.returncode == 0

    assert not ignored(".zakcode/settings.json")
    # The runtime's own files under the same dir: sessions, transcripts, markers.
    assert ignored(".zakcode/sessions/s.json")
    assert ignored(".zakcode/transcripts/t.jsonl")
    assert ignored(".zakcode/.current-session")


def test_the_seed_and_the_promotion_carry_the_settings():
    # Without these a vessel's workspace never gets the declaration, and its early wake
    # silently stops the day the runtime's built-in wake is retired.
    manifest = yaml.safe_load((PROJECT_ROOT / "core" / "config" / "seed-manifest.yaml")
                              .read_text(encoding="utf-8"))
    assert ".zakcode/settings.json" in [e.get("path") for e in manifest["include"]]
    # promotion-preflight.py by path (its name is not importable), as the manifest
    # parity test loads it. FRAMEWORK_PATHS is also the pull-adoption copy set.
    spec = importlib.util.spec_from_file_location(
        "promotion_preflight", CORE_SCRIPTS / "promotion-preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    assert ".zakcode/settings.json" in preflight.FRAMEWORK_PATHS
