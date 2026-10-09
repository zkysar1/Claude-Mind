#!/usr/bin/env python3
"""Pins for the worker Phase 1 walk and the supply-gap park it feeds ().

THE DEFECT: worker-loop Phase 1 cut the ranking to 10 or 40 rows FIRST and only
then asked `worker_execute.py goal-eligible` once per candidate. On 2026-09-28
00:09Z a worker Body ran `goal-selector.sh select --top 40`, made 0 gate calls,
posted "SELECT returned no eligible goal ... all unclaimed candidates are
agent-queue-fenced ... or reducer-only", and parked. 33 of that top 40 passed
the gate, and other Bodies claimed its rank-1 and rank-2 goals within 30 min.

THE CONTRACT PINNED HERE:
  1. `worker_execute.py select-walk --top N` judges the WHOLE ranking in the
     scorer's order and drops reducer-only rows BEFORE the cut, so a run of them
     at the top cannot hide claimable work below (the order-of-operations fix).
     The walk lives on the worker side: goal-selector stays role-blind
     (test_selection_stays_role_blind pins that half).
  2. Each kept row carries its verdict word; nothing is re-sorted (guard-5135); a field
     that is not text is judged, never raised on (g-375-157).
  3. The agent-queue claim probe (a claim-table scan) runs once per walk.
  4. The census is written only into an existing session dir.
  5. A supply-gap park is refused, exit 5, until every census row is claimed or
     declined by id with a reason; the reducer-gone park is unchanged.
  6. The census names the scorer's top pick and whether the walk dropped it; the
     claim gate reads that pair (g-375-133).
  7. The HIGH-goal pace forecast (g-375-152): a yielded HIGH row is dropped before
     the cut and listed in the census; MEDIUM and LOW rows are never forecast; k
     counts yielded and MEDIUM rows but not reducer-only ones; select-walk measures
     the basis once, only when the walk meets a HIGH row, logs that walk once, and
     takes every row when the measurement fails. A park whose walk left HIGH rows
     to faster workers stamps supply_seen_at, and the park cap runs from it.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import worker_execute as we  # noqa: E402


def _load_body_manifest():
    spec = importlib.util.spec_from_file_location("body_manifest", CORE_SCRIPTS / "body-manifest.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bm = _load_body_manifest()
SID = "worker-sid-0053"
AGENT = "alpha"


def _row(i, **extra):
    """A row in the selector's brief shape (goal-selector.py _brief_rows)."""
    row = {"goal_id": f"g-900-{i:02d}", "source": "world", "title": f"T{i}", "score": 10.0 - i / 100,
           "skill": None, "executable_by_role": None, "recurring": False, "routed_to_me": False,
           "why": "priority +1"}
    row.update(extra)
    return row


def _now_naive_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- 1-3: the walk -------------------------------------------------------------

def test_reducer_only_rows_are_dropped_BEFORE_the_cut():
    """Twelve reducer-only rows at the top used to fill a --top 10 view entirely."""
    ranked = [_row(i, skill="/reflect") for i in range(12)] + [_row(i) for i in range(12, 27)]
    kept, census = we.worker_view(ranked, 10)
    assert [r["goal_id"] for r in kept] == [f"g-900-{i:02d}" for i in range(12, 22)]
    assert {r["verdict"] for r in kept} == {"undetermined"}
    assert census["reducer_only_skipped"] == 12 and census["walked"] == 22
    assert census["ranked_total"] == 27 and census["undetermined"] == 10


def test_the_scorer_order_is_kept_and_every_row_names_its_verdict():
    ranked = [_row(0, skill="/tree"), _row(1, skill="/reflect"), _row(2),
              _row(3, executable_by_role="worker"), _row(4, executable_by_role="reducer"),
              _row(5)]
    kept, census = we.worker_view(ranked, 10)
    assert [(r["goal_id"], r["verdict"]) for r in kept] == [
        ("g-900-00", "eligible"), ("g-900-02", "undetermined"),
        ("g-900-03", "eligible"), ("g-900-05", "undetermined")]
    assert census["eligible"] == 2 and census["undetermined"] == 2
    assert census["reducer_only_skipped"] == 2 and census["walked"] == 6
    assert census["rows"] == [{"goal_id": r["goal_id"], "verdict": r["verdict"]} for r in kept]
    assert "verdict" not in ranked[0], "the selector's rows are not mutated"


def test_the_census_names_the_scorer_top_and_whether_the_walk_dropped_it():
    """Row 0 is the goal the selector's scorer-verdict sidecar records as its top
    pick. Only a drop of THAT row counts; a dropped row below it does not."""
    _, census = we.worker_view([_row(0, skill="/reflect"), _row(1), _row(2)], 10)
    assert (census["scorer_top"], census["scorer_top_dropped"]) == ("g-900-00", True)
    _, census = we.worker_view([_row(0), _row(1, skill="/reflect"), _row(2)], 10)
    assert (census["scorer_top"], census["scorer_top_dropped"]) == ("g-900-00", False)
    _, census = we.worker_view([], 10)
    assert (census["scorer_top"], census["scorer_top_dropped"]) == (None, False)


def test_a_field_that_is_not_text_is_judged_never_raised():
    """goal_eligibility promises a fallback for an unrecognised value, but a role or source that
    is not text (a stray True, a list, a number) raised in .strip() and took the walk down. It is
    read as text now, inside goal_eligibility, so every caller has it and a bad role still names
    itself (g-375-157). A skill never raised: normalize_skill reads it through str(). The marked
    row is the control."""
    ranked = [_row(0, executable_by_role=True), _row(1, executable_by_role=["worker"]),
              _row(2, skill=["/reflect"]), _row(3, source=7), _row(4, executable_by_role="worker")]
    kept, _census = we.worker_view(ranked, 10)
    assert [(r["goal_id"], r["verdict"]) for r in kept] == [
        ("g-900-00", "undetermined"), ("g-900-01", "undetermined"), ("g-900-02", "undetermined"),
        ("g-900-03", "undetermined"), ("g-900-04", "eligible")]
    for bad in (True, ["worker"], 7):
        assert "UNRECOGNISED" in we.goal_eligibility(None, bad).reason, bad


def test_the_agent_queue_claim_probe_runs_once_per_walk(monkeypatch):
    calls = []

    def probe(agent):
        calls.append(agent)
        return (False, "live-claims")

    monkeypatch.setattr(we, "_agent_queue_claim_probe", probe)
    ranked = [_row(i, source="agent") for i in range(5)] + [_row(i) for i in range(5, 8)]
    kept, census = we.worker_view(ranked, 10, agent=AGENT)
    assert len(calls) == 1, "one claim-table scan per walk, not one per agent-queue row"
    assert census["reducer_only_skipped"] == 5
    assert [r["goal_id"] for r in kept] == ["g-900-05", "g-900-06", "g-900-07"]


# --- the one-call CLI and the selector it runs ------------------------------------

def test_select_walk_prints_the_kept_rows_the_census_and_the_selector_banners(
        tmp_path, monkeypatch, capsys):
    ranked = [_row(0, executable_by_role="reducer")] + [_row(i) for i in range(1, 4)]
    selector_err = ("[goal-selector] strategic focus: a banner the Body must still see\n"
                    "[goal-selector] --top 1000000: showing 4 of 4 ranked candidates\n")
    monkeypatch.setattr(we, "_ranked_rows", lambda: (ranked, selector_err))
    sess = tmp_path / "sessions" / SID
    sess.mkdir(parents=True)
    monkeypatch.setattr(we, "agent_session_dir", lambda agent, sid: tmp_path / "sessions" / sid)
    monkeypatch.setenv("MIND_SID", SID)
    monkeypatch.setenv("MIND_AGENT", AGENT)
    assert we._main(["select-walk", "--top", "10"]) == 0
    cap = capsys.readouterr()
    rows = json.loads(cap.out)
    assert [(r["goal_id"], r["verdict"]) for r in rows] == [
        ("g-900-01", "undetermined"), ("g-900-02", "undetermined"), ("g-900-03", "undetermined")]
    assert "a banner the Body must still see" in cap.err
    assert "--top 1000000" not in cap.err, "the selector's slice line names a top never asked for"
    assert "[select-walk] showing 3 of 4" in cap.err
    assert "1 reducer-only row(s) among the first 4 dropped BEFORE the cut" in cap.err
    doc = json.loads((sess / we.SELECT_CENSUS_FILENAME).read_text(encoding="utf-8"))
    assert doc["sid"] == SID and [r["goal_id"] for r in doc["rows"]] == ["g-900-01", "g-900-02", "g-900-03"]
    assert (doc["scorer_top"], doc["scorer_top_dropped"]) == ("g-900-00", True), \
        "the file the claim gate reads carries the pair"


def test_select_walk_refuses_top_zero_and_reports_a_failed_selector(monkeypatch, capsys):
    assert we._main(["select-walk", "--top", "0"]) == 2

    def boom():
        raise RuntimeError("goal-selector.sh select exited 1: store unreachable")

    monkeypatch.setattr(we, "_ranked_rows", boom)
    assert we._main(["select-walk", "--top", "10"]) == 3
    cap = capsys.readouterr()
    assert cap.out == "", "a failed selector must never print an empty list"
    assert "store unreachable" in cap.err


def _fake_selector(tmp_path, body):
    script = tmp_path / "fake-selector.sh"
    script.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    script.chmod(0o755)
    return script


def test_ranked_rows_runs_the_selector_with_a_whole_ranking_slice(tmp_path, monkeypatch):
    """The real subprocess path, against a stand-in selector that echoes its args."""
    script = _fake_selector(tmp_path, (
        "echo \"[{\\\"goal_id\\\": \\\"g-900-01\\\", \\\"args\\\": \\\"$*\\\"}]\"\n"
        "echo '[goal-selector] a banner' >&2\n"))
    monkeypatch.setattr(we, "_SELECTOR", script)
    rows, err = we._ranked_rows()
    assert rows == [{"goal_id": "g-900-01", "args": f"select --top {we._SELECT_EVERY_ROW}"}]
    assert "a banner" in err


@pytest.mark.parametrize("body, words", [
    ("echo 'store unreachable' >&2; exit 4\n", "exited 4"),
    ("echo 'not json'\n", "printed no JSON"),
    ("echo '{\"goal_id\": \"g-900-01\"}'\n", "printed a dict"),
])
def test_ranked_rows_raises_rather_than_returning_an_empty_ranking(tmp_path, monkeypatch, body, words):
    monkeypatch.setattr(we, "_SELECTOR", _fake_selector(tmp_path, body))
    with pytest.raises(RuntimeError, match=words):
        we._ranked_rows()


# --- 4: the census write --------------------------------------------------------

def test_the_census_is_written_only_into_an_existing_session_dir(tmp_path):
    census = {"top": 40, "rows": [{"goal_id": "g-900-01", "verdict": "undetermined"}]}
    missing = tmp_path / "sessions" / "nope"
    we.write_select_census(census, missing)
    assert not missing.exists(), "a census write must never create a session dir"
    sess = tmp_path / "sessions" / "sid-1"
    sess.mkdir(parents=True)
    we.write_select_census(census, sess)
    doc = json.loads((sess / we.SELECT_CENSUS_FILENAME).read_text(encoding="utf-8"))
    assert doc["rows"] == census["rows"] and doc["top"] == 40 and doc["sid"] == "sid-1"
    age = (_now_naive_utc() - datetime.fromisoformat(doc["ts"])).total_seconds()
    assert 0 <= age < 120, "ts is naive UTC, the form supply_gap_refusals parses"
    assert list(sess.glob(".select-census.*")) == [], "no temp file left behind"


# --- 5: the refusal rules -------------------------------------------------------

def _census(top=40, rows=(), age_s=60):
    ts = (_now_naive_utc() - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%S")
    return {"ts": ts, "top": top, "walked": top, "eligible": 0, "undetermined": len(rows),
            "reducer_only_skipped": 0,
            "rows": [{"goal_id": g, "verdict": "undetermined"} for g in rows]}


def test_no_census_means_the_gate_was_never_asked():
    (reason,) = we.supply_gap_refusals(None, {})
    assert "no SELECT census" in reason


@pytest.mark.parametrize("census, words", [
    (_census(age_s=we.CENSUS_MAX_AGE_S + 120), "min old"),
    (dict(_census(), ts="yesterday"), "no readable ts"),
    (_census(top=10, rows=[f"g-900-{i:02d}" for i in range(10)]), "walk stopped at --top 10"),
])
def test_a_stale_unreadable_or_shallow_census_is_refused(census, words):
    (reason,) = we.supply_gap_refusals(census, {})
    assert words in reason


def test_each_undeclined_row_is_named_and_declined_rows_are_not():
    census = _census(rows=["g-900-01", "g-900-02", "g-900-03"])
    refusals = we.supply_gap_refusals(census, {"g-900-02": "needs the Studio box"})
    assert len(refusals) == 2
    assert refusals[0].startswith("g-900-01 is undetermined") and "--decline g-900-01=" in refusals[0]
    assert refusals[1].startswith("g-900-03 is undetermined")


def test_a_fully_answered_or_empty_census_allows_the_park():
    census = _census(rows=["g-900-01", "g-900-02"])
    assert we.supply_gap_refusals(census, {"g-900-01": "a", "g-900-02": "b"}) == []
    assert we.supply_gap_refusals(_census(rows=[]), {}) == [], "an empty walk is a real gap"
    short = _census(top=10, rows=["g-900-01"])
    assert we.supply_gap_refusals(short, {"g-900-01": "c"}) == [], \
        "a --top 10 view that came back short walked the whole ranking"


# --- 5 (CLI): park --supply-gap -------------------------------------------------

def _worker_body(tmp_path, monkeypatch):
    adir = tmp_path / "agents" / AGENT
    (adir / "session").mkdir(parents=True)
    (adir / "session" / "running-session-id").write_text("reducer-sid-9\n", encoding="utf-8")
    (adir / "session" / "working-memory.yaml").write_bytes(b"slot: x\n")
    bm.write_manifest(SID, AGENT, project_root=tmp_path, role="worker")
    monkeypatch.setattr(bm, "_project_root", lambda: tmp_path)
    return adir / "sessions" / SID


def _state(tmp_path):
    return bm.read_manifest(SID, AGENT, project_root=tmp_path)["body_state"]


def _park(capsys, *extra):
    rc = bm.main(["park", "--sid", SID, "--agent", AGENT, *extra])
    cap = capsys.readouterr()
    return rc, cap.out, cap.err


def test_a_supply_park_with_no_census_is_refused_and_parks_nothing(tmp_path, monkeypatch, capsys):
    _worker_body(tmp_path, monkeypatch)
    rc, out, err = _park(capsys, "--supply-gap")
    assert (rc, out.strip()) == (5, "refused") and "no SELECT census" in err
    assert _state(tmp_path) == "active"


def test_a_supply_park_names_the_rows_left_open(tmp_path, monkeypatch, capsys):
    sess = _worker_body(tmp_path, monkeypatch)
    (sess / we.SELECT_CENSUS_FILENAME).write_text(
        json.dumps(_census(rows=["g-900-01", "g-900-02"])), encoding="utf-8")
    rc, out, err = _park(capsys, "--supply-gap", "--decline", "g-900-01=needs the Studio box")
    assert rc == 5 and "g-900-02 is undetermined" in err and "g-900-01" not in err
    assert _state(tmp_path) == "active"


def test_a_supply_park_with_every_row_declined_parks_and_prints_the_census(
        tmp_path, monkeypatch, capsys):
    sess = _worker_body(tmp_path, monkeypatch)
    (sess / we.SELECT_CENSUS_FILENAME).write_text(
        json.dumps(_census(rows=["g-900-01", "g-900-02"])), encoding="utf-8")
    rc, out, _ = _park(capsys, "--supply-gap", "--decline", "g-900-01=a",
                       "--decline", "g-900-02=b")
    lines = out.strip().splitlines()
    assert rc == 0 and lines[0] == "parked"
    assert lines[1].startswith("select census --top 40:") and lines[1].endswith("declined 2")
    assert _state(tmp_path) == "parked"


@pytest.mark.parametrize("args", [["--decline", "g-900-01=a"], ["--supply-gap", "--decline", "g-900-01"],
                                  ["--supply-gap", "--decline", "g-900-01=  "]])
def test_a_misused_decline_is_a_usage_error(tmp_path, monkeypatch, capsys, args):
    _worker_body(tmp_path, monkeypatch)
    rc, _, err = _park(capsys, *args)
    assert rc == 2 and "--decline" in err
    assert _state(tmp_path) == "active"


def test_the_reducer_gone_park_is_unchanged(tmp_path, monkeypatch, capsys):
    """Positive control: no flag, no census, and the Phase 0.5 park still parks."""
    _worker_body(tmp_path, monkeypatch)
    rc, out, _ = _park(capsys)
    assert (rc, out.strip()) == (0, "parked") and _state(tmp_path) == "parked"


# --- 7: the HIGH-goal pace forecast () ------------------------------------

def _pace(yield_through=0, calls=None):
    """A pace(k) that yields every HIGH row at a place <= yield_through."""
    def pace(k):
        if calls is not None:
            calls.append(k)
        if k <= yield_through:
            return {"decision": "yield", "k": k, "fast_finish_hours": float(k), "own_finish_hours": 20.0}
        return {"decision": "take", "k": k, "fast_finish_hours": 99.0, "own_finish_hours": 20.0}
    return pace


def test_a_yielded_high_row_is_dropped_before_the_cut_and_listed():
    ranked = [_row(0, priority="HIGH"), _row(1, priority="HIGH"), _row(2, priority="MEDIUM"),
              _row(3, priority="HIGH")]
    kept, census = we.worker_view(ranked, 2, pace=_pace(yield_through=2))
    assert [r["goal_id"] for r in kept] == ["g-900-02", "g-900-03"]
    assert census["pace_yielded"] == 2 and census["scorer_top_yielded"] is True
    assert census["forecasts"] == [
        {"goal_id": "g-900-00", "k": 1, "decision": "yield", "fast_finish_hours": 1.0},
        {"goal_id": "g-900-01", "k": 2, "decision": "yield", "fast_finish_hours": 2.0},
        {"goal_id": "g-900-03", "k": 4, "decision": "take", "fast_finish_hours": 99.0}]
    assert census["rows"] == [{"goal_id": "g-900-02", "verdict": "undetermined"},
                              {"goal_id": "g-900-03", "verdict": "undetermined"}]


def test_medium_and_low_rows_are_never_forecast():
    calls = []
    ranked = [_row(0, priority="MEDIUM"), _row(1, priority="LOW"), _row(2), _row(3, priority="HIGH")]
    kept, census = we.worker_view(ranked, 10, pace=_pace(yield_through=100, calls=calls))
    assert calls == [4], "only the HIGH row is forecast, at its place among takeable rows"
    assert [r["goal_id"] for r in kept] == ["g-900-00", "g-900-01", "g-900-02"]
    assert census["pace_yielded"] == 1 and census["scorer_top_yielded"] is False


def test_k_counts_yielded_and_medium_rows_but_never_reducer_only_ones():
    calls = []
    ranked = [_row(0, skill="/reflect", priority="HIGH"),       # reducer-only: not a place
              _row(1, priority="HIGH"), _row(2, priority="MEDIUM"),
              _row(3, executable_by_role="reducer", priority="HIGH"),
              _row(4, priority="HIGH")]
    kept, census = we.worker_view(ranked, 10, pace=_pace(yield_through=1, calls=calls))
    assert calls == [1, 3]
    assert [r["goal_id"] for r in kept] == ["g-900-02", "g-900-04"]
    assert census["reducer_only_skipped"] == 2 and census["scorer_top_dropped"] is True
    assert census["scorer_top_yielded"] is False, "the top was dropped as reducer-only, not yielded"


def test_without_a_pace_the_walk_is_unchanged():
    kept, census = we.worker_view([_row(0, priority="HIGH"), _row(1, priority="HIGH")], 10)
    assert [r["goal_id"] for r in kept] == ["g-900-00", "g-900-01"]
    assert (census["pace_yielded"], census["forecasts"], census["scorer_top_yielded"]) == (0, [], False)


ARMED = {"own_harness": "zakcode", "own_pace_hours": 20.0, "window_hours": 20.0,
         "rate_per_hour": 1.0, "fast_pace_hours": 1.0, "armed": True,
         "faster": {"claude-code": {"live": 2, "pace_hours": 1.0, "closed_in_window": 20}},
         "reason": "faster workers closed 20 goal(s) in the last 20 h, 2 live now"}


def _select_walk(tmp_path, monkeypatch, capsys, ranked, basis=None, error=None):
    """Run select-walk with a stand-in measurement and log; return (stdout rows,
    stderr, census file, measurement calls, logged walks)."""
    import pace_forecast
    measured, logged = [], []

    def measure(agent, sid, now=None):
        measured.append((agent, sid))
        if error is not None:
            raise error
        return dict(basis)

    monkeypatch.setattr(pace_forecast, "measure_basis", measure)
    monkeypatch.setattr(pace_forecast, "log_walk",
                        lambda b, f, *, caller: logged.append((b, f, caller)))
    monkeypatch.setattr(we, "_ranked_rows", lambda: (ranked, ""))
    sess = tmp_path / "sessions" / SID
    sess.mkdir(parents=True)
    monkeypatch.setattr(we, "agent_session_dir", lambda agent, sid: tmp_path / "sessions" / sid)
    monkeypatch.setenv("MIND_SID", SID)
    monkeypatch.setenv("MIND_AGENT", AGENT)
    assert we._main(["select-walk", "--top", "10"]) == 0
    cap = capsys.readouterr()
    doc = json.loads((sess / we.SELECT_CENSUS_FILENAME).read_text(encoding="utf-8"))
    return json.loads(cap.out), cap.err, doc, measured, logged


def test_select_walk_measures_once_logs_once_and_hides_the_yielded_rows(tmp_path, monkeypatch, capsys):
    ranked = [_row(0, priority="HIGH"), _row(1, priority="MEDIUM"), _row(2, priority="HIGH")]
    rows, err, doc, measured, logged = _select_walk(tmp_path, monkeypatch, capsys, ranked, basis=ARMED)
    assert [r["goal_id"] for r in rows] == ["g-900-01"]
    assert measured == [(AGENT, SID)], "one measurement per walk, however many HIGH rows"
    assert "HIGH-goal pace forecast: faster workers closed 20 goal(s)" in err
    assert "2 HIGH row(s) left to faster sessions" in err
    assert doc["pace"] == ARMED and doc["pace_yielded"] == 2
    assert [(f["goal_id"], f["k"], f["decision"]) for f in doc["forecasts"]] == [
        ("g-900-00", 1, "yield"), ("g-900-02", 3, "yield")]
    assert len(logged) == 1 and logged[0] == (ARMED, doc["forecasts"], "worker_execute.py select-walk")


def test_a_forecast_module_that_will_not_import_shows_every_row(tmp_path, monkeypatch, capsys):
    """The forecast both walks share lives in pace_forecast (). If it cannot even be
    imported, the walk still stands and shows every row, as before the forecast existed."""
    import builtins
    real_import = builtins.__import__

    def no_pace(name, *args, **kwargs):
        if name == "pace_forecast":
            raise ImportError("pace_forecast unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(we, "_ranked_rows", lambda: ([_row(0, priority="HIGH"), _row(1)], ""))
    (tmp_path / "sessions" / SID).mkdir(parents=True)
    monkeypatch.setattr(we, "agent_session_dir", lambda agent, sid: tmp_path / "sessions" / sid)
    monkeypatch.setenv("MIND_SID", SID)
    monkeypatch.setenv("MIND_AGENT", AGENT)
    monkeypatch.setattr(builtins, "__import__", no_pace)
    assert we._main(["select-walk", "--top", "10"]) == 0
    cap = capsys.readouterr()
    assert [r["goal_id"] for r in json.loads(cap.out)] == ["g-900-00", "g-900-01"]
    assert "HIGH-goal pace forecast unavailable (ImportError: pace_forecast unavailable)" in cap.err


def test_a_walk_with_no_high_row_measures_and_logs_nothing(tmp_path, monkeypatch, capsys):
    ranked = [_row(0, priority="MEDIUM"), _row(1)]
    rows, err, doc, measured, logged = _select_walk(tmp_path, monkeypatch, capsys, ranked, basis=ARMED)
    assert [r["goal_id"] for r in rows] == ["g-900-00", "g-900-01"]
    assert (measured, logged) == ([], [])
    assert "pace" not in doc and "pace forecast" not in err


def test_a_failed_measurement_takes_every_row_and_says_so(tmp_path, monkeypatch, capsys):
    ranked = [_row(0, priority="HIGH"), _row(1, priority="HIGH")]
    rows, err, doc, measured, logged = _select_walk(
        tmp_path, monkeypatch, capsys, ranked, error=OSError("store unreachable"))
    assert [r["goal_id"] for r in rows] == ["g-900-00", "g-900-01"]
    assert len(measured) == 1, "a failed measurement is not retried within the walk"
    assert "forecast unavailable (OSError: store unreachable)" in err
    assert doc["pace"]["error"] == "OSError: store unreachable" and doc["pace_yielded"] == 0
    assert len(logged) == 1, "the failure is logged like any other walk"


def test_a_forecast_that_raises_takes_the_row_and_the_walk_stands(tmp_path, monkeypatch, capsys):
    import pace_forecast

    def boom(k, walk_basis):
        raise ZeroDivisionError("division by zero")

    monkeypatch.setattr(pace_forecast, "forecast", boom)
    ranked = [_row(0, priority="HIGH"), _row(1, priority="HIGH")]
    rows, err, doc, measured, logged = _select_walk(tmp_path, monkeypatch, capsys, ranked, basis=ARMED)
    assert [r["goal_id"] for r in rows] == ["g-900-00", "g-900-01"]
    assert "forecast unavailable (ZeroDivisionError: division by zero)" in err, \
        "positive control: the forecast ran and raised"
    assert doc["pace"]["error"] == "ZeroDivisionError: division by zero"
    assert doc["pace"]["rate_per_hour"] == ARMED["rate_per_hour"], "what was measured is kept"
    assert doc["pace_yielded"] == 0 and len(measured) == 1 and len(logged) == 1


def _set_manifest(tmp_path, **fields):
    data = bm.read_manifest(SID, AGENT, project_root=tmp_path)
    data.update(fields)
    path = tmp_path / "agents" / AGENT / "sessions" / SID / bm._MANIFEST_FILENAME
    path.write_text(bm._render_manifest(data), encoding="utf-8")


def _ago(hours):
    """A manifest stamp `hours` ago, on body-manifest's own clock (naive local)."""
    return (datetime.now() - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def test_a_park_that_left_high_rows_to_faster_workers_stamps_supply_seen(tmp_path, monkeypatch, capsys):
    sess = _worker_body(tmp_path, monkeypatch)
    (sess / we.SELECT_CENSUS_FILENAME).write_text(
        json.dumps(dict(_census(rows=["g-900-01"]), pace_yielded=3)), encoding="utf-8")
    rc, out, _ = _park(capsys, "--supply-gap", "--decline", "g-900-01=a")
    lines = out.strip().splitlines()
    assert rc == 0 and lines[0] == "parked"
    assert lines[1].endswith("declined 1; 3 HIGH row(s) left to faster workers")
    m = bm.read_manifest(SID, AGENT, project_root=tmp_path)
    assert m["supply_seen_at"] == m["parked_at"]


def test_a_park_with_no_yield_stamps_nothing(tmp_path, monkeypatch, capsys):
    sess = _worker_body(tmp_path, monkeypatch)
    (sess / we.SELECT_CENSUS_FILENAME).write_text(json.dumps(_census(rows=[])), encoding="utf-8")
    rc, out, _ = _park(capsys, "--supply-gap")
    assert rc == 0 and "faster workers" not in out
    assert "supply_seen_at" not in bm.read_manifest(SID, AGENT, project_root=tmp_path)


def test_a_re_park_that_saw_supply_restarts_the_cap_clock_and_resume_clears_it(tmp_path, monkeypatch):
    _worker_body(tmp_path, monkeypatch)
    assert bm.park_body(SID, AGENT, project_root=tmp_path) == "parked"
    _set_manifest(tmp_path, parked_at=_ago(bm.PARK_MAX_HOURS + 10), last_parked_at=_ago(2))
    assert bm.park_expired(SID, AGENT, project_root=tmp_path) is True, "positive control"
    assert bm.park_body(SID, AGENT, project_root=tmp_path, supply_seen=True) == "already-parked"
    m = bm.read_manifest(SID, AGENT, project_root=tmp_path)
    assert m["supply_seen_at"] == m["last_parked_at"] != m["parked_at"]
    assert bm.park_expired(SID, AGENT, project_root=tmp_path) is False
    bm.resume_body(SID, AGENT, project_root=tmp_path)
    assert "supply_seen_at" not in bm.read_manifest(SID, AGENT, project_root=tmp_path)


@pytest.mark.parametrize("seen, expired", [
    (None, True),                                   # no supply seen: the original clock
    (1, False),                                     # supply seen an hour ago
    (bm.PARK_MAX_HOURS + 20, True),                 # an OLDER stamp never extends the park
    ("not-a-time", False),                          # unreadable: stay parked (fail-safe)
])
def test_the_park_cap_runs_from_the_later_stamp(tmp_path, monkeypatch, seen, expired):
    _worker_body(tmp_path, monkeypatch)
    bm.park_body(SID, AGENT, project_root=tmp_path)
    stamp = _ago(seen) if isinstance(seen, (int, float)) else seen
    _set_manifest(tmp_path, parked_at=_ago(bm.PARK_MAX_HOURS + 10), supply_seen_at=stamp)
    assert bm.park_expired(SID, AGENT, project_root=tmp_path) is expired


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
