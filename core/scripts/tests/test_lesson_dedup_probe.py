"""Tests for core/scripts/lesson-dedup-probe.py (, gap-237).

Covers:
  - a zero-byte, non-JSON, wrong-shape, non-zero-exit or could-not-run retrieve
    reply is MALFUNCTION, never an empty hit list (guard-3707, guard-3362);
  - a well-shaped reply with empty lists IS "no hits";
  - both lessons-file shapes load, and a lesson missing either axis is refused;
  - the provenance join (guard-7379) matches rb source_goal OR origin_goal_id,
    and guardrail source as a WHOLE token, honouring item_ts with date-only
    created stamps;
  - main() runs 2 retrieves per lesson, reads each store once, reports the
    mechanism-only ids, and exits 0 / 2 / 3.
All retrieve and store calls go through a fake runner, so no daemon is touched.
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
CORE_SCRIPTS = SCRIPT_DIR.parent
sys.path.insert(0, str(CORE_SCRIPTS))

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "lesson_dedup_probe", CORE_SCRIPTS / "lesson-dedup-probe.py")
ldp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ldp)


def _reply(rb=(), guards=()):
    return json.dumps({"meta": {}, "reasoning_bank": list(rb), "guardrails": list(guards)})


RB_HITS = [{"id": f"rb-{i}", "title": f"lesson title {i}"} for i in range(1, 6)]
GUARD_HITS = [{"id": f"guard-{i}", "rule": f"RULE {i}"} for i in range(1, 6)]


# ---------------------------------------------------------------- parse_retrieve

def test_zero_byte_reply_is_malfunction_not_empty():
    out = ldp.parse_retrieve(0, "", None, 3)
    assert out["status"] == "MALFUNCTION"
    assert "zero-byte" in out["reason"]
    assert "rb" not in out and "guardrails" not in out


def test_unreadable_replies_are_malfunction():
    cases = [
        (0, "not json at all", None),                      # non-JSON
        (0, "[]", None),                                   # not an object
        (0, json.dumps({"meta": {}}), None),               # object without the hit lists
        (0, json.dumps({"reasoning_bank": [], "guardrails": None}), None),
        (1, _reply(RB_HITS, GUARD_HITS), None),            # non-zero exit, even with a body
        (None, "", "TimeoutExpired: took too long"),       # could not run
    ]
    for rc, out, err in cases:
        assert ldp.parse_retrieve(rc, out, err, 3)["status"] == "MALFUNCTION", (rc, out, err)


def test_well_shaped_empty_reply_is_no_hits():
    out = ldp.parse_retrieve(0, _reply(), None, 3)
    assert out == {"status": "ok", "rb": [], "guardrails": []}


def test_top_n_cut_and_text_extraction():
    out = ldp.parse_retrieve(0, _reply(RB_HITS, GUARD_HITS), None, 2)
    assert [h["id"] for h in out["rb"]] == ["rb-1", "rb-2"]
    assert [h["id"] for h in out["guardrails"]] == ["guard-1", "guard-2"]
    assert out["rb"][0]["text"] == "lesson title 1"
    assert out["guardrails"][1]["text"] == "RULE 2"


# ---------------------------------------------------------------- load_lessons

def test_load_lessons_accepts_both_shapes(tmp_path):
    as_map = tmp_path / "map.json"
    as_map.write_text(json.dumps({"a": ["subj a", "mech a"]}), encoding="utf-8")
    assert ldp.load_lessons(as_map) == [
        {"key": "a", "subject": "subj a", "mechanism": "mech a", "goal_id": None, "item_ts": None}]

    as_list = tmp_path / "list.json"
    as_list.write_text(json.dumps([{"subject": "s", "mechanism": "m",
                                    "goal_id": "g-1-2", "_item_ts": "2026-09-27T10:00:00"}]),
                       encoding="utf-8")
    (row,) = ldp.load_lessons(as_list)
    assert row["key"] == "L1" and row["goal_id"] == "g-1-2"
    assert row["item_ts"] == "2026-09-27T10:00:00"


def test_load_lessons_refuses_a_single_axis_and_bad_files(tmp_path):
    bad = [
        json.dumps({"a": ["only one"]}),
        json.dumps([{"subject": "s"}]),
        json.dumps([{"subject": "s", "mechanism": "   "}]),
        json.dumps([]),
        json.dumps("a string"),
        "{not json",
    ]
    for i, text in enumerate(bad):
        p = tmp_path / f"bad{i}.json"
        p.write_text(text, encoding="utf-8")
        try:
            ldp.load_lessons(p)
        except ldp.UsageError:
            continue
        raise AssertionError(f"accepted a bad lessons file: {text!r}")


# ---------------------------------------------------------------- provenance

def test_names_goal_is_whole_token():
    assert ldp.names_goal("g-115-10778", "g-115-10778")
    assert ldp.names_goal("replayed from g-115-10778 at close", "g-115-10778")
    assert not ldp.names_goal("g-115-107781", "g-115-10778")
    assert not ldp.names_goal("xg-115-10778", "g-115-10778")
    assert not ldp.names_goal("g-115-10778-a", "g-115-10778")
    assert not ldp.names_goal(None, "g-115-10778")


def test_on_or_after_handles_date_only_stamps():
    assert ldp.on_or_after("2026-09-27T12:00:00", None)
    assert ldp.on_or_after("2026-09-27T12:00:00", "2026-09-27T11:59:59")
    assert not ldp.on_or_after("2026-09-27T11:00:00", "2026-09-27T11:59:59")
    assert ldp.on_or_after("2026-09-27", "2026-09-27T23:00:00")      # same day, date-only
    assert not ldp.on_or_after("2026-09-26", "2026-09-27T00:00:01")
    assert ldp.on_or_after("", "2026-09-27T00:00:01")                 # unknown: shown, not hidden


def test_provenance_matches_either_rb_field_and_guardrail_source_token():
    lesson = {"goal_id": "g-9-1", "item_ts": "2026-09-20T00:00:00"}
    rb = [
        {"id": "rb-a", "title": "by source_goal", "source_goal": "g-9-1", "created": "2026-09-21T00:00:00"},
        {"id": "rb-b", "title": "by origin", "source_goal": "g-001-01", "origin_goal_id": "g-9-1",
         "created": "2026-09-22T00:00:00"},
        {"id": "rb-c", "title": "too early", "source_goal": "g-9-1", "created": "2026-09-19T00:00:00"},
        {"id": "rb-d", "title": "other goal", "source_goal": "g-9-10", "created": "2026-09-23T00:00:00"},
    ]
    guards = [
        {"id": "guard-a", "rule": "bare id", "source": "g-9-1", "created": "2026-09-21"},
        {"id": "guard-b", "rule": "in prose", "source": "measured on g-9-1 replay", "created": "2026-09-21T01:00:00"},
        {"id": "guard-c", "rule": "prefix only", "source": "g-9-10", "created": "2026-09-21T01:00:00"},
    ]
    out = ldp.provenance_hits(lesson, rb, guards)
    assert [h["id"] for h in out["rb"]] == ["rb-a", "rb-b"]
    assert out["rb"][1]["matched"] == "origin_goal_id"
    assert [h["id"] for h in out["guardrails"]] == ["guard-a", "guard-b"]


# ---------------------------------------------------------------- main end to end

class FakeRunner:
    def __init__(self, retrieve_replies, rb_store=None, guard_store=None):
        self.retrieve_replies = list(retrieve_replies)
        self.rb_store = rb_store if rb_store is not None else (0, "[]", None)
        self.guard_store = guard_store if guard_store is not None else (0, "[]", None)
        self.calls = []

    def __call__(self, script, *args, timeout):
        self.calls.append((Path(script).name,) + args)
        name = Path(script).name
        if name == "reasoning-bank-read.sh":
            return self.rb_store
        if name == "guardrails-read.sh":
            return self.guard_store
        assert name == "retrieve.sh"
        return self.retrieve_replies.pop(0)


def _lessons_file(tmp_path, lessons):
    p = tmp_path / "lessons.json"
    p.write_text(json.dumps(lessons), encoding="utf-8")
    return str(p)


def test_main_two_retrieves_per_lesson_and_mechanism_only(tmp_path, capsys):
    path = _lessons_file(tmp_path, [
        {"key": "one", "subject": "s1", "mechanism": "m1", "goal_id": "g-9-1"},
        {"key": "two", "subject": "s2", "mechanism": "m2"},
    ])
    rb_store = (0, json.dumps([{"id": "rb-p", "title": "t", "source_goal": "g-9-1",
                                "created": "2026-09-27T00:00:00"}]), None)
    runner = FakeRunner([
        (0, _reply(RB_HITS[:2], GUARD_HITS[:1]), None),     # one / subject
        (0, _reply(RB_HITS[1:3], GUARD_HITS[3:4]), None),   # one / mechanism
        (0, _reply(), None),                                # two / subject
        (0, _reply(), None),                                # two / mechanism
    ], rb_store=rb_store)
    rc = ldp.main(["--lessons-file", path, "--top", "3"], runner=runner)
    out = capsys.readouterr().out
    assert rc == 0
    names = [c[0] for c in runner.calls]
    assert names.count("retrieve.sh") == 4
    assert names.count("reasoning-bank-read.sh") == 1 and names.count("guardrails-read.sh") == 1
    retrieve_args = [c[1:] for c in runner.calls if c[0] == "retrieve.sh"]
    assert all("--read-only" in a and "--depth" in a for a in retrieve_args)
    assert "mechanism-only (guard-6927): rb-3, guard-4" in out
    assert "rb-p" in out and "no goal_id on this lesson" in out
    assert "every query answered" in out


def test_main_exits_3_and_says_malfunction_when_a_query_fails(tmp_path, capsys):
    path = _lessons_file(tmp_path, {"k": ["s", "m"]})
    runner = FakeRunner([(0, _reply(RB_HITS, GUARD_HITS), None), (0, "", None)])
    rc = ldp.main(["--lessons-file", path], runner=runner)
    out = capsys.readouterr().out
    assert rc == 3
    assert "MALFUNCTION: zero-byte reply" in out
    assert "NOT evidence of novelty" in out


def test_main_store_read_failure_is_malfunction_and_retrieves_still_run(tmp_path, capsys):
    path = _lessons_file(tmp_path, [{"subject": "s", "mechanism": "m", "goal_id": "g-9-1"}])
    runner = FakeRunner([(0, _reply(), None), (0, _reply(), None)],
                        rb_store=(0, "not json", None))
    rc = ldp.main(["--lessons-file", path, "--json"], runner=runner)
    report = json.loads(capsys.readouterr().out)
    assert rc == 3
    (row,) = report["lessons"]
    assert row["provenance"]["status"] == "MALFUNCTION"
    assert row["subject_hits"]["status"] == "ok" and row["mechanism_hits"]["status"] == "ok"
    assert report["store_sizes"] is None


def test_main_exits_2_on_unreadable_lessons_file(tmp_path, capsys):
    rc = ldp.main(["--lessons-file", str(tmp_path / "missing.json")],
                  runner=FakeRunner([]))
    assert rc == 2
    assert "cannot read lessons file" in capsys.readouterr().err
