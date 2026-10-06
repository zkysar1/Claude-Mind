"""Change since the last reading in the advisory ratchet lanes ().

The precheck ratchet lanes print REGRESSED with a count and a distance from the baseline,
never whether the count moved since this box last looked. These tests pin the shared answer
(core/scripts/_ratchet_delta.py) and its use in each lane:

* the comparison is made against THIS box's newest earlier row, found by time and never by
  position, and never against another box's row (the merged history interleaves boxes whose
  populations differ);
* it is read before the new row is appended, so a row is never its own predecessor;
* the new row carries `hostname` and nothing else changes shape: the baseline entry keeps
  its keys and its baseline value (a key beside `baseline` would be taken whole from one
  side by the cross-box merge, a key inside a history row survives the union);
* the line names no goal.

Hermetic: every lane runs main() against a tmp baselines file with its measurement stubbed.
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
import socket
import sys
import types
from pathlib import Path

import pytest
import yaml

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import _ratchet_delta as rd  # noqa: E402

NOW = "2026-10-03T18:12:23"
GOAL_ID = re.compile(r"\bg-\d+-\d+\b")


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _row(at, total, host="box-a"):
    row = {"recorded_at": at, "drift_total": total, "verdict": "regressed"}
    if host is not None:
        row["hostname"] = host
    return row


# ══════════════════════════ THE SHARED ANSWER ═════════════════════════════════

def test_unchanged_worsened_and_improved_are_told_apart():
    history = [_row("2026-10-03T16:50:38", 25)]
    assert rd.since_last_reading(history, 25, "box-a", NOW)["state"] == "unchanged"
    worse = rd.since_last_reading(history, 27, "box-a", NOW)
    assert (worse["state"], worse["previous"], worse["delta"]) == ("worsened", 25, 2)
    better = rd.since_last_reading(history, 23, "box-a", NOW)
    assert (better["state"], better["delta"]) == ("improved", -2)
    assert rd.describe(worse) == ("worsened since 2026-10-03T16:50:38 (1h21m ago) on box-a: "
                                  "25 -> 27 (+2)")
    assert rd.describe(better).endswith("25 -> 23 (-2)")


def test_the_newest_row_of_this_box_is_found_by_time_not_position():
    """The merge sorts history by canonical content, so the last list item is not the newest."""
    history = [_row("2026-10-03T17:30:00", 22), _row("2026-10-03T09:00:00", 30),
               _row("2026-10-03T12:00:00", 28)]
    got = rd.since_last_reading(history, 22, "box-a", NOW)
    assert got["previous_at"] == "2026-10-03T17:30:00" and got["state"] == "unchanged"


def test_another_boxs_newer_row_is_not_a_reading_of_this_box():
    """Comparing with the last row of ANOTHER box would report a population difference as
    a change: the readings 25 and 22 below are two boxes' views, not one box moving."""
    history = [_row("2026-10-03T16:50:38", 25, "box-a"), _row("2026-10-03T17:24:41", 22, "box-b")]
    got = rd.since_last_reading(history, 25, "box-a", NOW)
    assert got["state"] == "unchanged" and got["previous_at"] == "2026-10-03T16:50:38"
    assert rd.since_last_reading(history, 25, "box-c", NOW)["state"] == "none"


def test_rows_with_no_hostname_cannot_be_attributed_and_the_line_says_so():
    history = [_row("2026-10-03T16:50:38", 25, None), _row("2026-10-03T17:00:00", 25, None)]
    got = rd.since_last_reading(history, 25, "box-a", NOW)
    assert got["state"] == "none" and got["unattributed"] == 2
    assert rd.describe(got) == ("no earlier reading from this box (box-a) among the 2 "
                                "recorded; 2 earlier row(s) carry no hostname")


@pytest.mark.parametrize("history", [
    None, [], ["junk", 3, None],
    [{"hostname": "box-a", "drift_total": "25", "recorded_at": "2026-10-03T16:00:00"}],
    [{"hostname": "box-a", "drift_total": True, "recorded_at": "2026-10-03T16:00:00"}],
    [{"hostname": "box-a", "drift_total": 25}],
])
def test_unusable_rows_read_as_no_earlier_reading_and_never_raise(history):
    assert rd.since_last_reading(history, 25, "box-a", NOW)["state"] == "none"


def test_an_unparseable_stamp_drops_only_the_age():
    got = rd.since_last_reading([_row("sometime", 25)], 26, "box-a", NOW)
    assert got["state"] == "worsened" and got["age"] is None
    assert rd.describe(got) == "worsened since sometime on box-a: 25 -> 26 (+1)"


@pytest.mark.parametrize("earlier, age", [
    ("2026-10-03T18:12:00", "23s"), ("2026-10-03T18:02:23", "10m"),
    ("2026-10-03T16:07:23", "2h05m"), ("2026-10-01T15:12:23", "2d3h")])
def test_age_is_reported_in_the_largest_unit_that_fits(earlier, age):
    assert rd.since_last_reading([_row(earlier, 5)], 5, "box-a", NOW)["age"] == age


def test_no_state_names_a_goal():
    """A tracking-goal id printed in tool output outlives the goal and tells every later
    reader the defect is covered (guard-3263): the line states readings, never owners."""
    history = [_row("2026-10-03T16:50:38", 25)]
    for current in (25, 30, 20):
        assert not GOAL_ID.search(rd.describe(rd.since_last_reading(history, current,
                                                                    "box-a", NOW)))
    assert not GOAL_ID.search(rd.describe(rd.since_last_reading([], 1, "box-a", NOW)))


def test_box_name_prefers_the_environment_then_the_socket(monkeypatch):
    monkeypatch.setenv("HOSTNAME", "from-env")
    assert rd.box_name() == "from-env"
    monkeypatch.delenv("HOSTNAME")
    monkeypatch.setattr(socket, "gethostname", lambda: "from-socket")
    assert rd.box_name() == "from-socket"


def test_hostname_on_history_rows_survives_the_cross_box_merge():
    """The documented trap is a key BESIDE `baseline`; a key inside a history row is part of
    the row's content, so the content-union keeps it and the MIN baseline is untouched."""
    import coordination_merge as cm
    a = {"stalled_goals": {"baseline": 19, "last_recorded": "2026-10-03T17:24:41",
                           "last_verdict": "regressed",
                           "history": [_row("2026-10-03T17:24:41", 22, "box-a")]}}
    b = {"stalled_goals": {"baseline": 21, "last_recorded": "2026-10-03T18:12:23",
                           "last_verdict": "regressed",
                           "history": [_row("2026-10-03T18:12:23", 23, "box-b"),
                                       _row("2026-10-03T15:00:00", 25, None)]}}
    merged = yaml.safe_load(cm.merge_audit_baselines(
        yaml.safe_dump(a).encode("utf-8"), yaml.safe_dump(b).encode("utf-8")))["stalled_goals"]
    assert merged["baseline"] == 19
    by_time = {r["recorded_at"]: r for r in merged["history"]}
    assert len(merged["history"]) == 3
    assert by_time["2026-10-03T17:24:41"]["hostname"] == "box-a"
    assert by_time["2026-10-03T18:12:23"]["hostname"] == "box-b"
    assert "hostname" not in by_time["2026-10-03T15:00:00"]
    assert rd.since_last_reading(merged["history"], 22, "box-a", NOW)["state"] == "unchanged"


# ══════════════════════════ EVERY LANE TELLS THE SAME STORY ═══════════════════

def _story(run, baselines_path, key, steady, worse, monkeypatch):
    """Seed, repeat, regress, then ask from a second box. `run(count)` runs the lane once
    with its measurement set to `count` and returns what it printed."""
    monkeypatch.setenv("HOSTNAME", "box-a")
    first = run(steady)
    assert "since last reading" in first
    assert "no earlier reading from this box (box-a)" in first

    second = run(steady)
    assert re.search(r"unchanged since \S+ \(\S+ ago\) on box-a: %d\b" % steady, second), second

    before = yaml.safe_load(baselines_path.read_text(encoding="utf-8"))[key]
    third = run(worse)
    assert re.search(r"worsened since \S+ \(\S+ ago\) on box-a: %d -> %d \(\+%d\)"
                     % (steady, worse, worse - steady), third), third
    after = yaml.safe_load(baselines_path.read_text(encoding="utf-8"))[key]

    # the new attribute changed nothing the verdict or the merge reads
    assert after["baseline"] == before["baseline"] == steady
    assert after["last_verdict"] == "regressed"
    assert set(after) == set(before), "a key was added beside baseline"
    assert [r["hostname"] for r in after["history"]] == ["box-a"] * 3

    monkeypatch.setenv("HOSTNAME", "box-b")
    assert "no earlier reading from this box (box-b)" in run(worse)


def test_unchecked_write_lane_tells_the_story(tmp_path, monkeypatch, capsys):
    mod = _load("unchecked_write_ratchet_since", "unchecked-write-ratchet.py")
    path = tmp_path / "audit-baselines.yaml"
    monkeypatch.setattr(mod, "BASELINES_PATH", path)
    monkeypatch.setattr(sys, "argv", ["unchecked-write-ratchet.py"])

    def run(count):
        monkeypatch.setattr(mod, "_run_audit", lambda: {
            "verified": 73, "unverified": count, "verdict": "CONFIRMED",
            "population": {"write_wrappers": 80, "read_wrappers": 28, "skill_files": 90,
                           "call_sites": count + 73}})
        assert mod.main() == 0
        return capsys.readouterr().out

    _story(run, path, mod.KEY, 464, 500, monkeypatch)


def test_domain_term_lane_tells_the_story(tmp_path, monkeypatch, capsys):
    mod = _load("domain_term_ratchet_since", "domain-term-ratchet.py")
    path = tmp_path / "audit-baselines.yaml"
    monkeypatch.setattr(mod, "BASELINES_PATH", path)
    monkeypatch.setattr(sys, "argv", ["domain-term-ratchet.py"])

    def run(count):
        # gaps are registry terms minus blocklisted ones: `count` gaps = count + 1 terms, 1 listed
        monkeypatch.setattr(mod, "_run_census", lambda: {
            "files_scanned": 3313,
            "by_source": {mod.SOURCE: {"terms": count + 1, "blocklisted": 1}},
            "rows": [{"term": "t%d" % i, "sources": [mod.SOURCE], "blocklisted": False}
                     for i in range(count)]})
        assert mod.main() == 0
        return capsys.readouterr().out

    _story(run, path, mod.KEY, 5, 7, monkeypatch)


def test_goal_field_census_lane_tells_the_story(tmp_path, monkeypatch, capsys):
    mod = _load("goal_field_census_ratchet_since", "goal-field-census-ratchet.py")
    path = tmp_path / "audit-baselines.yaml"
    monkeypatch.setattr(mod, "BASELINES_PATH", path)
    monkeypatch.setattr(sys, "argv", ["goal-field-census-ratchet.py"])

    def run(count):
        goals = [{"id": "w-%d" % i, "source": "world", "title": "t", "status": "pending",
                  "zz_probe_%d" % i: "x"} for i in range(count)]

        def query(cmd, **kw):
            status = cmd[cmd.index("--goal-status") + 1]
            return types.SimpleNamespace(
                stdout=json.dumps(goals if status == "pending" else []),
                stderr="", returncode=0)

        monkeypatch.setattr(mod, "subprocess", types.SimpleNamespace(run=query))
        assert mod.main() == 0
        return capsys.readouterr().out

    _story(run, path, mod.KEY, 1, 3, monkeypatch)


def test_stalled_goal_lane_tells_the_story(tmp_path, monkeypatch, capsys):
    mod = _load("stalled_goal_ratchet_since", "stalled-goal-ratchet.py")
    import _paths
    monkeypatch.setattr(_paths, "META_DIR", tmp_path)
    path = tmp_path / "audit-baselines.yaml"
    old = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(timespec="seconds")

    def run(count):
        goals = [{"id": "s%d" % i, "status": "pending", "title": "t", "_source": "world",
                  "blocked_since": old} for i in range(count)]
        monkeypatch.setattr(mod, "_load_population", lambda: goals)
        monkeypatch.setattr(mod, "_blocked_ids", lambda: {g["id"]: "deferred" for g in goals})
        assert mod.main([]) == 0
        return capsys.readouterr().out

    _story(run, path, mod.METRIC_KEY, 1, 3, monkeypatch)


def test_the_stalled_lane_keeps_stdout_json_when_asked_for_json(tmp_path, monkeypatch, capsys):
    """Under --json the summary goes to stderr so stdout stays parseable; the new line
    must follow it there and not break the flag."""
    mod = _load("stalled_goal_ratchet_since_json", "stalled-goal-ratchet.py")
    import _paths
    monkeypatch.setattr(_paths, "META_DIR", tmp_path)
    old = (dt.datetime.now() - dt.timedelta(days=30)).isoformat(timespec="seconds")
    goals = [{"id": "s1", "status": "pending", "title": "t", "_source": "world",
              "blocked_since": old}]
    monkeypatch.setattr(mod, "_load_population", lambda: goals)
    monkeypatch.setattr(mod, "_blocked_ids", lambda: {"s1": "deferred"})
    assert mod.main(["--json"]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)                      # stdout is still one JSON document
    assert "since last reading" in captured.err
    assert "since last reading" not in captured.out
