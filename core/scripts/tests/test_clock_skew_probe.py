""" — ClockSkewProbe + health clock_posture regression tests.

The defect being guarded: a long-lived process keeps the TZ env it started
with, so its naive stamps are offset and it systematically LOSES last-write-
wins races against UTC-stamped peers, silently.

The tests that matter here are the ones that pin the SHAPE of the detection,
because every plausible-but-wrong shape passes on a healthy box:

  - test_daemon_skew_detected_even_when_self_clock_is_clean
        The whole point. A fresh subprocess is UTC-correct while the daemon is
        not; a probe that only checked its own clock passes on exactly the box
        that has the bug.
  - test_identically_skewed_processes_are_still_detected
        Kills the caller-side-diff design, which cancels to zero when both
        clocks are equally wrong.
  - test_missing_tz_offset_field_is_not_a_pass
        A daemon predating the field IS a long-lived process — the population
        most likely to be skewed. Silence there would be a vacuous zero.

guard-566: every timestamp below is computed RELATIVE to now, never literal.
"""
from __future__ import annotations

import datetime
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / "core" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_under_test", SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


class _Ctx:
    """Minimal WatchdogContext stand-in — the probe only reads project root."""

    def __init__(self, root, agent_dir=None):
        self.project_root_path = Path(root)
        self.agent_name = "testagent"
        # Sibling probes constructed by build_probes() read this; ClockSkewProbe
        # itself does not.
        self.agent_dir = Path(agent_dir) if agent_dir else Path(root) / "agents" / "testagent"

    def get_processes(self):
        return ""


def _probe(monkeypatch, health_payload, self_offset_s=0):
    """Build a ClockSkewProbe with both clock readings pinned."""
    p = WD.ClockSkewProbe(_Ctx(REPO))
    monkeypatch.setattr(WD, "daemon_health_json", lambda *a, **k: health_payload)
    monkeypatch.setattr(type(p), "_self_offset_s", staticmethod(lambda: self_offset_s))
    return p


def _health(tz_offset_s=0, **extra):
    """A health body with a RELATIVE naive_now (guard-566 — never a literal)."""
    now = datetime.datetime.now() + datetime.timedelta(seconds=tz_offset_s)
    body = {
        "ok": True, "pid": 4242, "port": 9999, "uptime_s": 86400.0,
        "naive_now": now.isoformat(timespec="seconds"),
        "tz_offset_s": tz_offset_s,
    }
    body.update(extra)
    return body


# ── The load-bearing shape tests ────────────────────────────────────────────

def test_daemon_skew_detected_even_when_self_clock_is_clean(monkeypatch):
    """THE case a self-only assertion cannot see: watchdog UTC, daemon EDT."""
    p = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    events = p.check()
    assert len(events) == 1, "a 4h-behind daemon must produce exactly one event"
    e = events[0]
    assert e.event == "clock_skew_detected"
    assert e.severity == "critical"
    assert e.payload["daemon_offset_s"] == -4 * 3600
    assert e.payload["self_offset_s"] == 0, (
        "self clock is clean — proving detection did NOT come from our own clock")
    assert "restart" in e.payload["remedy"]


def test_identically_skewed_processes_are_still_detected(monkeypatch):
    """Kills the caller-side-diff design: both clocks equally wrong -> a diff
    of the daemon's stamp against ours is ZERO, but the fleet is still broken."""
    off = -4 * 3600
    p = _probe(monkeypatch, _health(tz_offset_s=off), self_offset_s=off)
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "clock_skew_detected"
    assert events[0].payload["daemon_offset_s"] == off
    assert events[0].payload["self_offset_s"] == off


def test_missing_tz_offset_field_is_not_a_pass(monkeypatch):
    """A daemon predating the field is long-lived — do NOT report it clean."""
    body = _health()
    del body["tz_offset_s"]
    p = _probe(monkeypatch, body, self_offset_s=0)
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "clock_posture_unverifiable"
    assert events[0].payload["daemon_offset_s"] is None
    assert "NOT a clean reading" in events[0].payload["note"]


# ── Ordinary behaviour ──────────────────────────────────────────────────────

def test_clean_first_reading_is_silent(monkeypatch):
    p = _probe(monkeypatch, _health(tz_offset_s=0), self_offset_s=0)
    assert p.check() == [], "a healthy first tick must not emit"


def test_standing_skew_emits_once_not_every_tick(monkeypatch):
    p = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    assert len(p.check()) == 1
    assert p.check() == [], "state-deduped — a standing skew must not spam"
    assert p.check() == []


def test_recovery_after_skew_emits_cleared(monkeypatch):
    p = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    assert p.check()[0].event == "clock_skew_detected"
    monkeypatch.setattr(WD, "daemon_health_json", lambda *a, **k: _health(tz_offset_s=0))
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "clock_skew_cleared"
    assert events[0].payload["prev_state"] == "skewed"


def test_unreachable_daemon_is_fail_open(monkeypatch):
    """guard-597: reachability is DaemonHealthProbe's call, not ours."""
    p = _probe(monkeypatch, None, self_offset_s=0)
    assert p.check() == []


def test_sub_threshold_drift_is_not_skew(monkeypatch):
    """NTP jitter must not fire; only a wrong ZONE should."""
    p = _probe(monkeypatch, _health(tz_offset_s=5), self_offset_s=-3)
    assert p.check() == []


def test_threshold_separates_drift_from_smallest_real_tz_offset():
    """15 min (UTC+5:45-class offsets) is the smallest real zone step."""
    assert WD.ClockSkewProbe.THRESHOLD_S < 15 * 60
    assert WD.ClockSkewProbe.THRESHOLD_S > 30


def test_state_survives_tick_serialization(monkeypatch):
    """Tick mode runs each cycle in a NEW process — dedup depends on this."""
    p = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    assert len(p.check()) == 1
    saved = p.to_dict()
    p2 = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    p2.from_dict(saved)
    assert p2.check() == [], "restored state must suppress the re-emit"


def test_probe_is_registered(monkeypatch):
    """A probe absent from build_probes never runs (the orphan-sweep class)."""
    probes = WD.build_probes(_Ctx(REPO))
    assert any(isinstance(x, WD.ClockSkewProbe) for x in probes)
    assert "clock-skew" in [x.name for x in probes]


# ── The daemon-side half ────────────────────────────────────────────────────

def test_clock_posture_reports_this_process_offset():
    spec = importlib.util.spec_from_file_location(
        "health_clock", REPO / "mind_api" / "src" / "endpoints" / "health.py")
    # The module does package-relative imports at module scope; exercise the
    # pure function by compiling just it.
    src = (REPO / "mind_api" / "src" / "endpoints" / "health.py").read_text(encoding="utf-8")
    start = src.index("def clock_posture()")
    end = src.index("def health(")
    ns: dict = {"datetime": datetime}
    exec(compile(src[start:end], "clock_posture", "exec"), ns)
    out = ns["clock_posture"]()
    assert set(out) == {"naive_now", "utc_now", "tz_offset_s"}
    assert isinstance(out["tz_offset_s"], int)
    # naive_now must be rendered exactly as the framework mints stamps, so it
    # is directly comparable to what lands in stores.
    datetime.datetime.fromisoformat(out["naive_now"])
    # Self-consistency: the reported offset must equal the reported difference.
    delta = (datetime.datetime.fromisoformat(out["naive_now"])
             - datetime.datetime.fromisoformat(out["utc_now"])).total_seconds()
    assert abs(delta - out["tz_offset_s"]) <= 1


def test_health_response_carries_clock_fields():
    """The probe reads these off the health body — pin that they are wired in."""
    src = (REPO / "mind_api" / "src" / "endpoints" / "health.py").read_text(encoding="utf-8")
    assert "**clock_posture()" in src, (
        "clock_posture() must be spread into the health response, or "
        "ClockSkewProbe reads a body with no tz_offset_s and every daemon "
        "reports as unverifiable")


# ── fresh-eyes-code findings (2026-07-30, F-001/F-002/F-003) ────────────────
# All three were found by adversarial review of code written the same day, and
# all three were confirmed by probe before any fix was applied.

def test_arrival_at_clean_from_unverifiable_is_not_labelled_cleared(monkeypatch):
    """F-001. `clock_skew_cleared` asserts a skew was found and then fixed. A
    daemon predating tz_offset_s never had a measured skew — the posture was
    merely unreadable — so labelling its first post-restart tick "cleared" puts
    semantics in the event NAME that the observation does not support
    (guard-1008). This is the COMMON arrival, not an edge case."""
    body = _health()
    del body["tz_offset_s"]
    p = _probe(monkeypatch, body, self_offset_s=0)
    assert p.check()[0].event == "clock_posture_unverifiable"

    monkeypatch.setattr(WD, "daemon_health_json", lambda *a, **k: _health(tz_offset_s=0))
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "clock_posture_verified"
    assert events[0].payload["prev_state"] == "unverifiable"
    assert "cleared" not in events[0].summary


def test_arrival_at_clean_from_skewed_is_still_labelled_cleared(monkeypatch):
    """F-001 complement — the fix must DISCRIMINATE, not blanket-rename. A real
    measured skew that goes away is genuinely a recovery."""
    p = _probe(monkeypatch, _health(tz_offset_s=-4 * 3600), self_offset_s=0)
    assert p.check()[0].event == "clock_skew_detected"
    monkeypatch.setattr(WD, "daemon_health_json", lambda *a, **k: _health(tz_offset_s=0))
    events = p.check()
    assert events[0].event == "clock_skew_cleared"
    assert events[0].payload["prev_state"] == "skewed"


def test_self_skewed_daemon_clean_does_not_advise_restarting_the_daemon(monkeypatch):
    """F-002. Reachable case: the box TZ regresses AFTER the daemon started, so
    the daemon is on UTC and every freshly-spawned process is not. The old
    blanket remedy said "restart the daemon" while the payload's own
    daemon_offset_s read 0 — concluding a cause the emitted fields contradict
    (guard-1955). Restarting would move the one healthy process onto the wrong
    zone, so the wrong advice is actively harmful here, not merely unhelpful."""
    p = _probe(monkeypatch, _health(tz_offset_s=0), self_offset_s=-4 * 3600)
    events = p.check()
    assert len(events) == 1
    e = events[0]
    assert e.event == "clock_skew_detected"
    assert e.payload["daemon_offset_s"] == 0
    assert e.payload["skewed_side"] == "self"
    assert "daemon is on UTC" in e.payload["remedy"]
    assert "WORSE" in e.payload["remedy"], (
        "the remedy must warn that restarting the daemon makes this case worse")


def test_skewed_side_is_reported_for_each_of_the_three_shapes(monkeypatch):
    for daemon_off, self_off, expected in ((-4 * 3600, 0, "daemon"),
                                           (0, -4 * 3600, "self"),
                                           (-4 * 3600, -4 * 3600, "both")):
        p = _probe(monkeypatch, _health(tz_offset_s=daemon_off), self_offset_s=self_off)
        assert p.check()[0].payload["skewed_side"] == expected


def test_daemon_health_json_returns_none_on_http_client_exception(monkeypatch, tmp_path):
    """F-003. http.client.HTTPException derives from Exception ONLY — not
    OSError, not ValueError — so a truncated response escaped the except tuple
    and broke this function's own documented "returns None when unparseable"
    contract. The tick loop caught it, so nothing crashed; the probe just went
    silent by accident instead of failing open by design."""
    import http.client

    port_file = tmp_path / "daemon.port"
    port_file.write_text("9999", encoding="utf-8")
    monkeypatch.setattr(WD, "_rt_port_file", lambda root: port_file)

    def boom(*a, **k):
        raise http.client.IncompleteRead(b"truncated")

    monkeypatch.setattr(WD.urllib.request, "urlopen", boom)
    assert WD.daemon_health_json(REPO) is None, (
        "a malformed HTTP response must fail open to None, not propagate")


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
