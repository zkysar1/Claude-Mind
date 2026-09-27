"""test_agent_watchdog_release_train.py — ReleaseTrainProbe (agent-watchdog.py).

THE GAP THIS PROBE CLOSES (g-115-11017). promotion-runbook.md "Who cuts, and
WHEN" makes a >= 24h gap since the newest v* tag, with framework commits past
it, a FINDING to dispose of — and nothing surfaced it: a 38.8h gap went
unflagged on 2026-09-26 until a goal that needed the cut was picked up through
a directive.

The measurement itself is pinned against real git repos in
test_release_train.py; this file pins the PROBE's lifecycle around it (the
measurement is stubbed — guard-1094: no test writes production queue or board
state):

  1. Not the frontier -> inert, and says why.
  2. POSITIVE CONTROL — due with no open goal: critical event, files, posts.
  3. A lease another box already filed: dedup, fired, NO second board post.
  4. One event per episode; the fired latch re-validates on its cadence.
  5. A NEWER tag stalling retires the older tag's lease in the same tick.
  6. Clear path retires every open lease and emits `release_train_cleared`.
  7. Unmeasured -> files nothing, retires nothing.
  8. Filing shape — argv + JSON body via a captured subprocess.run.
  9. Retire shape — pending+unclaimed only, outcome_note BEFORE status.
 10. State round-trips; registered for the reducer only.
 11. iteration-close.sh wires the in-turn nudge AFTER the tick, stdout unredirected.
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _release_train as rt  # noqa: E402


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_release_train", CORE_SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()
CFG = {"stale_hours": 24, "ticks_to_file": 1, "ticks_to_revalidate": 50}


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.agent_name = "testagent"
        self.project_root_path = root
        self.agent_dir = root / "agents" / "testagent"


def _measure(tag="v2.12.84", age=30.0, n=5, error=None):
    return {"newest_tag": tag, "tag_created": "2026-09-26T07:44:25Z",
            "tag_age_hours": age, "commits_past": n,
            "commit_sample": ["abc1234 fix: something"] if n else [],
            "fetch_age_minutes": 3.0, "basis": "origin/main", "error": error}


def _goal(tag, gid="g-115-90001", status="pending", claimed_by=None):
    return {"id": gid, "status": status, "claimed_by": claimed_by,
            "origin_signal": rt.signal_for(tag), "_source": "world"}


def _probe(monkeypatch, tmp_path, *, role="frontier", measure=None, goals=None,
           cfg=None, stub=True):
    state = {"measure": measure or _measure(), "goals": list(goals or [])}
    monkeypatch.setattr(rt, "self_role", lambda world: role)
    monkeypatch.setattr(rt, "config", lambda *a, **k: dict(cfg or CFG))
    monkeypatch.setattr(rt, "framework_paths", lambda: ["core/scripts"])
    monkeypatch.setattr(rt, "measure", lambda root, paths, **k: state["measure"])
    monkeypatch.setattr(rt, "open_release_goals", lambda world, agent_dir=None: state["goals"])
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    p.state = state
    if stub:
        p.calls = {"file": [], "board": [], "retire": []}

        def fake_file(m, c, open_goals):
            p.calls["file"].append(m["newest_tag"])
            existing = [g["id"] for g in open_goals
                        if g.get("origin_signal") == rt.signal_for(m["newest_tag"])]
            if existing:
                return {"filed": False, "dedup": True, "goal_id": existing[0], "error": "dedup"}
            return {"filed": True, "goal_id": "g-test-01", "error": None}

        monkeypatch.setattr(p, "_file_release_goal", fake_file)
        monkeypatch.setattr(p, "_post_board_alert",
                            lambda m, v, g: (p.calls["board"].append(g) or
                                             {"posted": True, "msg_id": "msg-test"}))
        monkeypatch.setattr(p, "_retire_release_goals",
                            lambda goals, reason: (p.calls["retire"].append(
                                ([g["id"] for g in goals], reason)) or
                                {"attempted": True, "closed": [g["id"] for g in goals],
                                 "held": [], "detail": "closed"}))
    return p


# ── 1. frontier gate ─────────────────────────────────────────────────────────

def test_non_frontier_is_inert_and_says_why(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, role="downstream")
    called = []
    monkeypatch.setattr(rt, "measure", lambda *a, **k: called.append(1))
    assert p.check() == []
    assert called == []
    assert "self_role='downstream'" in p.last_reason


# ── 2-4. firing ──────────────────────────────────────────────────────────────

def test_positive_control_due_files_posts_and_fires(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path)
    events = p.check()
    assert len(events) == 1
    e = events[0]
    assert e.event == "release_train_stalled" and e.severity == "critical"
    assert p.calls["file"] == ["v2.12.84"]
    assert len(p.calls["board"]) == 1
    assert p.fired and p.fired_tag == "v2.12.84"
    assert e.payload["commits_past"] == 5 and e.payload["goal"]["goal_id"] == "g-test-01"


def test_discrimination_not_due_is_silent(monkeypatch, tmp_path):
    for m in (_measure(age=23.9), _measure(n=0)):
        p = _probe(monkeypatch, tmp_path, measure=m)
        assert p.check() == []
        assert p.calls["file"] == [] and p.calls["retire"] == []


def test_lease_filed_by_another_box_dedups_without_a_second_post(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, goals=[_goal("v2.12.84", gid="g-115-55555")])
    events = p.check()
    assert len(events) == 1 and events[0].payload["goal"]["dedup"] is True
    assert events[0].payload["goal"]["goal_id"] == "g-115-55555"
    assert p.calls["board"] == []
    assert p.fired


def test_one_event_per_episode_then_revalidates(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, cfg=dict(CFG, ticks_to_revalidate=3))
    assert len(p.check()) == 1
    assert p.check() == []
    assert len(p.check()) == 1          # tick 3: 3 % 3 == 0 re-validates
    assert p.calls["file"] == ["v2.12.84", "v2.12.84"]


def test_newer_tag_stalling_retires_the_older_lease(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, measure=_measure(tag="v2.12.85"),
               goals=[_goal("v2.12.84", gid="g-old")])
    p.fired, p.fired_tag = True, "v2.12.84"
    events = p.check()
    assert p.calls["retire"] == [(["g-old"], "a newer tag, v2.12.85, has been cut since "
                                             "this goal was filed")]
    assert p.calls["file"] == ["v2.12.85"]          # new episode, new lease
    assert len(events) == 1 and p.fired_tag == "v2.12.85"


# ── 6-7. clearing / unmeasured ───────────────────────────────────────────────

def test_clear_path_retires_every_open_lease(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, measure=_measure(tag="v2.12.85", age=1.0),
               goals=[_goal("v2.12.84")])
    p.fired, p.fired_tag = True, "v2.12.84"
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "release_train_cleared" and events[0].severity == "info"
    assert p.calls["retire"][0][0] == ["g-115-90001"]
    assert "v2.12.85 is 1.0h old" in p.calls["retire"][0][1]
    assert (p.fired, p.fired_tag, p.consecutive_breach) == (False, None, 0)


def test_quiet_when_never_fired_and_nothing_open(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, measure=_measure(age=1.0))
    assert p.check() == []
    assert p.calls["retire"] == []


def test_unmeasured_files_and_retires_nothing(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, measure=_measure(error="git tag rc=128: boom"),
               goals=[_goal("v2.12.84")])
    p.fired = True
    assert p.check() == []
    assert p.calls["file"] == [] and p.calls["retire"] == []
    assert p.fired is True                        # an unmeasured tick changes nothing
    assert p.last_reason.startswith("unmeasured:")


# ── 8. filing shape ──────────────────────────────────────────────────────────

def test_filing_shape(monkeypatch, tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"id": "g-115-99999"})
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["body"] = json.loads(kw["input"])
        return _Proc()

    monkeypatch.setattr(WD.subprocess, "run", fake_run)
    res = p._file_release_goal(_measure(), CFG, [])
    assert res == {"filed": True, "goal_id": "g-115-99999", "error": None}
    argv = captured["argv"]
    assert argv[1:3] == ["core/scripts/aspirations-add-goal.sh", WD.ESCALATION_ASP]
    assert argv[3:5] == ["--source", WD.ESCALATION_SOURCE]
    assert "--override-duplication" in argv
    body = captured["body"]
    assert body["origin_signal"] == rt.signal_for("v2.12.84")
    assert body["title"].startswith("Investigate: release train stalled - v2.12.84 is 30.0h old")
    assert body["priority"] == "HIGH" and body["participants"] == ["agent"]
    assert body["executable_by_role"] == "reducer"
    d = body["description"]
    # The re-measure command a reader is told to run is the one that exists
    # and reports this signal (guard-3727) — test_release_train.py runs it.
    assert "bash core/scripts/release-train-check.sh" in d
    assert "promotion-runbook.md" in d and "guard-5583" in d and "never --tags" in d
    assert "bash core/scripts/release.sh patch" in d
    assert "abc1234 fix: something" in d


def test_filing_dedups_on_the_exact_signal_without_running_anything(monkeypatch, tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    ran = []
    monkeypatch.setattr(WD.subprocess, "run", lambda *a, **k: ran.append(a))
    res = p._file_release_goal(_measure(), CFG, [_goal("v2.12.84", gid="g-x")])
    assert res["dedup"] is True and res["goal_id"] == "g-x" and ran == []


def test_board_post_shape(monkeypatch, tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    captured = {}

    class _Proc:
        returncode = 0
        stdout = "msg-1"
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"], captured["text"] = argv, kw["input"]
        return _Proc()

    monkeypatch.setattr(WD.subprocess, "run", fake_run)
    res = p._post_board_alert(_measure(), {"due": True, "reason": "r"}, {"goal_id": "g-1"})
    assert res == {"posted": True, "msg_id": "msg-1"}
    argv = captured["argv"]
    assert argv[1] == "core/scripts/board-post.sh"
    assert argv[argv.index("--channel") + 1] == "coordination"
    assert "v2.12.84" in argv[argv.index("--tags") + 1]
    assert "g-1" in captured["text"]


# ── 9. retire shape ──────────────────────────────────────────────────────────

def test_retire_pending_unclaimed_only_note_before_status(monkeypatch, tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    calls = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(WD.subprocess, "run", lambda argv, **kw: calls.append(argv) or _Proc())
    goals = [_goal("v2.12.84", gid="g-a"),
             _goal("v2.12.83", gid="g-b", claimed_by="bravo"),
             _goal("v2.12.82", gid="g-c", status="in-progress")]
    goals[0]["_source"] = "agent"
    res = p._retire_release_goals(goals, "v2.12.85 is 1.0h old")
    assert res["closed"] == ["g-a"]
    assert res["held"] == ["g-b:pending/claimed", "g-c:in-progress"]
    assert [c[2:4] for c in calls] == [["g-a", "outcome_note"], ["g-a", "status"]]
    assert calls[1][4] == "skipped"
    assert calls[0][-2:] == ["--source", "agent"]
    assert "v2.12.85 is 1.0h old" in calls[0][4]


def test_retire_reports_a_failed_close_as_held(monkeypatch, tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))

    class _Fail:
        returncode = 1
        stdout = ""
        stderr = "daemon down"

    monkeypatch.setattr(WD.subprocess, "run", lambda argv, **kw: _Fail())
    res = p._retire_release_goals([_goal("v2.12.84", gid="g-a")], "r")
    assert res["closed"] == [] and res["held"] == ["g-a:close-failed"]


# ── 10. state + registration ─────────────────────────────────────────────────

def test_state_round_trips(tmp_path):
    p = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    p.consecutive_breach, p.fired, p.fired_tag, p.last_reason = 7, True, "v2.12.84", "r"
    q = WD.ReleaseTrainProbe(_Ctx(tmp_path))
    q.from_dict(json.loads(json.dumps(p.to_dict())))
    assert (q.consecutive_breach, q.fired, q.fired_tag, q.last_reason) == (7, True, "v2.12.84", "r")
    q.from_dict({"fired_tag": 3, "last_reason": None})
    assert q.fired_tag is None and q.fired is False


def test_registered_for_reducer_not_worker(tmp_path):
    reducer = WD.WatchdogContext(agent_name="t", agent_dir=tmp_path, project_root_path=tmp_path)
    assert "release-train" in [p.name for p in WD.build_probes(reducer)]
    worker = WD.WatchdogContext(agent_name="t", agent_dir=tmp_path, project_root_path=tmp_path,
                                body_role="worker")
    assert "release-train" not in [p.name for p in WD.build_probes(worker)]
    assert "release-train" not in WD.WORKER_SAFE_PROBES


# ── 11. wiring ───────────────────────────────────────────────────────────────

def test_iteration_close_wires_the_nudge_after_the_tick_unredirected():
    """A report surface with no caller is indistinguishable from one that
    always reports clean (reclaim-routed-work.md). Pin the call, its order,
    and that its stdout reaches the loop in-turn."""
    src = (CORE_SCRIPTS / "iteration-close.sh").read_text(encoding="utf-8")
    tick = src.index('agent-watchdog.py")" --tick')
    nudge = src.index('release-train-check.py")" --nudge')
    assert tick < nudge
    call = src[nudge:src.index("|| true", nudge)]
    assert re.search(r"(?<!2)>>", call) is None, "the nudge's stdout must not be redirected"
