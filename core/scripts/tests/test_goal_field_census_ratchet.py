""" item 3 — the goal-field census ratchet gates the RIGHT number.

Three decisions in this ratchet are easy to "tidy" into something wrong later, and
all are load-bearing, so they are pinned here rather than left to the docstring.

1. IT MUST NOT RATCHET THE STRAY COUNT. Gating on "strays must fall" is the
   obvious reading of the goal and is UNSATISFIABLE: `aspirations.jsonl` is
   merge-protected by a COMMUTATIVE merge handler, so a key absent from a write
   and present remotely resolves to present — a migration that pops 34 keys
   writes successfully and changes nothing (measured 2026-08-18). A permanent
   WARN nobody can clear is worse than no measurement, because it trains readers
   to ignore the ratchet.

2. IT MUST ENUMERATE STATUSES FROM `aspirations.VALID_GOAL_STATUSES`. A
   hand-written six-status list omits `decomposed` and `superseded` and
   undercounted this exact metric by 2 goals and 1 distinct key while looking
   completely reasonable.

3. THE VERDICT MUST NOT DEPEND ON WHICH AGENT RAN IT (g-115-8691). The query
   wrapper returns the world queue UNIONED with the bound agent's private queue,
   so the old metric (distinct names over that union) read 148 / 149 / 150 / 148 /
   149 for five agents on one box, one store and one minute, and against a
   baseline merged by MIN no agent could ever read stable. The ratcheted number
   now reads WORLD rows only. The tests below drive the real `_census` and
   `main` against canned query output for several bindings, because a source-text
   pin cannot tell a world-only count from a union.
"""
import importlib.util
import json
import pathlib
import sys
import types

import pytest
import yaml

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from _goal_fields import GOAL_KNOWN_FIELDS, GOAL_STRAY_FIELDS  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "goal_field_census_ratchet", _SCRIPTS / "goal-field-census-ratchet.py")
ratchet = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ratchet)

_SRC = (_SCRIPTS / "goal-field-census-ratchet.py").read_text(encoding="utf-8")

UNREG = "zz_unregistered_probe_field"   # in neither the registry nor the stray list
STRAY = sorted(GOAL_STRAY_FIELDS)[0]    # a declared stray (refused by the gate)


def _goal(gid, source="world", *extra):
    row = {"id": gid, "source": source, "title": "t", "status": "pending"}
    row.update({name: "x" for name in extra})
    return row


def _use_query(monkeypatch, by_status):
    """Route the census's query call to canned rows; returns the statuses asked."""
    asked = []

    def run(cmd, **kw):
        status = cmd[cmd.index("--goal-status") + 1]
        asked.append(status)
        return types.SimpleNamespace(stdout=json.dumps(by_status.get(status, [])),
                                     stderr="", returncode=0)

    monkeypatch.setattr(ratchet, "subprocess", types.SimpleNamespace(run=run))
    return asked


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["goal-field-census-ratchet.py", *argv])
    rc = ratchet.main()
    return rc, capsys.readouterr().out


@pytest.fixture
def baselines(monkeypatch, tmp_path):
    path = tmp_path / "audit-baselines.yaml"
    monkeypatch.setattr(ratchet, "BASELINES_PATH", path)
    return path


def test_the_fixture_fields_measure_what_the_tests_claim():
    """A later registry change must not let a fixture name drift into another class."""
    assert {"id", "source", "title", "status"} <= GOAL_KNOWN_FIELDS
    assert UNREG not in GOAL_KNOWN_FIELDS and UNREG not in GOAL_STRAY_FIELDS
    assert STRAY not in GOAL_KNOWN_FIELDS


def test_statuses_come_from_the_ssot_not_a_hand_written_list():
    from aspirations import VALID_GOAL_STATUSES
    assert "VALID_GOAL_STATUSES" in _SRC
    # The omission that actually happened: a six-status list missing these two.
    assert "decomposed" in VALID_GOAL_STATUSES
    assert "superseded" in VALID_GOAL_STATUSES
    # And the module must not carry its own literal status list.
    assert '"pending", "in-progress", "completed"' not in _SRC


def test_the_ratcheted_metric_is_the_undeclared_name_count_not_the_stray_count():
    assert ratchet.KEY == "goal_field_undeclared_names"
    # EXACT, not a fallback chain of substrings. An `or ... or 'ratcheted_metric'
    # in _SRC` tail would pass on the mere presence of the word and assert
    # nothing at all — the weak-predicate failure this whole goal kept hitting.
    assert '"ratcheted_metric": "undeclared_names"' in _SRC
    # The verdict must be computed from the undeclared count, never from the stray
    # count or the total name count.
    assert 'cur = current["undeclared_names"]' in _SRC
    assert 'cur = current["stray_occurrences"]' not in _SRC
    assert 'cur = current["distinct_keys"]' not in _SRC


def test_stray_count_is_recorded_as_reported_but_not_ratcheted():
    """If someone later gates on this, the baseline entry should contradict them."""
    assert "reported_not_ratcheted" in _SRC
    assert "stray_occurrences" in _SRC


def test_the_strays_are_reported_by_NAME_not_only_by_COUNT(monkeypatch, baselines, capsys):
    """`strays` (name -> count) was built in _census and thrown away, so every
    consumer learned "17 stray field name(s)" and had no way to discover which.
    Reporting is this metric's whole job — it is deliberately not gated — so a
    count without identities makes it unactionable. Measured 2026-08-30: naming
    them surfaced camelCase leaks (desiredEndState, lastAchieved, scheduleType),
    a kebab-case leak (complete-by), and two test artifacts (__noop, _probe)
    sitting in the production store, none of which the count could reveal.
    """
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", STRAY),
                                         _goal("g-1-2", "world", STRAY)]})
    _, out = _run(monkeypatch, capsys, "--dry-run")
    assert f"stray fields: {STRAY}(2)" in out, "the text lane must name them"
    _, out = _run(monkeypatch, capsys, "--json", "--dry-run")
    assert json.loads(out)["current"]["strays"] == {STRAY: 2}


def test_a_long_name_list_says_it_was_cut_and_json_carries_every_name(
        monkeypatch, baselines, capsys):
    """guard-1760: a tool must not silently truncate and read as complete."""
    names = sorted(GOAL_STRAY_FIELDS)[:14]
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", *names)]})
    _, out = _run(monkeypatch, capsys, "--dry-run")
    assert "and 2 more (--json for all)" in out
    _, out = _run(monkeypatch, capsys, "--json", "--dry-run")
    assert len(json.loads(out)["current"]["strays"]) == 14


def test_stray_names_and_counts_stay_internally_consistent(monkeypatch):
    """A self-check the census can always make about itself: the number of names
    must equal len(strays), and their counts must sum to stray_occurrences. If a
    future refactor emits a filtered or capped dict into the payload, these two
    stop agreeing and the report becomes quietly wrong rather than loudly so."""
    s0, s1, s2 = sorted(GOAL_STRAY_FIELDS)[:3]
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", s0, s1, s2),
                                         _goal("g-1-2", "world", s0, s1),
                                         _goal("g-1-3", "world", s0, s1)]})
    census = ratchet._census()
    assert len(census["strays"]) == census["stray_names"] == 3
    assert sum(census["strays"].values()) == census["stray_occurrences"] == 7
    # Widest first; ties break by name, so successive runs stay diffable.
    assert list(census["strays"]) == [s0, s1, s2]


def test_a_zero_population_is_refused_rather_than_recorded_as_a_clean_zero(
        monkeypatch, baselines, capsys):
    """rb-245: an empty result means the query broke, not that the fleet is empty.

    An unreachable store reads `undeclared_names: 0`, which is the GOOD value, so
    without this guard a dead query records a clean all-clear. Agent rows alone do
    not count: the ratcheted population is the world queue.
    """
    _use_query(monkeypatch, {"pending": [_goal("g-001-01", "agent")]})
    rc, out = _run(monkeypatch, capsys, "--json")
    assert rc == 0 and json.loads(out)["verdict"] == "skipped"
    assert not baselines.exists(), "a skipped run must not write a baseline"


def test_the_baseline_is_never_raised_on_a_regression():
    """A ratchet that raises its baseline on regression stops being a ratchet."""
    assert 'verdict, new_baseline = "regressed", prior' in _SRC


def test_a_regression_names_the_fields_and_keeps_the_baseline_where_it_was(
        monkeypatch, baselines, capsys):
    """The old message sent every reader to the bypass ledger and said to leave the
    baseline alone until the schema shrank back. Both pointed at a time trend that
    cannot explain a per-agent constant (g-115-8691). The reader now gets the
    offending names, the one file that resolves them, and the reason a re-seed is
    futile."""
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world")]})
    _, out = _run(monkeypatch, capsys, "--json")
    assert json.loads(out)["verdict"] == "seeded"
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", UNREG)]})
    _, out = _run(monkeypatch, capsys, "--json")
    result = json.loads(out)
    assert result["verdict"] == "regressed" and result["baseline"] == 0
    message = result["message"]
    assert f"{UNREG}(1)" in message and "_goal_fields.py" in message
    assert "DO NOT RE-SEED" in message
    assert "ledger" not in message.lower()
    # And the stored baseline really stayed put (a ratchet never raises it).
    assert yaml.safe_load(baselines.read_text(encoding="utf-8"))[ratchet.KEY]["baseline"] == 0


def test_unparseable_query_output_raises_instead_of_becoming_a_clean_zero(monkeypatch):
    """guard-2298: a shape change must not be laundered into a confident 0."""
    monkeypatch.setattr(ratchet, "subprocess", types.SimpleNamespace(
        run=lambda cmd, **kw: types.SimpleNamespace(stdout="<html>", stderr="",
                                                    returncode=0)))
    with pytest.raises(RuntimeError, match=r"unparseable.*\(6 bytes\)"):
        ratchet._census()


def test_the_census_asks_for_every_status(monkeypatch):
    from aspirations import VALID_GOAL_STATUSES
    asked = _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world")]})
    ratchet._census()
    assert sorted(asked) == sorted(VALID_GOAL_STATUSES)


def test_the_verdict_does_not_depend_on_which_agent_ran_it(monkeypatch, baselines, capsys):
    """THE DEFECT (): one box, one store, only MIND_AGENT varied, and the
    old metric read 148 / 149 / 150 / 148 / 149. The world queue is identical for
    every binding; only the bound agent's private rows differ. Re-introducing the
    union makes `bravo` read 2 against a baseline of 1 and this fails."""
    world = [_goal("g-1-1", "world", UNREG), _goal("g-1-2", "world")]
    bindings = {
        "alpha": [],
        "bravo": [_goal("g-001-01", "agent", "bravo_only_field")],
        "echo": [_goal("g-001-01", "agent", "read_from", "echo_only_a", "echo_only_b")],
    }
    seen, queues = [], {}
    for name, private in bindings.items():
        _use_query(monkeypatch, {"pending": world + private})
        _, out = _run(monkeypatch, capsys, "--json")
        result = json.loads(out)
        seen.append((result["verdict"], result["baseline"],
                     result["current"]["undeclared_names"]))
        queues[name] = result["current"]["agent_queue"]
    # The first binding seeds at the world's 1; every later one reads the same.
    assert seen == [("seeded", 1, 1), ("stable", 1, 1), ("stable", 1, 1)]
    # The private queue is REPORTED beside the verdict, and `read_from` is no field.
    assert queues["alpha"]["undeclared"] == {}
    assert queues["bravo"]["undeclared"] == {"bravo_only_field": 1}
    assert queues["echo"]["undeclared"] == {"echo_only_a": 1, "echo_only_b": 1}


def test_registering_a_field_on_purpose_does_not_move_the_count(monkeypatch, baselines, capsys):
    """The second defect: distinct names rose on every deliberate registration, and a
    baseline that can only shrink made the first one a permanent WARN."""
    registered = next(n for n in sorted(GOAL_KNOWN_FIELDS)
                      if n not in {"id", "source", "title", "status"})
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world")]})
    _run(monkeypatch, capsys, "--json")
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", registered)]})
    _, out = _run(monkeypatch, capsys, "--json")
    result = json.loads(out)
    assert result["verdict"] == "stable" and result["current"]["undeclared_names"] == 0
    assert result["current"]["distinct_keys"] == 5    # reported, and it did grow


def test_classifying_a_name_ratchets_the_baseline_down_and_the_next_run_is_stable(
        monkeypatch, baselines, capsys):
    """rb-7390: the second run's verdict flip is the proof the key is read."""
    verdicts = []
    for rows in ([_goal("g-1-1", "world", UNREG)], [_goal("g-1-1", "world")],
                 [_goal("g-1-1", "world")]):
        _use_query(monkeypatch, {"pending": rows})
        _, out = _run(monkeypatch, capsys, "--json")
        result = json.loads(out)
        verdicts.append((result["verdict"], result["baseline"]))
    assert verdicts == [("seeded", 1), ("ratcheted", 0), ("stable", 0)]
    record = yaml.safe_load(baselines.read_text(encoding="utf-8"))[ratchet.KEY]
    assert len(record["history"]) == 3 and record["scope"] == "source=world"


def test_a_declared_stray_is_reported_but_not_counted_as_undeclared(
        monkeypatch, baselines, capsys):
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", STRAY)]})
    census = ratchet._census()
    assert census["undeclared_names"] == 0
    assert census["strays"] == {STRAY: 1} and census["stray_occurrences"] == 1


def test_a_query_time_marker_is_never_counted_as_a_field(monkeypatch):
    """rb-12748: the endpoint stamps `read_from` on agent rows it does not hold the
    runner claim for. It is not stored, so it must not move any number."""
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", "read_from"),
                                         _goal("g-001-01", "agent", "read_from")]})
    census = ratchet._census()
    assert census["undeclared_names"] == 0 and census["agent_queue"]["undeclared"] == {}
    assert census["distinct_keys"] == 4                  # id, source, title, status


def test_a_name_carried_only_by_a_second_row_of_the_same_goal_is_still_seen(monkeypatch):
    """A goal can sit in two statuses at once (measured 2026-10-03: 2 world ids). A
    first-seen-wins dedupe by id hid three names that only the second row carried,
    while the write-time gate this ratchet audits covers both rows."""
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world")],
                             "superseded": [_goal("g-1-1", "world", UNREG)]})
    census = ratchet._census()
    assert census["goals_scanned"] == 1                  # still ONE goal
    assert census["undeclared"] == {UNREG: 1}            # and its name is seen


def test_rows_with_no_source_key_fail_loudly_instead_of_reading_as_an_empty_world(
        monkeypatch, baselines, capsys):
    """guard-2298: without the per-row `source` stamp every row would land in the
    agent queue and the run would report a clean 'store unreachable'."""
    rows = [{"id": "g-1-1", "title": "t", "status": "pending"}]
    _use_query(monkeypatch, {"pending": rows})
    with pytest.raises(RuntimeError, match="source"):
        ratchet._census()
    monkeypatch.setattr(sys, "argv", ["goal-field-census-ratchet.py"])
    assert ratchet.main() == 2
    assert not baselines.exists()


def test_the_first_two_output_lines_keep_the_shape_verify_learning_reads(
        monkeypatch, baselines, capsys):
    """The /verify-learning check runs the script through `head -2` and expects the
    first line to start `[goal-field-census-ratchet] <VERDICT>:`."""
    _use_query(monkeypatch, {"pending": [_goal("g-1-1", "world", UNREG),
                                         _goal("g-001-01", "agent", "agent_side")]})
    _, out = _run(monkeypatch, capsys)
    lines = out.splitlines()
    assert lines[0].startswith("[goal-field-census-ratchet] SEEDED:")
    assert lines[1].startswith("  world goals=1 undeclared=1 ")
    assert f"undeclared fields: {UNREG}(1)" in out
    assert "bound-agent queue: 1 goal(s), 1 undeclared name(s): agent_side(1)" in out
