"""test_agent_watchdog_dependency_funnel.py — DependencyFunnelProbe (agent-watchdog.py).

THE GAP THIS PROBE CLOSES. A fleet of N Bodies is at most as parallel as its
claimable frontier is wide, and until 2026-08-29 nothing measured the width.
That day a live 8-Body deployment held 15 pending goals, frontier 0, all gated
on ONE in-progress goal; five Bodies closed for lack of work and each one's
close message ("all goals dependency-blocked") was the only trace. The fix was
a hand-relaxation of four consumers to the root's interface — which is exactly
the remedy the probe's goal now prescribes, with the commands.

The census itself is pinned in test_frontier_census.py; this file pins the
PROBE's lifecycle around it (the census is stubbed here — guard-1094: no test
writes production queue or board state):

  1. Config — defaults come from aspirations.yaml (guard-308).
  2. POSITIVE CONTROL — a frontier-0 census fires critical, files, posts.
  3. DISCRIMINATION — frontier 1 is silent; frontier 0 under min_gated is
     silent (a nearly-empty queue is not a funnel).
  4. Gated only on unknown ids → info event, NO goal (nothing to relax).
  5. One event per episode — the second tick of the same funnel is silent.
  6. Clear path emits `dependency_funnel_cleared` and calls the retire path
     with an EMPTY keep-set (guard-3419 release).
  7. A moved funnel retires the earlier root's goal in the same tick.
  8. Filing shape — argv and JSON body, via a captured subprocess.run.
  9. Dedup — an open goal for the root short-circuits filing.
 10. Retire shape — pending+unclaimed only, outcome_note BEFORE status,
     --source from the goal's queue.
 11. State round-trips (tick mode persists across processes).
 12. Registered in build_probes for the reducer, NOT for a worker.
 13. A census failure is an info event, never a crash.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import _frontier  # noqa: E402


def _load_watchdog():
    spec = importlib.util.spec_from_file_location(
        "agent_watchdog_funnel", CORE_SCRIPTS / "agent-watchdog.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


WD = _load_watchdog()


class _Ctx:
    def __init__(self, root: Path) -> None:
        self.agent_name = "testagent"
        self.project_root_path = root
        self.agent_dir = root / "agents" / "testagent"


def _root(gid="g-006-03", **kw):
    r = {"id": gid, "title": f"Build: module for {gid}", "status": "in-progress",
         "asp_id": "asp-006", "source": "world", "claimed_by": "coach", "gates": 5}
    r.update(kw)
    return r


def _census(claimable=0, gated=5, roots=None, unknown=None, bodies=None):
    return {
        "claimable_count": claimable,
        "claimable": [f"g-0-{i}" for i in range(claimable)],
        "gated_count": gated,
        "gated": [f"g-006-{n:02d}" for n in range(6, 6 + gated)],
        "roots": [_root(gates=gated)] if roots is None else roots,
        "unknown_blockers": unknown or [],
        "pending_total": claimable + gated,
        "in_progress": 1, "deferred": 0, "blocked": 0, "user_only": 0, "recurring": 0,
        "active_aspirations": 1,
        "bodies": bodies or {"active": 3, "closed_recent": 5, "scanned": 8},
        "parse_skipped": 0, "stores_scanned": [],
    }


def _probe(monkeypatch, tmp_path, census, *, stub=True):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    monkeypatch.setattr(p, "_census", lambda cfg: census)
    if stub:
        p.calls = {"file": [], "board": [], "retire": []}
        monkeypatch.setattr(p, "_file_funnel_goal",
                            lambda c, r, cfg: (p.calls["file"].append((c, r)) or
                                               {"filed": True, "goal_id": "g-test-01", "error": None}))
        monkeypatch.setattr(p, "_post_board_alert",
                            lambda c, r, g: (p.calls["board"].append((r, g)) or
                                             {"posted": True, "msg_id": "msg-test"}))
        monkeypatch.setattr(p, "_retire_funnel_goals",
                            lambda keep_roots: (p.calls["retire"].append(set(keep_roots)) or
                                                {"attempted": False, "detail": None}))
    return p


# ── 1. config ────────────────────────────────────────────────────────────────

def test_config_defaults_come_from_aspirations_yaml():
    import yaml
    cfg_path = CORE_SCRIPTS.parent / "config" / "aspirations.yaml"
    with cfg_path.open(encoding="utf-8") as f:
        declared = (yaml.safe_load(f) or {}).get("dependency_funnel") or {}
    assert declared, "aspirations.yaml must declare a dependency_funnel block"
    got = WD._dependency_funnel_config()
    for key, value in declared.items():
        assert got[key] == value, f"{key}: probe read {got[key]}, config declares {value}"
    assert set(got) == {"min_gated", "ticks_to_file", "ticks_to_revalidate", "lookback_hours",
                        "liveness_hours"}


# ── 2-3. positive control and discrimination ─────────────────────────────────

def test_positive_control_frontier_zero_fires(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census(claimable=0, gated=5))
    events = p.check()
    assert len(events) == 1
    ev = events[0]
    assert (ev.probe, ev.event, ev.severity) == ("dependency-funnel", "dependency_funnel", "critical")
    assert ev.payload["gated_count"] == 5 and ev.payload["claimable_count"] == 0
    assert ev.payload["roots"][0]["id"] == "g-006-03"
    assert ev.payload["goal"]["goal_id"] == "g-test-01"
    assert ev.payload["board"]["posted"] is True
    assert "g-006-03" in ev.summary and "closed-recent=5" in ev.summary
    assert p.calls["file"][0][1]["id"] == "g-006-03"
    assert p.fired is True


def test_discrimination_frontier_one_is_silent(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census(claimable=1, gated=14))
    assert p.check() == []
    assert p.calls["file"] == []


def test_discrimination_under_min_gated_is_silent(monkeypatch, tmp_path):
    """Two goals waiting on one is the ordinary tail of an aspiration."""
    p = _probe(monkeypatch, tmp_path, _census(claimable=0, gated=2))
    assert p.check() == []
    assert p.calls["file"] == []


# ── 4. unresolvable ──────────────────────────────────────────────────────────

def test_gated_only_on_unknown_ids_is_info_not_a_goal(monkeypatch, tmp_path):
    c = _census(claimable=0, gated=4, roots=[], unknown=[("g-1-01", "g-9-99")])
    p = _probe(monkeypatch, tmp_path, c)
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "dependency_funnel_unresolvable"
    assert events[0].severity == "info"
    assert "g-9-99" in events[0].summary
    assert p.calls["file"] == [] and p.fired is False


# ── 5. one event per episode ─────────────────────────────────────────────────

def test_second_tick_of_same_funnel_is_silent(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census())
    assert len(p.check()) == 1
    assert p.check() == []
    assert len(p.calls["file"]) == 1
    assert p.consecutive == 2


# ── 6-7. clear + moved funnel ────────────────────────────────────────────────

def test_clear_path_emits_cleared_and_releases(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census())
    p.check()
    monkeypatch.setattr(p, "_census", lambda cfg: _census(claimable=4, gated=3))
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "dependency_funnel_cleared" and events[0].severity == "info"
    assert p.fired is False and p.consecutive == 0
    # The release path runs with NO roots kept — every funnel goal retires.
    assert p.calls["retire"][-1] == set()


def test_quiet_when_never_fired_and_nothing_to_retire(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census(claimable=7, gated=0))
    assert p.check() == []
    assert p.calls["retire"] == [set()]   # the release path still runs (box-local `fired` is not trusted)


def test_moved_funnel_keeps_only_the_new_root(monkeypatch, tmp_path):
    p = _probe(monkeypatch, tmp_path, _census(roots=[_root("g-1-01")]))
    p.check()
    assert p.calls["retire"][-1] == {"g-1-01"}


# ── 8-9. filing shape + dedup ────────────────────────────────────────────────

def test_filing_shape(monkeypatch, tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    captured = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps({"id": "g-006-99"})
        stderr = ""

    def fake_run(argv, **kw):
        captured["argv"] = argv
        captured["body"] = json.loads(kw["input"])
        return _Proc()

    monkeypatch.setattr(WD.subprocess, "run", fake_run)
    import pointer_freshness
    monkeypatch.setattr(pointer_freshness, "open_goal_exists", lambda *a, **k: False)
    res = p._file_funnel_goal(_census(), _root(source="bravo"), WD._dependency_funnel_config())
    assert res == {"filed": True, "goal_id": "g-006-99", "error": None}
    argv = captured["argv"]
    assert argv[1:4] == ["core/scripts/aspirations-add-goal.sh", "asp-006", "--source"]
    assert argv[4] == "bravo"
    assert "--override-duplication" in argv
    body = captured["body"]
    assert body["origin_signal"] == f"{_frontier.FUNNEL_SIGNAL_PREFIX}g-006-03"
    assert body["title"].startswith("Investigate: dependency funnel")
    assert "g-006-03" in body["title"] and "frontier is 0" in body["title"]
    assert body["priority"] == "HIGH" and body["participants"] == ["agent"]
    d = body["description"]
    # The remedy carries the exact commands an idle Body needs — not a hint.
    assert "aspirations-update-goal.sh <consumer-id> blocked_by '[]'" in d
    assert "frontier-check.sh" in d
    assert "interface stub" in d
    assert "never mark the root completed" in d
    assert "g-006-06" in d   # the gated ids are named


def test_dedup_short_circuits_filing(monkeypatch, tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    ran = []
    monkeypatch.setattr(WD.subprocess, "run", lambda *a, **k: ran.append(a))
    import pointer_freshness
    monkeypatch.setattr(pointer_freshness, "open_goal_exists", lambda *a, **k: True)
    res = p._file_funnel_goal(_census(), _root(), WD._dependency_funnel_config())
    assert res["dedup"] is True and res["filed"] is False
    assert ran == []


# ── 10. retire shape ─────────────────────────────────────────────────────────

def test_retire_pending_unclaimed_only_with_source(monkeypatch, tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    sig = _frontier.FUNNEL_SIGNAL_PREFIX
    index = {
        "g-1-01": {"id": "g-1-01", "status": "pending", "origin_signal": f"{sig}g-9-A", "_source": "world"},
        "g-1-02": {"id": "g-1-02", "status": "pending", "origin_signal": f"{sig}g-9-B", "_source": "bravo"},
        "g-1-03": {"id": "g-1-03", "status": "pending", "origin_signal": f"{sig}g-9-C",
                   "claimed_by": "alpha", "_source": "world"},
        "g-1-04": {"id": "g-1-04", "status": "in-progress", "origin_signal": f"{sig}g-9-D", "_source": "world"},
    }
    monkeypatch.setattr(_frontier, "load_goal_index", lambda w, a: (index, [], {}))
    calls = []

    class _Proc:
        returncode = 0
        stdout = "{}"
        stderr = ""

    monkeypatch.setattr(WD.subprocess, "run", lambda argv, **kw: (calls.append(argv) or _Proc()))
    res = p._retire_funnel_goals(keep_roots={"g-9-A"})
    assert res["closed"] == ["g-1-02"] and res["held"] == []
    # outcome_note BEFORE status, and --source from the goal's own queue.
    assert [c[2:4] for c in calls] == [["g-1-02", "outcome_note"], ["g-1-02", "status"]]
    assert calls[1][4] == "skipped"
    assert all(c[-2:] == ["--source", "bravo"] for c in calls)


def test_retire_reports_a_failed_close_as_held(monkeypatch, tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    sig = _frontier.FUNNEL_SIGNAL_PREFIX
    index = {"g-1-01": {"id": "g-1-01", "status": "pending",
                        "origin_signal": f"{sig}g-9-A", "_source": "world"}}
    monkeypatch.setattr(_frontier, "load_goal_index", lambda w, a: (index, [], {}))

    class _Proc:
        returncode = 1
        stdout = ""
        stderr = "refused"

    monkeypatch.setattr(WD.subprocess, "run", lambda argv, **kw: _Proc())
    res = p._retire_funnel_goals(keep_roots=set())
    assert res["closed"] == [] and res["held"] == ["g-1-01:close-failed"]


# ── 11-13. state, registration, fail-open ────────────────────────────────────

def test_state_round_trips(tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    p.consecutive, p.fired = 3, True
    q = WD.DependencyFunnelProbe(_Ctx(tmp_path))
    q.from_dict(json.loads(json.dumps(p.to_dict())))
    assert (q.consecutive, q.fired) == (3, True)
    q.from_dict("garbage")
    assert (q.consecutive, q.fired) == (3, True)


def test_registered_for_reducer_not_worker(tmp_path):
    reducer = WD.WatchdogContext(agent_name="t", agent_dir=tmp_path, project_root_path=tmp_path)
    names = [p.name for p in WD.build_probes(reducer)]
    assert "dependency-funnel" in names
    worker = WD.WatchdogContext(agent_name="t", agent_dir=tmp_path, project_root_path=tmp_path,
                                body_role="worker")
    assert "dependency-funnel" not in [p.name for p in WD.build_probes(worker)]
    assert "dependency-funnel" not in WD.WORKER_SAFE_PROBES


def test_census_failure_is_an_info_event_not_a_crash(monkeypatch, tmp_path):
    p = WD.DependencyFunnelProbe(_Ctx(tmp_path))

    def boom(cfg):
        raise RuntimeError("store unreadable")

    monkeypatch.setattr(p, "_census", boom)
    events = p.check()
    assert len(events) == 1
    assert events[0].event == "dependency_funnel_unmeasured" and events[0].severity == "info"
    assert "store unreadable" in events[0].payload["error"]


@pytest.mark.parametrize("gated", [3, 15])
def test_min_gated_boundary(monkeypatch, tmp_path, gated):
    """min_gated is inclusive: exactly 3 gated goals on frontier 0 fires."""
    p = _probe(monkeypatch, tmp_path, _census(claimable=0, gated=gated))
    assert len(p.check()) == 1
