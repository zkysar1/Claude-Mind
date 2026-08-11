""": worker-goal retrospective — planning, dedup, and merge provenance.

Two halves, matching the two halves of the change:

  1. `body-merge._stamp_merged_goal_ids` — the ONLY place the fleet records
     worker-completion. Everything downstream is undecidable without it, so the
     set-difference and its fail-safe (unreadable WM -> no attribution, never a
     false one) are pinned here.
  2. `worker_retrospective.decide` + `retrospect` — the dedup mechanism the goal
     requires a test for: a goal is retrospected AT MOST ONCE, proven by a real
     round trip (plan -> apply -> re-plan) rather than by asserting the marker
     field is written.

Daemon-safe: pure dict/file arithmetic plus stubbed lane runners. No live store
is read or written, so this needs no `daemon_integration` marker.

Run:
  STORAGE_BACKEND=local python -m pytest core/scripts/tests/test_worker_retrospective.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge_wr", "body-merge.py")
import worker_retrospective as wr  # noqa: E402


# ─────────────── half 1: merge provenance (body-merge.py) ───────────────

def test_completed_goal_ids_reads_dict_rows():
    """The live top-level WM slot holds dict rows keyed by goal_id."""
    wm = {"goals_completed_this_session": [
        {"goal_id": "g-306-1", "aspiration_id": "asp-306", "work_class": "framework"},
        {"goal_id": "g-306-2", "_item_ts": "2026-08-07T00:00:00"},
    ]}
    assert merge._completed_goal_ids(wm) == ["g-306-1", "g-306-2"]


def test_completed_goal_ids_reads_bare_string_rows():
    """`counted_goals_this_session` is a list of bare ids — accepted too."""
    assert merge._completed_goal_ids(
        {"goals_completed_this_session": ["g-1-1", "g-1-2"]}) == ["g-1-1", "g-1-2"]


def test_completed_goal_ids_ignores_non_list_and_junk_rows():
    """loop_state's identically-named key is an INT; junk rows contribute nothing."""
    assert merge._completed_goal_ids({"goals_completed_this_session": 353}) == []
    assert merge._completed_goal_ids({}) == []
    assert merge._completed_goal_ids(
        {"goals_completed_this_session": [None, 7, {"no_goal_id": 1}]}) == []


def test_stamp_merged_goal_ids_is_the_set_difference(tmp_path):
    wm = tmp_path / "wm.yaml"
    wm.write_text(yaml.safe_dump({"goals_completed_this_session": [
        {"goal_id": "g-1-1"}, {"goal_id": "g-2-2"}, {"goal_id": "g-3-3"},
    ]}), encoding="utf-8")
    summary = {}
    merge._stamp_merged_goal_ids(wm, {"g-1-1"}, summary)
    assert summary["merged_goal_ids"] == ["g-2-2", "g-3-3"]


def test_stamp_merged_goal_ids_dedups_within_the_post_state(tmp_path):
    wm = tmp_path / "wm.yaml"
    wm.write_text(yaml.safe_dump({"goals_completed_this_session": [
        {"goal_id": "g-9-9"}, {"goal_id": "g-9-9"},
    ]}), encoding="utf-8")
    summary = {}
    merge._stamp_merged_goal_ids(wm, set(), summary)
    assert summary["merged_goal_ids"] == ["g-9-9"]


def test_stamp_merged_goal_ids_attributes_nothing_when_wm_unreadable(tmp_path):
    """Fail-safe direction: a plumbing fault must not manufacture attribution."""
    summary = {}
    merge._stamp_merged_goal_ids(tmp_path / "absent.yaml", set(), summary)
    assert summary["merged_goal_ids"] == []


def test_generalize_down_reports_merged_goal_ids_key_when_dormant(tmp_path):
    """The key is present even on the dormant single-runner no-op path."""
    pr = tmp_path
    (pr / "agents" / "solo" / "session").mkdir(parents=True)
    summary = merge.generalize_down("solo", project_root=pr)
    assert summary["merged_goal_ids"] == []


# ───────────── half 2: retrospective planning + dedup (the proof) ─────────────

def _rec(gid, **over):
    base = {"id": gid, "aspiration_id": wr.aspiration_of(gid), "title": f"T {gid}",
            "category": "framework-architecture", "work_class": "framework",
            "completed_by": "zeta", "outcome_note": "done", "_src": "world"}
    base.update(over)
    return base


def test_aspiration_of_derives_and_rejects():
    assert wr.aspiration_of("g-306-198") == "asp-306"
    assert wr.aspiration_of("g-1-1") == "asp-1"
    assert wr.aspiration_of("not-a-goal") is None
    assert wr.aspiration_of("") is None


def test_decide_plans_an_unmarked_goal():
    out = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})
    assert [p["goal_id"] for p in out["plan"]] == ["g-306-1"]
    assert out["skipped"] == []


def test_decide_skips_an_already_marked_goal():
    """The dedup predicate itself: a marker present means never again."""
    rec = _rec("g-306-1", **{wr.MARKER_FIELD: "2026-08-07T00:00:00|zeta|worker-retrospective"})
    out = wr.decide(["g-306-1"], {"g-306-1": rec})
    assert out["plan"] == []
    assert out["skipped"][0]["reason"] == wr.SKIP_ALREADY


def test_decide_treats_blank_marker_as_unmarked():
    rec = _rec("g-306-1", **{wr.MARKER_FIELD: "   "})
    assert len(wr.decide(["g-306-1"], {"g-306-1": rec})["plan"]) == 1


def test_decide_collapses_duplicate_ids():
    out = wr.decide(["g-306-1", "g-306-1"], {"g-306-1": _rec("g-306-1")})
    assert len(out["plan"]) == 1


def test_decide_skips_malformed_and_missing():
    out = wr.decide(["oops", "g-306-9"], {})
    reasons = {s["reason"] for s in out["skipped"]}
    assert reasons == {wr.SKIP_BAD_ID, wr.SKIP_NO_RECORD}
    assert out["plan"] == []


def test_decide_carries_the_source_queue_through():
    """The marker write must target the queue the goal lives in, not `world`."""
    out = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1", _src="agent")})
    assert out["plan"][0]["source"] == "agent"


class _Recorder:
    """Stands in for the lane writers so no live store is touched."""

    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)
        self.marker_writes = []

    def lane(self, name):
        def _fn(item, agent, now_iso, root, *a):
            self.calls.append((name, item["goal_id"]))
            return (1 if name in self.fail else 0, "", "boom" if name in self.fail else "")
        return _fn

    def marker(self, store):
        def _fn(item, agent, now_iso, root):
            self.marker_writes.append(item["goal_id"])
            store[item["goal_id"]][wr.MARKER_FIELD] = f"{now_iso}|{agent}|{wr.MARKER_SOURCE}"
            return (0, "", "")
        return _fn


def _stub(monkeypatch, rec: _Recorder, store):
    monkeypatch.setattr(wr, "_lane_team_state", rec.lane("team_state"))
    monkeypatch.setattr(wr, "_lane_journal", rec.lane("journal"))
    monkeypatch.setattr(wr, "_lane_findings", rec.lane("findings"))
    monkeypatch.setattr(wr, "_lane_impk", rec.lane("impk"))
    monkeypatch.setattr(wr, "_write_marker", rec.marker(store))


def test_retrospect_runs_four_lanes_and_marks(monkeypatch):
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder()
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    assert sorted(n for n, _ in rec.calls) == sorted(wr.RUN_LANES)
    assert out["lanes_written"] == 3          # impk is not an artifact of itself
    assert out["marked"] is True
    assert out["pending_judgment_lanes"] == list(wr.REPORT_LANES)


def test_a_goal_cannot_be_retrospected_twice(monkeypatch):
    """The outcome-2 proof: plan -> apply -> re-plan yields no second run."""
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder()
    _stub(monkeypatch, rec, store)

    first = wr.decide(["g-306-1"], store)
    assert len(first["plan"]) == 1
    wr.retrospect(first["plan"][0], "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    calls_after_first = len(rec.calls)

    second = wr.decide(["g-306-1"], store)
    assert second["plan"] == []
    assert second["skipped"][0]["reason"] == wr.SKIP_ALREADY
    for item in second["plan"]:                     # deliberately empty
        wr.retrospect(item, "zeta", "2026-08-07T00:00:01", Path("/nonexistent"))
    assert len(rec.calls) == calls_after_first      # no lane ran a second time


def test_marker_is_withheld_when_every_lane_failed(monkeypatch):
    """Marking a goal whose lanes all failed would suppress the retry forever."""
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder(fail=("team_state", "journal", "findings", "impk"))
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    assert out["lanes_written"] == 0
    assert out["marked"] is False
    assert rec.marker_writes == []
    assert wr.MARKER_FIELD not in store["g-306-1"]
    assert len(wr.decide(["g-306-1"], store)["plan"]) == 1   # retried next pass


def test_impk_artifacts_count_reports_only_lanes_that_landed(monkeypatch):
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder(fail=("findings",))
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    assert out["lanes"]["impk"]["artifacts_count"] == 2
    assert out["marked"] is True    # partial success still counts as progress


# ─────────── half 3: lane ARGV construction (the stub blind spot) ───────────
#
# Every test above stubs the lane runners, so none of them can see a defect in
# how a lane builds its arguments. A fresh-eyes pass found exactly that: the
# team-state lane hand-escaped backslashes and quotes and THEN json.dumps'd,
# double-escaping both. These tests exercise the real lane functions with only
# `_run` captured, which is the narrowest seam that still covers argv shape.

def _capture(monkeypatch):
    seen = {}

    def _fake_run(argv, timeout=90):
        seen["argv"] = list(argv)
        return (0, "", "")

    monkeypatch.setattr(wr, "_run", _fake_run)
    return seen


def _flag(argv, name):
    return argv[argv.index(name) + 1]


def test_team_state_lane_does_not_double_escape_quotes_or_backslashes(monkeypatch):
    """A title with " and \\ must round-trip EXACTLY, not gain literal escapes."""
    title = 'Fix the "broken" C:\\path\\thing'
    seen = _capture(monkeypatch)
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1", title=title)})["plan"][0]
    wr._lane_team_state(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    payload = json.loads(_flag(seen["argv"], "--value"))
    assert payload["key_finding"] == f"[{wr.MARKER_SOURCE}] {title}"
    assert "\\\\" not in payload["key_finding"]
    assert '\\"' not in payload["key_finding"]


def test_team_state_lane_value_is_pure_ascii(monkeypatch):
    """guard-662: arbitrary inbound title text must not put cp1252 bytes on the pipe."""
    seen = _capture(monkeypatch)
    item = wr.decide(["g-306-1"],
                     {"g-306-1": _rec("g-306-1", title="em\u2014dash and \u201csmart\u201d")})["plan"][0]
    wr._lane_team_state(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    raw = _flag(seen["argv"], "--value")
    raw.encode("ascii")                                  # raises if non-ASCII leaked
    assert json.loads(raw)["key_finding"].endswith("em\u2014dash and \u201csmart\u201d")


def test_team_state_lane_collapses_newlines(monkeypatch):
    seen = _capture(monkeypatch)
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1", title="a\nb")})["plan"][0]
    wr._lane_team_state(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    assert "\n" not in json.loads(_flag(seen["argv"], "--value"))["key_finding"]


def test_lane_argv_flags_match_each_wrapper_surface(monkeypatch):
    """Pin the flags each lane passes — these were verified against the real
    wrappers' arg parsers, so a rename downstream should redden here."""
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})["plan"][0]
    root = Path("/nonexistent")

    seen = _capture(monkeypatch)
    wr._lane_journal(item, "zeta", "2026-08-07T00:00:00", root)
    for f in ("--goal", "--outcome-class", "--summary", "--work-class"):
        assert f in seen["argv"], f

    seen = _capture(monkeypatch)
    wr._lane_findings(item, "zeta", "2026-08-07T00:00:00", root)
    for f in ("--goal", "--aspiration", "--category", "--source"):
        assert f in seen["argv"], f

    seen = _capture(monkeypatch)
    wr._lane_impk(item, "zeta", "2026-08-07T00:00:00", root, 3)
    # `velocity` is a bare POSITIONAL: state-update-audit.sh execs
    # `python3 state-update-audit.py "$@"` and its DISPATCH maps the name.
    assert "velocity" in seen["argv"]
    assert _flag(seen["argv"], "--artifacts-count") == "3"

    seen = _capture(monkeypatch)
    wr._lane_team_state(item, "zeta", "2026-08-07T00:00:00", root)
    assert _flag(seen["argv"], "--field") == "recent_completions"
    assert _flag(seen["argv"], "--operation") == "append"


# ──────── : reducer-only guard against skip-and-exit-0 writers ────────
#
# THE DEFECT THESE PIN. `journal-append.sh` (~L109-116) logs a BODY=worker SKIP
# to stderr and `exit 0`. `retrospect` reads `rc == 0` as landed, so on a worker
# the journal lane is counted without being written, `artifacts_count` is
# inflated, and — because the marker is stamped whenever `wrote > 0`, and the
# other three lanes DO write from a worker — the retry is suppressed FOREVER for
# a goal now missing a lane.
#
# The goal requires "a regression test proven to redden when the skip condition
# is re-introduced". Deleting the `role == "worker"` refusal in `main` reddens
# `test_worker_body_is_refused_before_any_lane_runs` (rc 3 -> 0) AND
# `test_refusal_runs_before_lanes_and_marker` (0 lane calls -> 4, 0 markers -> 1),
# which is the harm itself and not a proxy for it.


def _fake_wm(tmp_path: Path, agent: str, sid: str) -> Path:
    """Create the forked per-session WM file that MAKES a Body a worker."""
    d = tmp_path / "agents" / agent / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "working-memory.yaml").write_text("slots: {}\n", encoding="utf-8")
    return d / "working-memory.yaml"


def test_body_role_worker_reducer_unknown(tmp_path, monkeypatch):
    """The predicate is the forked sessions/<SID>/working-memory.yaml, derived
    locally per guard-2445 — the same one journal-append.sh keys its skip on."""
    monkeypatch.setattr(wr, "agent_dir", lambda a: tmp_path / "agents" / a,
                        raising=False)
    monkeypatch.delenv("MIND_SID", raising=False)

    # unknown: no sid at all — unevaluated, NOT "reducer" (guard-2913)
    assert wr.body_role("zeta") == "unknown"
    assert wr.body_role("", sid="s1") == "unknown"

    # reducer: sid known, no forked WM
    assert wr.body_role("zeta", sid="s1") == "reducer"

    # worker: the forked WM exists for THIS sid
    _fake_wm(tmp_path, "zeta", "s1")
    assert wr.body_role("zeta", sid="s1") == "worker"
    # ...and is scoped to that sid, not to the agent
    assert wr.body_role("zeta", sid="s2") == "reducer"


def test_body_role_is_unknown_when_the_resolver_raises(monkeypatch):
    """A failed `agent_dir` yields `unknown`, never a fabricated `reducer`.

    This branch had NO coverage before g-306-262. The old code fell back to
    `root / "agents" / agent` — a path that cannot exist, so `.exists()` was
    False and the function returned a confident `reducer` it never verified,
    admitting a WORKER into the reducer-only path and stamping a marker that
    suppresses the retry permanently. Asserting `unknown` here is what keeps
    the wrong verdict from being reintroduced as a "harmless" fallback.
    """
    def _boom(_a):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(wr, "agent_dir", _boom, raising=False)
    # sid is present and the agent is named, so the ONLY thing that can make
    # this unknown is the resolver failure — the branch under test.
    assert wr.body_role("zeta", sid="s1") == "unknown"


def test_body_role_bakes_in_no_literal_agents_segment():
    """No literal `agents/` path segment survives in the module ().

    CLAUDE.md's "literal-string hardcoder" class is invisible to all three of
    its documented audit greps, so the only thing that can hold this line is a
    test that reads the source. A reintroduced fallback reddens here even if it
    is spelled differently from the one that was removed.
    """
    src = Path(wr.__file__).read_text(encoding="utf-8")
    assert '"agents"' not in src
    assert "'agents'" not in src


def test_worker_body_is_refused_before_any_lane_runs(tmp_path, monkeypatch, capsys):
    """rc=3 + a structured refusal naming the body role."""
    _fake_wm(tmp_path, "zeta", "sid-w")
    monkeypatch.setenv("MIND_SID", "sid-w")
    monkeypatch.setattr(wr, "body_role", lambda a, sid=None: "worker")

    rc = wr.main(["--agent", "zeta", "--goal-ids", "g-306-1", "--apply"])
    assert rc == 3
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert doc["error"] == wr.REFUSE_NOT_REDUCER
    assert doc["body_role"] == "worker"


def test_refusal_runs_before_lanes_and_marker(tmp_path, monkeypatch):
    """The refusal must precede EVERY side effect — no lane call, no marker.

    This is the test the goal asks for: it fails if the guard is removed,
    because a worker would then run all four lanes (journal silently declining)
    and stamp the marker on the inflated count.
    """
    calls = {"lanes": 0, "markers": 0, "records": 0}

    def _lane(*a, **k):
        calls["lanes"] += 1
        return 0, "", ""

    for name in ("_lane_team_state", "_lane_journal", "_lane_findings", "_lane_impk"):
        monkeypatch.setattr(wr, name, _lane)
    monkeypatch.setattr(wr, "_write_marker",
                        lambda *a, **k: (calls.__setitem__("markers", calls["markers"] + 1),
                                         (0, "", ""))[1])
    monkeypatch.setattr(wr, "load_records",
                        lambda ids, root=None: (calls.__setitem__("records", calls["records"] + 1),
                                                {})[1])
    monkeypatch.setattr(wr, "body_role", lambda a, sid=None: "worker")

    rc = wr.main(["--agent", "zeta", "--goal-ids", "g-306-1,g-306-2", "--apply"])
    assert rc == 3
    assert calls == {"lanes": 0, "markers": 0, "records": 0}


def test_reducer_and_unknown_both_proceed_and_report_the_role(monkeypatch, capsys):
    """`unknown` must NOT be refused — the check is unevaluated, not failed —
    but it must be REPORTED, never silently folded into `reducer`."""
    monkeypatch.setattr(wr, "load_records", lambda ids, root=None: {})
    for role in ("reducer", "unknown"):
        monkeypatch.setattr(wr, "body_role", lambda a, sid=None, _r=role: _r)
        rc = wr.main(["--agent", "zeta", "--goal-ids", "g-306-1"])
        assert rc == 0, role
        doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert doc["body_role"] == role
        assert "error" not in doc
