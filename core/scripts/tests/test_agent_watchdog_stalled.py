"""test_agent_watchdog_stalled.py — StalledProbe (agent-watchdog.py, ).

The watchdog false-OK (root cause #5 of the 2026-07-04 own-cloud fleet-wedge,
g-328-19): a FRESH runner-heartbeat masked wedged loops. HeartbeatProbe only
fires on a STALE heartbeat, so a loop that keeps re-ticking the heartbeat every
iteration while goal execution is frozen (execution-diary stale for DAYS) was
reported healthy the whole time. StalledProbe closes that gap: it fires on the
CONJUNCTION — heartbeat FRESH (process alive) + execution-diary STALE beyond a
threshold (no progress) while RUNNING.

Verification criterion (g-328-24): "unit test: fresh-heartbeat +
stale-diary-beyond-threshold classifies as STALLED."

Tests:
  1. Pure classify_stalled() — the classification the goal names:
     fresh-hb + stale-diary + RUNNING -> "stalled"; plus every branch that must
     NOT be STALLED (fresh diary -> progress; stale heartbeat -> None, that is
     HeartbeatProbe's job; IDLE -> None; missing signal -> None) and the
     strict-greater boundary.
  2. StalledProbe with real mtime-controlled files — emits the critical
     stalled_during_running event, dedups per episode, and emits stall_recovered
     when the diary advances. IDLE emits nothing. State round-trips.
  3. Config invariant: stalled_diary_stale_minutes > runner_heartbeat.stale_minutes
     (the g-328-25-shaped invariant that keeps the verdict from being inert).
  4. StalledProbe is registered in build_probes().
"""
from __future__ import annotations

import importlib.util
import os
import sys
import time
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

# Test thresholds (seconds). Invariant preserved: DIARY (600) > HEARTBEAT (60).
HB_THRESH = 60.0
DIARY_THRESH = 600.0


# ── 1. Pure classifier — classify_stalled() ─────────────────────────────────

def test_classify_fresh_heartbeat_stale_diary_running_is_stalled():
    """THE verification criterion: fresh heartbeat + stale diary (beyond
    threshold) + RUNNING classifies as STALLED."""
    verdict = WD.classify_stalled(
        agent_state="RUNNING",
        heartbeat_age_s=5.0,        # FRESH (< 60)
        diary_age_s=700.0,          # STALE (> 600)
        heartbeat_stale_threshold_s=HB_THRESH,
        diary_stale_threshold_s=DIARY_THRESH,
    )
    assert verdict == "stalled"


def test_classify_fresh_heartbeat_fresh_diary_is_progress():
    verdict = WD.classify_stalled(
        "RUNNING", 5.0, 120.0, HB_THRESH, DIARY_THRESH
    )
    assert verdict == "progress"


def test_classify_stale_heartbeat_is_none_not_stalled():
    """A STALE heartbeat is HeartbeatProbe's stale_during_running, NOT this
    wedge. classify_stalled must return None so the loop-died case is never
    mislabelled STALLED — even when the diary is also stale."""
    verdict = WD.classify_stalled(
        "RUNNING", 5000.0, 9000.0, HB_THRESH, DIARY_THRESH
    )
    assert verdict is None


def test_classify_idle_is_none():
    """A stale diary while IDLE is expected (the runner stopped) — never STALLED."""
    verdict = WD.classify_stalled(
        "IDLE", 5.0, 700.0, HB_THRESH, DIARY_THRESH
    )
    assert verdict is None


def test_classify_missing_heartbeat_signal_is_none():
    assert WD.classify_stalled("RUNNING", None, 700.0, HB_THRESH, DIARY_THRESH) is None


def test_classify_missing_diary_signal_is_none():
    assert WD.classify_stalled("RUNNING", 5.0, None, HB_THRESH, DIARY_THRESH) is None


def test_classify_diary_boundary_strict_greater():
    """At exactly the diary threshold the diary is NOT yet stalled (strict >),
    mirroring phase-wedge's strict-greater boundary."""
    assert WD.classify_stalled(
        "RUNNING", 5.0, DIARY_THRESH, HB_THRESH, DIARY_THRESH
    ) == "progress"
    assert WD.classify_stalled(
        "RUNNING", 5.0, DIARY_THRESH + 0.5, HB_THRESH, DIARY_THRESH
    ) == "stalled"


def test_classify_heartbeat_boundary_still_fresh_at_threshold():
    """At exactly the heartbeat threshold the heartbeat is still FRESH (age <=
    threshold), so a stale diary alongside it is STALLED."""
    assert WD.classify_stalled(
        "RUNNING", HB_THRESH, 700.0, HB_THRESH, DIARY_THRESH
    ) == "stalled"
    # Just past the heartbeat threshold -> stale heartbeat -> None.
    assert WD.classify_stalled(
        "RUNNING", HB_THRESH + 0.5, 700.0, HB_THRESH, DIARY_THRESH
    ) is None


# ── 2. StalledProbe integration (real files, controlled mtimes) ─────────────

def _session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "agents" / "delta" / "session"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ctx(tmp_path: Path):
    return WD.WatchdogContext(
        agent_name="delta",
        agent_dir=tmp_path / "agents" / "delta",
        project_root_path=tmp_path,
    )


def _probe(tmp_path: Path):
    p = WD.StalledProbe(_ctx(tmp_path))
    # Override the config-read thresholds with small test values (invariant held).
    p.heartbeat_threshold_seconds = HB_THRESH
    p.diary_threshold_seconds = DIARY_THRESH
    return p


def _write(path: Path, content: str, age_s: float) -> None:
    """Write content and set the file's mtime to now - age_s."""
    path.write_text(content, encoding="utf-8")
    when = time.time() - age_s
    os.utime(path, (when, when))


def test_probe_fires_stalled_during_running(tmp_path):
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)        # FRESH
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 700.0)  # STALE

    p = _probe(tmp_path)
    events = p.check()
    assert len(events) == 1
    ev = events[0]
    assert ev.probe == "stalled"
    assert ev.event == "stalled_during_running"
    assert ev.severity == "critical"
    assert ev.include_processes is True
    assert ev.payload["agent_state"] == "RUNNING"
    assert ev.payload["diary_age_seconds"] > DIARY_THRESH
    assert ev.payload["heartbeat_age_seconds"] < HB_THRESH
    assert p.last_state == "stalled"


def test_probe_dedups_within_episode(tmp_path):
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 700.0)

    p = _probe(tmp_path)
    assert len(p.check()) == 1        # first: fires
    assert len(p.check()) == 0        # second, same stalled state: deduped
    assert p.last_state == "stalled"


def test_probe_emits_recovery_when_diary_advances(tmp_path):
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 700.0)

    p = _probe(tmp_path)
    assert p.check()[0].event == "stalled_during_running"

    # Diary advances (fresh) — progress resumed.
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n{"e":"y"}\n', 5.0)
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "stall_recovered"
    assert events[0].severity == "info"
    assert p.last_state == "ok"


def test_probe_silent_when_idle(tmp_path):
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "IDLE", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 700.0)

    p = _probe(tmp_path)
    assert p.check() == []
    # last_state untouched (None verdict) — still the initial "unknown".
    assert p.last_state == "unknown"


def test_probe_silent_when_diary_fresh(tmp_path):
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 60.0)  # FRESH

    p = _probe(tmp_path)
    assert p.check() == []
    assert p.last_state == "ok"


def test_probe_silent_when_heartbeat_stale(tmp_path):
    """Stale heartbeat + stale diary -> HeartbeatProbe's territory, not ours."""
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5000.0)     # STALE
    _write(sess / "execution-diary.jsonl", '{"e":"x"}\n', 9000.0)

    p = _probe(tmp_path)
    assert p.check() == []


def test_probe_silent_when_diary_missing(tmp_path):
    """A missing diary (fresh session before the first goal executes) is not a
    wedge signal — no false positive."""
    sess = _session_dir(tmp_path)
    _write(sess / "agent-state", "RUNNING", 0)
    _write(sess / "runner-heartbeat", "tick", 5.0)
    # No execution-diary.jsonl written.
    p = _probe(tmp_path)
    assert p.check() == []


def test_probe_state_round_trips(tmp_path):
    p = _probe(tmp_path)
    p.last_state = "stalled"
    d = p.to_dict()
    assert d == {"last_state": "stalled"}
    p2 = _probe(tmp_path)
    p2.from_dict(d)
    assert p2.last_state == "stalled"


# ── 3. Config invariant (-shaped: diary_stale MUST exceed hb_stale) ──

def test_config_invariant_diary_stale_exceeds_heartbeat_stale():
    """: stalled_diary_stale_minutes MUST be > runner_heartbeat.stale_minutes.

    Deep-close LLM work legitimately freezes the diary AND ages the heartbeat
    TOGETHER for 30-45 min (aspirations.yaml runner_heartbeat comment). If the
    diary threshold were <= the heartbeat threshold, a diary stale past it would
    imply a heartbeat also stale past ITS threshold, so classify_stalled's
    fresh-heartbeat gate fails first and the STALLED verdict is INERT — the probe
    would never fire. Only a genuine wedge (heartbeat re-ticked FRESH while the
    diary freezes far longer) can present fresh-heartbeat WITH diary-stale > this
    threshold. If a future edit lowers stalled_diary_stale_minutes to <=
    stale_minutes, the probe silently stops detecting the wedge; this test fails
    loudly to prevent that."""
    import yaml
    cfg = CORE_SCRIPTS.parent / "config" / "aspirations.yaml"
    with open(cfg, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    rh = data.get("runner_heartbeat") or {}
    diary = rh.get("stalled_diary_stale_minutes")
    stale = rh.get("stale_minutes")
    assert diary is not None, "aspirations.yaml runner_heartbeat.stalled_diary_stale_minutes missing"
    assert stale is not None, "aspirations.yaml runner_heartbeat.stale_minutes missing"
    assert float(diary) > float(stale), (
        "g-328-24 invariant VIOLATED: stalled_diary_stale_minutes (%s) must be > "
        "stale_minutes (%s), else the StalledProbe fresh-heartbeat gate makes the "
        "STALLED verdict inert (a diary stale past a <=-heartbeat threshold implies "
        "a stale heartbeat)." % (diary, stale)
    )


# ── 4. Registration ─────────────────────────────────────────────────────────

def test_stalled_probe_registered(tmp_path):
    probes = WD.build_probes(_ctx(tmp_path))
    names = [p.name for p in probes]
    assert "stalled" in names, f"StalledProbe not registered in build_probes(): {names}"


def test_env_override_diary_threshold(monkeypatch):
    """STALLED_DIARY_STALE_MINUTES overrides the config value (test hook)."""
    monkeypatch.setenv("STALLED_DIARY_STALE_MINUTES", "42")
    assert WD._diary_stale_threshold_seconds() == 42 * 60.0
    monkeypatch.setenv("STALLED_DIARY_STALE_MINUTES", "not-a-number")
    # Malformed -> ignored -> falls through to config (180) or its 180 fallback.
    assert WD._diary_stale_threshold_seconds() >= 60 * 60.0
