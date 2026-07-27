"""test_dry_idle_cycle_cache.py -- -d dry-cycle short-circuit (Layer 4).

Exercises the fast-path that collapses consecutive DRY cycles
(dry-idle-cycle-cache.py) plus the cache WRITER wired into dry-idle-tick.py
(write_baseline_cache / delete_cache).

Structure mirrors test_quiescence_cycle_cache.py:
  - pure evaluate_cache decision matrix (every MISS reason + the HIT case)
  - cache I/O round-trip + delete
  - fully-monkeypatched cmd_check integration (3 hits -> cap forces full cycle;
    new-work / not-in-dry / no-cache / stale cheap misses)
Plus dry-specific coverage the quiescence twin has no analog for:
  - _goal_wake_time timer lanes (defer / recurring / blocker / abstention / min)
  - _scan_queue against real temp aspiration files (goal_count + earliest_wake)
  - _hit_sleep_seconds cap-at-earliest_wake (the DRY divergence from the 600s cap)
  - _is_stale (productive-interlude leaves a stale baseline)
  - wiring anchors (SKILL.md check + tick writer) via content greps

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
# without depending on the runner's env (mirrors test_quiescence_cycle_cache).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

dcc = importlib.import_module("dry-idle-cycle-cache")

if _SAVED_AGENT is None:
    os.environ.pop("MIND_AGENT", None)
else:
    os.environ["MIND_AGENT"] = _SAVED_AGENT


# --- helpers -----------------------------------------------------------------

def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def _mk_cache(now, **over):
    """A valid cache that evaluate_cache returns HIT for, before overrides."""
    base = {
        "goal_count": 5,
        "earliest_wake_at": _iso(now + timedelta(hours=2)),
        "sleep_seconds": 480,
        "streak": 3,
        "cycle_count": 0,
        "written_at": _iso(now),
        "last_hit_at": None,
    }
    base.update(over)
    return base


def _hit_kwargs(now, cache, **over):
    """Default kwargs that, with a fresh _mk_cache, produce a HIT."""
    kw = dict(
        cache=cache,
        dry_active=True,
        stale=False,
        blocked_remaining=None,
        current_goal_count=cache["goal_count"],
        current_earliest_wake_at=cache["earliest_wake_at"],
        pending_signal=False,
        now=now,
        cap=dcc.DEFAULT_CAP,
    )
    kw.update(over)
    return kw


# --- pure evaluate_cache decision matrix -------------------------------------

def test_evaluate_no_cache():
    now = datetime.now()
    d, r = dcc.evaluate_cache(None, True, False, None, 5, None, False, now)
    assert d == "miss" and r == "no-cache"


def test_evaluate_not_in_dry():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now), dry_active=False))
    assert d == "miss" and r == "not-in-dry"


def test_evaluate_stale():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now), stale=True))
    assert d == "miss" and r == "stale-dry"


def test_evaluate_blocked_sleep_active():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now), blocked_remaining=dcc.DEFER_REMAINING_S + 30))
    assert d == "miss" and r == "blocked-sleep-active"


def test_evaluate_blocked_sleep_just_under_threshold_is_hit():
    now = datetime.now()
    d, _ = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now), blocked_remaining=dcc.DEFER_REMAINING_S - 1))
    assert d == "hit"


def test_evaluate_cap_reached():
    now = datetime.now()
    cache = _mk_cache(now, cycle_count=dcc.DEFAULT_CAP)  # 3 >= cap 3
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, cache))
    assert d == "miss" and r.startswith("cap-reached")


def test_evaluate_under_cap_is_hit():
    now = datetime.now()
    for cc in range(dcc.DEFAULT_CAP):  # 0,1,2 under cap 3
        d, _ = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now, cycle_count=cc)))
        assert d == "hit", f"cycle_count={cc} should hit"


def test_evaluate_new_work():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now), current_goal_count=6))
    assert d == "miss" and r.startswith("new-work")


def test_evaluate_fewer_goals_is_hit():
    # Goals removed (completed/archived elsewhere) must NOT block -- fewer goals
    # cannot make a dry queue non-dry. Only a count INCREASE does.
    now = datetime.now()
    d, _ = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now), current_goal_count=4))
    assert d == "hit"


def test_evaluate_timer_elapsed():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now),
        current_earliest_wake_at=_iso(now - timedelta(minutes=1))))
    assert d == "miss" and r == "timer-imminent-or-elapsed"


def test_evaluate_timer_imminent():
    # A timer within MIN_SHORTCIRCUIT_S is "imminent" -> MISS (not worth a
    # sub-minute short-circuit; let the full cycle pick up the ready goal).
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now),
        current_earliest_wake_at=_iso(now + timedelta(seconds=dcc.MIN_SHORTCIRCUIT_S - 5))))
    assert d == "miss" and r == "timer-imminent-or-elapsed"


def test_evaluate_timer_far_future_is_hit():
    now = datetime.now()
    d, _ = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now),
        current_earliest_wake_at=_iso(now + timedelta(hours=3))))
    assert d == "hit"


def test_evaluate_no_timer_is_hit():
    # Truly empty queue (no deferred/recurring/blocked goals) -> no timer -> HIT
    # (the interruptible sleep still wakes early on signal files).
    now = datetime.now()
    d, _ = dcc.evaluate_cache(**_hit_kwargs(
        now, _mk_cache(now), current_earliest_wake_at=None))
    assert d == "hit"


def test_evaluate_pending_signal():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now), pending_signal=True))
    assert d == "miss" and r == "pending-wake-signal"


def test_evaluate_clean_hit():
    now = datetime.now()
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, _mk_cache(now)))
    assert d == "hit" and r == "ok"


# --- baseline-timer-elapsed guard () -------------------------------
# The elapsed-recurring false-HIT class: a recurring goal whose next-due was
# FUTURE at write time (stored into earliest_wake_at) ELAPSES mid-sleep. The
# shared _wake_timers future-only guard () drops the now-past due-time
# from the FRESH rescan, so current_earliest_wake_at jumps to a LATER goal (or
# None) and goal_count is unchanged -> pre-fix false HIT that sleeps through the
# now-due recurring goal (alpha finding msg-4248, 2026-07-24). The STORED
# baseline earliest_wake_at forces the MISS.

def test_evaluate_baseline_timer_elapsed_fresh_scan_later():
    # Stored baseline wake ELAPSED; fresh scan reports a LATER goal (the dropped
    # recurring due left a later defer/blocker as the new soonest). Pre-fix the
    # fresh-scan timer check saw only the far-future wake -> HIT. Fix -> MISS.
    now = datetime.now()
    cache = _mk_cache(now, earliest_wake_at=_iso(now - timedelta(minutes=1)))
    d, r = dcc.evaluate_cache(**_hit_kwargs(
        now, cache,
        current_earliest_wake_at=_iso(now + timedelta(hours=3))))
    assert d == "miss" and r == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_elapsed_fresh_scan_none():
    # Same drop-out, but the recurring goal was the ONLY timer -> fresh scan
    # returns None. Pre-fix: no fresh timer -> HIT (test_evaluate_no_timer_is_hit
    # shape). Fix: stored baseline elapsed -> MISS.
    now = datetime.now()
    cache = _mk_cache(now, earliest_wake_at=_iso(now - timedelta(minutes=1)))
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, cache, current_earliest_wake_at=None))
    assert d == "miss" and r == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_imminent():
    # A stored baseline wake WITHIN MIN_SHORTCIRCUIT_S is imminent -> MISS
    # (boundary parity with the fresh-scan timer-imminent check).
    now = datetime.now()
    cache = _mk_cache(now,
                      earliest_wake_at=_iso(now + timedelta(seconds=dcc.MIN_SHORTCIRCUIT_S - 5)))
    d, r = dcc.evaluate_cache(**_hit_kwargs(
        now, cache, current_earliest_wake_at=_iso(now + timedelta(hours=3))))
    assert d == "miss" and r == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_far_future_is_hit():
    # The guard must NOT over-trigger: a stored baseline wake still comfortably in
    # the future leaves the HIT intact (the steady dry-trough short-circuit).
    now = datetime.now()
    cache = _mk_cache(now, earliest_wake_at=_iso(now + timedelta(hours=2)))
    d, r = dcc.evaluate_cache(**_hit_kwargs(now, cache, current_earliest_wake_at=None))
    assert d == "hit" and r == "ok"


# --- _is_stale ---------------------------------------------------------------

def test_is_stale_fresh_is_false():
    now = datetime.now()
    anchor = _iso(now - timedelta(seconds=100))
    assert dcc._is_stale(anchor, 480, now) is False


def test_is_stale_old_is_true():
    now = datetime.now()
    # older than sleep_seconds + DRY_STALE_MARGIN_S
    anchor = _iso(now - timedelta(seconds=480 + dcc.DRY_STALE_MARGIN_S + 60))
    assert dcc._is_stale(anchor, 480, now) is True


def test_is_stale_unparseable_is_true():
    now = datetime.now()
    assert dcc._is_stale(None, 480, now) is True
    assert dcc._is_stale("not-a-ts", 480, now) is True


# --- _goal_wake_time timer lanes ---------------------------------------------

def test_goal_wake_time_deferred_until():
    now = datetime.now()
    du = now + timedelta(hours=5)
    g = {"status": "pending", "defer_reason": "x", "deferred_until": _iso(du)}
    assert dcc._goal_wake_time(g, now) == du.replace(microsecond=0)


def test_goal_wake_time_defer_set_at_default_timeout():
    now = datetime.now()
    set_at = now - timedelta(hours=10)
    g = {"status": "pending", "defer_reason": "x", "defer_reason_set_at": _iso(set_at)}
    expected = set_at.replace(microsecond=0) + timedelta(hours=dcc._DEFAULT_DEFER_TIMEOUT_H)
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_defer_explicit_timeout():
    now = datetime.now()
    set_at = now - timedelta(hours=1)
    g = {"status": "pending", "defer_reason": "x",
         "defer_reason_set_at": _iso(set_at), "defer_reason_timeout": 4}
    expected = set_at.replace(microsecond=0) + timedelta(hours=4)
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_defer_lingering_on_blocked_ignored():
    # defer_reason LINGERS after a goal transitions pending -> blocked; a BLOCKED
    # goal with a stale defer_reason must NOT contribute a defer wake (the 
    # case: status=blocked yet a vestigial defer fired a 4.4d-past wake). A blocked
    # goal is gated by its blocker lane, not its stale defer -- with no blocker_ref
    # here, the goal carries no computable wake at all. .
    now = datetime.now()
    set_at = now - timedelta(hours=200)  # defer TTL long expired in the past
    g = {"status": "blocked", "defer_reason": "x", "defer_reason_set_at": _iso(set_at)}
    assert dcc._goal_wake_time(g, now) is None


def test_goal_wake_time_recurring_interval_hours():
    now = datetime.now()
    last = now - timedelta(hours=1)
    g = {"status": "pending", "recurring": True,
         "lastAchievedAt": _iso(last), "interval_hours": 6}
    expected = last.replace(microsecond=0) + timedelta(hours=6)
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_recurring_remind_days():
    now = datetime.now()
    last = now - timedelta(hours=1)
    g = {"status": "pending", "recurring": True,
         "lastAchievedAt": _iso(last), "remind_days": 2}
    expected = last.replace(microsecond=0) + timedelta(hours=48)
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_recurring_never_run_is_none():
    now = datetime.now()
    g = {"status": "pending", "recurring": True}  # no lastAchievedAt
    assert dcc._goal_wake_time(g, now) is None


def test_goal_wake_time_recurring_overdue_future_only_ignored():
    # FUTURE-ONLY (): a recurring goal whose next-due is in the PAST
    # (overdue) contributes NO recurring wake -- emitting the past due-time only
    # floors quiescence. With no other lane, the goal carries no computable wake.
    now = datetime.now()
    last = now - timedelta(hours=30)  # interval 24h -> due 6h AGO (overdue)
    g = {"status": "pending", "recurring": True,
         "lastAchievedAt": _iso(last), "interval_hours": 24}
    assert dcc._goal_wake_time(g, now) is None


def test_goal_wake_time_recurring_overdue_but_precond_gated_uses_precond():
    # The  case: recurring-overdue (past due-time) AND precond-gated
    # (resolves_no_earlier_than in the future). The past recurring-due is skipped
    # (future-only); the FUTURE precond release is the real wake -- NOT the past
    # recurring-due that used to pin the floor. .
    now = datetime.now()
    last = now - timedelta(hours=30)          # recurring-due 6h AGO
    rnb = now + timedelta(hours=4)            # precond releases in 4h
    g = {"status": "pending", "recurring": True,
         "lastAchievedAt": _iso(last), "interval_hours": 24,
         "preconditions": [{"resolves_no_earlier_than": _iso(rnb)}]}
    assert dcc._goal_wake_time(g, now) == rnb.replace(microsecond=0)


def test_goal_wake_time_blocker_expiry():
    now = datetime.now()
    exp = now + timedelta(hours=3)
    g = {"status": "blocked", "blocker_ref": {"external_id": "x", "expires_at": _iso(exp)}}
    assert dcc._goal_wake_time(g, now) == exp.replace(microsecond=0)


def test_goal_wake_time_blocker_lingering_on_pending_ignored():
    # blocker_ref LINGERS after a goal un-blocks (blocked -> pending); a PENDING
    # goal with a stale blocker_ref must NOT contribute a blocker wake (the
    #  /  floor bug -- both pending + recurring, achieved today,
    # yet a MAY blocker_ref.expires_at pinned earliest_wake_at to a past date).
    # Only the real (recurring) wake counts. .
    now = datetime.now()
    stale = now - timedelta(hours=1)  # blocker "expired" in the past
    last = now - timedelta(hours=1)
    g = {"status": "pending", "recurring": True,
         "blocker_ref": {"external_id": "x", "expires_at": _iso(stale)},
         "lastAchievedAt": _iso(last), "interval_hours": 24}
    expected = last.replace(microsecond=0) + timedelta(hours=24)  # FUTURE recurring wake
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_abstention():
    # Abstention wake fires ONLY for a CURRENTLY-abstained goal (status ==
    # "abstained"). .
    now = datetime.now()
    ab = now - timedelta(hours=1)
    g = {"status": "abstained", "abstained_at": _iso(ab)}
    expected = ab.replace(microsecond=0) + timedelta(hours=dcc._ABSTENTION_TIMEOUT_H)
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_abstention_lingering_on_pending_ignored():
    # abstained_at LINGERS after a goal un-abstains; a PENDING goal with a stale
    # abstained_at must NOT contribute an abstention wake (the  floor
    # bug -- a pending+recurring goal achieved today whose May abstained_at
    # pinned earliest_wake_at to a past date). Only its real (recurring) wake
    # counts. .
    now = datetime.now()
    ab = now - timedelta(hours=1)  # abstention would have "expired" in the past
    last = now - timedelta(hours=1)
    g = {"status": "pending", "recurring": True, "abstained_at": _iso(ab),
         "lastAchievedAt": _iso(last), "interval_hours": 24}
    expected = last.replace(microsecond=0) + timedelta(hours=24)  # FUTURE recurring wake
    assert dcc._goal_wake_time(g, now) == expected


def test_goal_wake_time_terminal_status_returns_none():
    # Terminal goals have no future 'becomes executable' event -- no wake timer
    # in ANY lane, even with a lingering lastAchievedAt / abstained_at /
    # blocker_ref. The non-defer lanes previously lacked this guard, so terminal
    # goals contributed stale PAST wakes that floored quiescence. .
    now = datetime.now()
    past = now - timedelta(hours=1)
    for st in ("completed", "skipped", "expired", "retired"):
        g = {"status": st, "recurring": True, "lastAchievedAt": _iso(past),
             "interval_hours": 1, "abstained_at": _iso(past),
             "blocker_ref": {"external_id": "x", "expires_at": _iso(past)}}
        assert dcc._goal_wake_time(g, now) is None, f"{st} should carry no wake timer"


def test_goal_wake_time_no_timers_is_none():
    now = datetime.now()
    g = {"status": "pending", "title": "plain executable goal"}
    assert dcc._goal_wake_time(g, now) is None


def test_goal_wake_time_picks_min():
    # min() across two SIMULTANEOUSLY-active lanes. Under the status-gated guards
    # ( / ) defer fires only for pending and blocker only for
    # blocked, so those two can no longer co-fire on one goal; pair the pending
    # defer lane with the status-agnostic recurring lane instead.
    now = datetime.now()
    soon = now + timedelta(hours=1)
    last = now - timedelta(hours=1)  # recurring next-due = last + 10h = now + 9h (later)
    g = {
        "status": "pending",
        "defer_reason": "y", "deferred_until": _iso(soon),
        "recurring": True, "lastAchievedAt": _iso(last), "interval_hours": 10,
    }
    assert dcc._goal_wake_time(g, now) == soon.replace(microsecond=0)


# --- _hit_sleep_seconds (the DRY cap-at-earliest_wake divergence) -------------

def test_hit_sleep_uses_cached_curve_when_no_timer():
    now = datetime.now()
    cache = _mk_cache(now, sleep_seconds=480)
    assert dcc._hit_sleep_seconds(cache, None, now) == 480


def test_hit_sleep_not_extended_past_curve():
    # A far-future timer must not LENGTHEN the sleep beyond the curve value.
    now = datetime.now()
    cache = _mk_cache(now, sleep_seconds=480)
    far = _iso(now + timedelta(hours=5))
    assert dcc._hit_sleep_seconds(cache, far, now) == 480


def test_hit_sleep_capped_at_near_timer():
    # A timer nearer than the curve value caps the sleep so we never oversleep
    # the moment a goal becomes executable (the soundness lever for long sleeps).
    # microsecond=0 so the second-granularity _iso round-trip is exact (a
    # sub-second `now` would floor 199.2 -> 199, which is correct behavior but
    # not what this boundary assertion is pinning).
    now = datetime.now().replace(microsecond=0)
    cache = _mk_cache(now, sleep_seconds=480)
    near = now + timedelta(seconds=200)
    assert dcc._hit_sleep_seconds(cache, _iso(near), now) == 200


def test_hit_sleep_floored():
    now = datetime.now()
    cache = _mk_cache(now, sleep_seconds=30)  # below MIN floor
    assert dcc._hit_sleep_seconds(cache, None, now) == dcc.MIN_SHORTCIRCUIT_S


# --- cache I/O ---------------------------------------------------------------

def test_cache_round_trip_persists_increment(tmp_path, monkeypatch):
    monkeypatch.setattr(dcc, "AGENT_DIR", tmp_path)
    (tmp_path / "session").mkdir()
    now = datetime.now()
    dcc._write_cache(_mk_cache(now, cycle_count=1))
    again = dcc._read_cache()
    assert again["cycle_count"] == 1
    again["cycle_count"] += 1
    dcc._write_cache(again)
    assert dcc._read_cache()["cycle_count"] == 2


def test_delete_cache_removes_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(dcc, "AGENT_DIR", tmp_path)
    (tmp_path / "session").mkdir()
    dcc._write_cache(_mk_cache(datetime.now()))
    assert dcc._cache_path().exists()
    dcc.delete_cache()
    assert not dcc._cache_path().exists()
    dcc.delete_cache()  # absent -> no error (idempotent)


def test_read_cache_absent_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(dcc, "AGENT_DIR", tmp_path)
    (tmp_path / "session").mkdir()
    assert dcc._read_cache() is None


# --- _scan_queue against real temp aspiration files --------------------------

def _seed_aspirations(path, aspirations):
    path.write_text("\n".join(json.dumps(a) for a in aspirations) + "\n",
                    encoding="utf-8")


def test_scan_queue_counts_and_earliest(tmp_path, monkeypatch):
    import _paths
    now = datetime.now()
    agent_dir = tmp_path / "agent"
    world_dir = tmp_path / "world"
    agent_dir.mkdir()
    world_dir.mkdir()

    near = now + timedelta(hours=2)
    far = now + timedelta(hours=9)
    _seed_aspirations(agent_dir / "aspirations.jsonl", [
        {"id": "asp-a", "goals": [
            {"id": "g-1", "status": "pending"},  # no timer
            {"id": "g-2", "status": "blocked",
             "blocker_ref": {"external_id": "x", "expires_at": _iso(far)}},
        ]},
    ])
    _seed_aspirations(world_dir / "aspirations.jsonl", [
        {"id": "asp-w", "goals": [
            {"id": "g-3", "status": "pending", "defer_reason": "d",
             "deferred_until": _iso(near)},  # the earliest wake
        ]},
    ])

    monkeypatch.setattr(dcc, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "WORLD_DIR", world_dir)

    goal_count, earliest = dcc._scan_queue(now)
    assert goal_count == 3
    assert earliest == near.replace(microsecond=0).isoformat(timespec="seconds")


def test_write_baseline_cache_round_trips_through_scan(tmp_path, monkeypatch):
    # dry-idle-tick.py's entry point: write_baseline_cache scans + writes a
    # cycle_count=0 baseline that _read_cache round-trips.
    import _paths
    now = datetime.now()
    agent_dir = tmp_path / "agent"
    world_dir = tmp_path / "world"
    (agent_dir / "session").mkdir(parents=True)
    world_dir.mkdir()
    _seed_aspirations(agent_dir / "aspirations.jsonl", [
        {"id": "asp-a", "goals": [{"id": "g-1", "status": "pending"}]},
    ])
    _seed_aspirations(world_dir / "aspirations.jsonl", [])

    monkeypatch.setattr(dcc, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "AGENT_DIR", agent_dir)
    monkeypatch.setattr(_paths, "WORLD_DIR", world_dir)

    dcc.write_baseline_cache(480, 3, now)
    cache = dcc._read_cache()
    assert cache["cycle_count"] == 0
    assert cache["sleep_seconds"] == 480
    assert cache["streak"] == 3
    assert cache["goal_count"] == 1
    assert cache["last_hit_at"] is None
    assert "written_at" in cache


# --- cmd_check integration (fully monkeypatched -- no daemon, no I/O) ---------

def _isolate_cmd_check(monkeypatch, tmp_path, *, streak=2, goal_count=5,
                       earliest=None, blocked=None, pending=False,
                       auth_earliest=None, auth_raises=None):
    """Wire cmd_check's external reads to hermetic stand-ins (mirrors the
    quiescence integration harness). Returns nothing -- the cache file under
    tmp_path/session is the shared state.

    auth_earliest / auth_raises control the facet-2 authoritative recheck
    (g-115-3062): default auth_earliest=None means the AUTHORITATIVE store agrees
    with the local HIT (no imminent wake), so pre-facet-2 HIT tests pass unchanged.
    Set auth_earliest to an ISO to simulate the store showing a sooner wake than the
    local mirror; set auth_raises to an exception to simulate an unreadable store."""
    (tmp_path / "session").mkdir(exist_ok=True)
    monkeypatch.setattr(dcc, "AGENT_DIR", tmp_path)
    monkeypatch.setattr(dcc, "_dry_signal", lambda: {"streak": streak})
    monkeypatch.setattr(dcc, "_blocked_sleep_remaining", lambda now: blocked)
    monkeypatch.setattr(dcc, "_pending_wake_signal", lambda: pending)
    monkeypatch.setattr(dcc, "_scan_queue", lambda now: (goal_count, earliest))

    def _fake_auth(now, **kw):
        if auth_raises is not None:
            raise auth_raises
        return auth_earliest
    monkeypatch.setattr(dcc, "authoritative_earliest_wake_at", _fake_auth)
    monkeypatch.delenv("DRY_IDLE_CACHE_CAP", raising=False)


def test_cmd_check_three_hits_then_cap_forces_full(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path, goal_count=5,
                       earliest=_iso(now + timedelta(hours=2)))
    dcc._write_cache(_mk_cache(now, cycle_count=0,
                               earliest_wake_at=_iso(now + timedelta(hours=2))))

    for expected_after in (1, 2, 3):
        rc = dcc.cmd_check(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "=== DRY-IDLE CACHE HIT ===" in out, \
            f"cycle expected HIT (post-count {expected_after})"
        assert "DRY_SLEEP=1 bash core/scripts/interruptible-sleep.sh" in out
        assert dcc._read_cache()["cycle_count"] == expected_after

    # 4th cycle: cycle_count == cap -> cheap gate forces a MISS, counter frozen.
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out
    assert dcc._read_cache()["cycle_count"] == 3


def test_cmd_check_authoritative_imminent_forces_miss(tmp_path, monkeypatch, capsys):
    # facet-2 (): every cheap LOCAL gate would HIT (local scan shows a
    # far-future earliest, cache fresh) but the AUTHORITATIVE store shows a wake
    # imminent within MIN_SHORTCIRCUIT_S -> MISS, no HIT directive, counter frozen
    # (guard-1139 / : the local mirror was stale about a sooner wake).
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path, goal_count=5,
                       earliest=_iso(now + timedelta(hours=2)),
                       auth_earliest=_iso(now + timedelta(seconds=10)))
    dcc._write_cache(_mk_cache(now, cycle_count=0,
                               earliest_wake_at=_iso(now + timedelta(hours=2))))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out
    assert dcc._read_cache()["cycle_count"] == 0  # MISS -> counter NOT incremented


def test_cmd_check_authoritative_read_error_fails_open_to_miss(tmp_path, monkeypatch, capsys):
    # facet-2 fail-open: an unreadable authoritative store (any non-FileNotFound
    # error) MISSes (full cycle), never sleeps on the local-only decision -- the
    # `except -> return 0` direction, guard-1139.
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path, goal_count=5,
                       earliest=_iso(now + timedelta(hours=2)),
                       auth_raises=RuntimeError("s3 unreachable"))
    dcc._write_cache(_mk_cache(now, cycle_count=0,
                               earliest_wake_at=_iso(now + timedelta(hours=2))))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out
    assert dcc._read_cache()["cycle_count"] == 0


def test_cmd_check_new_work_misses(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    # cached goal_count 5, live scan reports 6 -> new work arrived.
    _isolate_cmd_check(monkeypatch, tmp_path, goal_count=6,
                       earliest=_iso(now + timedelta(hours=2)))
    dcc._write_cache(_mk_cache(now, cycle_count=0, goal_count=5))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out
    assert dcc._read_cache()["cycle_count"] == 0  # untouched on miss


def test_cmd_check_baseline_timer_elapsed_misses(tmp_path, monkeypatch, capsys):
    #  end-to-end regression: the stored baseline earliest_wake_at has
    # ELAPSED (a recurring goal crossed its due-time mid-sleep) while the FRESH
    # _scan_queue reports the recurring goal dropped out (future-only guard) ->
    # a LATER soonest timer, SAME goal_count. Pre-fix cmd_check HIT (sleeps through
    # the now-due goal); the fix MISSes so the full cycle lets the selector pick
    # up the due recurring goal.
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path, goal_count=5,
                       earliest=_iso(now + timedelta(hours=3)))  # fresh scan: later
    dcc._write_cache(_mk_cache(now, cycle_count=0, goal_count=5,
                               earliest_wake_at=_iso(now - timedelta(minutes=1))))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out
    assert dcc._read_cache()["cycle_count"] == 0  # untouched on miss


def test_cmd_check_not_in_dry_cheap_miss(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path, streak=0)  # dry period ended
    dcc._write_cache(_mk_cache(now, cycle_count=0))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""
    assert dcc._read_cache()["cycle_count"] == 0


def test_cmd_check_stale_cheap_miss(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    _isolate_cmd_check(monkeypatch, tmp_path)
    # written_at far in the past -> stale baseline (productive interlude ran).
    stale_at = _iso(now - timedelta(seconds=480 + dcc.DRY_STALE_MARGIN_S + 120))
    dcc._write_cache(_mk_cache(now, cycle_count=0, written_at=stale_at, last_hit_at=None))
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== DRY-IDLE CACHE HIT ===" not in out


def test_cmd_check_no_cache_is_cheap_miss(tmp_path, monkeypatch, capsys):
    _isolate_cmd_check(monkeypatch, tmp_path)
    rc = dcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""


# --- wiring anchors (pseudocode/shell -- pinned by content greps) ------------

def test_skill_md_wires_dry_cache_check():
    text = (REPO / ".claude" / "skills" / "aspirations" / "SKILL.md").read_text(encoding="utf-8")
    assert "dry-idle-cycle-cache.py check" in text, "SKILL.md lost the dry-cache check call"
    assert "=== DRY-IDLE CACHE HIT ===" in text, "SKILL.md lost the HIT sentinel branch"
    assert "Phase -0.5e.0b" in text, "SKILL.md lost the dry-cache phase anchor"
    assert "g-115-2084-d" in text, "SKILL.md lost the provenance anchor"


def test_tick_wires_cache_writer():
    text = (REPO / "core" / "scripts" / "dry-idle-tick.py").read_text(encoding="utf-8")
    assert "dry-idle-cycle-cache" in text, "tick lost the cache-module import"
    assert "write_baseline_cache" in text, "tick lost the dry-tick baseline write"
    assert "delete_cache" in text, "tick lost the non-dry cache invalidation"
