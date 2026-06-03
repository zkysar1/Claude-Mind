"""test_daemon_health_probe.py — DaemonHealthProbe (agent-watchdog.py, ).

Proactive daemon-death detection on the watchdog tick cadence: ping the daemon
health endpoint, and on guard-597-CONFIRMED death emit a critical event +
delegate a race-safe respawn to rt_ensure_running. These tests pin the
behaviors the goal's verification requires, with NO real daemon and NO real
bash subprocess (the two module-level helpers are monkeypatched):

  1. Dead daemon during a quiet window -> detected + respawn delegated
     (outcome 2: "simulated dead daemon ... is detected and respawned").
  2. A confirmed-dead respawn whose post-probe comes up -> verified_up True.
  3. guard-597: a single timeout that answers on re-probe is slow-but-alive
     -> daemon_slow info event, NO respawn (the false-positive guard-597 bars).
  4. Healthy daemon -> no event, no respawn.
  5. Recovery -> daemon_recovered info; observe-only (RT_NO_AUTOSPAWN) detects
     without respawning; cross-tick state round-trips (to_dict/from_dict).

Plus the real (un-mocked) daemon_health_probe against a missing/dead port file
-> False, pinning the faithful rt_is_up replica that runs every tick.
"""
from __future__ import annotations

import importlib.util
import socket
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load_watchdog():
    # agent-watchdog.py is hyphenated — load via importlib for its symbols.
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog", CORE_SCRIPTS / "agent-watchdog.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


def _ctx(tmp_path: Path):
    return WD.WatchdogContext(
        agent_name="delta",
        agent_dir=tmp_path / "agents" / "delta",
        project_root_path=tmp_path,
    )


def _probe(tmp_path: Path):
    p = WD.DaemonHealthProbe(_ctx(tmp_path))
    p.CONFIRM_GAP_S = 0  # no real sleeps in tests
    return p


# ── 1. dead daemon -> detected + respawn delegated (outcome 2) ──────────────
def test_dead_daemon_detected_and_respawned(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_NO_AUTOSPAWN", raising=False)
    # Health probe reports down on the initial probe + every re-probe + the
    # post-respawn verify (daemon stayed dead).
    monkeypatch.setattr(WD, "daemon_health_probe", lambda root, timeout=1.0: False)
    calls = {"respawn": 0}

    def fake_respawn(root):
        calls["respawn"] += 1
        return {"attempted": True, "subprocess_rc": 0, "detail": "spawned"}

    monkeypatch.setattr(WD, "daemon_respawn", fake_respawn)

    p = _probe(tmp_path)
    events = p.check()

    assert calls["respawn"] == 1, "respawn must be delegated on confirmed death"
    assert len(events) == 1
    ev = events[0]
    assert ev.event == "daemon_unreachable"
    assert ev.severity == "critical"
    assert ev.payload["respawn"]["attempted"] is True
    assert ev.payload["consecutive_unreachable"] == 1
    assert ev.include_processes is True


# ── 2. respawn brings the daemon back -> verified_up True ───────────────────
def test_respawn_verified_up(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_NO_AUTOSPAWN", raising=False)
    seq = {"n": 0}
    down_count = 1 + WD.DaemonHealthProbe.CONFIRM_PROBES  # initial + re-probes

    def health(root, timeout=1.0):
        seq["n"] += 1
        return seq["n"] > down_count  # up only on the post-respawn verify probe

    monkeypatch.setattr(WD, "daemon_health_probe", health)
    monkeypatch.setattr(WD, "daemon_respawn",
                        lambda root: {"attempted": True, "subprocess_rc": 0})

    p = _probe(tmp_path)
    events = p.check()

    assert events[0].event == "daemon_unreachable"
    assert events[0].payload["respawn"]["verified_up"] is True
    assert p.prev_reachable is True  # next tick sees up, won't re-emit


# ── 3. guard-597: slow-but-alive -> NO respawn ──────────────────────────────
def test_slow_but_alive_not_respawned(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_NO_AUTOSPAWN", raising=False)
    seq = {"n": 0}

    def health(root, timeout=1.0):
        seq["n"] += 1
        return seq["n"] >= 2  # first probe down, re-probe up (slow-but-alive)

    monkeypatch.setattr(WD, "daemon_health_probe", health)
    calls = {"respawn": 0}
    monkeypatch.setattr(
        WD, "daemon_respawn",
        lambda root: calls.__setitem__("respawn", calls["respawn"] + 1) or {},
    )

    p = _probe(tmp_path)
    events = p.check()

    assert calls["respawn"] == 0, "guard-597: a slow-but-alive daemon must NOT be respawned"
    assert len(events) == 1
    assert events[0].event == "daemon_slow"
    assert events[0].severity == "info"
    assert p.prev_reachable is True
    assert p.consecutive_unreachable == 0


# ── 4. healthy daemon -> no event, no respawn ───────────────────────────────
def test_healthy_daemon_no_event(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_NO_AUTOSPAWN", raising=False)
    monkeypatch.setattr(WD, "daemon_health_probe", lambda root, timeout=1.0: True)
    calls = {"respawn": 0}
    monkeypatch.setattr(
        WD, "daemon_respawn",
        lambda root: calls.__setitem__("respawn", calls["respawn"] + 1) or {},
    )

    p = _probe(tmp_path)
    events = p.check()

    assert events == []
    assert calls["respawn"] == 0
    assert p.prev_reachable is True


# ── 5a. recovery emits an info event and resets the counter ─────────────────
def test_recovery_emits_info(tmp_path, monkeypatch):
    monkeypatch.delenv("RT_NO_AUTOSPAWN", raising=False)
    monkeypatch.setattr(WD, "daemon_health_probe", lambda root, timeout=1.0: True)

    p = _probe(tmp_path)
    p.prev_reachable = False  # was confirmed dead last tick
    p.consecutive_unreachable = 3
    events = p.check()

    assert len(events) == 1
    assert events[0].event == "daemon_recovered"
    assert events[0].severity == "info"
    assert p.consecutive_unreachable == 0
    assert p.prev_reachable is True


# ── 5b. RT_NO_AUTOSPAWN -> detect but do not respawn (observe-only) ─────────
def test_no_autospawn_detects_without_respawn(tmp_path, monkeypatch):
    monkeypatch.setenv("RT_NO_AUTOSPAWN", "1")
    monkeypatch.setattr(WD, "daemon_health_probe", lambda root, timeout=1.0: False)
    calls = {"respawn": 0}
    monkeypatch.setattr(
        WD, "daemon_respawn",
        lambda root: calls.__setitem__("respawn", calls["respawn"] + 1) or {},
    )

    p = _probe(tmp_path)
    events = p.check()

    assert calls["respawn"] == 0, "RT_NO_AUTOSPAWN=1 must suppress respawn (observe-only)"
    assert len(events) == 1
    assert events[0].event == "daemon_unreachable"
    assert events[0].payload["respawn"]["attempted"] is False


# ── 5c. cross-tick state survives serialization ─────────────────────────────
def test_state_roundtrip(tmp_path):
    p = _probe(tmp_path)
    p.prev_reachable = False
    p.consecutive_unreachable = 2
    saved = p.to_dict()

    q = _probe(tmp_path)
    q.from_dict(saved)
    assert q.prev_reachable is False
    assert q.consecutive_unreachable == 2


# ── 6. real (un-mocked) health probe — faithful rt_is_up replica ────────────
def test_real_health_probe_no_port_file(tmp_path, monkeypatch):
    # No daemon.port under tmp_path/mind_api/state -> replica returns False
    # (matches rt_base_url empty -> rt_is_up rc1).
    monkeypatch.delenv("RT_PORT_FILE", raising=False)
    monkeypatch.delenv("RT_DIR", raising=False)
    assert WD.daemon_health_probe(tmp_path) is False


def test_real_health_probe_dead_port(tmp_path, monkeypatch):
    # Port file points at a bound-then-closed port: nothing is listening, so
    # the GET is refused -> URLError caught -> False. Exercises the real
    # urllib path the probe runs every tick.
    monkeypatch.delenv("RT_PORT_FILE", raising=False)
    monkeypatch.delenv("RT_DIR", raising=False)
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    state = tmp_path / "mind_api" / "state"
    state.mkdir(parents=True)
    (state / "daemon.port").write_text(str(free_port), encoding="utf-8")
    assert WD.daemon_health_probe(tmp_path, timeout=0.5) is False
