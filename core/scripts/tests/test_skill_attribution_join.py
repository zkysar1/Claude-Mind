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


# --------------------------------------------------------------------------
# Interval construction + locate
# --------------------------------------------------------------------------

def test_build_goal_windows_intervals():
    diary = [
        {"goal_id": "g-1-1", "timestamp": "2026-07-21T10:00:00"},
        {"goal_id": "g-1-1", "timestamp": "2026-07-21T10:05:00"},
        {"goal_id": "g-1-2", "timestamp": "2026-07-21T10:30:00"},
    ]
    assert sa.build_goal_windows(diary) == [
        ("g-1-1", "2026-07-21T10:00:00", "2026-07-21T10:30:00"),
        ("g-1-2", "2026-07-21T10:30:00", None),
    ]


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


# --------------------------------------------------------------------------
# Reconsolidation candidate builder (skill-evaluate)
# --------------------------------------------------------------------------

def test_reconsolidation_candidates_threshold_and_priority():
    join = {
        "per_skill": {
            "bad-skill": {"success": 1, "failure": 4, "unknown": 0, "classified": 5, "success_rate": 0.2},
            "ok-skill": {"success": 9, "failure": 1, "unknown": 0, "classified": 10, "success_rate": 0.9},
        },
        "failing": [{"skill": "bad-skill", "goal_id": "g-1", "ts": "t1", "agent": "aa"}],
    }
    quality = {"bad-skill": {"aggregate": {"overall": 0.2}}}
    cands = se.build_reconsolidation_candidates(join, quality, min_failures=2, min_fail_rate=0.2)
    assert len(cands) == 1  # ok-skill (fail_rate 0.1) filtered out
    c = cands[0]
    assert c["skill"] == "bad-skill"
    assert c["failure_rate"] == 0.8
    assert c["reconsolidation_priority"] == 0.64  # 0.8 * (1 - 0.2)
    assert c["recent_failing_goals"] == ["g-1"]


def test_reconsolidation_no_quality_is_neutral():
    join = {
        "per_skill": {"x": {"success": 0, "failure": 3, "unknown": 0, "classified": 3, "success_rate": 0.0}},
        "failing": [{"skill": "x", "goal_id": "g-1", "ts": "t", "agent": "a"}],
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


def test_open_origin_signals_collects_open_only(monkeypatch):
    reads = {
        "world": {"aspirations": [{"goals": [
            {"status": "pending", "origin_signal": "sig-open-1"},
            {"status": "completed", "origin_signal": "sig-done"},    # closed -> excluded
            {"status": "in-progress", "origin_signal": "sig-open-2"},
            {"status": "pending"},                                    # no signal -> skipped
        ]}]},
        "agent": {"aspirations": [{"goals": [
            {"status": "pending", "origin_signal": "sig-agent-open"},
        ]}]},
    }
    fake = types.SimpleNamespace(
        aspirations_read=lambda source, active: source,
        tolerant_decode_aggregate=lambda label, out: reads[out],
    )
    monkeypatch.setitem(sys.modules, "_rt", fake)
    assert se._open_origin_signals() == {"sig-open-1", "sig-open-2", "sig-agent-open"}


def test_open_origin_signals_fail_open_on_read_error(monkeypatch):
    def boom(source, active):
        raise RuntimeError("daemon down")
    fake = types.SimpleNamespace(aspirations_read=boom,
                                 tolerant_decode_aggregate=lambda l, o: None)
    monkeypatch.setitem(sys.modules, "_rt", fake)
    assert se._open_origin_signals() == set()   # fail-open, no raise


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


def test_cmd_reconsolidation_apply_files_and_dedups(monkeypatch, capsys):
    import argparse
    cands = [{"skill": "foo-skill", "failure_rate": 0.8},
             {"skill": "bar-skill", "failure_rate": 0.7}]
    monkeypatch.setattr(se, "_load_skill_attribution", lambda: types.SimpleNamespace(
        find_agent_dirs=lambda: ["aa"], parse_since=lambda s: None,
        compute_join=lambda agents, since_dt=None: {"per_skill": {}, "failing": []}))
    monkeypatch.setattr(se, "read_yaml", lambda p: {"skills": {}})
    monkeypatch.setattr(se, "build_reconsolidation_candidates", lambda *a, **k: cands)
    # foo-skill already has an open goal -> suppressed; bar-skill is fresh -> filed
    monkeypatch.setattr(se, "_open_origin_signals",
                        lambda: {"investigate:skill-reconsolidation-foo-skill"})
    monkeypatch.setattr(se, "file_reconsolidation_investigate",
                        lambda c, target_asp="asp-115": "g-115-7777")
    args = argparse.Namespace(agent=None, since="", min_failures=2,
                              min_fail_rate=0.2, apply=True, target_asp="asp-115")
    se.cmd_reconsolidation(args)
    out = json.loads(capsys.readouterr().out)
    assert out["target_asp"] == "asp-115"
    assert out["suppressed_dedup"] == [
        {"skill": "foo-skill", "origin_signal": "investigate:skill-reconsolidation-foo-skill"}]
    assert out["filed"] == [
        {"skill": "bar-skill", "goal_id": "g-115-7777",
         "origin_signal": "investigate:skill-reconsolidation-bar-skill"}]


def test_cmd_reconsolidation_no_apply_omits_filing_keys(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(se, "_load_skill_attribution", lambda: types.SimpleNamespace(
        find_agent_dirs=lambda: ["aa"], parse_since=lambda s: None,
        compute_join=lambda agents, since_dt=None: {"per_skill": {}, "failing": []}))
    monkeypatch.setattr(se, "read_yaml", lambda p: {"skills": {}})
    monkeypatch.setattr(se, "build_reconsolidation_candidates",
                        lambda *a, **k: [{"skill": "z", "failure_rate": 0.9}])
    # without --apply, the dedup/filing helpers must NOT be called
    monkeypatch.setattr(se, "_open_origin_signals",
                        lambda: (_ for _ in ()).throw(AssertionError("called without --apply")))
    args = argparse.Namespace(agent=None, since="", min_failures=2,
                              min_fail_rate=0.2, apply=False, target_asp="asp-115")
    se.cmd_reconsolidation(args)
    out = json.loads(capsys.readouterr().out)
    assert "filed" not in out and "suppressed_dedup" not in out
    assert out["candidate_count"] == 1


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
