"""PeerLivenessProbe: transition semantics, box-local paging dedup, and the
shared notify chokepoint (owner directive 2026-09-05, foxtrot ~9h dark).

The probe's classifier is tested in test_peer_liveness.py; here `scan` is
stubbed so each test drives the probe with a hand-built report.
"""
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from _paths import PROJECT_ROOT
except Exception:
    PROJECT_ROOT = Path(__file__).resolve().parents[3]

WD_PATH = PROJECT_ROOT / "core" / "scripts" / "agent-watchdog.py"


def _load():
    spec = importlib.util.spec_from_file_location("agent_watchdog_peer_under_test", WD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


wd = _load()
import peer_liveness as pl  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    import _paths
    world = tmp_path / "_isolated_world"
    (world / "team-state" / "agents").mkdir(parents=True)
    monkeypatch.setitem(sys.modules, "_paths", _paths)
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setenv(wd.PeerLivenessProbe.BOX_STATE_DIR_ENV, str(tmp_path / "boxstate"))
    monkeypatch.delenv(wd.PeerLivenessProbe.REALERT_ENV, raising=False)
    return world


def _ctx(tmp_path, role="reducer"):
    return wd.WatchdogContext(agent_name="alpha", agent_dir=tmp_path, project_root_path=PROJECT_ROOT,
                              body_role=role)


def _peer(agent, verdict, **extra):
    base = {"agent": agent, "verdict": verdict, "reason": f"{verdict} because test",
            "last_active": "2026-09-04T22:25:11", "last_active_age_min": 570.0,
            "in_flight": "g-326-85", "live_phase": "phase-4-execute g-326-85", "provenance": "authoritative",
            "signals": {"diary": {"ts": "2026-09-04T22:25:11", "readable": True, "fresh": False},
                        "board": {"ts": "2026-09-04T19:21:00", "readable": True, "fresh": False},
                        "goals": {"ts": "2026-09-04T19:21:00", "readable": True, "fresh": False}}}
    base.update(extra)
    return base


def _report(*peers, blind=False):
    return {"self": "alpha", "checked_at": "2026-09-05T08:00:00", "stale_hours": 3.0,
            "peers": list(peers), "blind": blind,
            "blind_cause": "roster came from the local mirror" if blind else None,
            "roster_provenance": "local-mirror" if blind else "authoritative"}


@pytest.fixture
def notify(monkeypatch):
    calls = []

    def _rec(subject, message, *, allow_duplicate=None):
        calls.append({"subject": subject, "message": message, "allow_duplicate": allow_duplicate})
        return {"sent": True, "rc": 0, "detail": "stub"}
    monkeypatch.setattr(wd, "_notify_decision_needed", _rec)
    return calls


def _drive(monkeypatch, tmp_path, reports):
    """Run one probe instance through successive scan reports; return the events per tick."""
    seq = iter(reports)
    monkeypatch.setattr(pl, "scan", lambda *_a, **_k: next(seq))
    probe = wd.PeerLivenessProbe(_ctx(tmp_path))
    probe.initialize()
    ticks = []
    for _ in reports:
        ticks.append(probe.check())
    return probe, ticks


# ── registration ─────────────────────────────────────────────────────────────

def test_registered_on_reducer_and_excluded_on_worker(tmp_path):
    reducer = {p.name for p in wd.build_probes(_ctx(tmp_path, "reducer"))}
    worker = {p.name for p in wd.build_probes(_ctx(tmp_path, "worker"))}
    assert "peer-liveness" in reducer
    assert "peer-liveness" not in worker
    assert "peer-liveness" not in wd.WORKER_SAFE_PROBES


# ── transitions ──────────────────────────────────────────────────────────────

def test_stalled_peer_logs_once_and_pages_once(monkeypatch, tmp_path, notify):
    stalled = _report(_peer("foxtrot", "stalled"), _peer("bravo", "alive"))
    probe, ticks = _drive(monkeypatch, tmp_path, [stalled, stalled, stalled])
    first = {e.event: e for e in ticks[0]}
    assert first["peer_stalled"].severity == "critical"
    assert "foxtrot" in first["peer_stalled"].summary
    assert first["peer_stall_paged"].payload["new_episode"] is True
    assert ticks[1] == [] and ticks[2] == []
    assert len(notify) == 1
    assert "foxtrot" in notify[0]["subject"] and notify[0]["allow_duplicate"] is None
    assert probe.prev["foxtrot"] == "stalled" and probe.prev["bravo"] == "alive"
    state = json.loads((tmp_path / "boxstate" / wd.PeerLivenessProbe.BOX_STATE_NAME).read_text())
    assert state["foxtrot"]["since"] == "2026-09-04T22:25:11"


def test_recovery_emits_cleared_and_drops_the_box_state(monkeypatch, tmp_path, notify):
    stalled = _report(_peer("foxtrot", "stalled"))
    alive = _report(_peer("foxtrot", "alive", last_active="2026-09-05T07:26:51"))
    probe, ticks = _drive(monkeypatch, tmp_path, [stalled, alive, alive])
    assert [e.event for e in ticks[1]] == ["peer_liveness_cleared"]
    assert ticks[2] == []
    state = json.loads((tmp_path / "boxstate" / wd.PeerLivenessProbe.BOX_STATE_NAME).read_text())
    assert state == {}
    assert len(notify) == 1


def test_new_episode_after_recovery_pages_again(monkeypatch, tmp_path, notify):
    ep1 = _report(_peer("foxtrot", "stalled", last_active="2026-09-04T22:25:11"))
    alive = _report(_peer("foxtrot", "alive", last_active="2026-09-05T07:26:51"))
    ep2 = _report(_peer("foxtrot", "stalled", last_active="2026-09-05T09:00:00"))
    _, ticks = _drive(monkeypatch, tmp_path, [ep1, alive, ep2])
    assert [e.event for e in ticks[2]] == ["peer_stalled", "peer_stall_paged"]
    assert len(notify) == 2 and notify[1]["allow_duplicate"] is None


def test_realert_window_repages_with_allow_duplicate(monkeypatch, tmp_path, notify):
    monkeypatch.setenv(wd.PeerLivenessProbe.REALERT_ENV, "0")
    stalled = _report(_peer("foxtrot", "stalled"))
    _, ticks = _drive(monkeypatch, tmp_path, [stalled, stalled])
    assert [e.event for e in ticks[0]] == ["peer_stalled", "peer_stall_paged"]
    assert [e.event for e in ticks[1]] == ["peer_stall_paged"]
    assert ticks[1][0].payload["new_episode"] is False
    assert len(notify) == 2
    assert notify[1]["allow_duplicate"] and "foxtrot" in notify[1]["allow_duplicate"]


def test_slow_peer_is_reported_once_and_never_paged(monkeypatch, tmp_path, notify):
    slow = _report(_peer("foxtrot", "slow"))
    _, ticks = _drive(monkeypatch, tmp_path, [slow, slow])
    assert [e.event for e in ticks[0]] == ["peer_slow"]
    assert ticks[0][0].severity == "info"
    assert ticks[1] == []
    assert notify == []


@pytest.mark.parametrize("verdict", ["alive", "stopped", "retired", "unknown"])
def test_non_alerting_verdicts_are_silent(monkeypatch, tmp_path, notify, verdict):
    rep = _report(_peer("foxtrot", verdict))
    _, ticks = _drive(monkeypatch, tmp_path, [rep, rep])
    assert ticks == [[], []]
    assert notify == []


def test_blind_roster_warns_once_and_clears(monkeypatch, tmp_path, notify):
    blind = _report(blind=True)
    ok = _report(_peer("bravo", "alive"))
    _, ticks = _drive(monkeypatch, tmp_path, [blind, blind, ok])
    assert [e.event for e in ticks[0]] == ["peer_liveness_probe_blind"]
    assert ticks[0][0].severity == "warning"
    assert ticks[1] == []
    assert [e.event for e in ticks[2]] == ["peer_liveness_probe_blind_cleared"]
    assert notify == []


def test_scan_failure_is_swallowed_not_raised(monkeypatch, tmp_path, notify):
    def boom(*_a, **_k):
        raise RuntimeError("no backend")
    monkeypatch.setattr(pl, "scan", boom)
    probe = wd.PeerLivenessProbe(_ctx(tmp_path))
    probe.initialize()
    assert probe.check() == []
    assert notify == []


def test_tick_state_roundtrip(monkeypatch, tmp_path, notify):
    stalled = _report(_peer("foxtrot", "stalled"))
    probe, _ = _drive(monkeypatch, tmp_path, [stalled])
    saved = json.loads(json.dumps(probe.to_dict()))
    fresh = wd.PeerLivenessProbe(_ctx(tmp_path))
    fresh.from_dict(saved)
    assert fresh.prev == probe.prev
    fresh.from_dict({"prev": "garbage"})
    assert fresh.prev == {}


# ── fleet-shared breadcrumb ──────────────────────────────────────────────────

def test_breadcrumb_seen_elsewhere_skips_the_mail_but_arms_the_clock(monkeypatch, tmp_path, notify):
    monkeypatch.setattr(wd, "_peer_stall_breadcrumb_seen", lambda episode: True)
    stalled = _report(_peer("foxtrot", "stalled"))
    probe, ticks = _drive(monkeypatch, tmp_path, [stalled, stalled])
    assert [e.event for e in ticks[0]] == ["peer_stalled", "peer_stall_paged_elsewhere"]
    assert ticks[1] == []
    assert notify == []
    state = json.loads((tmp_path / "boxstate" / wd.PeerLivenessProbe.BOX_STATE_NAME).read_text())
    assert state["foxtrot"]["notified"]["reason"] == "paged by another box"


def test_first_box_posts_the_breadcrumb_after_a_real_send(monkeypatch, tmp_path, notify):
    posted = []
    monkeypatch.setattr(wd, "_peer_stall_breadcrumb_seen", lambda episode: False)
    monkeypatch.setattr(wd, "_peer_stall_breadcrumb_post",
                        lambda agent, episode, subject: posted.append((agent, episode)) or {"posted": True})
    stalled = _report(_peer("foxtrot", "stalled"))
    _drive(monkeypatch, tmp_path, [stalled])
    assert posted == [("foxtrot", "peer-stall-foxtrot-2026-09-04T22-25-11")]
    assert len(notify) == 1


def test_breadcrumb_is_not_consulted_for_a_realert(monkeypatch, tmp_path, notify):
    monkeypatch.setenv(wd.PeerLivenessProbe.REALERT_ENV, "0")
    calls = []
    monkeypatch.setattr(wd, "_peer_stall_breadcrumb_seen", lambda episode: calls.append(episode) or False)
    stalled = _report(_peer("foxtrot", "stalled"))
    _drive(monkeypatch, tmp_path, [stalled, stalled])
    assert len(calls) == 1
    assert len(notify) == 2


def test_breadcrumb_helpers_are_inert_under_pytest(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_NOTIFY_ALLOW_PYTEST", raising=False)
    assert wd._peer_stall_breadcrumb_seen("peer-stall-x") is False
    assert wd._peer_stall_breadcrumb_post("x", "peer-stall-x", "s")["posted"] is False
    assert wd._peer_stall_episode_key("foxtrot", "2026-09-04T22:25:11") == "peer-stall-foxtrot-2026-09-04T22-25-11"


# ── the mail body ────────────────────────────────────────────────────────────

def test_message_head_is_deterministic_per_episode_and_names_the_detector_last(tmp_path):
    peer = _peer("foxtrot", "stalled")
    rep_a = dict(_report(peer), checked_at="2026-09-05T08:00:00")
    rep_b = dict(_report(peer), checked_at="2026-09-05T08:40:00")
    sub_a, body_a = wd._peer_stall_message(peer, rep_a, "alpha")
    sub_b, body_b = wd._peer_stall_message(peer, rep_b, "bravo")
    assert sub_a == sub_b
    assert body_a[:400] == body_b[:400]
    assert body_a != body_b
    assert "foxtrot" in sub_a and "2026-09-04T22:25" in sub_a
    assert "/start foxtrot" in body_a
    assert "execution diary head: 2026-09-04T22:25:11" in body_a


def test_message_marks_unreadable_and_empty_signals_differently(tmp_path):
    peer = _peer("foxtrot", "stalled",
                 signals={"diary": {"ts": None, "readable": True}, "board": {"ts": None, "readable": False},
                          "goals": {"ts": "2026-09-01T00:00:00", "readable": True}})
    _, body = wd._peer_stall_message(peer, _report(peer), "alpha")
    assert "execution diary head: none found" in body
    assert "last board post: unreadable" in body
    assert "last goal claim/completion: 2026-09-01T00:00:00" in body


# ── the shared notify chokepoint ─────────────────────────────────────────────

def test_notify_is_inert_under_pytest_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_WATCHDOG_NOTIFY_ALLOW_PYTEST", raising=False)
    got = wd._notify_decision_needed("s", "m")
    assert got == {"sent": False, "reason": "suppressed under pytest"}
    assert wd._notify_critical_memory("s", "m") == got


def test_notify_forwards_allow_duplicate_to_the_script(monkeypatch):
    monkeypatch.setenv("AGENT_WATCHDOG_NOTIFY_ALLOW_PYTEST", "1")
    captured = {}

    class _Proc:
        returncode = 4
        stdout = "duplicate"
        stderr = ""

    def fake_run(argv, **_kw):
        captured["argv"] = [str(a) for a in argv]
        return _Proc()
    monkeypatch.setattr(wd.subprocess, "run", fake_run)
    got = wd._notify_decision_needed("subj", "body", allow_duplicate="still stalled")
    assert got["sent"] is False and got["rc"] == 4
    argv = captured["argv"]
    assert argv[argv.index("--allow-duplicate") + 1] == "still stalled"
    assert argv[argv.index("--category") + 1] == "decision-needed"
    got2 = wd._notify_decision_needed("subj", "body")
    assert "--allow-duplicate" not in captured["argv"]
    assert got2["rc"] == 4
