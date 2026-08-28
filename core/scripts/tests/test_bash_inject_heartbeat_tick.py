"""bash-agent-inject.py ticks a Body's liveness at TOOL-CALL cadence — .

The hook fires before every Bash call. For a bound session that IS a Body (its
`body-heartbeat-<SID>.json` carrier exists) it spawns heartbeat-tick.sh once per
_shared_tick.SHARED_HEARTBEAT_INTERVAL_S: the FULL tick when the SID is the
running-session-id (on the diary path's own `claim-renewal-last` window), and
`--body-only` for any other Body (on a per-SID stamp under core/logs). A session
with no carrier never ticks and never gets one.

WHAT THESE TESTS PIN (all through the real function, spawn captured):
  1. no carrier -> no spawn (an observer / assistant session is not a Body).
  2. reducer SID + stale window -> FULL tick, and `claim-renewal-last` stamped
     BEFORE the spawn so a slow tick cannot re-fire on the next call.
  3. reducer SID + fresh window -> no spawn (rate limit).
  4. non-reducer SID -> `--body-only`, on its own per-SID stamp, never the
     reducer's window.
  5. under pytest WITHOUT the opt-in the spawn is refused (g-115-5310
     chokepoint, shared with execution-diary.py).
  6. end-to-end through main(): the tick rides an ordinary hook call and the
     emitted updatedInput is unchanged by it.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CORE_SCRIPTS))

import _shared_tick  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "bash_agent_inject", CORE_SCRIPTS / "bash-agent-inject.py")
bai = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bai)

AGENT = "alpha"
SID = "sid-hook-tick-0001"
OTHER = "sid-hook-tick-0002"


def _root(tmp_path: Path, *, carrier: bool = True, running: str | None = SID) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    sess = root / "agents" / AGENT / "session"
    sess.mkdir(parents=True)
    if carrier:
        (sess / f"body-heartbeat-{SID}.json").write_text(json.dumps({"sid": SID}), encoding="utf-8")
        (sess / f"body-heartbeat-{OTHER}.json").write_text(json.dumps({"sid": OTHER}), encoding="utf-8")
    if running is not None:
        (sess / "running-session-id").write_text(running, encoding="utf-8")
    return root, sess


def _capture(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(_shared_tick, "spawn_detached",
                        lambda *a, **k: calls.append((a, k)))
    # The sanctioned opt-in: the spawn is captured, nothing reaches a daemon.
    monkeypatch.setenv("MIND_DIARY_SHARED_TICK_TEST", "1")
    return calls


def _backdate(p: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(p, (t, t))


def test_no_carrier_means_no_tick(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path, carrier=False)
    bai._maybe_tick_heartbeat(AGENT, SID, root)
    assert calls == [], "a session without a Body carrier must never tick"
    assert not (sess / f"body-heartbeat-{SID}.json").exists(), (
        "the hook must never CREATE a carrier — /start and the tick own that")


def test_reducer_gets_the_full_tick_on_the_diary_window(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path)
    stamp = sess / "claim-renewal-last"
    stamp.touch()
    _backdate(stamp, _shared_tick.SHARED_HEARTBEAT_INTERVAL_S + 5)
    bai._maybe_tick_heartbeat(AGENT, SID, root)
    assert len(calls) == 1, f"expected one spawn, got {calls!r}"
    args, kwargs = calls[0]
    assert args[1] == AGENT and args[2] == SID
    assert kwargs["body_only"] is False, "the reducer must run the FULL tick"
    assert time.time() - stamp.stat().st_mtime < 5, (
        "claim-renewal-last must be stamped before the spawn (rate limit)")


def test_reducer_inside_the_window_does_not_tick(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path)
    (sess / "claim-renewal-last").touch()  # fresh
    bai._maybe_tick_heartbeat(AGENT, SID, root)
    assert calls == [], "a fresh window must suppress the spawn"


def test_non_reducer_body_gets_body_only_on_its_own_stamp(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path, running=SID)
    (sess / "claim-renewal-last").touch()  # the REDUCER's fresh window
    bai._maybe_tick_heartbeat(AGENT, OTHER, root)
    assert len(calls) == 1, (
        "a worker Body must tick on its OWN stamp — the reducer's fresh window "
        f"must not suppress it. calls={calls!r}")
    args, kwargs = calls[0]
    assert args[2] == OTHER and kwargs["body_only"] is True
    assert (root / "core" / "logs" / "heartbeat-hook" / OTHER).exists(), (
        "the per-SID stamp was not written")
    # Second call inside the window: suppressed.
    bai._maybe_tick_heartbeat(AGENT, OTHER, root)
    assert len(calls) == 1


def test_missing_running_session_id_is_treated_as_non_reducer(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path, running=None)
    bai._maybe_tick_heartbeat(AGENT, SID, root)
    assert len(calls) == 1 and calls[0][1]["body_only"] is True, (
        "with no running-session-id nothing proves this SID is the reducer; "
        "the agent-wide legs must not be advanced on a guess")


def test_pytest_without_opt_in_refuses_the_spawn(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(_shared_tick, "spawn_detached",
                        lambda *a, **k: calls.append((a, k)))
    monkeypatch.delenv("MIND_DIARY_SHARED_TICK_TEST", raising=False)
    root, sess = _root(tmp_path)
    bai._maybe_tick_heartbeat(AGENT, SID, root)
    assert calls == [], (
        "under pytest without the opt-in the tick must be refused — its "
        "team-state leg is the phantom-shard writer (g-115-5310)")


def test_main_end_to_end_ticks_and_leaves_the_injection_unchanged(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    root, sess = _root(tmp_path)
    fake = SimpleNamespace(agent=AGENT)
    monkeypatch.setattr(bai, "resolve_binding_with_diagnostics", lambda sid, root_: (fake, None))
    monkeypatch.setattr(bai, "_mark_binding_resolved", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(bai, "_log_binding_miss_once", lambda *a, **k: None, raising=False)
    # main() resolves project_root from SCRIPT_DIR; point the agent-dir helper at
    # the staged root so the carrier lookup lands in tmp, not the live repo.
    monkeypatch.setattr(bai, "_agent_dir", lambda _root, name: root / "agents" / name)
    payload = {"session_id": SID, "tool_name": "Bash",
               "tool_input": {"command": "bash core/scripts/wm-read.sh loop_state"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    try:
        bai.main()
    except SystemExit:
        pass
    result = json.loads(out.getvalue().strip())
    updated = result["hookSpecificOutput"]["updatedInput"]
    assert f"export MIND_AGENT={AGENT}; " in updated["command"]
    assert f"export MIND_SID={SID};" in updated["command"]
    assert len(calls) == 1 and calls[0][1]["body_only"] is False
