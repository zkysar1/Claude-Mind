"""test_dry_spin_guard.py --  loop-entry backstop for a NARRATED
all-blocked yield.

The defect (measured on coach / claude-mind zc-03, 2026-09-03 02:10Z): a cycle
routed to all_blocked, the LLM narrated B6.5/B7/B7.2 as done WITHOUT running
them, and the cycle wrote nothing -- no blocked_sleep_until, no registered sleep
job, no execution-diary row, quiescence.last_check_at 16h stale. Every existing
loop-entry fast path CONSUMES state the previous cycle must have written, so
nothing noticed and the loop reloaded the ~75-minute handler back to back.

Structure mirrors test_dry_idle_cycle_cache.py:
  - pure evaluate() decision matrix: the coach HIT shape + every named MISS
    reason (each of the goal's five negative controls asserts on its own REASON,
    not merely on the absence of output)
  - read_loop_state envelope tolerance + read_marker extraction
  - fully-monkeypatched cmd_check integration: directive fires on the coach
    shape; each control suppresses it
  - directive-shape assertions: the emitted sleep is the registered Tier-A
    DRY_SLEEP=1 interruptible-sleep job (guard-967 / guard-1230) so stop-hook
    Gate 2.6 allows the turn-end
  - idempotence: a fire stamps the marker so the guard cannot re-fire on it
  - wiring anchors via content greps (config key on both surfaces, the
    goal-selector marker call site, the SKILL.md check)

Timestamps are computed DYNAMICALLY (now +/- delta) per guard-566 so the suite
never rots against a frozen date.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
REPO = CORE_SCRIPTS.parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Set MIND_AGENT around the module-level import so _paths resolves AGENT_DIR
# without depending on the runner's env (mirrors test_dry_idle_cycle_cache).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

dsg = importlib.import_module("dry-spin-guard")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


GAP = 120  # min_reentry_gap_s default


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _coach_marker(now, **over):
    """The 2026-09-03 02:10Z shape: a FRESH all_blocked route whose handler
    wrote no sleep state."""
    base = {
        "at": _iso(now - timedelta(seconds=20)),
        "sid": "coach-sid-0001",
        "sleep_registered": False,
        "sleep_seconds": None,
    }
    base.update(over)
    return base


def _hit_kwargs(now, marker, **over):
    """Defaults that, with a fresh _coach_marker, produce a HIT."""
    kw = dict(
        marker=marker,
        now=now,
        gap_s=GAP,
        blocked_remaining=None,   # blocked_sleep_until unset
        sleep_registered_job=False,  # no background sleep job
        diary_active=False,       # no diary row after the marker
    )
    kw.update(over)
    return kw


class _Args:
    explain = False


# --- pure decision matrix ----------------------------------------------------

def test_coach_shape_is_a_hit():
    """The measured incident shape must fire."""
    now = datetime.now()
    decision, reason = dsg.evaluate(**_hit_kwargs(now, _coach_marker(now)))
    assert decision == "hit", reason
    assert reason == "narrated-yield"


def test_control_5_marker_absent_is_a_miss():
    """Older deployments / a cycle that never routed all_blocked."""
    now = datetime.now()
    for absent in (None, {}, "not-a-dict", []):
        decision, reason = dsg.evaluate(**_hit_kwargs(now, absent))
        assert decision == "miss"
        assert reason == "no-marker"


def test_control_2_sleep_already_stamped_is_a_miss():
    """B7/B7.2 ran and stamped the marker -- the handler is not narrated."""
    now = datetime.now()
    marker = _coach_marker(now, sleep_registered=True, sleep_seconds=480)
    decision, reason = dsg.evaluate(**_hit_kwargs(now, marker))
    assert decision == "miss"
    assert reason == "sleep-already-stamped"


def test_control_3_marker_older_than_gap_is_a_miss():
    """A genuine long sleep elapsed -> normal entry."""
    now = datetime.now()
    marker = _coach_marker(now, at=_iso(now - timedelta(seconds=GAP + 5)))
    decision, reason = dsg.evaluate(**_hit_kwargs(now, marker))
    assert decision == "miss"
    assert reason.startswith("marker-stale:")


def test_marker_exactly_at_gap_is_a_miss_boundary():
    """The gate is age >= gap, so the boundary itself does NOT fire."""
    now = datetime.now()
    marker = _coach_marker(now, at=_iso(now - timedelta(seconds=GAP)))
    decision, reason = dsg.evaluate(**_hit_kwargs(now, marker))
    assert decision == "miss"
    assert reason.startswith("marker-stale:")


def test_control_1_blocked_sleep_active_is_a_miss():
    """blocked_sleep_until set -> idle-tick.sh owns this cycle."""
    now = datetime.now()
    decision, reason = dsg.evaluate(
        **_hit_kwargs(now, _coach_marker(now), blocked_remaining=300.0))
    assert decision == "miss"
    assert reason == "blocked-sleep-active"


def test_control_2b_registered_sleep_job_is_a_miss():
    """A live background sleep job -> the handler really did yield."""
    now = datetime.now()
    decision, reason = dsg.evaluate(
        **_hit_kwargs(now, _coach_marker(now), sleep_registered_job=True))
    assert decision == "miss"
    assert reason == "sleep-job-registered"


def test_control_4_diary_activity_after_marker_is_a_miss():
    """Previous route executing: a script wrote a diary row after the route."""
    now = datetime.now()
    decision, reason = dsg.evaluate(
        **_hit_kwargs(now, _coach_marker(now), diary_active=True))
    assert decision == "miss"
    assert reason == "diary-activity-after-marker"


def test_unparseable_and_future_markers_are_misses():
    """Clock skew and garbage both resolve toward NOT sleeping."""
    now = datetime.now()
    d, r = dsg.evaluate(**_hit_kwargs(now, _coach_marker(now, at="not-a-date")))
    assert (d, r) == ("miss", "marker-unparseable")
    d, r = dsg.evaluate(**_hit_kwargs(now, _coach_marker(now, at=None)))
    assert (d, r) == ("miss", "marker-unparseable")
    d, r = dsg.evaluate(
        **_hit_kwargs(now, _coach_marker(now, at=_iso(now + timedelta(seconds=90)))))
    assert (d, r) == ("miss", "marker-in-future")


def test_negative_controls_are_all_distinct_reasons():
    """Each control must be independently diagnosable -- a shared reason string
    would make a regression in one look like another."""
    now = datetime.now()
    reasons = set()
    for kw in (
        _hit_kwargs(now, None),
        _hit_kwargs(now, _coach_marker(now, sleep_registered=True)),
        _hit_kwargs(now, _coach_marker(now, at=_iso(now - timedelta(seconds=999)))),
        _hit_kwargs(now, _coach_marker(now), blocked_remaining=300.0),
        _hit_kwargs(now, _coach_marker(now), sleep_registered_job=True),
        _hit_kwargs(now, _coach_marker(now), diary_active=True),
    ):
        reasons.add(dsg.evaluate(**kw)[1])
    assert len(reasons) == 6, reasons


# --- reader ------------------------------------------------------------------

def test_read_marker_extracts_from_loop_state():
    now = datetime.now()
    marker = _coach_marker(now)
    ls = {"goals_completed": 3, "signals": {"dry_idle": {"streak": 0},
                                            "last_all_blocked": marker}}
    assert dsg.read_marker(loop_state=ls) == marker


def test_read_marker_tolerates_missing_signals():
    for ls in ({}, {"signals": None}, {"signals": {}}, {"signals": {"last_all_blocked": 7}}):
        assert dsg.read_marker(loop_state=ls) is None


def test_read_loop_state_fails_open_and_tolerates_envelope(monkeypatch):
    """A non-zero rc, unparseable stdout, or a non-dict body must all yield {}
    (-> no marker -> MISS), and a {"value": {...}} envelope must unwrap."""
    class _R:
        def __init__(self, rc, out):
            self.returncode, self.stdout, self.stderr = rc, out, ""

    monkeypatch.setattr(dsg.subprocess, "run", lambda *a, **k: _R(1, "{}"))
    assert dsg.read_loop_state() == {}
    monkeypatch.setattr(dsg.subprocess, "run", lambda *a, **k: _R(0, "not json"))
    assert dsg.read_loop_state() == {}
    monkeypatch.setattr(dsg.subprocess, "run", lambda *a, **k: _R(0, "null"))
    assert dsg.read_loop_state() == {}
    monkeypatch.setattr(dsg.subprocess, "run",
                        lambda *a, **k: _R(0, json.dumps({"value": {"signals": {"x": 1}}})))
    assert dsg.read_loop_state() == {"signals": {"x": 1}}
    monkeypatch.setattr(dsg.subprocess, "run",
                        lambda *a, **k: _R(0, json.dumps({"signals": {"x": 2}})))
    assert dsg.read_loop_state() == {"signals": {"x": 2}}


def test_probe_helpers_fail_open_toward_miss(monkeypatch):
    """sleep_job_pending and diary_activity_after must fail toward "there IS a
    sleep / there WAS activity" so an unreadable probe can never license a
    sleep."""
    def _boom(*a, **k):
        raise OSError("probe unavailable")
    monkeypatch.setattr(dsg.subprocess, "run", _boom)
    assert dsg.sleep_job_pending() is True
    assert dsg.diary_activity_after(_iso(datetime.now())) is True
    # blocked_sleep_remaining fails toward None, which does NOT stand the guard
    # down on its own -- the other two probes still can. None means "unknown",
    # and evaluate() only treats a POSITIVE remaining as blocked-sleep-active.
    assert dsg.blocked_sleep_remaining(datetime.now()) is None


def test_blocked_sleep_read_goes_through_the_bash_wrapper(monkeypatch):
    """B7 writes blocked_sleep_until with wm-set.sh (bash -> sends X-Mind-Sid ->
    BODY WM). Reading it via the python daemon client would hit the AGENT-WIDE WM
    on a worker and see None -- a reader/writer store mismatch. Pin the wrapper."""
    seen = {}

    class _R:
        returncode, stdout, stderr = 0, '"2026-09-04T18:00:00"', ""

    def _fake(argv, **k):
        seen["argv"] = argv
        return _R()
    monkeypatch.setattr(dsg.subprocess, "run", _fake)
    dsg.blocked_sleep_remaining(datetime.now())
    joined = " ".join(str(a) for a in seen["argv"])
    assert "wm-read.sh" in joined
    assert "blocked_sleep_until" in joined
    # guard-580: never a bare "bash" argv[0].
    assert seen["argv"][0] != "bash"


def test_diary_empty_is_a_genuine_absence(monkeypatch):
    """An EMPTY diary is real evidence of no activity, not a probe failure --
    otherwise a fresh agent could never trigger the guard."""
    class _R:
        returncode, stdout, stderr = 0, "[]", ""
    monkeypatch.setattr(dsg.subprocess, "run", lambda *a, **k: _R())
    assert dsg.diary_activity_after(_iso(datetime.now())) is False


def test_diary_row_older_than_marker_is_not_activity(monkeypatch):
    now = datetime.now()
    row = [{"timestamp": _iso(now - timedelta(minutes=30))}]

    class _R:
        returncode, stdout, stderr = 0, json.dumps(row), ""
    monkeypatch.setattr(dsg.subprocess, "run", lambda *a, **k: _R())
    assert dsg.diary_activity_after(_iso(now - timedelta(seconds=20))) is False


# --- cmd_check integration ---------------------------------------------------

def _wire(monkeypatch, marker, *, blocked=None, job=False, diary=False,
          tick=None, stamped=None):
    monkeypatch.setattr(dsg, "read_marker", lambda *a, **k: marker)
    monkeypatch.setattr(dsg, "blocked_sleep_remaining", lambda now: blocked)
    monkeypatch.setattr(dsg, "sleep_job_pending", lambda: job)
    monkeypatch.setattr(dsg, "diary_activity_after", lambda at: diary)
    monkeypatch.setattr(dsg, "_run_dry_idle_tick",
                        lambda: tick if tick is not None
                        else {"dry": True, "streak": 2, "sleep_seconds": 240})
    # `stamped if stamped is not None else []`, NOT `stamped or []`: an EMPTY
    # list is falsy, so the `or` form appends to a throwaway and the idempotence
    # assertion below silently passes against nothing.
    sink = stamped if stamped is not None else []
    monkeypatch.setattr(dsg, "_stamp_sleep", lambda s: sink.append(s))


def test_cmd_check_emits_directive_on_coach_shape(monkeypatch, capsys):
    now = datetime.now()
    stamped = []
    _wire(monkeypatch, _coach_marker(now), stamped=stamped)
    rc = dsg.cmd_check(_Args())
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-SPIN GUARD ===" in out
    # The one tool call must be the REGISTERED Tier-A sleep (guard-967/1230),
    # not a ScheduleWakeup and not a bare sleep.
    assert "DRY_SLEEP=1" in out
    assert "interruptible-sleep.sh 240" in out
    assert "run_in_background=true" in out
    assert "ScheduleWakeup" not in out
    # And it must tell the caller not to reload the handler.
    assert "DO NOT load the all-blocked handler" in out
    # Idempotence: the fire stamps the marker so it cannot fire twice.
    assert stamped == [240]


def test_cmd_check_silent_on_every_negative_control(monkeypatch, capsys):
    now = datetime.now()
    cases = {
        "marker-absent": dict(marker=None),
        "sleep-stamped": dict(marker=_coach_marker(now, sleep_registered=True)),
        "marker-stale": dict(marker=_coach_marker(
            now, at=_iso(now - timedelta(seconds=GAP + 60)))),
        "blocked-sleep-set": dict(marker=_coach_marker(now), blocked=300.0),
        "sleep-job-live": dict(marker=_coach_marker(now), job=True),
        "diary-activity": dict(marker=_coach_marker(now), diary=True),
    }
    for name, kw in cases.items():
        _wire(monkeypatch, **kw)
        rc = dsg.cmd_check(_Args())
        out = capsys.readouterr().out
        assert rc == 0, name
        assert out == "", f"{name} emitted a directive: {out!r}"


def test_cmd_check_misses_when_tick_declines(monkeypatch, capsys):
    """A directive we cannot SIZE is a directive we must not emit."""
    now = datetime.now()
    for tick in ({"dry": False}, {}, {"dry": True, "sleep_seconds": 0},
                 {"dry": True, "sleep_seconds": -5}):
        _wire(monkeypatch, _coach_marker(now), tick=tick)
        rc = dsg.cmd_check(_Args())
        assert rc == 0
        assert capsys.readouterr().out == "", tick


def test_cmd_check_never_raises_when_tick_returns_none(monkeypatch, capsys):
    now = datetime.now()
    monkeypatch.setattr(dsg, "read_marker", lambda *a, **k: _coach_marker(now))
    monkeypatch.setattr(dsg, "blocked_sleep_remaining", lambda now_: None)
    monkeypatch.setattr(dsg, "sleep_job_pending", lambda: False)
    monkeypatch.setattr(dsg, "diary_activity_after", lambda at: False)
    monkeypatch.setattr(dsg, "_run_dry_idle_tick", lambda: None)
    assert dsg.cmd_check(_Args()) == 0
    assert capsys.readouterr().out == ""


def test_cheap_gates_run_before_any_probe(monkeypatch, capsys):
    """The guard runs on EVERY loop entry, so the common path (no marker) must
    not pay for the blocked-sleep / job / diary probes."""
    calls = []
    monkeypatch.setattr(dsg, "read_marker", lambda *a, **k: None)
    monkeypatch.setattr(dsg, "blocked_sleep_remaining",
                        lambda now: calls.append("blocked"))
    monkeypatch.setattr(dsg, "sleep_job_pending", lambda: calls.append("job"))
    monkeypatch.setattr(dsg, "diary_activity_after",
                        lambda at: calls.append("diary"))
    assert dsg.cmd_check(_Args()) == 0
    assert calls == [], f"probes ran on the cheap-miss path: {calls}"


# --- wiring anchors ----------------------------------------------------------

def test_config_key_present_on_both_surfaces():
    """goal check 3: the key must exist with a default AND a comment naming the
    goal, in the fallback DEFAULTS and in the live config."""
    dry_idle = (CORE_SCRIPTS / "_dry_idle.py").read_text(encoding="utf-8")
    asp = (CORE_SCRIPTS.parent / "config" / "aspirations.yaml").read_text(encoding="utf-8")
    assert "min_reentry_gap_s" in dry_idle
    assert "min_reentry_gap_s" in asp
    assert "g-357-88" in dry_idle
    assert "g-357-88" in asp
    import _dry_idle as di
    assert di.DEFAULTS["min_reentry_gap_s"] == 120


def test_goal_selector_writes_the_marker():
    """The marker must be written by a SCRIPT at the all_blocked emission, or
    nothing this guard reads can exist."""
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert "def _write_allblocked_marker(" in src
    # The CALL must sit immediately before the verdict emission, in the same
    # branch. Anchor on the emission and look BACKWARD -- searching forward for
    # "_write_allblocked_marker()" finds the `def` line first (the substring is
    # contained in "def _write_allblocked_marker():"), which is what made the
    # first version of this assertion measure the distance between the
    # definition and the emission instead.
    idx_emit = src.index('"all_blocked": True')
    window = src[max(0, idx_emit - 500):idx_emit]
    assert "_write_allblocked_marker()" in window, \
        "marker write is not in the branch that emits the all_blocked verdict"


def test_single_writer_owns_both_marker_ops():
    """Marker + sleep stamp must go through the loop_state single writer, not a
    second writer (governed-store-write-classes.md)."""
    src = (CORE_SCRIPTS / "loop-state-bump-counters.py").read_text(encoding="utf-8")
    assert "--all-blocked-marker" in src
    assert "--all-blocked-sleep" in src
    assert "loop_state_cas_retry" in src


def test_skill_wires_the_check():
    """The guard is inert without a call site (guard-3448: a gate is only as
    broad as its entry points)."""
    skill = (REPO / ".claude" / "skills" / "aspirations" / "SKILL.md").read_text(
        encoding="utf-8")
    assert "dry-spin-guard.py check" in skill
    assert "=== DRY-SPIN GUARD ===" in skill
