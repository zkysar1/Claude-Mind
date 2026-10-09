#!/usr/bin/env python3
"""Pins for the HIGH-goal pace forecast (, , core/scripts/pace_forecast.py).

THE RULE PINNED HERE: a session leaves a HIGH goal to faster sessions exactly when
k / R + P_fast < P_own, R being the goals a worker may take that the faster sessions live
NOW closed in the last P_own hours, and takes it whenever any input is missing. The worked
example in the module docstring is the first test, with the numbers measured
2026-10-07T05:25Z (zakcode Body workers 21.7 h per goal, Claude Code workers 1.2 h, 33
Claude Code closes in 21.7 h).

  1. The crossover lands where the arithmetic says, and the live count arms the forecast
     without scaling R.
  2. R counts closes inside the window, the window is P_own, and a close with no
     first-claim stamp still counts as throughput.
  3. Every missing input disarms the forecast, and a disarmed forecast takes.
  4. A pace is a MEDIAN of stamped worker closes, keyed on the close's harness, over the
     live and archive records with the live record winning.
  5. A live Body is one whose carrier body_hold calls live, whose state is active, whose
     agent is this one and whose harness is published.
  6. measure_basis wires the three readers together.
  7. Each walk's gate-firings row carries enough to recompute every decision in it, and
     logging never raises (the goal's check: "every forecast decision is logged with k,
     R, both finish times and the decision").
  8. R counts only sessions live now, and only goals a worker may take (g-375-157): a
     stopped or parked session's closes do not count, a goal counts when it is marked for a
     worker or a worker closed it (so a live reducer's own closes count only for goals marked
     for a worker), a sweep counts for whoever ran each goal, a field that is not text is
     judged and never raises, and the 2026-10-07 morning case, where the reducer closed
     unmarked goals, disarms.
  9. The reducer's walk applies the same forecast, to HIGH rows marked for a worker only,
     and writes a census the claim gate sanctions, and the reducer's pick runs it. An
     unmarked HIGH row is taken unforecast, and the rows below it are still judged: only a
     forecast take ends the walk. A ranking it yields whole reads as all-blocked,
     in the selector's own keys and with the selector's route marker, and the all-blocked
     handler's re-checks run the same walk, so a queue of yields never sends the loop
     straight back. A failed forecast shows every row, a missing session dir is said out
     loud, the walk imports on the standard library alone, and its forecast line prints
     before the ranking.

Sections 1-3 and 7 drive the arithmetic through _basis, which credits every close of a
harness to a live session of it: the case where the live set and R already agreed.

Hermetic: every store is a tmp file, no carrier is read from a backend, and the firing
is captured by a spy, never written.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import pace_forecast as pf  # noqa: E402

NOW = datetime(2026, 10, 7, 6, 0, 0)


def _basis(own, closes, live, now=NOW):
    """basis() with `live` as {harness: Bodies live} and every close of a harness credited to
    its first live session, so R counts exactly the harness's closes."""
    live_sids = {h: [f"{h}-{i}" for i in range(n)] for h, n in live.items()}
    session_closes = {sids[0]: [end for _start, end in closes.get(h, ())]
                      for h, sids in live_sids.items() if sids}
    return pf.basis(own, closes, live_sids, session_closes, now)


def _runs(n, hours, ago=1.0, step=0.0, start=True):
    """n closes of `hours` each, the newest ending `ago` hours before NOW and each
    next one `step` hours earlier. start=False drops the first-claim stamp."""
    out = []
    for i in range(n):
        end = NOW - timedelta(hours=ago + i * step)
        out.append((end - timedelta(hours=hours) if start else None, end))
    return out


# The 05:25Z reading: 33 Claude Code closes inside the 21.7 h window, zakcode's own
# closes long before it (they set its pace, not the rate).
CLOSES = {"zakcode": _runs(5, 21.7, ago=100),
          "claude-code": _runs(33, 1.2, ago=0.5, step=0.6)}


# --- 1. the worked example ------------------------------------------------------

def test_the_crossover_is_where_the_two_finish_times_meet():
    b = _basis("zakcode", CLOSES, {"claude-code": 11, "zakcode": 7}, NOW)
    assert b["armed"] and b["window_hours"] == 21.7
    assert (b["rate_per_hour"], b["fast_pace_hours"], b["own_pace_hours"]) == (33 / 21.7, 1.2, 21.7)
    assert b["faster"] == {"claude-code": {"live": 11, "pace_hours": 1.2, "closed_in_window": 33}}
    assert pf.forecast(1, b)["decision"] == "yield"
    assert pf.forecast(31, b)["decision"] == "yield"     # 31 / 1.5207 + 1.2 = 21.58
    assert pf.forecast(32, b)["decision"] == "take"      # 32 / 1.5207 + 1.2 = 22.24


def test_the_live_count_arms_the_forecast_but_does_not_scale_the_rate():
    """The first draft modelled R as live / pace: 11 / 1.2 = 9.2 an hour, six times what
    the same workers closed. R is now what they closed, whatever the live count."""
    one = _basis("zakcode", CLOSES, {"claude-code": 1}, NOW)
    many = _basis("zakcode", CLOSES, {"claude-code": 11}, NOW)
    assert one["rate_per_hour"] == many["rate_per_hour"] == 33 / 21.7


def test_a_verdict_carries_its_numbers():
    v = pf.forecast(3, _basis("zakcode", CLOSES, {"claude-code": 5}, NOW))
    assert v == {"decision": "yield", "k": 3, "fast_finish_hours": round(3 / (33 / 21.7) + 1.2, 2),
                 "own_finish_hours": 21.7, "reason": "faster sessions finish it first"}


def test_equal_finish_times_take():
    """The fast path must be strictly sooner: on a tie the worker in hand keeps it."""
    closes = {"zakcode": _runs(5, 3.0, ago=50),
              "claude-code": _runs(5, 1.0, ago=50) + _runs(3, 1.0, ago=0.5, step=0.5)}
    b = _basis("zakcode", closes, {"claude-code": 1}, NOW)
    assert b["rate_per_hour"] == 1.0            # 3 closes in the 3 h window
    assert pf.forecast(2, b)["decision"] == "take"      # 2 / 1 + 1 == 3
    assert pf.forecast(1, b)["decision"] == "yield"


# --- 2. the rate ------------------------------------------------------------------

def test_the_window_is_the_own_pace_and_its_edge_is_inclusive():
    since = NOW - timedelta(hours=21.7)
    edge = [(since - timedelta(hours=1.2), since),
            (since - timedelta(hours=1.2, seconds=1), since - timedelta(seconds=1))]
    closes = {"zakcode": _runs(5, 21.7, ago=100),
              "claude-code": _runs(5, 1.2, ago=100) + edge}
    b = _basis("zakcode", closes, {"claude-code": 2}, NOW)
    assert b["faster"]["claude-code"]["closed_in_window"] == 1
    assert b["rate_per_hour"] == 1 / 21.7


def test_a_close_with_no_first_claim_counts_as_throughput_but_not_as_pace():
    closes = {"zakcode": _runs(5, 21.7, ago=100),
              "claude-code": _runs(5, 1.2, ago=100) + _runs(3, 0, ago=1, step=1, start=False)}
    assert pf.harness_paces(closes)["claude-code"] == {"median_hours": 1.2, "n": 5}
    b = _basis("zakcode", closes, {"claude-code": 1}, NOW)
    assert b["faster"]["claude-code"]["closed_in_window"] == 3


def test_a_tiny_rate_is_kept_exact_and_never_divides_by_zero():
    """A rounded R could reach 0.0 and raise in forecast (found by the fresh-eyes review):
    one close in a 6000 h window is 1/6000 an hour, not 0."""
    closes = {"zakcode": _runs(5, 6000, ago=7000),
              "claude-code": _runs(5, 1.0, ago=6500) + _runs(1, 1.0, ago=1)}
    b = _basis("zakcode", closes, {"claude-code": 1}, NOW)
    assert b["armed"] and b["rate_per_hour"] == 1 / 6000
    assert pf.forecast(1, b)["decision"] == "take"      # 6000 + 1 > 6000


def test_two_faster_harnesses_add_their_closes_and_the_slower_pace_is_used():
    closes = dict(CLOSES, other=_runs(5, 2.0, ago=1, step=1))
    b = _basis("zakcode", closes, {"claude-code": 2, "other": 2}, NOW)
    assert b["rate_per_hour"] == (33 + 5) / 21.7
    assert b["fast_pace_hours"] == 2.0, "the fast path is never flattered"


# --- 3. a missing input disarms -------------------------------------------------

@pytest.mark.parametrize("own, closes, live, reason", [
    ("", CLOSES, {"claude-code": 5}, "this session's harness is unknown"),
    ("zakcode", dict(CLOSES, zakcode=_runs(4, 21.7, ago=100)), {"claude-code": 5},
     "the zakcode pace has fewer than 5 worker closes"),
    ("zakcode", dict(CLOSES, **{"claude-code": _runs(4, 1.2, ago=1)}), {"claude-code": 5},
     "no live session is measured faster"),
    ("claude-code", CLOSES, {"zakcode": 7}, "no live session is measured faster"),
    ("zakcode", CLOSES, {"zakcode": 7}, "no live session is measured faster"),
    ("zakcode", CLOSES, {"claude-code": 0}, "no live session is measured faster"),
    ("zakcode", dict(CLOSES, **{"claude-code": _runs(9, 1.2, ago=30)}), {"claude-code": 5},
     "no live faster session closed a goal a worker may take in the last 21.7 h"),
])
def test_every_missing_input_disarms_and_a_disarmed_forecast_takes(own, closes, live, reason):
    b = _basis(own, closes, live, NOW)
    assert (b["armed"], b["reason"]) == (False, reason)
    assert pf.forecast(1, b) == {"decision": "take", "k": 1, "reason": reason}


# --- 4. paces from the world queue ------------------------------------------------

def _goal(gid, *, start, hours, harness="zakcode", role="worker", status="completed", sid=None,
          **fields):
    end = datetime.fromisoformat(start) + timedelta(hours=hours)
    goal = {"id": gid, "status": status, "started": start,
            "completed_at": end.strftime("%Y-%m-%dT%H:%M:%S"),
            "completed_by_role": role, "completed_by_harness": harness, **fields}
    if sid:
        # The usual close: the session that ran the goal closes it. A sweep passes its own
        # executed_by_sid, which wins.
        goal.setdefault("completed_by_sid", sid)
        goal.setdefault("executed_by_sid", sid)
    return goal


def _world(tmp_path, live_goals, archive_goals=()):
    world = tmp_path / "world"
    world.mkdir()
    (world / "aspirations.jsonl").write_text(
        json.dumps({"id": "asp-1", "goals": list(live_goals)}) + "\n", encoding="utf-8")
    if archive_goals:
        (world / "aspirations-archive.jsonl").write_text(
            json.dumps({"id": "asp-0", "goals": list(archive_goals)}) + "\n", encoding="utf-8")
    return world


def test_worker_closes_and_paces_come_from_stamped_worker_closes(tmp_path):
    s = "2026-10-01T00:00:00"
    no_start = _goal("g-1-10", start=s, hours=1)
    no_start.pop("started")
    world = _world(tmp_path, [
        _goal("g-1-01", start=s, hours=1), _goal("g-1-02", start=s, hours=2),
        _goal("g-1-03", start=s, hours=90),           # one long tail does not move a median
        _goal("g-1-04", start=s, hours=1, harness="claude-code"),
        _goal("g-1-05", start=s, hours=500, role=""),           # not a worker close
        _goal("g-1-06", start=s, hours=500, harness=""),        # unstamped: unknown
        _goal("g-1-07", start=s, hours=500, status="pending"),  # not closed
        dict(_goal("g-1-08", start=s, hours=1), completed_at="not a time"),
        dict(_goal("g-1-09", start=s, hours=1), completed_at=s),  # zero length
        no_start,
    ])
    closes, by_session = pf.read_closes(world)
    assert {h: len(v) for h, v in closes.items()} == {"zakcode": 5, "claude-code": 1}, \
        "g-1-09 and g-1-10 are closes: throughput, though they carry no duration"
    assert pf.harness_paces(closes) == {"claude-code": {"median_hours": 1.0, "n": 1},
                                        "zakcode": {"median_hours": 2.0, "n": 3}}
    assert by_session == {}, "no session was asked for"


def test_the_archive_counts_and_the_live_record_wins_a_goal_in_both(tmp_path):
    s = "2026-09-20T00:00:00"
    world = _world(tmp_path,
                   [_goal("g-2-01", start=s, hours=4)],
                   [_goal("g-2-01", start=s, hours=400), _goal("g-2-02", start=s, hours=6)])
    assert pf.harness_paces(pf.read_closes(world)[0]) == {"zakcode": {"median_hours": 5.0, "n": 2}}


def test_session_closes_are_the_asked_sessions_closes_of_work_a_worker_may_take(tmp_path):
    """R's input: by the sid of the session that ran each goal, and only goals a worker may take,
    marked for one or closed by one. The reducer's own closes carry no role or harness stamp, so
    its close of an unmarked goal says nothing and counts for nobody, and a goal only the reducer
    may take never counts, whoever closed it. "Marked" is worker_execute's verdict, not the role
    field alone: a skill a worker may run marks a goal, and a reducer skill beside a worker role
    makes it reducer-only."""
    s = "2026-10-06T00:00:00"
    world = _world(tmp_path, [
        _goal("g-5-01", start=s, hours=1, sid="r1", role=None, harness=None,
              executable_by_role="worker"),
        _goal("g-5-02", start=s, hours=2, sid="r1", role=None, harness=None, skill="/reflect"),
        _goal("g-5-03", start=s, hours=3, sid="r1", role=None, harness=None,
              executable_by_role="reducer"),
        _goal("g-5-04", start=s, hours=4, sid="w1", harness="claude-code"),   # a worker's: counts
        _goal("g-5-05", start=s, hours=5, sid="w2", harness="claude-code"),   # not asked for
        _goal("g-5-06", start=s, hours=6, sid="r1", status="pending"),        # not closed
        _goal("g-5-07", start=s, hours=7, sid="r1", role=None, harness=None),  # unmarked
        _goal("g-5-08", start=s, hours=8, sid="w1", harness="claude-code", skill="/reflect"),
        _goal("g-5-09", start=s, hours=9, sid="r1", role=None, harness=None,
              executable_by_role="worker", skill="/reflect"),                 # contradicted
        _goal("g-5-10", start=s, hours=10, sid="r1", role=None, harness=None, skill="/tree"),
    ])
    closes, by_session = pf.read_closes(world, ["r1", "w1", ""])
    assert by_session == {"r1": [datetime(2026, 10, 6, 1, 0), datetime(2026, 10, 6, 10, 0)],
                          "w1": [datetime(2026, 10, 6, 4, 0)]}
    assert {h: len(v) for h, v in closes.items()} == {"claude-code": 3}, \
        "the paces still come from every stamped worker close, asked for or not"


def test_a_close_counts_for_the_session_that_ran_it(tmp_path):
    """A sweep that closes another session's work credits that session, never the sweeper, and a
    goal no session ran counts for none. The sweeper's own close is the control. Every goal is
    marked for a worker, so the crediting alone decides."""
    s = "2026-10-06T00:00:00"
    worker = {"role": None, "harness": None, "executable_by_role": "worker"}
    world = _world(tmp_path, [
        _goal("g-5-11", start=s, hours=1, sid="r1", **worker),
        _goal("g-5-12", start=s, hours=2, sid="r1", executed_by_sid="w1", **worker),
        _goal("g-5-13", start=s, hours=3, sid="r1", executed_by_sid=None, **worker),
    ])
    assert pf.read_closes(world, ["r1", "w1"])[1] == {
        "r1": [datetime(2026, 10, 6, 1, 0)], "w1": [datetime(2026, 10, 6, 2, 0)]}


def test_a_field_that_is_not_text_is_judged_never_raised(tmp_path):
    """goal_eligibility strips each field. A value that is not text reaches it as text and reads
    as unrecognised, its documented fallback, so one bad record cannot take a walk or R down."""
    for bad in ({"executable_by_role": True}, {"executable_by_role": ["worker"]}, {"source": 7},
                {"skill": ["/reflect"]}):
        assert pf._worker_word(dict(bad)) in {"eligible", "undetermined", "reducer-only"}, bad
    s = "2026-10-06T00:00:00"
    world = _world(tmp_path, [_goal("g-5-21", start=s, hours=1, sid="w1", executable_by_role=True)])
    assert pf.read_closes(world, ["w1"])[1] == {"w1": [datetime(2026, 10, 6, 1, 0)]}


def test_an_offset_timestamp_is_read_as_utc():
    assert pf._stamp("2026-10-07T02:00:00+02:00") == datetime(2026, 10, 7, 0, 0, 0)
    assert pf._stamp("2026-10-07T00:00:00Z") == datetime(2026, 10, 7, 0, 0, 0)
    assert pf._stamp(None) is None and pf._stamp("") is None


# --- 5. live Bodies ----------------------------------------------------------------

def _carrier(sid, *, agent="alpha", harness="claude-code", state="active", age_min=5):
    ts = (NOW - timedelta(minutes=age_min)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"agent": agent, "sid": sid,
            "doc": {"sid": sid, "ts": ts, "body_state": state, "harness": harness}}


def test_only_live_active_bodies_of_this_agent_with_a_harness_count():
    rows = [
        _carrier("s1"), _carrier("s2"), _carrier("s3", harness="zakcode"),
        _carrier("s4", state="parked"),           # takes nothing until it resumes
        _carrier("s5", state=""),                 # an interactive session, no Body manifest
        _carrier("s6", age_min=101),              # stale past the carrier window
        _carrier("s7", agent="bravo"),            # another agent's queue
        _carrier("s8", harness=""),               # no harness published yet
        _carrier("s9", state="closed-stale"),
        {"agent": "alpha", "sid": "s10", "doc": {}},
        "not a row",
    ]
    assert pf.live_sessions(rows, "alpha", NOW) == {"claude-code": ["s1", "s2"], "zakcode": ["s3"]}


def test_a_carrier_written_for_another_sid_is_not_its_bodys():
    row = _carrier("s1")
    row["doc"]["sid"] = "someone-else"
    assert pf.live_sessions([row], "alpha", NOW) == {}


def test_own_harness_is_what_this_sessions_carrier_publishes():
    rows = [_carrier("s1", harness="claude-code"), _carrier("me", harness="zakcode")]
    assert pf.own_harness(rows, "me") == "zakcode"
    assert pf.own_harness(rows, "absent") == ""
    assert pf.own_harness(rows, "") == ""


# --- 6. the wiring ------------------------------------------------------------------

def test_measure_basis_reads_carriers_and_closes_once_each(tmp_path, monkeypatch):
    import _paths
    import worker_stall
    old = (NOW - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%S")
    recent = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S")
    world = _world(tmp_path, [_goal(f"g-3-{i:02d}", start=old, hours=20) for i in range(5)]
                   + [_goal(f"g-4-{i:02d}", start=recent, hours=1, harness="claude-code",
                            sid=("c1", "c2")[i % 2]) for i in range(5)])
    rows = [_carrier("me", harness="zakcode"), _carrier("c1"), _carrier("c2")]
    calls = []
    monkeypatch.setattr(worker_stall, "enumerate_carriers",
                        lambda root: (calls.append(root) or rows, {"read_via": "authoritative"}))
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setattr(_paths, "agents_root", lambda: tmp_path / "agents")
    b = pf.measure_basis("alpha", "me", now=NOW)
    assert calls == [tmp_path / "agents"]
    assert b["armed"] and b["own_harness"] == "zakcode" and b["own_pace_hours"] == 20.0
    assert b["faster"] == {"claude-code": {"live": 2, "pace_hours": 1.0, "closed_in_window": 5}}
    assert b["rate_per_hour"] == 0.25 and b["carriers_read_via"] == "authoritative"
    assert pf.forecast(1, b)["decision"] == "yield"       # 1 / 0.25 + 1 = 5 < 20


# --- 7. the log ---------------------------------------------------------------------

def _walk(b, ks):
    """worker_view's census entries for HIGH rows at places `ks`."""
    out = []
    for k in ks:
        v = pf.forecast(k, b)
        out.append({"goal_id": f"g-9-{k:02d}", "k": k, "decision": v["decision"],
                    "fast_finish_hours": v.get("fast_finish_hours")})
    return out


def test_a_logged_walk_lets_a_reader_recompute_every_decision():
    b = dict(_basis("zakcode", CLOSES, {"claude-code": 11}, NOW), carriers_read_via="authoritative")
    path, decision, extra = pf.walk_firing(b, _walk(b, range(1, 41)))
    assert (path, decision) == ("yielded", "block")
    assert (extra["high_rows"], extra["yielded"]) == (40, 31)
    logged = extra["basis"]
    assert logged["carriers_read_via"] == "authoritative" and "error" not in logged
    for goal_id, k, fast_finish, logged_decision in extra["forecasts"]:
        fast = k / logged["rate_per_hour"] + logged["fast_pace_hours"]
        assert fast_finish == round(fast, 2)
        assert logged_decision == ("yield" if fast < logged["own_pace_hours"] else "take"), goal_id
    json.dumps(extra)  # the row is stored verbatim, so it must serialize


@pytest.mark.parametrize("walk_basis, ks, want", [
    (_basis("zakcode", CLOSES, {"claude-code": 11}, NOW), [40, 50], ("all_taken", "pass")),
    (_basis("claude-code", CLOSES, {"zakcode": 7}, NOW), [1, 2], ("disarmed", "noop")),
    ({"armed": False, "error": "OSError: boom", "reason": "forecast unavailable (OSError: boom)"},
     [1], ("measure_failed", "fail_open")),
])
def test_each_path_maps_to_its_decision(walk_basis, ks, want):
    path, decision, extra = pf.walk_firing(walk_basis, _walk(walk_basis, ks))
    assert (path, decision) == want and extra["decision_path"] == path
    assert extra["high_rows"] == len(ks)
    if not walk_basis.get("armed"):
        assert "forecasts" not in extra, "a disarmed walk took every row for the logged reason"
        assert extra["basis"]["reason"] == walk_basis["reason"]


def test_log_walk_writes_one_firing_under_the_registered_id(monkeypatch):
    import _gate_log
    seen = []
    monkeypatch.setattr(_gate_log, "log", lambda *a, **kw: seen.append((a, kw)))
    b = _basis("zakcode", CLOSES, {"claude-code": 11}, NOW)
    pf.log_walk(b, _walk(b, [1, 40]), caller=pf.WALK_CALLER)
    ((args, kwargs),) = seen
    assert args == (pf.GATE_ID, "block")
    assert kwargs["caller"] == "pace_forecast.py walk", "the row names the walk that logged it"
    assert kwargs["trigger_matched"] == "yielded" and kwargs["extra"]["yielded"] == 1


def test_log_walk_never_raises(monkeypatch):
    import _gate_log

    def boom(*_a, **_kw):
        raise OSError("spool unwritable")

    monkeypatch.setattr(_gate_log, "log", boom)
    pf.log_walk(_basis("zakcode", CLOSES, {"claude-code": 1}, NOW), [], caller=pf.WALK_CALLER)
    pf.log_walk({"armed": True}, None, caller=pf.WALK_CALLER)  # a malformed call is swallowed too


def test_the_gate_id_is_registered_and_every_decision_is_valid():
    import yaml
    import _gate_log
    registry = yaml.safe_load(
        (CORE_SCRIPTS.parent / "config" / "gates.yaml").read_text(encoding="utf-8"))
    entries = [g for g in registry["gates"] if g.get("id") == pf.GATE_ID]
    assert len(entries) == 1, f"{pf.GATE_ID} not registered exactly once"
    assert entries[0]["instrumented"] is True
    assert entries[0]["script"] == "core/scripts/pace_forecast.py"
    assert {s.get("file") for s in entries[0]["sites"]} == {
        "core/scripts/worker_execute.py", "core/scripts/pace_forecast.py"}, "both walks are sites"
    assert set(pf.DECISION_BY_PATH) == {"yielded", "all_taken", "disarmed", "measure_failed"}
    for path, decision in pf.DECISION_BY_PATH.items():
        assert decision in _gate_log._VALID_DECISIONS, f"{path} -> {decision}"


# --- 8. R counts sessions live now, and only work a worker may take () -----------

def test_a_stopped_sessions_closes_stop_counting():
    """The 33 closes came from seven sessions. With two of them live, R is those two's closes
    alone: the live count no longer stands in for the five that stopped."""
    ends = [end for _start, end in CLOSES["claude-code"]]
    sids = [f"w{i}" for i in range(7)]
    by_session = {sid: ends[i::7] for i, sid in enumerate(sids)}
    two = pf.basis("zakcode", CLOSES, {"claude-code": ["w0", "w1"]}, by_session, NOW)
    assert two["faster"]["claude-code"] == {"live": 2, "pace_hours": 1.2, "closed_in_window": 10}
    assert two["rate_per_hour"] == 10 / 21.7
    seven = pf.basis("zakcode", CLOSES, {"claude-code": sids}, by_session, NOW)
    assert seven["rate_per_hour"] == 33 / 21.7, "positive control: all seven live is the whole R"


def _morning(tmp_path, monkeypatch, *, reducer_work, reducer_state="active", workers_back=0):
    """2026-10-07 from 04:15Z: seven Claude Code workers stopped, the Claude Code reducer r1
    live, this zakcode Body measuring. `reducer_work` is what r1 closed in the window."""
    import _paths
    import worker_stall
    old = (NOW - timedelta(hours=80)).strftime("%Y-%m-%dT%H:%M:%S")
    recent = (NOW - timedelta(hours=8)).strftime("%Y-%m-%dT%H:%M:%S")
    goals = ([_goal(f"g-6-{i:02d}", start=old, hours=21.7) for i in range(5)]
             + [_goal(f"g-7-{i:02d}", start=recent, hours=1.2, harness="claude-code",
                      sid=f"w{i % 7}") for i in range(33)]
             + [_goal(f"g-8-{i:02d}", start=recent, hours=1, role=None, harness=None, sid="r1",
                      **fields) for i, fields in enumerate(reducer_work)])
    world = _world(tmp_path, goals)
    rows = ([_carrier("me", harness="zakcode"), _carrier("r1", state=reducer_state)]
            + [_carrier(f"w{i}", age_min=5 if i < workers_back else 300) for i in range(7)])
    monkeypatch.setattr(worker_stall, "enumerate_carriers",
                        lambda root: (rows, {"read_via": "authoritative"}))
    monkeypatch.setattr(_paths, "WORLD_DIR", world)
    monkeypatch.setattr(_paths, "agents_root", lambda: tmp_path / "agents")
    return pf.measure_basis("alpha", "me", now=NOW)


ONLY_THE_REDUCERS = [{"skill": "/reflect"}, {"executable_by_role": "reducer"}, {"skill": "/reflect"}]
# What a reducer mostly closes: goals with neither a skill nor a role.
UNMARKED = [{}, {"skill": None}, {}]
MARKED_FOR_A_WORKER = [{"executable_by_role": "worker"}] * 3
# r1 closes three goals the stopped workers w0..w2 ran: a sweep.
SWEPT = [{"executed_by_sid": f"w{i}", "executable_by_role": "worker"} for i in range(3)]
SWEPT_UNMARKED = [{"executed_by_sid": f"w{i}"} for i in range(3)]
NOTHING_CLOSED = "no live faster session closed a goal a worker may take in the last 21.7 h"


@pytest.mark.parametrize("reducer_work, reducer_state, workers_back, live, closed, reason", [
    # The first version stayed armed on both, on the stopped workers' 33 closes; the second on
    # the reducer's closes of unmarked goals, which say nothing about a worker's places.
    (ONLY_THE_REDUCERS, "active", 0, 1, 0, NOTHING_CLOSED),
    (UNMARKED, "active", 0, 1, 0, NOTHING_CLOSED),
    (MARKED_FOR_A_WORKER, "active", 0, 1, 3, None),  # the reducer counts for goals marked so
    (UNMARKED, "parked", 0, None, None, "no live session is measured faster"),
    (ONLY_THE_REDUCERS, "active", 1, 2, 5, None),    # one worker back: its five closes
    # A sweep counts for whoever ran each goal: nobody live, then w0 for the one it ran...
    (SWEPT, "active", 0, 1, 0, NOTHING_CLOSED),
    (SWEPT, "active", 1, 2, 6, None),
    # ...and a sweep of an unmarked goal counts for nobody.
    (SWEPT_UNMARKED, "active", 1, 2, 5, None),
])
def test_the_morning_of_2026_10_07(tmp_path, monkeypatch, reducer_work, reducer_state,
                                   workers_back, live, closed, reason):
    b = _morning(tmp_path, monkeypatch, reducer_work=reducer_work, reducer_state=reducer_state,
                 workers_back=workers_back)
    if live is None:
        assert b["faster"] == {}
    else:
        assert b["faster"] == {"claude-code": {"live": live, "pace_hours": 1.2,
                                               "closed_in_window": closed}}
    if reason is not None:
        assert (b["armed"], b["reason"]) == (False, reason)
        assert pf.forecast(1, b)["decision"] == "take", "a disarmed forecast takes"
    else:
        assert b["armed"] and b["rate_per_hour"] == closed / 21.7


# --- 9. the reducer's walk () ------------------------------------------------------

def _full(i, *, priority="HIGH", **fields):
    """A full selector row, the reducer's shape: its priority only as raw's number. Marked for
    a worker, so the reducer may leave it to faster ones; executable_by_role=None gives the
    unmarked kind most goals are."""
    return {"goal_id": f"g-950-{i:02d}", "source": "world", "skill": None,
            "executable_by_role": "worker",
            "raw": {"priority": {"HIGH": 3, "MEDIUM": 2, "LOW": 1}[priority]}, **fields}


def _stub_pace(yield_through, calls=None):
    """pace(k) that yields places 1..yield_through and takes every later one."""
    def pace(k):
        if calls is not None:
            calls.append(k)
        if k <= yield_through:
            return {"decision": "yield", "k": k, "fast_finish_hours": float(k), "own_finish_hours": 20.0}
        return {"decision": "take", "k": k, "fast_finish_hours": 99.0, "own_finish_hours": 20.0}
    return pace


def test_the_reducer_walk_drops_yielded_high_rows_and_keeps_the_scorer_order():
    rows = [_full(0), _full(1, priority="MEDIUM"), _full(2), _full(3), _full(4)]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=3))
    assert [r["goal_id"] for r in kept] == ["g-950-01", "g-950-03", "g-950-04"]
    assert kept[0] is rows[1], "rows pass through as the selector wrote them"
    assert [(f["goal_id"], f["k"], f["decision"]) for f in census["forecasts"]] == [
        ("g-950-00", 1, "yield"), ("g-950-02", 3, "yield"), ("g-950-03", 4, "take")]
    assert (census["scorer_top"], census["scorer_top_yielded"], census["pace_yielded"]) == (
        "g-950-00", True, 2)
    assert census["rows"] == [{"goal_id": g} for g in ("g-950-01", "g-950-03", "g-950-04")]


def test_a_row_only_the_reducer_can_take_is_kept_never_forecast_and_never_a_place():
    calls = []
    rows = [_full(0, skill="/reflect"),               # a reducer lifecycle stage
            _full(1, executable_by_role="reducer"),   # declared reducer-only
            _full(2, source="agent"),                 # agent queue: open only on the claim box
            _full(3), _full(4)]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=1, calls=calls))
    assert calls == [1, 2], "the first row a worker may take is place 1"
    assert [r["goal_id"] for r in kept] == ["g-950-00", "g-950-01", "g-950-02", "g-950-04"]
    assert census["scorer_top_yielded"] is False and census["pace_yielded"] == 1


def test_the_reducer_never_leaves_an_unmarked_goal_to_the_workers():
    """Most goals carry neither a skill nor a role. If one is reducer-only work no field marks, a
    worker releases it, so leaving it would leave it to nobody: the reducer takes it unforecast.
    It is still a place k, so the marked row below it is forecast at place 2. The same rows
    marked for a worker are the control, and an unmarked MEDIUM row is a place k the same way."""
    calls = []
    rows = [_full(0, executable_by_role=None), _full(1)]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=9, calls=calls))
    assert (calls, kept, census["pace_yielded"]) == ([2], rows[:1], 1)
    calls.clear()
    kept, census = pf.reducer_view([_full(0), _full(1)], _stub_pace(yield_through=9, calls=calls))
    assert (calls, kept, census["pace_yielded"]) == ([1, 2], [], 2), "control: marked rows"
    calls.clear()
    rows = [_full(0, priority="MEDIUM", executable_by_role=None), _full(1)]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=9, calls=calls))
    assert calls == [2] and [r["goal_id"] for r in kept] == ["g-950-00"]


def test_after_a_yield_an_unmarked_high_row_is_kept_and_the_rows_below_it_are_judged():
    """A yield, then a row of every other kind, then a marked HIGH row. The rows a worker may not
    take or need not be forecast stay where they are, the unmarked HIGH row among them, and the
    marked HIGH row below them is still forecast, at place 4: an unforecast take proves nothing
    about later rows, only a forecast one does. Yielding through place 3 is the control, where
    place 4 is taken and the walk ends there."""
    rows = [_full(0), _full(1, priority="MEDIUM", executable_by_role=None),
            _full(2, executable_by_role="reducer"), _full(3, executable_by_role=None), _full(4)]
    calls = []
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=9, calls=calls))
    assert calls == [1, 4], "the unmarked HIGH row is place 3 and is never forecast"
    assert kept == rows[1:4]
    assert (census["scorer_top_yielded"], census["pace_yielded"]) == (True, 2)
    assert census["rows"] == [{"goal_id": r["goal_id"]} for r in rows[1:4]]
    calls.clear()
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=3, calls=calls))
    assert (calls, kept, census["pace_yielded"]) == ([1, 4], rows[1:], 1), "control: place 4 takes"


def test_the_walk_stops_judging_at_its_first_high_take(monkeypatch):
    """forecast() only grows with k, so after one HIGH take no later row can yield: the rest
    of the ranking passes through unjudged, which keeps the reducer's walk cheap."""
    judged = []
    real = pf._worker_word
    monkeypatch.setattr(pf, "_worker_word", lambda row: judged.append(row["goal_id"]) or real(row))
    rows = [_full(0, priority="MEDIUM"), _full(1), _full(2), _full(3, skill="/reflect")]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=0))
    assert judged == ["g-950-00", "g-950-01"] and kept == rows and census["walked"] == 2


def test_the_census_lists_at_most_census_rows_kept_rows():
    rows = [_full(i, priority="MEDIUM") for i in range(pf.CENSUS_ROWS + 5)]
    kept, census = pf.reducer_view(rows, _stub_pace(yield_through=0))
    assert len(kept) == len(rows) and len(census["rows"]) == pf.CENSUS_ROWS


def test_a_full_row_names_its_priority_through_raw():
    assert pf._priority({"raw": {"priority": 3}}) == "HIGH"
    assert pf._priority({"raw": {"priority": 2}}) == "MEDIUM"
    assert pf._priority({"priority": "high"}) == "HIGH"
    assert pf._priority({}) is None
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    assert 'PRIORITY_MAP = {"HIGH": 3, "MEDIUM": 2, "LOW": 1}' in src, "the mirrored map moved"
    assert pf._RAW_PRIORITY_NAMES == {3: "HIGH", 2: "MEDIUM", 1: "LOW"}


ARMED = {"own_harness": "zakcode", "own_pace_hours": 20.0, "window_hours": 20.0,
         "rate_per_hour": 1.0, "fast_pace_hours": 1.0, "armed": True,
         "faster": {"claude-code": {"live": 2, "pace_hours": 1.0, "closed_in_window": 20}},
         "reason": "live faster sessions closed 20 goal(s) a worker may take in the last 20 h, 2 live now"}
DISARMED = {"own_harness": "claude-code", "own_pace_hours": 1.2, "armed": False,
            "reason": "no live session is measured faster"}


def _run_walk(tmp_path, monkeypatch, capsys, raw, basis=ARMED, session=True):
    """walk_main on `raw` with a stand-in measurement and log; return (rc, stdout, stderr,
    census written or None, measurement calls, logged walks). session=False leaves the
    session dir uncreated."""
    import _paths
    measured, logged = [], []
    monkeypatch.setattr(pf, "measure_basis",
                        lambda agent, sid, now=None: measured.append((agent, sid)) or dict(basis))
    monkeypatch.setattr(pf, "log_walk", lambda b, f, *, caller: logged.append((b, f, caller)))
    sess = tmp_path / "sessions" / "sid-r"
    if session:
        sess.mkdir(parents=True)
    monkeypatch.setattr(_paths, "agent_session_dir", lambda agent, sid: tmp_path / "sessions" / sid)
    monkeypatch.setenv("MIND_AGENT", "alpha")
    monkeypatch.setenv("MIND_SID", "sid-r")
    rc = pf.walk_main(raw.encode("utf-8"))
    cap = capsys.readouterr()
    census = sess / "select-census.json"
    doc = json.loads(census.read_text(encoding="utf-8")) if census.exists() else None
    return rc, cap.out, cap.err, doc, measured, logged


def _load_claim_gate():
    spec = importlib.util.spec_from_file_location("scorer_verdict_gate_t",
                                                  CORE_SCRIPTS / "scorer-verdict-gate.py")
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    return gate


def test_a_yielded_top_is_hidden_and_its_census_sanctions_the_claim_at_the_gate(
        tmp_path, monkeypatch, capsys):
    rows = [_full(0), _full(1, priority="MEDIUM"), _full(2), _full(3, skill="/reflect")]
    rc, out, err, doc, measured, logged = _run_walk(
        tmp_path, monkeypatch, capsys, json.dumps(rows, indent=2) + "\n")
    assert rc == 0 and [r["goal_id"] for r in json.loads(out)] == ["g-950-01", "g-950-03"]
    assert measured == [("alpha", "sid-r")], "one measurement per walk"
    assert "2 HIGH row(s) left to faster sessions" in err
    assert (doc["walker"], doc["sid"], doc["scorer_top"], doc["scorer_top_yielded"]) == (
        "reducer", "sid-r", "g-950-00", True)
    assert doc["pace"] == ARMED and logged == [(ARMED, doc["forecasts"], pf.WALK_CALLER)]
    # The consumer reads this census exactly as it reads a worker's.
    gate = _load_claim_gate()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    verdict = {"top_goal_id": "g-950-00", "ts": now.strftime("%Y-%m-%dT%H:%M:%S")}
    path, code, _msg, event = gate._classify(verdict, "g-950-01", "", now,
                                             census=doc, census_now=now)
    assert (path, code, event["code"]) == ("walk_yielded_top", 0, "pace-yield")
    path, code, _msg, _event = gate._classify(verdict, "g-950-01", "", now,
                                              census=dict(doc, scorer_top_yielded=False),
                                              census_now=now)
    assert (path, code) == ("unsanctioned_deviation", 2), "negative control: no yield, no sanction"


def test_nothing_yielded_passes_the_selector_output_through_byte_for_byte(
        tmp_path, monkeypatch, capsys):
    """The everyday case: a Claude Code reducer, whose harness nothing beats. The pick's full
    rows print as json.dumps(indent=2) would print them, so the selector's brief `--top` shape,
    one compact row per line, is the one that shows a walk rewriting what it was given."""
    rows = [_full(0, priority="MEDIUM"), _full(1), _full(2)]
    raw = json.dumps(rows, indent=2) + "\n"
    rc, out, err, doc, measured, logged = _run_walk(tmp_path, monkeypatch, capsys, raw,
                                                    basis=DISARMED)
    assert (rc, out) == (0, raw)
    assert "HIGH-goal pace forecast: no live session is measured faster; 0 HIGH row(s)" in err
    assert (doc["scorer_top_yielded"], doc["pace_yielded"]) == (False, 0)
    assert len(logged) == 1 and logged[0][2] == pf.WALK_CALLER
    brief = "[\n" + ",\n".join(json.dumps(r) for r in rows) + "\n]\n"
    assert _run_walk(tmp_path / "brief", monkeypatch, capsys, brief, basis=DISARMED)[:2] == (
        0, brief)


def test_a_non_ascii_title_survives_both_paths(tmp_path, monkeypatch, capsys):
    """The walk writes UTF-8 bytes itself, so no console codec can refuse or rewrite a title."""
    title = "naïve — ünïcode ✓"
    raw = json.dumps([_full(0, priority="MEDIUM", title=title)], indent=2,
                     ensure_ascii=False) + "\n"
    assert _run_walk(tmp_path, monkeypatch, capsys, raw)[:2] == (0, raw)
    raw = json.dumps([_full(0), _full(1, priority="MEDIUM", title=title)], indent=2,
                     ensure_ascii=False) + "\n"
    rc, out, err, doc, measured, logged = _run_walk(tmp_path / "again", monkeypatch, capsys, raw)
    assert rc == 0 and [r["title"] for r in json.loads(out)] == [title]


def test_a_ranking_left_wholly_to_faster_sessions_reads_as_all_blocked(
        tmp_path, monkeypatch, capsys):
    """An empty list would read as an empty queue: the loop would generate work, re-check, find
    the yields again and re-enter at once. All-blocked sends it to the handler's wait instead."""
    rows = [_full(0, title="first"), _full(1)]
    rc, out, err, doc, measured, logged = _run_walk(
        tmp_path, monkeypatch, capsys, json.dumps(rows, indent=2) + "\n")
    report = json.loads(out)
    assert rc == 0 and report["all_blocked"] is True and report["candidates"] == []
    assert (report["blocked_count"], report["by_reason"]) == (2, {pf.YIELD_REASON: 2})
    assert [(b["goal_id"], b["title"], b["reason"]) for b in report["blocked_goals"]] == [
        ("g-950-00", "first", pf.YIELD_REASON), ("g-950-01", "", pf.YIELD_REASON)]
    assert report["blocked_goals"][0]["detail"] == (
        "faster sessions finish it first: in 2.0 h, against 20.0 h here")
    assert "2 HIGH row(s) left to faster sessions" in err
    assert (doc["scorer_top_yielded"], doc["rows"]) == (True, [])
    # Negative control: one row it may not judge keeps the list, and so the pick.
    rows.append(_full(2, priority="MEDIUM"))
    rc, out, *_ = _run_walk(tmp_path / "again", monkeypatch, capsys,
                            json.dumps(rows, indent=2) + "\n")
    assert [r["goal_id"] for r in json.loads(out)] == ["g-950-02"]


def test_the_all_yielded_report_has_the_keys_goal_selector_gives_its_own():
    """aspirations-select and the all-blocked handler read the report by the selector's keys, so
    they are pinned to the selector's source, not to a copy of them."""
    src = (CORE_SCRIPTS / "goal-selector.py").read_text(encoding="utf-8")
    at = src.index('"all_blocked": True')
    block = src[src.rindex("print(json.dumps({", 0, at):src.index("}, indent=2))", at)]
    selector_keys = set(re.findall(r'"(\w+)":', block))
    assert {"all_blocked", "blocked_goals", "detail"} <= selector_keys, "the block was not found"
    census = {"forecasts": [{"goal_id": "g-950-00", "k": 1, "decision": "yield",
                             "fast_finish_hours": 2.0}]}
    report = pf.all_yielded_report([_full(0)], census, ARMED)
    assert set(report) | set(report["blocked_goals"][0]) == selector_keys


def test_the_all_blocked_report_passes_through_and_writes_no_census(tmp_path, monkeypatch, capsys):
    raw = json.dumps({"all_blocked": True, "blocked_count": 2, "by_reason": {}}) + "\n"
    assert _run_walk(tmp_path, monkeypatch, capsys, raw)[:2] == (0, raw)
    rc, out, err, doc, measured, logged = _run_walk(tmp_path / "again", monkeypatch, capsys, raw)
    assert (doc, measured, logged) == (None, [], [])


def test_a_walk_that_fails_shows_every_row(tmp_path, monkeypatch, capsys):
    """Nothing in the forecast may cost the reducer its pick: a failure prints a warning and
    passes the selector's output through, as before the forecast existed."""
    raw = json.dumps([_full(0), _full(1, priority="MEDIUM")], indent=2) + "\n"
    rc, out, *_ = _run_walk(tmp_path, monkeypatch, capsys, raw)
    assert rc == 0 and out != raw, "control: this ranking yields its top"

    def boom(row):
        raise RuntimeError("eligibility unreadable")

    monkeypatch.setattr(pf, "_worker_word", boom)
    rc, out, err, doc, *_ = _run_walk(tmp_path / "again", monkeypatch, capsys, raw)
    assert (rc, out, doc) == (0, raw, None)
    assert "[pace-walk] WARNING: the HIGH-goal pace forecast failed (RuntimeError" in err


def test_a_missing_session_dir_is_said_out_loud(tmp_path, monkeypatch, capsys):
    """write_select_census skips a missing dir in silence, and the claim gate then refuses a
    claim below the yielded top with nothing to say why, so the walk says it. The ranking and the
    logged walk are the control run's: a missing census costs the pick nothing."""
    raw = json.dumps([_full(0), _full(1, priority="MEDIUM")], indent=2) + "\n"
    rc, out, err, doc, _, logged = _run_walk(tmp_path, monkeypatch, capsys, raw)
    assert doc is not None and "no select census written" not in err, "control: the dir exists"
    rc, out_missing, err, doc, _, logged_missing = _run_walk(
        tmp_path / "again", monkeypatch, capsys, raw, session=False)
    assert (rc, doc) == (0, None)
    assert (out_missing, logged_missing) == (out, logged), "the control's ranking and log"
    assert "[pace-walk] WARNING: no select census written (FileNotFoundError: no session dir" in err


def test_the_walk_starts_on_the_standard_library_alone():
    """A module the forecast needs that will not import must fail inside walk_main's guard, so
    importing the walk itself pulls in nothing else. Loading one on use is the control."""
    import os
    import subprocess
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import pace_forecast as pf; "
            "names = ('body_hold', 'worker_execute', 'worker_stall', '_paths', '_gate_log'); "
            "print(sorted(m for m in names if m in sys.modules)); "
            "pf.live_sessions([], 'a', None); print('body_hold' in sys.modules)")
    r = subprocess.run([sys.executable, "-c", code, str(CORE_SCRIPTS)], env=env,
                       capture_output=True, text=True, timeout=60)
    assert (r.returncode, r.stdout.split()) == (0, ["[]", "True"]), r.stderr[-400:]


def _spy_subprocess(monkeypatch):
    import subprocess
    calls = []

    def run(argv, **_kw):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_the_all_yielded_route_leaves_the_selectors_marker(tmp_path, monkeypatch, capsys):
    """dry-spin-guard tells a handler that ran from a narrated one by the marker the route leaves
    (g-357-88), so the walk's all-blocked route leaves it, before the report prints."""
    calls = _spy_subprocess(monkeypatch)
    order = []
    real_write = pf._write
    monkeypatch.setattr(pf, "_write", lambda data: order.append(len(calls)) or real_write(data))
    monkeypatch.setenv("GOAL_SELECTOR_ALLBLOCKED_MARKER", "1")
    rc, out, *_ = _run_walk(tmp_path, monkeypatch, capsys,
                            json.dumps([_full(0), _full(1)], indent=2) + "\n")
    assert rc == 0 and json.loads(out)["all_blocked"] is True
    assert calls == [[sys.executable, str(CORE_SCRIPTS / "loop-state-bump-counters.py"),
                      "--all-blocked-marker"]]
    assert order == [1], "the marker is written before the report prints"
    calls.clear()
    rc, out, *_ = _run_walk(tmp_path / "again", monkeypatch, capsys,
                            json.dumps([_full(0), _full(1, priority="MEDIUM")], indent=2) + "\n")
    assert isinstance(json.loads(out), list) and calls == [], "a kept row is a pick, not a route"


def test_under_pytest_the_marker_waits_for_its_opt_in(tmp_path, monkeypatch, capsys):
    """A fixture marker in a real deployment's loop_state would read as a live route, so pytest
    gets none unless it opts in, as goal-selector refuses it. The test above is the control."""
    calls = _spy_subprocess(monkeypatch)
    monkeypatch.delenv("GOAL_SELECTOR_ALLBLOCKED_MARKER", raising=False)
    rc, out, err, *_ = _run_walk(tmp_path, monkeypatch, capsys,
                                 json.dumps([_full(0)], indent=2) + "\n")
    assert json.loads(out)["all_blocked"] is True and calls == []
    assert "all_blocked marker SUPPRESSED under pytest" in err


def test_the_forecast_speaks_before_the_ranking(tmp_path, monkeypatch, capsys):
    """The full ranking can run to megabytes, and a line printed after it is the one a reader's
    view cuts, so the forecast's line goes first."""
    seen = []
    real_write = pf._write

    def spy(data):
        seen.append(capsys.readouterr().err)
        real_write(data)

    monkeypatch.setattr(pf, "_write", spy)
    _run_walk(tmp_path, monkeypatch, capsys,
              json.dumps([_full(0), _full(1, priority="MEDIUM")], indent=2) + "\n")
    assert len(seen) == 1 and "HIGH-goal pace forecast" in seen[0]


@pytest.mark.parametrize("raw, says", [("", "no selector output"), ("  \n", "no selector output"),
                                       ("[{not json", "is not JSON")])
def test_no_selector_output_fails_and_never_reads_as_an_empty_queue(
        raw, says, tmp_path, monkeypatch, capsys):
    rc, out, err, doc, measured, logged = _run_walk(tmp_path, monkeypatch, capsys, raw)
    assert (rc, out, doc, measured) == (3, "", None, []) and "[pace-walk] FATAL" in err
    assert says in err


def test_the_command_line_takes_walk_and_nothing_else():
    import os
    import subprocess
    # The whole MIND_ namespace goes (rb-2312): an inherited agent and sid would aim the
    # child's census write and gate log at a live session.
    env = {k: v for k, v in os.environ.items() if not k.startswith("MIND_")}
    script = str(CORE_SCRIPTS / "pace_forecast.py")
    ok = subprocess.run([sys.executable, script, "walk"], input="[]\n", env=env,
                        capture_output=True, text=True, timeout=60)
    assert (ok.returncode, ok.stdout) == (0, "[]\n")
    bad = subprocess.run([sys.executable, script], input="[]\n", env=env,
                         capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2 and "usage" in bad.stderr


def _selector_calls(skill):
    """Every line of a skill that runs the selector, in any spelling (`Bash: goal-selector.sh`,
    `bash core/scripts/goal-selector.sh`, a direct goal-selector.py), comment mark stripped."""
    text = (CORE_SCRIPTS.parent.parent / ".claude" / "skills" / skill
            / "SKILL.md").read_text(encoding="utf-8")
    return [ln.strip().lstrip("#").strip() for ln in text.splitlines()
            if "goal-selector" in ln and ("Bash:" in ln or "bash " in ln)]


def test_the_reducers_pick_pipes_the_selector_through_the_walk():
    """The call site is where this class of change goes wrong (guard-2783), so it is pinned: the
    skill's one selector call is the pipe, exactly."""
    calls = _selector_calls("aspirations-select")
    assert calls == ["Bash: goal-selector.sh | py -3 core/scripts/pace_forecast.py walk"]


def test_the_all_blocked_re_checks_ask_through_the_same_walk():
    """A raw re-check would list the yields as new work and send the loop straight back to a
    pick that hides them again (the dry spin of g-115-2084). So every selector call in the
    handler is the walk's pipe, exactly, or the `blocked` listing, which picks nothing."""
    piped = "Bash: goal-selector.sh | py -3 core/scripts/pace_forecast.py walk"
    calls = _selector_calls("aspirations-all-blocked")
    assert sorted(set(calls)) == ["Bash: goal-selector.sh blocked", piped]
    assert calls.count(piped) == 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
