"""test_quiescence_cycle_cache.py --  hash-collapse short-circuit.

Exercises the fast-path that collapses identical quiescence cycles
(quiescence-cycle-cache.py) plus the cross-iteration cache WRITER added to
quiescence-gate.py (_write_cycle_cache).

The goal's sandbox checks, mapped to tests:
  1. "induce 3 identical-hash cycles -> 4th forces full cycle"
        -> test_cmd_check_three_hits_then_cap_forces_full
  2. "hash change -> no short-circuit on the new hash"
        -> test_evaluate_hash_changed / test_evaluate_hash_none
  3. "counter resets on hash change or new signal"
        -> the gate WRITER always writes cycle_count=0
           (test_gate_writer_payload_shape) and a changed hash yields a new
           cache, so the reader never carries a stale counter forward.
  4. "cap at 3 forces full cycle"
        -> test_evaluate_cap_reached + the cmd_check integration above.

Plus the verification.outcomes:
  - "cache file written on each quiescence approval"
        -> test_gate_writer_payload_shape (round-trips gate -> reader)
  - "hash-matched short-circuit fires when wake_outcome null + hash unchanged"
        -> test_evaluate_hit_*
  - expiry / new-work / pending-signal MISS branches -> test_evaluate_*

Pure-function tests call evaluate_cache directly (no I/O). The integration
test drives cmd_check against a real temp cache file with a FAKE quiescence-gate
module injected into sys.modules so no daemon / goal-selector is touched
(guard-862-style isolation; rb-225/rb-247 -- no bash subprocess).
Timestamps are computed DYNAMICALLY (now +/- delta) per guard-566.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

# Set MIND_AGENT around the module-level import so _paths resolves AGENT_DIR
# without depending on the test runner's env (mirrors test_cadence_signal_gate).
_SAVED_AGENT = os.environ.get("MIND_AGENT")
os.environ.setdefault("MIND_AGENT", "alpha")

qcc = importlib.import_module("quiescence-cycle-cache")
qg = importlib.import_module("quiescence-gate")

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
        "blocker_set_hash": "abc123",
        "blocker_refs": [
            {"external_id": "pq-x", "expires_at": _iso(now + timedelta(hours=2))},
        ],
        "sleep_seconds": 600,
        "goal_count": 5,
        "cycle_count": 0,
        "wake_outcome": None,
        "approved_at": _iso(now),
    }
    base.update(over)
    return base


def _hit_args(now, cache):
    """Default kwargs that, with a fresh _mk_cache, produce a HIT."""
    return dict(
        cache=cache,
        active_snapshot={"entered_at": _iso(now)},
        blocked_remaining=None,
        current_hash=cache["blocker_set_hash"],
        current_goal_count=cache["goal_count"],
        pending_signal=False,
        current_earliest_wake_at=None,
        now=now,
        cap=qcc.DEFAULT_CAP,
    )


# --- pure-function decision matrix -------------------------------------------

def test_evaluate_no_cache():
    now = datetime.now()
    decision, reason = qcc.evaluate_cache(
        None, True, None, "abc123", 5, False, None, now)
    assert decision == "miss" and reason == "no-cache"


def test_evaluate_not_in_quiescence():
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["active_snapshot"] = None  # no live quiescence sleep
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "not-in-quiescence"


def test_evaluate_blocked_sleep_active():
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["blocked_remaining"] = qcc.DEFER_REMAINING_S + 30  # timer mid-flight
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "blocked-sleep-active"


def test_evaluate_blocked_sleep_just_under_threshold_is_hit():
    # blocked_remaining at/under DEFER_REMAINING_S must NOT block the hit.
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["blocked_remaining"] = qcc.DEFER_REMAINING_S - 1
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "hit", reason


def test_evaluate_cap_reached():
    now = datetime.now()
    cache = _mk_cache(now, cycle_count=qcc.DEFAULT_CAP)  # 3 >= cap 3
    decision, reason = qcc.evaluate_cache(**_hit_args(now, cache))
    assert decision == "miss" and reason.startswith("cap-reached")


def test_evaluate_under_cap_is_hit():
    now = datetime.now()
    for cc in range(qcc.DEFAULT_CAP):  # 0,1,2 all under cap 3
        cache = _mk_cache(now, cycle_count=cc)
        decision, _ = qcc.evaluate_cache(**_hit_args(now, cache))
        assert decision == "hit", f"cycle_count={cc} should hit"


# --- elapsed-recurring safety invariant () -------------------------
#  fixed an elapsed-recurring false-HIT in dry-idle-cycle-cache: a
# recurring goal's due-time elapsed mid-sleep, _wake_timers' future-only guard
# () dropped the now-past due from the fresh rescan, and the fresh-scan
# timer check therefore never fired -> the cache slept through the now-due goal.
#  investigated whether quiescence-cycle-cache shares that gap. It does
# NOT — a not-due recurring goal is invisible to the blocker hash (collect_blocked
# excludes "recurring cooldown"), so the structural entry to the path exists, BUT
# quiescence is protected by the invariant IMMINENT_CHECK_HORIZON >= MAX_SLEEP:
# both equal SLEEP_CAP_S (600s). evaluate_cache MISSes on any wake within
# SLEEP_CAP_S of now (the imminent horizon), and _emit_hit_directive caps every
# emitted sleep at SLEEP_CAP_S. Because a 600s sleep advances `now` by at most the
# 600s imminent window, every future recurring due is flagged "imminent" on the
# check within one sleep-length BEFORE it elapses -> MISS -> full cycle. The due
# never elapses INSIDE a sleep to reach the future-only-dropped state. dry-idle was
# exposed precisely because it VIOLATES this invariant (imminent horizon 60s <<
# max sleep 7200s curve). These two tests pin the invariant so a future edit that
# raises the sleep above the imminent horizon (or lowers the horizon) fails loud.
#
# RECONCILED (): the invariant protects the elapsed-recurring case ONLY
# while check cadence stays <= the horizon; a cadence OVERSHOOT (re-entry latency, or
# a future max_sleep raise) reopens it. evaluate_cache now ALSO checks the STORED
# baseline earliest_wake_at (D2), closing the gap by defense-in-depth regardless of
# cadence -- so "does NOT share the gap" is now "the gap is CLOSED, not merely avoided
# by the cadence invariant." See the test_evaluate_baseline_timer_* tests below.

def _emitted_sleep_seconds(cache, now_wake=None):
    import re
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        qcc._emit_hit_directive(cache, qcc.DEFAULT_CAP, earliest_wake_at=now_wake)
    m = re.search(r"interruptible-sleep\.sh\s+(\d+)", buf.getvalue())
    assert m, f"no sleep directive emitted:\n{buf.getvalue()}"
    return int(m.group(1))


def test_emit_sleep_never_exceeds_cap_g115_3034():
    # MAX_SLEEP invariant: even a cache asking for a huge sleep is capped at
    # SLEEP_CAP_S, so the emitted sleep can never exceed the imminent horizon.
    now = datetime.now()
    cache = _mk_cache(now, sleep_seconds=99999)
    assert _emitted_sleep_seconds(cache) <= qcc.SLEEP_CAP_S


def test_wake_at_sleep_cap_is_imminent_miss_g115_3034():
    # IMMINENT_HORIZON invariant: a fresh wake exactly SLEEP_CAP_S out is flagged
    # imminent -> MISS. Paired with the cap above, this proves imminent_horizon ==
    # max_sleep == SLEEP_CAP_S, so a recurring due is always caught while still
    # future-imminent (one sleep before it elapses) and never sleeps through.
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["current_earliest_wake_at"] = _iso(now + timedelta(seconds=qcc.SLEEP_CAP_S))
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "timer-imminent-or-elapsed"


# --- defense-in-depth: stored-baseline elapsed guard ( / ) ----
# The  invariant tests above protect the elapsed-recurring case only while
# cadence <= horizon. A cadence overshoot lets a recurring due elapse inside a sleep;
# _wake_timers' future-only guard then drops it from the fresh rescan (-> a LATER goal
# or None) with hash + goal_count unchanged. The STORED baseline earliest_wake_at
# (written by quiescence-gate at approval, evaluate_cache D2) catches that residual
# case regardless of the fresh scan, making correctness independent of the cadence
# premise. Twin of dry-idle's test_evaluate_baseline_timer_* (where the same gap was a
# LIVE bug, ). Shared helper: _idle_cache_common.wake_timer_elapsed.

def test_evaluate_baseline_timer_elapsed_g115_3060():
    # AC1 shape: stored baseline wake ELAPSED (a recurring due crossed its due-time
    # mid-sleep) while the fresh scan DROPPED it -> None. Pre-fix: fresh-only check ->
    # HIT (sleeps through the now-due goal). Fix: stored baseline elapsed -> MISS.
    now = datetime.now()
    cache = _mk_cache(now, earliest_wake_at=_iso(now - timedelta(minutes=1)))
    args = _hit_args(now, cache)
    args["current_earliest_wake_at"] = None  # future-only guard dropped the due
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_elapsed_fresh_scan_later_g115_3060():
    # Same drop-out, but the fresh scan reports a LATER goal (not None) as the new
    # soonest wake. The stored baseline must still force the MISS.
    now = datetime.now()
    cache = _mk_cache(now, earliest_wake_at=_iso(now - timedelta(minutes=1)))
    args = _hit_args(now, cache)
    args["current_earliest_wake_at"] = _iso(now + timedelta(hours=3))
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_imminent_g115_3060():
    # A stored baseline wake WITHIN SLEEP_CAP_S (imminent, not yet elapsed) also
    # MISSes -- parity with the fresh-scan imminent check. This IS the 
    # invariant expressed on the stored baseline: a due within one horizon is caught
    # before it elapses.
    now = datetime.now()
    cache = _mk_cache(now,
                      earliest_wake_at=_iso(now + timedelta(seconds=qcc.SLEEP_CAP_S - 30)))
    args = _hit_args(now, cache)
    args["current_earliest_wake_at"] = None
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "baseline-timer-elapsed"


def test_evaluate_baseline_timer_far_future_is_hit_g115_3060():
    # The guard must NOT over-trigger: a stored baseline wake comfortably beyond the
    # imminent horizon (SLEEP_CAP_S) leaves the steady-quiescence HIT intact.
    now = datetime.now()
    cache = _mk_cache(now,
                      earliest_wake_at=_iso(now + timedelta(seconds=qcc.SLEEP_CAP_S + 120)))
    args = _hit_args(now, cache)
    args["current_earliest_wake_at"] = None
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "hit" and reason == "ok"


def test_evaluate_hash_changed():
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["current_hash"] = "deadbeef"  # blocker set changed
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "hash-changed"


def test_evaluate_hash_none():
    # Caller passed current_hash=None (cheap-gate short-circuit) -> miss, no
    # dereference of a None hash.
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["current_hash"] = None
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "hash-changed"


def test_evaluate_blocker_expired():
    now = datetime.now()
    cache = _mk_cache(now, blocker_refs=[
        {"external_id": "pq-old", "expires_at": _iso(now - timedelta(minutes=1))},
    ])
    decision, reason = qcc.evaluate_cache(**_hit_args(now, cache))
    assert decision == "miss" and reason.startswith("blocker-expired")


def test_evaluate_blocker_expiry_unparseable():
    now = datetime.now()
    cache = _mk_cache(now, blocker_refs=[
        {"external_id": "pq-bad", "expires_at": "not-a-timestamp"},
    ])
    decision, reason = qcc.evaluate_cache(**_hit_args(now, cache))
    assert decision == "miss" and reason == "blocker-expiry-unparseable"


def test_evaluate_ref_without_expiry_is_hit():
    # A blocker_ref with no expires_at must not block (None expiry == no gate).
    now = datetime.now()
    cache = _mk_cache(now, blocker_refs=[{"external_id": "pq-noexp"}])
    decision, _ = qcc.evaluate_cache(**_hit_args(now, cache))
    assert decision == "hit"


def test_evaluate_new_work():
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))  # cached goal_count 5
    args["current_goal_count"] = 6  # a new goal arrived
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason.startswith("new-work")


def test_evaluate_fewer_goals_is_hit():
    # Goals removed during the sleep (archival) must NOT block -- only NEW work
    # (count increase) does.
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["current_goal_count"] = 4  # fewer than cached 5
    decision, _ = qcc.evaluate_cache(**args)
    assert decision == "hit"


def test_evaluate_pending_signal():
    now = datetime.now()
    args = _hit_args(now, _mk_cache(now))
    args["pending_signal"] = True
    decision, reason = qcc.evaluate_cache(**args)
    assert decision == "miss" and reason == "pending-blocker-signal"


def test_evaluate_clean_hit():
    now = datetime.now()
    decision, reason = qcc.evaluate_cache(**_hit_args(now, _mk_cache(now)))
    assert decision == "hit" and reason == "ok"


# --- gate WRITER -> fast-path READER cross-module contract --------------------

def test_ssot_cache_name_matches():
    # The writer's filename constant MUST equal the reader's, or the gate
    # writes a file the fast path never reads.
    assert qg.CYCLE_CACHE_NAME == qcc.CACHE_NAME


def test_gate_writer_payload_shape(tmp_path, monkeypatch):
    monkeypatch.setattr(qg, "AGENT_DIR", tmp_path)
    (tmp_path / "session").mkdir()
    now = datetime.now()
    refs = [
        {"external_id": "pq-a", "expires_at": _iso(now + timedelta(hours=2))},
        {"external_id": "pq-b", "expires_at": _iso(now + timedelta(hours=3))},
        "not-a-dict",  # must be skipped by the writer's isinstance filter
    ]
    qg._write_cycle_cache("h1", refs, 600, 7, now, earliest_wake_at=None)

    written = json.loads(
        (tmp_path / "session" / qg.CYCLE_CACHE_NAME).read_text(encoding="utf-8"))
    assert written["blocker_set_hash"] == "h1"
    assert written["cycle_count"] == 0          # counter reset on every approval
    assert written["wake_outcome"] is None
    assert written["goal_count"] == 7
    assert written["sleep_seconds"] == 600
    assert "approved_at" in written
    assert "earliest_wake_at" in written
    # Only the 2 dict refs survive; each carries external_id + expires_at.
    assert len(written["blocker_refs"]) == 2
    assert {r["external_id"] for r in written["blocker_refs"]} == {"pq-a", "pq-b"}
    assert all("expires_at" in r for r in written["blocker_refs"])

    # The reader must accept what the writer produced as a HIT.
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    cache = qcc._read_cache()
    decision, reason = qcc.evaluate_cache(
        cache=cache, active_snapshot={"entered_at": _iso(now)},
        blocked_remaining=None, current_hash="h1", current_goal_count=7,
        pending_signal=False, current_earliest_wake_at=None, now=now)
    assert decision == "hit", reason


def test_gate_writer_failsoft_when_no_agent_dir(monkeypatch):
    # AGENT_DIR None -> no write, no exception (fail-soft contract).
    monkeypatch.setattr(qg, "AGENT_DIR", None)
    qg._write_cycle_cache("h1", [], 600, 1, datetime.now(),
                          earliest_wake_at=None)  # must not raise


def test_cache_write_read_round_trip_persists_increment(tmp_path, monkeypatch):
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    (tmp_path / "session").mkdir()
    now = datetime.now()
    qcc._write_cache(_mk_cache(now, cycle_count=1))
    again = qcc._read_cache()
    assert again["cycle_count"] == 1
    again["cycle_count"] += 1
    qcc._write_cache(again)
    assert qcc._read_cache()["cycle_count"] == 2


# --- headline integration: 3 identical hits then the cap forces a full cycle --

class _FakeGate(types.SimpleNamespace):
    """Stand-in for the quiescence-gate module inside cmd_check.

    Provides exactly the 4 attributes cmd_check dereferences off `qg`:
    _wm_read_loop_state, _collect_blocked_entries, _compute_hash,
    _total_goal_count. The hash is FIXED so it matches the seeded cache,
    making every cycle 'identical'.
    """


def _make_fake_gate(now, fixed_hash="abc123", goal_count=5):
    fg = _FakeGate()
    fg._wm_read_loop_state = lambda: {
        "signals": {"quiescence": {"active_snapshot": {"entered_at": _iso(now)}}}
    }
    fg._collect_blocked_entries = lambda: [
        {"goal_id": "g-x", "blocker_ref":
            {"external_id": "pq-x", "expires_at": _iso(now + timedelta(hours=2))}}
    ]
    fg._compute_hash = lambda refs: fixed_hash
    fg._total_goal_count = lambda: goal_count
    return fg


def test_cmd_check_three_hits_then_cap_forces_full(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    sess = tmp_path / "session"
    sess.mkdir()

    # Seed the cache exactly as the gate would on approval (cycle_count=0).
    cache0 = _mk_cache(now, cycle_count=0)
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    qcc._write_cache(cache0)

    # Hermetic stubs: no daemon (blocked_remaining), no real signal files,
    # and a fake gate module so no goal-selector import happens.
    monkeypatch.setattr(qcc, "_blocked_sleep_remaining", lambda now: None)
    monkeypatch.setattr(qcc, "_pending_blocker_signal", lambda: False)
    monkeypatch.setitem(sys.modules, "quiescence-gate", _make_fake_gate(now))
    # facet-2 (): the authoritative recheck runs on every HIT. Stub it to
    # agree (no imminent wake) so this pre-facet-2 HIT test is unchanged.
    monkeypatch.setattr(qcc, "authoritative_earliest_wake_at", lambda now, **kw: None)
    monkeypatch.delenv("QUIESCENCE_CACHE_CAP", raising=False)  # use DEFAULT_CAP=3

    # Cycles 1-3: identical blocker set -> HIT, cycle_count increments each time.
    for expected_after in (1, 2, 3):
        rc = qcc.cmd_check(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "=== QUIESCENCE CACHE HIT ===" in out, \
            f"cycle expected HIT (post-count {expected_after})"
        assert qcc._read_cache()["cycle_count"] == expected_after

    # Cycle 4: cycle_count is now 3 == cap -> cheap gate forces a MISS (full
    # cycle). No directive emitted, counter NOT advanced past the cap.
    rc = qcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== QUIESCENCE CACHE HIT ===" not in out, \
        "4th identical cycle must MISS (cap forces full reload)"
    assert qcc._read_cache()["cycle_count"] == 3


def _seed_quiescence_hit(tmp_path, monkeypatch, now):
    """Shared setup for the facet-2 cmd_check tests: seed a fresh cache + hermetic
    stubs so every cheap LOCAL gate would HIT. The caller then stubs
    authoritative_earliest_wake_at to drive the facet-2 recheck."""
    (tmp_path / "session").mkdir(exist_ok=True)
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    qcc._write_cache(_mk_cache(now, cycle_count=0))
    monkeypatch.setattr(qcc, "_blocked_sleep_remaining", lambda now: None)
    monkeypatch.setattr(qcc, "_pending_blocker_signal", lambda: False)
    monkeypatch.setitem(sys.modules, "quiescence-gate", _make_fake_gate(now))
    monkeypatch.delenv("QUIESCENCE_CACHE_CAP", raising=False)


def test_cmd_check_authoritative_imminent_forces_miss(tmp_path, monkeypatch, capsys):
    # facet-2 (): cheap LOCAL gates would HIT, but the AUTHORITATIVE store
    # shows a wake imminent within SLEEP_CAP_S -> MISS, counter frozen (guard-1139 /
    # : local mirror was stale about a sooner wake).
    now = datetime.now()
    _seed_quiescence_hit(tmp_path, monkeypatch, now)
    monkeypatch.setattr(qcc, "authoritative_earliest_wake_at",
                        lambda now, **kw: _iso(now + timedelta(seconds=30)))
    rc = qcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== QUIESCENCE CACHE HIT ===" not in out
    assert qcc._read_cache()["cycle_count"] == 0  # MISS -> counter NOT incremented


def test_cmd_check_authoritative_read_error_fails_open_to_miss(tmp_path, monkeypatch, capsys):
    # facet-2 fail-open: an unreadable authoritative store MISSes (full cycle),
    # never sleeps on the local-only decision -- guard-1139.
    now = datetime.now()
    _seed_quiescence_hit(tmp_path, monkeypatch, now)

    def _boom(now, **kw):
        raise RuntimeError("s3 unreachable")
    monkeypatch.setattr(qcc, "authoritative_earliest_wake_at", _boom)
    rc = qcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== QUIESCENCE CACHE HIT ===" not in out
    assert qcc._read_cache()["cycle_count"] == 0


def test_cmd_check_hash_change_misses(tmp_path, monkeypatch, capsys):
    now = datetime.now()
    (tmp_path / "session").mkdir()
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    qcc._write_cache(_mk_cache(now, cycle_count=0))  # cached hash abc123

    monkeypatch.setattr(qcc, "_blocked_sleep_remaining", lambda now: None)
    monkeypatch.setattr(qcc, "_pending_blocker_signal", lambda: False)
    # Fake gate reports a DIFFERENT live hash -> blocker set drifted.
    monkeypatch.setitem(sys.modules, "quiescence-gate",
                        _make_fake_gate(now, fixed_hash="deadbeef"))
    monkeypatch.delenv("QUIESCENCE_CACHE_CAP", raising=False)

    rc = qcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert "=== QUIESCENCE CACHE HIT ===" not in out
    # No short-circuit, and the counter was NOT touched on a miss.
    assert qcc._read_cache()["cycle_count"] == 0


def test_cmd_check_no_cache_is_cheap_miss(tmp_path, monkeypatch, capsys):
    # No cache file at all -> earliest cheap miss, no gate import attempted.
    (tmp_path / "session").mkdir()
    monkeypatch.setattr(qcc, "AGENT_DIR", tmp_path)
    rc = qcc.cmd_check(None)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == ""
