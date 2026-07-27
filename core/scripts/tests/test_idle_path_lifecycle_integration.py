"""test_idle_path_lifecycle_integration.py -- cross-component integration harness
for the idle-path cache lifecycle (g-115-3061, story-c of the g-115-3059
consolidation; design board msg-20260725-081224-zeta-5183).

The per-component unit tests already exist and exercise each helper/cache in
ISOLATION with heavy mocking:
  - test_idle_cache_common.py    -> wake_timer_elapsed (facet-1) + authoritative_
                                    earliest_wake_at (facet-2), fake backend
  - test_dry_idle_cycle_cache.py -> dry cmd_check with _scan_queue / _read_cache /
                                    authoritative_earliest_wake_at all monkeypatched
  - test_quiescence_cycle_cache.py -> quiescence cmd_check with a _FakeGate

This harness is the MISSING CROSS-COMPONENT test. It drives the REAL dry-idle
cache LIFECYCLE -- write_baseline_cache -> cmd_check short-circuit -> wake --
against a tmp world with REAL file I/O for the cache file, the aspirations
queues (read by the REAL _wake_timers.scan_queue), AND the REAL facet-2
authoritative read (_idle_cache_common.authoritative_earliest_wake_at over a
LocalBackend for S1/S3, and an injected store for S2). It mocks ONLY the two
genuinely daemon-coupled WM readers -- _dry_signal and _blocked_sleep_remaining
-- because R3 (no live-daemon dependency) forbids spinning a daemon just to seed
`loop_state.signals.dry_idle.streak` and `blocked_sleep_until`.

Three scenarios, each with a TWIN POSITIVE CONTROL (rb-5078 / rb-5084: prove the
MISS is the scenario's specific defect, not a broken setup, and that the HIT path
still fires):

  S1 elapsed-recurring   (g-115-3033 / g-115-3046): a recurring goal FUTURE at
     cache-write ELAPSES mid-sleep. The FRESH rescan drops the now-past due
     (future-only guard g-115-3018) and would falsely HIT; the STORED baseline
     earliest_wake_at, checked via facet-1 wake_timer_elapsed, catches it -> MISS.
     Positive control: baseline still future -> HIT. Proof it was the BASELINE,
     not the fresh scan: the fresh current_earliest is NOT imminent at wake time.

  S2 stale-local-cache   (g-115-3015): the LOCAL mirror shows the goal far-future
     (local scan -> HIT), but the AUTHORITATIVE store shows it IMMINENT. The
     facet-2 authoritative recheck (g-115-3062, guard-1139) catches the divergence
     -> MISS. Positive control: store agrees with the mirror -> HIT.

  S3 conjunctive-gate    (g-115-3018): a recurring goal gated by a LIVE abstention.
     Its recurring next-due is PAST, but it is NOT executable (abstained until a
     future expiry). _goal_wake_time HONORS the conjunctive gate -- it drops the
     past recurring-due and wakes on the FUTURE abstention-expiry -- so the cache
     correctly HITs (sleeps to the gate release) instead of a false MISS on the
     stale past due.

Discipline notes:
  - STORAGE_BACKEND=local is pinned by conftest.py (guard-955 / g-115-1875) -- no
    os.environ mutation here.
  - No module-level os.environ mutation and no sys.modules stub (guard-1165):
    AGENT_DIR / WORLD_DIR are pointed at tmp via per-test monkeypatch fixtures,
    the daemon readers are stubbed per-test, and the clock is frozen per-check.
  - Timestamps are DYNAMIC (base = datetime.now(); everything is base +/- delta)
    per guard-566, so the suite never rots against a frozen date.
  - The dry-idle cache is the driver for all three scenarios: it exercises the
    SHARED components (_wake_timers.scan_queue / _goal_wake_time, _idle_cache_
    common.wake_timer_elapsed / authoritative_earliest_wake_at) that the
    quiescence cache also consumes. S4/S5 (quiescence-cache lifecycle + the
    conjunctive S3 driven through quiescence) are the SHOULD slice (story-d).
"""
from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# AGENT_DIR may be None at import (the fixture overrides it per-test); the module
# binds it without failing, so no module-level MIND_AGENT dance is needed here.
dcc = importlib.import_module("dry-idle-cycle-cache")
import _paths  # noqa: E402
import storage_backend  # noqa: E402
from _wake_timers import _goal_wake_time  # noqa: E402


# --- helpers -----------------------------------------------------------------

def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _recurring_goal(gid, next_due, *, interval_h=24, status="pending", **extra):
    """A recurring goal whose next-due == next_due (lastAchievedAt = next_due -
    interval). interval_h defaults to 24 so the NEXT recurrence after next_due is
    far in the future -- the load-bearing S1 property (the fresh rescan jumps to a
    far-future recurrence once next_due elapses)."""
    last = next_due - timedelta(hours=interval_h)
    g = {"id": gid, "status": status, "recurring": True,
         "interval_hours": interval_h, "lastAchievedAt": _iso(last)}
    g.update(extra)
    return g


def _abstained_recurring(gid, recurring_next_due, abstained_expiry, *, interval_h=1):
    """A recurring goal that is CURRENTLY abstained: recurring next-due =
    recurring_next_due (may be past), abstention expiry = abstained_expiry
    (abstained_at + ABSTENTION_TIMEOUT_H). status='abstained' so the abstention
    lane is live and the goal is not executable."""
    last = recurring_next_due - timedelta(hours=interval_h)
    abstained_at = abstained_expiry - timedelta(hours=72)  # _wake_timers.ABSTENTION_TIMEOUT_H
    return {"id": gid, "status": "abstained", "recurring": True,
            "interval_hours": interval_h, "lastAchievedAt": _iso(last),
            "abstained_at": _iso(abstained_at)}


def _seed_queue(path, *goals):
    """Write one active aspiration wrapping the given goals to path (JSONL)."""
    rec = {"id": "asp-test", "status": "active", "goals": list(goals)}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _freeze_now(monkeypatch, t):
    """Freeze dry-idle-cycle-cache's ONLY internal clock (cmd_check's
    `now = datetime.now()`) at t. write_baseline_cache / _scan_queue / evaluate_
    cache all take `now` explicitly, so this is the single uncontrolled call."""
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return t
    monkeypatch.setattr(dcc, "datetime", _Frozen)


def _check_at(monkeypatch, capsys, t):
    """Drive the REAL dry cmd_check with the clock frozen at t; return (rc, stdout).
    Empty stdout == MISS; a '=== DRY-IDLE CACHE HIT ===' banner == short-circuit."""
    capsys.readouterr()  # clear any prior capture
    _freeze_now(monkeypatch, t)
    rc = dcc.cmd_check(None)
    return rc, capsys.readouterr().out


class _StoreView:
    """A storage backend whose authoritative read returns a FIXED queue view for
    every path, modeling own-cloud where the store can be fresher than the local
    mirror. Only read_authoritative_bytes is exercised by facet-2."""

    def __init__(self, view_bytes):
        self._bytes = view_bytes

    def read_authoritative_bytes(self, path):  # noqa: ARG002 -- path-agnostic view
        return self._bytes


@pytest.fixture
def dry_world(tmp_path, monkeypatch):
    """A tmp world wired into the dry-idle cache: real cache + aspirations file
    I/O, _paths pointed at tmp, the two daemon-coupled WM readers stubbed to a
    live dry state. Everything else (scan, evaluate, authoritative recheck,
    _pending_wake_signal) stays REAL."""
    agent_dir = tmp_path / "agent"
    world_dir = tmp_path / "world"
    (agent_dir / "session").mkdir(parents=True)
    world_dir.mkdir(parents=True)

    # Point the cache's own AGENT_DIR (for _cache_path) AND _paths (scan_queue +
    # authoritative_earliest_wake_at both read _paths at CALL time) at tmp.
    monkeypatch.setattr(dcc, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "WORLD_DIR", world_dir)
    # ...and PIN sys.modules["_paths"] to the object just patched. Those call-time
    # reads are DEFERRED `from _paths import WORLD_DIR, AGENT_DIR` statements
    # (_wake_timers.scan_queue), which resolve through sys.modules -- NOT through
    # the module object this file imported at collection time. Sibling suite tests
    # purge "_paths" from sys.modules to force a sandbox re-import
    # (test_compact_restore_loop_state_shape.py, test_atomic_write_fallback.py)
    # and never restore it, so sys.modules can hold a DIFFERENT, unpatched _paths
    # still pointing at the live repo. Without this pin the scan silently reads
    # the REAL agent+world queues instead of the tmp one -- green standalone, red
    # in-suite. setitem is auto-restored at teardown. (guard-577 patches the
    # constant; this pins the module identity the lookup goes through.)
    monkeypatch.setitem(sys.modules, "_paths", _paths)

    # The only two daemon-coupled readers -> hermetic stubs (R3: no live daemon).
    monkeypatch.setattr(dcc, "_dry_signal", lambda: {"streak": 2})
    monkeypatch.setattr(dcc, "_blocked_sleep_remaining", lambda now: None)
    monkeypatch.delenv("DRY_IDLE_CACHE_CAP", raising=False)

    return SimpleNamespace(agent=agent_dir, world=world_dir, monkeypatch=monkeypatch)


# --- S1: elapsed-recurring ( / ) -------------------------

def test_s1_elapsed_recurring_baseline_catches_the_wake(dry_world, capsys):
    mp = dry_world.monkeypatch
    base = datetime.now().replace(microsecond=0)
    due = base + timedelta(seconds=300)  # future at write, beyond the 60s margin

    _seed_queue(dry_world.agent / "aspirations.jsonl",
                _recurring_goal("g-rec", due, interval_h=24))

    # cache-write: baseline earliest_wake_at == the recurring next-due (base+300s).
    dcc.write_baseline_cache(sleep_seconds=300, streak=2, now=base)
    cache = dcc._read_cache()
    assert cache is not None
    # Self-diagnosing guard: the tmp queue holds exactly ONE goal. A count in the
    # thousands means the scan escaped to the live repo queues (see the
    # sys.modules pin in dry_world) -- a far clearer signal than the bare
    # timestamp mismatch that escape would otherwise surface as.
    assert cache["goal_count"] == 1
    assert cache["earliest_wake_at"] == _iso(due)

    # POSITIVE CONTROL -- short-circuit at base+10s: baseline not elapsed,
    # authoritative (real LocalBackend, same tmp file) agrees -> HIT.
    rc, out = _check_at(mp, capsys, base + timedelta(seconds=10))
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" in out

    # WAKE -- at base+310s the recurring due (base+300s) has ELAPSED. The fresh
    # rescan drops the now-past due (future-only guard) and jumps to the next
    # recurrence ~24h out; goal_count is unchanged -> a FRESH-ONLY check would
    # falsely HIT. The STORED baseline is elapsed -> MISS (empty stdout).
    wake = base + timedelta(seconds=310)
    rc, out = _check_at(mp, capsys, wake)
    assert rc == 0
    assert out.strip() == ""  # MISS: no HIT directive emitted

    # Prove the BASELINE caught it, not the fresh scan (the  point):
    #   - the fresh current_earliest is NOT imminent at wake time, yet
    #   - evaluate_cache MISSes with reason 'baseline-timer-elapsed'.
    cache = dcc._read_cache()
    gc, fresh = dcc._scan_queue(wake)
    assert not dcc.wake_timer_elapsed(fresh, wake, dcc.MIN_SHORTCIRCUIT_S), \
        "fresh rescan must have dropped the elapsed due (future-only guard) -- " \
        "so a fresh-only check would have falsely HIT"
    decision, reason = dcc.evaluate_cache(
        cache, True, False, None, gc, fresh, False, wake)
    assert decision == "miss"
    assert reason == "baseline-timer-elapsed"


# --- S2: stale-local-cache () --------------------------------------

def test_s2_stale_local_mirror_authoritative_recheck_catches_the_wake(dry_world, capsys):
    mp = dry_world.monkeypatch
    base = datetime.now().replace(microsecond=0)
    local_due = base + timedelta(seconds=3600)  # local mirror: far-future -> would HIT

    _seed_queue(dry_world.agent / "aspirations.jsonl",
                _recurring_goal("g-rec", local_due, interval_h=24))
    dcc.write_baseline_cache(sleep_seconds=600, streak=2, now=base)

    # POSITIVE CONTROL -- authoritative store AGREES with the mirror (far-future).
    # Inject a store view echoing the local queue so the recheck is a no-op -> HIT.
    agree_bytes = (json.dumps({"id": "asp-test", "status": "active",
                   "goals": [_recurring_goal("g-rec", local_due, interval_h=24)]})
                   + "\n").encode("utf-8")
    mp.setattr(storage_backend, "get_backend", lambda: _StoreView(agree_bytes))
    rc, out = _check_at(mp, capsys, base + timedelta(seconds=5))
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" in out

    # DEFECT -- the AUTHORITATIVE store shows the goal IMMINENT (base+30s, within
    # the 60s margin) while the local mirror still says far-future. The facet-2
    # recheck reads the store, not the mirror -> MISS. (Fresh baseline re-write so
    # the positive control's cycle_count/last_hit do not confound this check.)
    auth_due = base + timedelta(seconds=30)
    fresh_bytes = (json.dumps({"id": "asp-test", "status": "active",
                   "goals": [_recurring_goal("g-rec", auth_due, interval_h=24)]})
                   + "\n").encode("utf-8")
    mp.setattr(storage_backend, "get_backend", lambda: _StoreView(fresh_bytes))
    dcc.write_baseline_cache(sleep_seconds=600, streak=2, now=base)
    rc, out = _check_at(mp, capsys, base + timedelta(seconds=5))
    assert rc == 0
    assert out.strip() == ""  # MISS: authoritative recheck caught the stale mirror

    # Prove the DIVERGENCE is exactly what the recheck saw: the LOCAL scan is
    # far-future (would HIT) while the AUTHORITATIVE read is imminent.
    _, local_fresh = dcc._scan_queue(base)
    assert not dcc.wake_timer_elapsed(local_fresh, base, dcc.MIN_SHORTCIRCUIT_S)
    auth = dcc.authoritative_earliest_wake_at(base)
    assert dcc.wake_timer_elapsed(auth, base, dcc.MIN_SHORTCIRCUIT_S)


# --- S3: conjunctive-gate () ---------------------------------------

def test_s3_conjunctive_gate_recurring_honors_live_abstention(dry_world, capsys):
    mp = dry_world.monkeypatch
    base = datetime.now().replace(microsecond=0)
    recurring_past_due = base - timedelta(seconds=3600)   # recurring interval already lapsed
    abstain_expiry = base + timedelta(seconds=7200)       # but abstained until the future

    goal = _abstained_recurring("g-rec", recurring_past_due, abstain_expiry, interval_h=1)
    _seed_queue(dry_world.agent / "aspirations.jsonl", goal)

    # DIRECT proof of the conjunctive gate (): _goal_wake_time DROPS the
    # past recurring-due and returns the FUTURE abstention-expiry -- the recurring
    # lane honors the live gate instead of emitting a stale past wake.
    w = _goal_wake_time(goal, base)
    assert w == abstain_expiry
    assert w > base

    # LIFECYCLE: the baseline records the FUTURE gate-release, not the past due.
    dcc.write_baseline_cache(sleep_seconds=600, streak=2, now=base)
    cache = dcc._read_cache()
    assert cache["goal_count"] == 1  # tmp queue, not the live repo (see dry_world)
    assert cache["earliest_wake_at"] == _iso(abstain_expiry)

    # short-circuit at base+5s -> baseline far-future, not imminent, no stale
    # past-due false-MISS, authoritative (real LocalBackend) agrees -> HIT. The
    # cache correctly sleeps toward the gate release.
    rc, out = _check_at(mp, capsys, base + timedelta(seconds=5))
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" in out
