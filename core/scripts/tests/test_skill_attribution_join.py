#!/usr/bin/env python3
"""Tests for the skill-attribution invocation->outcome join ().

Covers the discovery drift fix (agents_root routing + ledger marker), the
outcome resolver, the interval join, journal parsing, the end-to-end
compute_join, and skill-evaluate's reconsolidation candidate builder.

The two scripts have hyphenated filenames (not importable via `import`), so
they are importlib-loaded from their file paths. skill-attribution's
module-level `sys.path.insert(0, SCRIPT_DIR)` puts core/scripts on the path,
which is why it is loaded FIRST (skill-evaluate's `from _paths import ...`
depends on it).
"""
import importlib.util
import json
import os
import sys
import types

import pytest


def _load(fname, modname):
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), fname)
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sa = _load("skill-attribution.py", "skill_attribution")   # load first (adds core/scripts to path)
se = _load("skill-evaluate.py", "skill_evaluate")


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def _mk_agent(root, name, invocations, diary=None, journal_text=None):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "session"), exist_ok=True)
    _write_jsonl(os.path.join(d, "skill-invocations.jsonl"), invocations)
    if diary is not None:
        _write_jsonl(os.path.join(d, "session", "execution-diary.jsonl"), diary)
    if journal_text is not None:
        jd = os.path.join(d, "journal", "2026", "07")
        os.makedirs(jd, exist_ok=True)
        with open(os.path.join(jd, "2026-07-21.md"), "w", encoding="utf-8") as f:
            f.write(journal_text)
    return d


@pytest.fixture
def agents_root(tmp_path, monkeypatch):
    root = tmp_path / "agents"
    root.mkdir()
    monkeypatch.setattr(sa._paths, "agents_root", lambda: str(root))
    return str(root)


# --------------------------------------------------------------------------
# Discovery drift fix (the pre- depth-1 PROJECT_ROOT scan found ZERO)
# --------------------------------------------------------------------------

def test_find_agent_dirs_uses_agents_root_and_ledger_marker(agents_root):
    _mk_agent(agents_root, "aa", [{"ts": "2026-07-21T10:00:00", "skill": "reflect"}])
    _mk_agent(agents_root, "bb", [{"ts": "2026-07-21T10:00:00", "skill": "prime"}])
    # a dir WITHOUT a skill-invocations.jsonl ledger is NOT discovered
    os.makedirs(os.path.join(agents_root, "not-an-agent"))
    assert sa.find_agent_dirs() == ["aa", "bb"]


def test_read_invocations_reads_from_agents_root(agents_root):
    _mk_agent(agents_root, "aa", [
        {"ts": "2026-07-21T10:00:00", "skill": "reflect", "agent": "aa", "sid": "s1"},
    ])
    rows = sa.read_invocations("aa")
    assert len(rows) == 1 and rows[0]["skill"] == "reflect"


# --------------------------------------------------------------------------
# Outcome resolver (all five branches)
# --------------------------------------------------------------------------

def test_window_outcome_journal_success():
    assert sa._resolve_window_outcome("g-1-1", "t0", "t1", False,
                                      {"g-1-1": "deep"}, []) == "success"


def test_window_outcome_deferred_failure():
    assert sa._resolve_window_outcome("g-1-1", "t0", "t1", False,
                                      {"g-1-1": "deferred"}, []) == "failure"


def test_window_outcome_close_success():
    assert sa._resolve_window_outcome("g-1-1", "2026-07-21T10:00:00",
                                      "2026-07-21T10:30:00", False, {},
                                      ["2026-07-21T10:15:00"]) == "success"


def test_window_outcome_close_outside_window_not_success():
    # a close AFTER the window end does not belong to this goal
    assert sa._resolve_window_outcome("g-1-1", "2026-07-21T10:00:00",
                                      "2026-07-21T10:30:00", False, {},
                                      ["2026-07-21T10:45:00"]) == "failure"


def test_window_outcome_inflight_unknown():
    assert sa._resolve_window_outcome("g-1-1", "t0", None, True, {}, []) == "unknown"


def test_window_outcome_started_never_closed_failure():
    assert sa._resolve_window_outcome("g-1-1", "t0", "t1", False, {}, []) == "failure"


@pytest.mark.parametrize("status", ["completed", "decomposed"])
def test_window_outcome_store_status_clears_absence_failure(status):
    # No success signal on THIS box, but the store says the goal is done: absence is not
    # a failure (). Same call without the oracle stays a failure.
    args = ("g-1-1", "t0", "t1", False, {}, [])
    assert sa._resolve_window_outcome(*args) == "failure"
    assert sa._resolve_window_outcome(*args, goal_status={"g-1-1": status}) == "success"


@pytest.mark.parametrize("oracle", [
    {"g-1-1": "pending"}, {"g-1-1": "skipped"}, {"g-9-9": "completed"}, {}, None])
def test_window_outcome_store_status_only_clears_completed(oracle):
    # Not completed/decomposed, unknown to the store, empty oracle, no oracle: unchanged.
    assert sa._resolve_window_outcome("g-1-1", "t0", "t1", False, {}, [],
                                      goal_status=oracle) == "failure"


def test_window_outcome_explicit_deferred_survives_store_status():
    # 'deferred' is EVIDENCE of failure; only the absence-based failure is overruled.
    assert sa._resolve_window_outcome("g-1-1", "t0", "t1", False, {"g-1-1": "deferred"}, [],
                                      goal_status={"g-1-1": "completed"}) == "failure"


def test_window_outcome_inflight_stays_unknown_whatever_the_store_says():
    assert sa._resolve_window_outcome("g-1-1", "t0", None, True, {}, [],
                                      goal_status={"g-1-1": "completed"}) == "unknown"


# --------------------------------------------------------------------------
# Interval construction + locate
# --------------------------------------------------------------------------

def test_build_goal_windows_intervals():
    diary = [
        {"entry_type": "scorer_override", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "phase_start", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:05:00"},
        {"entry_type": "phase_start", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:30:00"},
    ]
    assert sa.build_goal_windows(diary) == [
        ("g-1-1", "2026-07-21T10:00:00", "2026-07-21T10:30:00"),
        ("g-1-2", "2026-07-21T10:30:00", None),
    ]


def test_build_goal_windows_ignores_rows_about_another_goal():
    # g-1-1 is executing; a finding and an observation ABOUT g-1-2 land mid-run. They must
    # neither split g-1-1's window nor mint a never-closed phantom window for g-1-2
    # (: the phantom-window class).
    diary = [
        {"entry_type": "phase_start", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "finding", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:10:00"},
        {"entry_type": "observation", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:12:00"},
        {"entry_type": "decision", "goal_id": "g-1-3", "timestamp": "2026-07-21T10:15:00"},
        {"entry_type": "phase_end", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:20:00"},
    ]
    assert sa.build_goal_windows(diary) == [("g-1-1", "2026-07-21T10:00:00", None)]


def test_build_goal_windows_ignores_non_goal_ids():
    # 'precheck' / 'none' are tokens, not goals: no window to score, so no phantom failure.
    diary = [
        {"entry_type": "phase_start", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "phase_start", "goal_id": "precheck", "timestamp": "2026-07-21T10:10:00"},
        {"entry_type": "phase_start", "goal_id": "none", "timestamp": "2026-07-21T10:20:00"},
    ]
    assert sa.build_goal_windows(diary) == [("g-1-1", "2026-07-21T10:00:00", None)]


def test_locate_invocation_before_first_is_unknown():
    wo = [("g-1-1", "t5", "t9", "success")]
    assert sa._locate_invocation("t1", wo) == ("unknown", None)


def test_locate_invocation_open_last_window_catches_later_ts():
    wo = [("g-1-1", "t0", "t5", "failure"), ("g-1-2", "t5", None, "unknown")]
    assert sa._locate_invocation("t9", wo) == ("unknown", "g-1-2")


# --------------------------------------------------------------------------
# Journal parsing
# --------------------------------------------------------------------------

def test_read_journal_outcomes(agents_root):
    txt = ("## 09:33 — Goal: g-315-435 (g-315-435)\nOutcome: deep\nValue: x\n\n"
           "## 11:06 — Goal: g-315-436\nOutcome: routine\n")
    _mk_agent(agents_root, "aa", [], journal_text=txt)
    out = sa.read_journal_outcomes("aa")
    assert out.get("g-315-435") == "deep"
    assert out.get("g-315-436") == "routine"


# --------------------------------------------------------------------------
# End-to-end compute_join
# --------------------------------------------------------------------------

def test_compute_join_end_to_end(agents_root):
    diary = [
        {"entry_type": "scorer_override", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "phase_start", "phase": "phase-4-execute", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:01:00"},
        {"entry_type": "scorer_override", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:30:00"},
        {"entry_type": "phase_end", "phase": "phase-12-productivity", "timestamp": "2026-07-21T10:45:00"},
        {"entry_type": "scorer_override", "goal_id": "g-1-3", "timestamp": "2026-07-21T11:00:00"},
    ]
    invs = [
        {"ts": "2026-07-21T10:02:00", "skill": "reflect", "agent": "aa", "sid": "s1"},  # g-1-1 -> failure
        {"ts": "2026-07-21T10:31:00", "skill": "reflect", "agent": "aa", "sid": "s1"},  # g-1-2 -> success
        {"ts": "2026-07-21T11:05:00", "skill": "prime", "agent": "aa", "sid": "s1"},    # g-1-3 in-flight -> unknown
        {"ts": "2026-07-21T09:00:00", "skill": "prime", "agent": "aa", "sid": "s1"},    # pre-goal -> unknown
    ]
    _mk_agent(agents_root, "aa", invs, diary=diary)
    join = sa.compute_join(["aa"])
    ps = join["per_skill"]
    assert ps["reflect"]["success"] == 1
    assert ps["reflect"]["failure"] == 1
    assert ps["reflect"]["classified"] == 2
    assert ps["reflect"]["success_rate"] == 0.5
    assert ps["prime"]["unknown"] == 2
    assert ps["prime"]["classified"] == 0
    assert ps["prime"]["success_rate"] is None
    assert any(f["skill"] == "reflect" and f["goal_id"] == "g-1-1" for f in join["failing"])


def test_compute_join_journal_success_overrides_no_close(agents_root):
    # goal with journal 'deep' but no diary close -> success (journal wins)
    diary = [
        {"entry_type": "scorer_override", "goal_id": "g-2-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "scorer_override", "goal_id": "g-2-2", "timestamp": "2026-07-21T10:30:00"},
    ]
    invs = [{"ts": "2026-07-21T10:05:00", "skill": "reflect", "agent": "aa", "sid": "s1"}]
    txt = "## 10:00 — Goal: g-2-1 (g-2-1)\nOutcome: deep\n"
    _mk_agent(agents_root, "aa", invs, diary=diary, journal_text=txt)
    join = sa.compute_join(["aa"])
    assert join["per_skill"]["reflect"]["success"] == 1
    assert join["per_skill"]["reflect"]["failure"] == 0


def test_compute_join_empty_agent_skipped(agents_root):
    # agent with a ledger but zero rows contributes nothing, no crash
    _mk_agent(agents_root, "aa", [])
    join = sa.compute_join(["aa"])
    assert join["per_skill"] == {}
    assert join["failing"] == []


# : two goals, g-1-1 never closed (a non-last window), g-1-2 open (last).
_STATUS_DIARY = [
    {"entry_type": "phase_start", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
    {"entry_type": "phase_start", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:30:00"},
]
_STATUS_INVS = [
    {"ts": "2026-07-21T10:05:00", "skill": "reflect", "agent": "aa", "sid": "s1"},  # g-1-1
    {"ts": "2026-07-21T10:06:00", "skill": "prime", "agent": "aa", "sid": "s1"},    # g-1-1
    {"ts": "2026-07-21T10:35:00", "skill": "prime", "agent": "aa", "sid": "s1"},    # g-1-2 open
]


def test_compute_join_reports_classified_invocations(agents_root):
    # classifiable_ceiling counts diary-SPAN membership, classified counts WINDOW
    # membership; they are different figures, so BOTH are emitted (zeta 2026-08-20: 6574
    # classified against a stated ceiling of 2023).
    _mk_agent(agents_root, "aa", _STATUS_INVS, diary=_STATUS_DIARY)
    cov = sa.compute_join(["aa"])["diary_coverage"]
    assert cov["invocations"] == 3
    assert cov["classifiable_ceiling"] == 2          # 10:05 and 10:06 sit inside the span
    assert cov["classified_invocations"] == 2        # both fall in g-1-1's (failed) window
    assert cov["ceiling_ratio"] == round(2 / 3, 4)


def test_compute_join_store_status_clears_failure_and_reports_controls(agents_root):
    _mk_agent(agents_root, "aa", _STATUS_INVS, diary=_STATUS_DIARY)

    # No oracle: today's behaviour, g-1-1 is scored a failure by absence.
    base = sa.compute_join(["aa"])
    assert base["per_skill"]["reflect"]["failure"] == 1
    assert base["goal_status_check"]["oracle_goals"] is None

    # The store says g-1-1 completed: its window is cleared, nothing is left failing.
    cleared = sa.compute_join(["aa"], goal_status={"g-1-1": "completed"})
    assert cleared["per_skill"]["reflect"] == {
        "success": 1, "failure": 0, "unknown": 0, "classified": 1, "success_rate": 1.0}
    assert cleared["failing"] == []
    assert cleared["goal_status_check"] == {
        "oracle_goals": 1, "windows_cleared": 1,
        "failure_windows": 0, "failure_windows_status_unknown": 0}

    # Status known and NOT completed: still a failure, and it is a CHECKED one.
    known = sa.compute_join(["aa"], goal_status={"g-1-1": "pending"})
    assert [f["goal_id"] for f in known["failing"]] == ["g-1-1", "g-1-1"]
    assert known["goal_status_check"]["failure_windows"] == 1
    assert known["goal_status_check"]["failure_windows_status_unknown"] == 0

    # A BLIND oracle (empty map) clears nothing and says every failure went unchecked.
    blind = sa.compute_join(["aa"], goal_status={})
    assert blind["goal_status_check"]["oracle_goals"] == 0
    assert blind["goal_status_check"]["windows_cleared"] == 0
    assert blind["goal_status_check"]["failure_windows_status_unknown"] == 1


def test_goal_status_map_drops_ambiguous_ids():
    goals = [
        {"id": "g-1-1", "status": "completed"},
        {"id": "g-1-2", "status": "completed"}, {"id": "g-1-2", "status": "skipped"},   # collision
        {"id": "g-1-3", "status": "pending"}, {"id": "g-1-3", "status": "pending"},     # agree
        {"id": "g-1-4", "status": None},                                                # no status
    ]
    assert sa.goal_status_map(goals) == {"g-1-1": "completed", "g-1-3": "pending"}


def _goal_store_stub(monkeypatch, reads):
    """Stub the lazily-imported daemon client. `reads` maps (source, archive) -> payload
    (a JSON-able body) or an Exception instance to raise."""
    def fake_read(source, active=False, archive=False):
        r = reads[(source, archive)]
        if isinstance(r, Exception):
            raise r
        return json.dumps(r)
    monkeypatch.setitem(sys.modules, "_rt", types.SimpleNamespace(
        aspirations_read=fake_read,
        tolerant_decode_aggregate=lambda label, raw: json.loads(raw) if raw else None))


def test_read_goals_covers_live_archive_and_census_evicted(monkeypatch):
    # A goal in a completed aspiration is absent from every live read, and an aged-out
    # terminal goal survives only as a bare id in the census (guard-1555, ).
    _goal_store_stub(monkeypatch, {
        ("world", False): {"aspirations": [{"id": "asp-1", "goals": [
            {"id": "g-1-1", "status": "pending", "origin_signal": "sig-a"}],
            "archived_census": {"evicted_ids": {"completed": ["g-1-7"], "skipped": ["g-1-8"]}}}]},
        ("world", True): [{"id": "asp-2", "goals": [
            {"id": "g-2-1", "status": "completed", "completed_at": "2026-09-01T00:00:00"}]}],
        ("agent", False): {"aspirations": []},
        ("agent", True): [],
    })
    goals, errors = sa.read_goals()
    assert errors == []
    statuses = sa.goal_status_map(goals)
    assert statuses == {"g-1-1": "pending", "g-2-1": "completed",
                        "g-1-7": "completed", "g-1-8": "skipped"}
    live = next(g for g in goals if g["id"] == "g-1-1")
    assert live["origin_signal"] == "sig-a" and live["source"] == "world"
    assert next(g for g in goals if g["id"] == "g-2-1")["completed_at"] == "2026-09-01T00:00:00"
    assert next(g for g in goals if g["id"] == "g-1-7").get("evicted") is True


def test_read_goals_returns_failed_reads_instead_of_swallowing_them(monkeypatch):
    # An empty oracle must be distinguishable from a healthy one that cleared nothing.
    _goal_store_stub(monkeypatch, {
        ("world", False): {"aspirations": [{"id": "asp-1", "goals": [
            {"id": "g-1-1", "status": "completed"}]}]},
        ("world", True): RuntimeError("archive down"),
        ("agent", False): {"aspirations": []},
        ("agent", True): [],
    })
    goals, errors = sa.read_goals()
    assert errors == ["world/archive: archive down"]
    assert sa.goal_status_map(goals) == {"g-1-1": "completed"}   # the readable half survives


# --------------------------------------------------------------------------
# Reconsolidation candidate builder (skill-evaluate)
# --------------------------------------------------------------------------

def test_reconsolidation_candidates_threshold_and_priority():
    join = {
        "per_skill": {
            "bad-skill": {"success": 1, "failure": 4, "unknown": 0, "classified": 5, "success_rate": 0.2},
            "ok-skill": {"success": 9, "failure": 1, "unknown": 0, "classified": 10, "success_rate": 0.9},
        },
        "failing": [{"skill": "bad-skill", "goal_id": "g-1", "ts": "t1", "agent": "aa"},
                    {"skill": "bad-skill", "goal_id": "g-2", "ts": "t2", "agent": "aa"}],
    }
    quality = {"bad-skill": {"aggregate": {"overall": 0.2}}}
    cands = se.build_reconsolidation_candidates(join, quality, min_failures=2, min_fail_rate=0.2)
    assert len(cands) == 1  # ok-skill (fail_rate 0.1) filtered out
    c = cands[0]
    assert c["skill"] == "bad-skill"
    assert c["failure_rate"] == 0.8
    assert c["reconsolidation_priority"] == 0.64  # 0.8 * (1 - 0.2)
    assert c["recent_failing_goals"] == ["g-1", "g-2"]
    assert c["distinct_failing_goals"] == 2


def test_reconsolidation_no_quality_is_neutral():
    join = {
        "per_skill": {"x": {"success": 0, "failure": 3, "unknown": 0, "classified": 3, "success_rate": 0.0}},
        "failing": [{"skill": "x", "goal_id": "g-1", "ts": "t", "agent": "a"},
                    {"skill": "x", "goal_id": "g-2", "ts": "t", "agent": "a"}],
    }
    cands = se.build_reconsolidation_candidates(join, {}, min_failures=2, min_fail_rate=0.2)
    assert cands[0]["reconsolidation_priority"] == 0.5  # 1.0 * (1 - 0.5 neutral)
    assert cands[0]["current_quality_overall"] is None


def test_reconsolidation_below_threshold_empty():
    join = {
        "per_skill": {"x": {"success": 5, "failure": 1, "unknown": 0, "classified": 6, "success_rate": 0.833}},
        "failing": [{"skill": "x", "goal_id": "g-1", "ts": "t", "agent": "a"}],
    }
    # 1 failure < min_failures=2 -> filtered
    assert se.build_reconsolidation_candidates(join, {}, min_failures=2, min_fail_rate=0.2) == []


def _one_goal_confound_join():
    # 6 failing invocations, ALL behind one goal's window: the shape measured 2026-08-12
    # (131 attributions, exactly one distinct goal) -- a coverage artifact, not a skill signal.
    return {
        "per_skill": {"x": {"success": 0, "failure": 6, "unknown": 0, "classified": 6, "success_rate": 0.0}},
        "failing": [{"skill": "x", "goal_id": "g-1-1", "ts": "t%d" % i, "agent": "a"} for i in range(6)],
    }


def test_reconsolidation_needs_two_distinct_failing_goals():
    join = _one_goal_confound_join()
    # Passes the old gates (6 failures, rate 1.0) and is still not a candidate.
    assert se.build_reconsolidation_candidates(join, {}, min_failures=2, min_fail_rate=0.2) == []
    # The gate is a parameter, not a hardcode: relaxed to 1 it surfaces, and says "1 goal".
    cands = se.build_reconsolidation_candidates(
        join, {}, min_failures=2, min_fail_rate=0.2, min_distinct_goals=1)
    assert cands[0]["distinct_failing_goals"] == 1
    assert cands[0]["recent_failing_goals"] == ["g-1-1"]   # distinct ids, not 6 copies of one
    assert se.MIN_DISTINCT_FAILING_GOALS == 2


def test_reconsolidation_distinct_count_ignores_other_skills_and_repeats():
    join = {
        "per_skill": {"x": {"success": 0, "failure": 4, "unknown": 0, "classified": 4, "success_rate": 0.0},
                      "y": {"success": 0, "failure": 2, "unknown": 0, "classified": 2, "success_rate": 0.0}},
        "failing": [
            {"skill": "x", "goal_id": "g-1-1", "ts": "t1", "agent": "a"},
            {"skill": "y", "goal_id": "g-9-9", "ts": "t2", "agent": "a"},   # another skill's goal
            {"skill": "x", "goal_id": "g-1-1", "ts": "t3", "agent": "a"},   # repeat
            {"skill": "x", "goal_id": "g-1-2", "ts": "t4", "agent": "a"},
            {"skill": "x", "goal_id": "g-1-2", "ts": "t5", "agent": "a"},
            {"skill": "y", "goal_id": "g-9-9", "ts": "t6", "agent": "a"},
        ],
    }
    cands = {c["skill"]: c for c in
             se.build_reconsolidation_candidates(join, {}, min_failures=2, min_fail_rate=0.2)}
    assert set(cands) == {"x"}                       # y: 2 failures, ONE goal -> gated out
    assert cands["x"]["failing_invocations"] == 4
    assert cands["x"]["distinct_failing_goals"] == 2
    assert cands["x"]["recent_failing_goals"] == ["g-1-1", "g-1-2"]


# --------------------------------------------------------------------------
# --apply self-filing (): slug, open-signal dedup base, advisory
# record shape (+fail-open), and the cmd_reconsolidation dedup/filing loop.
# _rt is imported LAZILY inside the helpers, so it is stubbed via
# monkeypatch.setitem(sys.modules, "_rt", ...) — the import resolves the stub.
# --------------------------------------------------------------------------

def test_recon_slug_normalizes():
    assert se._recon_slug("My Failing Skill!") == "my-failing-skill"
    assert se._recon_slug("/reflect-on-outcome") == "reflect-on-outcome"
    assert se._recon_slug("a" * 100) == "a" * 48   # capped at 48


def test_open_origin_signals_collects_open_only():
    # Pure over read_goals() records (: the goal store is read ONCE per run).
    goals = [
        {"id": "g-1-1", "status": "pending", "origin_signal": "sig-open-1"},
        {"id": "g-1-2", "status": "completed", "origin_signal": "sig-done"},   # closed -> excluded
        {"id": "g-1-3", "status": "in-progress", "origin_signal": "sig-open-2"},
        {"id": "g-1-4", "status": "pending"},                                   # no signal -> skipped
        {"id": "g-1-5", "status": "pending", "origin_signal": "sig-agent-open", "source": "agent"},
    ]
    assert se.open_origin_signals(goals) == {"sig-open-1", "sig-open-2", "sig-agent-open"}


def test_recent_closed_signals_windows_statuses_and_newest_wins():
    # rb-3523: the same finding inside the window is noise, after it a real regression.
    import datetime as _dt
    now = _dt.datetime(2026, 10, 4, 12, 0, 0)
    goals = [
        {"id": "g-1-1", "status": "skipped", "origin_signal": "sig-skipped",
         "completed_at": "2026-10-02T03:15:22"},                          # 2d, closed-as-noise
        {"id": "g-1-2", "status": "completed", "origin_signal": "sig-completed",
         "completed_at": "2026-10-01T00:00:00"},
        {"id": "g-1-3", "status": "completed", "origin_signal": "sig-old",
         "completed_at": "2026-09-01T00:00:00"},                          # 33d -> outside the 14d window
        {"id": "g-1-4", "status": "pending", "origin_signal": "sig-open",
         "completed_at": "2026-10-03T00:00:00"},                          # not closed
        {"id": "g-1-5", "status": "completed", "origin_signal": "sig-undated"},   # cannot be windowed
        {"id": "g-1-6", "status": "completed", "evicted": True},          # census id: no signal/time
        {"id": "g-1-7", "status": "completed", "origin_signal": "sig-twice",
         "completed_at": "2026-10-01T00:00:00"},
        {"id": "g-1-8", "status": "expired", "origin_signal": "sig-twice",
         "completed_at": "2026-10-03T00:00:00"},                          # newer closure wins
        {"id": "g-1-9", "status": "superseded", "origin_signal": "sig-superseded",
         "completed_at": "2026-10-03T06:00:00"},
    ]
    got = se.recent_closed_signals(goals, window_days=14, now=now)
    assert set(got) == {"sig-skipped", "sig-completed", "sig-twice", "sig-superseded"}
    assert got["sig-twice"] == {"goal_id": "g-1-8", "status": "expired",
                                "closed_at": "2026-10-03T00:00:00"}
    assert got["sig-skipped"]["goal_id"] == "g-1-1"
    # Same data, a 1-day window: only closures newer than 2026-10-03T12:00 remain.
    assert set(se.recent_closed_signals(goals, window_days=1, now=now)) == set()
    assert se.CLOSED_DEDUP_WINDOW_DAYS == 14


def test_file_reconsolidation_investigate_record_shape(monkeypatch):
    captured = {}

    def fake_add(asp, record, source="world", overrides=None):
        captured.update(asp=asp, record=record, source=source, overrides=overrides)
        return {"goal": {"id": "g-115-9001"}}

    monkeypatch.setitem(sys.modules, "_rt",
                        types.SimpleNamespace(aspirations_add_goal=fake_add))
    cand = {"skill": "My Failing Skill!", "failing_invocations": 4,
            "classified_invocations": 5, "failure_rate": 0.8,
            "current_quality_overall": 0.2, "reconsolidation_priority": 0.64,
            "recent_failing_goals": ["g-1", "g-2"]}
    gid = se.file_reconsolidation_investigate(cand, target_asp="asp-115")
    assert gid == "g-115-9001"
    rec = captured["record"]
    assert rec["origin_signal"] == "investigate:skill-reconsolidation-my-failing-skill"
    assert rec["participants"] == ["agent"]
    assert rec["category"] == "skill-quality"
    assert rec["intended_agent"] == "either"
    assert set(rec["tags"]) == {"skill-reconsolidation", "advisory"}
    assert "ADVISORY" in rec["description"] and "Do NOT auto-modify" in rec["description"]
    assert captured["asp"] == "asp-115" and captured["source"] == "world"
    assert "Duplication" in captured["overrides"]


def test_file_reconsolidation_investigate_fail_open(monkeypatch):
    def boom(asp, record, source="world", overrides=None):
        raise RuntimeError("add failed")

    monkeypatch.setitem(sys.modules, "_rt",
                        types.SimpleNamespace(aspirations_add_goal=boom))
    cand = {"skill": "x", "failure_rate": 0.9, "recent_failing_goals": []}
    assert se.file_reconsolidation_investigate(cand) is None   # fail-open, returns None


def _recon_args(**over):
    import argparse
    ns = dict(agent=None, since="", min_failures=2, min_fail_rate=0.2, apply=True,
              target_asp="asp-115", min_distinct_goals=2, max_file=1)
    ns.update(over)
    return argparse.Namespace(**ns)


def _stub_recon_env(monkeypatch, cands, goals=None, errors=None, join=None):
    """cmd_reconsolidation over a stubbed attribution module: the join, the goal store and
    the candidate list are canned, so only the filing logic is under test. The default goal
    store is non-empty (one completed goal) so the status oracle is not blind."""
    goals = [{"id": "g-0-0", "status": "completed"}] if goals is None else goals
    monkeypatch.setattr(se, "_load_skill_attribution", lambda: types.SimpleNamespace(
        find_agent_dirs=lambda: ["aa"], parse_since=lambda s: None,
        read_goals=lambda: (goals, errors or []), goal_status_map=sa.goal_status_map,
        compute_join=lambda agents, since_dt=None, goal_status=None:
            join or {"per_skill": {}, "failing": []}))
    monkeypatch.setattr(se, "read_yaml", lambda p: {"skills": {}})
    monkeypatch.setattr(se, "build_reconsolidation_candidates", lambda *a, **k: cands)


def _record_filings(monkeypatch):
    filed_skills = []

    def fake_file(c, target_asp="asp-115"):
        filed_skills.append(c["skill"])
        return "g-115-70%02d" % len(filed_skills)

    monkeypatch.setattr(se, "file_reconsolidation_investigate", fake_file)
    return filed_skills


def _sig(skill):
    return "investigate:skill-reconsolidation-" + skill


def test_cmd_reconsolidation_apply_files_and_dedups(monkeypatch, capsys):
    cands = [{"skill": "foo-skill", "failure_rate": 0.8},
             {"skill": "bar-skill", "failure_rate": 0.7}]
    # foo-skill already has an open goal -> suppressed; bar-skill is fresh -> filed
    goals = [{"id": "g-0-0", "status": "completed"},
             {"id": "g-1-1", "status": "pending", "origin_signal": _sig("foo-skill")}]
    _stub_recon_env(monkeypatch, cands, goals=goals)
    _record_filings(monkeypatch)
    se.cmd_reconsolidation(_recon_args())
    out = json.loads(capsys.readouterr().out)
    assert out["target_asp"] == "asp-115"
    assert out["suppressed_dedup"] == [{"skill": "foo-skill", "origin_signal": _sig("foo-skill")}]
    assert out["filed"] == [{"skill": "bar-skill", "goal_id": "g-115-7001",
                             "origin_signal": _sig("bar-skill")}]
    assert out["deferred_by_cap"] == [] and out["suppressed_closed_recent"] == []


def test_cmd_reconsolidation_apply_files_at_most_max_file(monkeypatch, capsys):
    # : the incident filed 12 advisory goals in one scan. Cap per run, worst-first.
    cands = [{"skill": s, "failure_rate": 0.9} for s in ("aa-skill", "bb-skill", "cc-skill")]
    _stub_recon_env(monkeypatch, cands)
    filed = _record_filings(monkeypatch)
    se.cmd_reconsolidation(_recon_args())                       # default cap
    out = json.loads(capsys.readouterr().out)
    assert se.DEFAULT_MAX_FILE == 1 and out["max_file"] == 1
    assert filed == ["aa-skill"]                                # the highest-priority one
    assert [f["skill"] for f in out["filed"]] == ["aa-skill"]
    assert [d["skill"] for d in out["deferred_by_cap"]] == ["bb-skill", "cc-skill"]

    del filed[:]
    se.cmd_reconsolidation(_recon_args(max_file=2))
    out = json.loads(capsys.readouterr().out)
    assert filed == ["aa-skill", "bb-skill"]
    assert [d["skill"] for d in out["deferred_by_cap"]] == ["cc-skill"]

    del filed[:]
    se.cmd_reconsolidation(_recon_args(max_file=0))             # 0 disables filing
    out = json.loads(capsys.readouterr().out)
    assert filed == [] and out["filed"] == [] and len(out["deferred_by_cap"]) == 3


def test_cmd_reconsolidation_cap_counts_filings_not_suppressed_skills(monkeypatch, capsys):
    # A suppressed skill must not burn the cap: the next candidate still gets filed.
    cands = [{"skill": "open-skill", "failure_rate": 0.9},
             {"skill": "fresh-skill", "failure_rate": 0.8}]
    goals = [{"id": "g-0-0", "status": "completed"},
             {"id": "g-1-1", "status": "pending", "origin_signal": _sig("open-skill")}]
    _stub_recon_env(monkeypatch, cands, goals=goals)
    filed = _record_filings(monkeypatch)
    se.cmd_reconsolidation(_recon_args())
    out = json.loads(capsys.readouterr().out)
    assert filed == ["fresh-skill"] and out["deferred_by_cap"] == []


def test_cmd_reconsolidation_apply_skips_recently_closed(monkeypatch, capsys):
    # rb-3523 / : a skill whose reconsolidation goal CLOSED inside the window is
    # noise; one closed long ago is a legitimate recurrence and is filed again.
    import datetime as _dt
    now = _dt.datetime.now()
    recent = (now - _dt.timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S")
    old = (now - _dt.timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S")
    cands = [{"skill": "noisy-skill", "failure_rate": 0.9},
             {"skill": "stale-skill", "failure_rate": 0.8}]
    goals = [{"id": "g-0-0", "status": "completed"},
             {"id": "g-1-1", "status": "skipped", "origin_signal": _sig("noisy-skill"),
              "completed_at": recent},
             {"id": "g-1-2", "status": "completed", "origin_signal": _sig("stale-skill"),
              "completed_at": old}]
    _stub_recon_env(monkeypatch, cands, goals=goals)
    filed = _record_filings(monkeypatch)
    se.cmd_reconsolidation(_recon_args())
    out = json.loads(capsys.readouterr().out)
    assert filed == ["stale-skill"]
    assert out["suppressed_closed_recent"] == [
        {"skill": "noisy-skill", "origin_signal": _sig("noisy-skill"),
         "goal_id": "g-1-1", "status": "skipped", "closed_at": recent}]
    assert out["closed_window_days"] == 14


@pytest.mark.parametrize("goals, errors, reason", [
    ([{"id": "g-0-0", "status": "completed"}], ["world/archive: daemon down"],
     "goal store unreadable: world/archive: daemon down"),
    ([], [], "goal store returned no goals"),
])
def test_cmd_reconsolidation_apply_refuses_when_goal_store_is_blind(
        monkeypatch, capsys, goals, errors, reason):
    # A join scored without the store files exactly the false failures the oracle exists
    # to clear. Fail visibly and file nothing (guard-3563).
    _stub_recon_env(monkeypatch, [{"skill": "z", "failure_rate": 0.9}], goals=goals, errors=errors)
    monkeypatch.setattr(se, "file_reconsolidation_investigate",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("filed while blind")))
    se.cmd_reconsolidation(_recon_args())
    out = json.loads(capsys.readouterr().out)
    assert out["apply_refused"] == reason
    assert out["filed"] == []
    assert out["candidate_count"] == 1                          # the report itself still renders
    assert out["goal_status_check"]["read_errors"] == errors


def test_cmd_reconsolidation_no_apply_omits_filing_keys(monkeypatch, capsys):
    _stub_recon_env(monkeypatch, [{"skill": "z", "failure_rate": 0.9}])
    # without --apply, the dedup/filing helpers must NOT be called
    for name in ("open_origin_signals", "recent_closed_signals", "file_reconsolidation_investigate"):
        monkeypatch.setattr(se, name,
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called without --apply")))
    se.cmd_reconsolidation(_recon_args(apply=False))
    out = json.loads(capsys.readouterr().out)
    for key in ("filed", "suppressed_dedup", "suppressed_closed_recent", "deferred_by_cap",
                "apply_refused"):
        assert key not in out
    assert out["candidate_count"] == 1


def test_cmd_reconsolidation_zero_candidates_still_emits_a_nonnull_ceiling(
        agents_root, monkeypatch, capsys):
    # : S4.6 was told to print the classifiable ceiling next to candidate_count, but
    # the command never emitted it, so the instruction read None on every run -- and a 0 from a
    # blind run printed identically to a 0 from a healthy fleet. Real join, real diaries.
    diary = [
        {"entry_type": "phase_start", "goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"entry_type": "phase_start", "goal_id": "g-1-2", "timestamp": "2026-07-21T10:30:00"},
    ]
    invs = [{"ts": "2026-07-21T10:05:00", "skill": "reflect", "agent": "aa", "sid": "s1"},
            {"ts": "2026-07-21T10:06:00", "skill": "reflect", "agent": "aa", "sid": "s1"}]
    _mk_agent(agents_root, "aa", invs, diary=diary)
    monkeypatch.setattr(sa, "read_goals", lambda: ([{"id": "g-9-9", "status": "pending"}], []))
    monkeypatch.setattr(se, "_load_skill_attribution", lambda: sa)
    monkeypatch.setattr(se, "read_yaml", lambda p: {"skills": {}})
    se.cmd_reconsolidation(_recon_args(apply=False))
    out = json.loads(capsys.readouterr().out)
    # 2 failures, rate 1.0, but both behind ONE goal -> not a candidate.
    assert out["candidate_count"] == 0 and out["reconsolidation_candidates"] == []
    cov = out["diary_coverage"]
    assert cov["classifiable_ceiling"] == 2 and cov["classifiable_ceiling"] is not None
    assert cov["ceiling_ratio"] == 1.0 and cov["invocations"] == 2
    assert cov["classified_invocations"] == 2
    assert out["threshold"]["min_distinct_goals"] == 2
    assert out["goal_status_check"] == {
        "oracle_goals": 1, "windows_cleared": 0, "failure_windows": 1,
        "failure_windows_status_unknown": 1, "read_errors": []}


# --- : read_execution_diary must read the STORE, not the local cache ---
#
# execution-diary.jsonl is sync_tier: continuity and NOT machine-local, so under
# own-cloud the authoritative copy is in S3 and the local tree is a read-through
# cache populated PER-AGENT (owncloud-pull.sh is --agent-scoped; /start pulls the
# bound agent only). A peer's diary is therefore simply absent on this box, and
# the old `os.path.exists(path)` gate returned [] for every agent but self.
#
# Measured cc-02 2026-07-31 BEFORE the fix: diaries local for 1 of 5 agents while
# all 5 were live in S3; 4 of 5 agents contributed zero goal windows; fleet
# classification rate 0.3043% (45/14788). After: all 5 nonzero, 1.2848% (190/14788),
# which is 100% of what is structurally classifiable (exactly 190 invocations fall
# inside any diary span -- the residual is retention asymmetry, not a join defect).
#
# These two tests are a matched pair and must stay that way: the first fails under
# the old implementation, the second passes under BOTH. Together they prove the
# change discriminates rather than merely passing (guard-1943).


class _StoreOnlyBackend:
    """Backend whose content exists ONLY in the store -- never on local disk."""

    def __init__(self, payload):
        self._payload = payload
        self.read_paths = []

    def read_text(self, path, encoding="utf-8", *, force_fresh=False):
        self.read_paths.append(str(path))
        if str(path) in self._payload:
            return self._payload[str(path)]
        raise FileNotFoundError(str(path))


def test_read_execution_diary_reads_store_when_local_absent(agents_root, monkeypatch):
    """The regression guard: absent locally, present in the store -> rows returned."""
    import storage_backend

    path = os.path.join(str(agents_root), "peer", "session", "execution-diary.jsonl")
    assert not os.path.exists(path), "fixture must NOT create the file locally"

    rows = [{"timestamp": "2026-07-30T11:00:00", "goal_id": "g-1", "event": "phase_start"},
            {"timestamp": "2026-07-30T10:00:00", "goal_id": "g-1", "event": "phase_start"}]
    backend = _StoreOnlyBackend({path: "\n".join(json.dumps(r) for r in rows)})
    monkeypatch.setattr(storage_backend, "get_backend", lambda: backend)

    got = sa.read_execution_diary("peer")

    # Under the old os.path.exists() gate this is [] -- the whole defect.
    assert len(got) == 2, "diary present in the store must be read despite absent local cache"
    assert [r["timestamp"] for r in got] == ["2026-07-30T10:00:00", "2026-07-30T11:00:00"], \
        "rows must still be timestamp-sorted"
    assert backend.read_paths == [path], "must read via the backend, not the filesystem"


def test_read_execution_diary_absent_in_store_returns_empty(agents_root, monkeypatch):
    """Genuine absence stays an empty list -- FileNotFoundError is not an error path."""
    import storage_backend

    backend = _StoreOnlyBackend({})           # store has nothing
    monkeypatch.setattr(storage_backend, "get_backend", lambda: backend)
    assert sa.read_execution_diary("ghost") == []


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
