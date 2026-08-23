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
    monkeypatch.setattr(wr, "_lane_experience", rec.lane("experience"))
    monkeypatch.setattr(wr, "_lane_encoding", rec.lane("encoding"))
    monkeypatch.setattr(wr, "_lane_impk", rec.lane("impk"))
    monkeypatch.setattr(wr, "_write_marker", rec.marker(store))


# The lanes whose INPUT can be absent, so they SKIP rather than run. Derived
# here once: every assertion below subtracts from RUN_LANES rather than listing
# lane names, so adding a capture-fed lane updates the expectations by editing
# this set. ( added `encoding` and broke two assertions that had
# hardcoded `experience` as the only absent-able lane.)
CAPTURE_FED = {"experience", "encoding"}


def test_retrospect_runs_four_lanes_and_marks(monkeypatch):
    """No captures reached this reducer, so the capture-fed lanes are inapplicable.

    Every OTHER lane derives what it writes from the goal record, so all of them
    always run; only the CAPTURE_FED lanes have an input that can be absent. They
    are SKIPPED here rather than failed, and a skip must not count as a write.
    """
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder()
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"))
    assert sorted(n for n, _ in rec.calls) == sorted(
        set(wr.RUN_LANES) - CAPTURE_FED)
    assert out["lanes"]["experience"]["skipped"] == wr.SKIP_NO_CAPTURE
    assert out["lanes"]["experience"]["ok"] is False
    assert out["lanes"]["encoding"]["skipped"] == wr.SKIP_NO_ENCODING
    assert out["lanes"]["encoding"]["ok"] is False
    assert out["lanes_written"] == 3          # impk is not an artifact of itself
    assert out["marked"] is True
    assert out["pending_judgment_lanes"] == list(wr.REPORT_LANES)


def test_retrospect_runs_the_experience_lane_when_a_capture_arrived(monkeypatch):
    """An EXP capture runs the experience lane; the encoding lane still skips.

    The two capture-fed lanes are joined to DIFFERENT slots, so a capture for one
    must not activate the other — that independence is what this asserts.
    """
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder()
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    captures = {"g-306-1": [{"goal_id": "g-306-1", "execution_summary": "did a thing"}]}
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"),
                        captures)
    assert sorted(n for n, _ in rec.calls) == sorted(
        set(wr.RUN_LANES) - {"encoding"})
    assert out["lanes"]["encoding"]["skipped"] == wr.SKIP_NO_ENCODING
    assert out["lanes"]["experience"]["ok"] is True
    assert out["lanes"]["experience"]["entries"] == 1
    assert out["lanes_written"] == 4
    # The imp@k lane must see the experience write in its artifact count —
    # otherwise the retrospective under-reports what it produced.
    assert out["lanes"]["impk"]["artifacts_count"] == 4


def test_a_capture_for_a_different_goal_does_not_leak_into_this_one(monkeypatch):
    """Captures are joined by goal_id. A near-miss must skip, never mis-encode."""
    store = {"g-306-1": _rec("g-306-1")}
    rec = _Recorder()
    _stub(monkeypatch, rec, store)
    item = wr.decide(["g-306-1"], store)["plan"][0]
    out = wr.retrospect(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"),
                        {"g-306-2": [{"goal_id": "g-306-2", "note": "other goal"}]})
    assert out["lanes"]["experience"]["skipped"] == wr.SKIP_NO_CAPTURE
    assert "experience" not in [n for n, _ in rec.calls]


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

def _capture(monkeypatch, stdout=""):
    seen = {}

    # `**kw` because the experience lane passes `stdin=` — a fake narrower than
    # the real signature would TypeError instead of testing anything.
    def _fake_run(argv, timeout=90, **kw):
        seen["argv"] = list(argv)
        seen["stdin"] = kw.get("stdin")
        return (0, stdout, "")

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

    seen = _capture(monkeypatch)
    wr._lane_experience(item, "zeta", "2026-08-07T00:00:00", root,
                        [{"goal_id": "g-306-1", "execution_summary": "x" * 40}])
    for f in ("--goal", "--skill-slug", "--category", "--summary", "--trace-file"):
        assert f in seen["argv"], f
    assert _flag(seen["argv"], "--skill-slug") == wr.EXP_SKILL_SLUG


# ──────────────── : the experience lane ────────────────
#
# The lane differs from its four siblings in one way that drives every test
# here: its input is the worker's `exp_capture` slot, which can be absent,
# heterogeneous, or shaped differently from what the record schema accepts.


def test_index_captures_buckets_by_goal_and_drops_unjoinable_rows():
    doc = [{"goal_id": "g-1-1", "note": "a"}, {"goal_id": "g-1-1", "note": "b"},
           {"goal_id": "g-2-2", "note": "c"},
           {"note": "no goal id"}, {"goal_id": "   "}, "junk", None]
    idx = wr.index_captures(doc)
    assert sorted(idx) == ["g-1-1", "g-2-2"]
    assert [e["note"] for e in idx["g-1-1"]] == ["a", "b"]     # order preserved


def test_index_captures_returns_empty_for_a_non_list_slot():
    for value in (None, {}, "", 7):
        assert wr.index_captures(value) == {}


def test_anchor_strings_become_the_key_content_objects_the_schema_requires():
    """exp_capture writes STRINGS; `_validate_record` rejects anything but dicts
    carrying both `key` and `content`. Pinning the transform pins the join."""
    objs = wr.exp_anchor_objects([{"verbatim_anchors": ["rc=2", "core/x.py:41"]}])
    assert all(isinstance(o, dict) and "key" in o and "content" in o for o in objs)
    assert [o["content"] for o in objs] == ["rc=2", "core/x.py:41"]


def test_anchor_objects_dedup_across_entries_and_preserve_a_dict_anchor():
    objs = wr.exp_anchor_objects([
        {"verbatim_anchors": ["dup", {"key": "given", "content": "kept"}]},
        {"verbatim_anchors": ["dup", "  ", "fresh"]},
    ])
    assert [o["content"] for o in objs] == ["dup", "kept", "fresh"]
    assert objs[1]["key"] == "given"          # an explicit key is not overwritten


def test_anchor_truncation_announces_itself_rather_than_dropping_silently():
    # Every number below is a LITERAL on purpose. The first version of this test
    # sized its input as `ANCHOR_RECORD_MAX + 7` and asserted `== MAX + 1`, which
    # made it self-adjusting: raising the cap to 9999 left it green while the
    # record grew unbounded. A mutation proof caught it (M1, VACUOUS), so the
    # bound is pinned by value here and the input size no longer tracks it.
    assert wr.ANCHOR_RECORD_MAX == 25
    objs = wr.exp_anchor_objects(
        [{"verbatim_anchors": [f"anchor-value-{i}" for i in range(32)]}])
    assert len(objs) == 26                     # 25 kept + 1 truncation notice
    assert objs[-1]["key"] == "anchors-truncated"
    assert "7 further anchor" in objs[-1]["content"]


def test_summary_prefers_narrative_and_falls_back_to_the_title():
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1", title="A title")})["plan"][0]
    assert wr.exp_summary([{"execution_summary": "  ran   the thing  "}], item) \
        == "ran the thing"
    # The looser live shape carries `note`/`lesson` instead — measured, not assumed.
    assert wr.exp_summary([{"note": "loose shape"}], item) == "loose shape"
    assert "A title" in wr.exp_summary([{}], item)
    assert "A title" in wr.exp_summary([], item)


def test_render_trace_carries_every_captured_field():
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})["plan"][0]
    body = wr.render_trace(item, [{
        "execution_summary": "SUMMARY-TOKEN",
        "key_decisions": ["DECISION-TOKEN"],
        "verbatim_anchors": ["ANCHOR-TOKEN"],
        "outcome_class": "deep",
        "surprise_level": 6,
        "what_worked": "WORKED-TOKEN",
    }], "zeta", "2026-08-07T00:00:00")
    for token in ("SUMMARY-TOKEN", "DECISION-TOKEN", "ANCHOR-TOKEN",
                  "WORKED-TOKEN", "deep", "g-306-1"):
        assert token in body, token
    # A trace shorter than MIN_TRACE_BYTES (200) only warns at the endpoint, but
    # a real capture should clear it comfortably — a near-empty .md would be a
    # worse record than none.
    assert len(body.encode("utf-8")) > 200


def test_render_trace_survives_the_loose_capture_shape():
    """4 of 12 live entries carry no execution_summary. They must still render."""
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})["plan"][0]
    body = wr.render_trace(item, [{"goal_id": "g-306-1", "lesson": "LESSON-TOKEN"}],
                           "zeta", "2026-08-07T00:00:00")
    assert "LESSON-TOKEN" in body


def test_experience_lane_sends_type_and_anchors_on_stdin(monkeypatch):
    """Neither field has a CLI flag on the wrapper, so a regression that dropped
    the stdin payload would silently file records with no anchors at all."""
    seen = _capture(monkeypatch)
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})["plan"][0]
    wr._lane_experience(item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"),
                        [{"verbatim_anchors": ["rc=2"]}])
    payload = json.loads(seen["stdin"])
    assert payload["type"] == wr.EXP_TYPE
    assert payload["verbatim_anchors"] == [{"key": "anchor-01", "content": "rc=2"}]


def test_experience_lane_writes_a_real_trace_file_and_cleans_it_up(monkeypatch):
    """The endpoint rejects a missing or empty trace, so the staged file must
    actually exist with content AT CALL TIME — and must not be left behind."""
    seen = {}

    def _fake_run(argv, timeout=90, **kw):
        path = Path(argv[argv.index("--trace-file") + 1])
        seen["existed"] = path.exists()
        seen["bytes"] = path.stat().st_size if path.exists() else 0
        seen["path"] = path
        return (1, "", "boom")            # failure path: cleanup must still fire

    monkeypatch.setattr(wr, "_run", _fake_run)
    item = wr.decide(["g-306-1"], {"g-306-1": _rec("g-306-1")})["plan"][0]
    rc, _out, _err = wr._lane_experience(
        item, "zeta", "2026-08-07T00:00:00", Path("/nonexistent"),
        [{"execution_summary": "y" * 300}])
    assert seen["existed"] is True and seen["bytes"] > 200
    assert rc == 1
    assert not seen["path"].exists()      # no orphan left on the failure path


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
