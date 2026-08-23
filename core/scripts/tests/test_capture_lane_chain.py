"""Per-lane WRITE -> STAGE -> MERGE -> CONSUME chain for the capture slots ().

THE GAP THIS FILLS. 22 test files touch the worker-learning bridge, and the
lanes' registry membership, caps, reset survival, prune survival and fast-lane
behaviour are each pinned. What was NOT pinned is a capture payload's JOURNEY:
test_body_merge_retrospective_chain.py is the file named for the chain, but its
8 tests cover the merge->retrospective PLUMBING (stdout passthrough, rc
preservation, noop handling, and one asserting the retired exp_capture_drain.py
is gone) and are slot-agnostic. So the pipe was pinned and the water going
through it was not — for ANY of the four lanes. That is the g-115-6054
"a representative member is not coverage of a family" lesson one level up: here
not even a representative existed.

Measured 2026-08-22 (alpha worker Body, hostname cc-07, uname -r
6.8.0-137-generic, own-cloud) while executing g-306-204; per-slot grep counts in
that file were spark 0, exp 5, hyp 0, encoding 0, and all 5 exp hits sit inside
the retired-drain test.

SCOPE, stated so a reader does not over-trust a green: this covers WRITE ->
STAGE -> MERGE with the REAL merge (`body-merge.generalize_down`), then hands
the merged value to the CONSUMER'S OWN transform (`worker_retrospective
.index_captures`). It does NOT cover the consumer's final daemon hop —
`_load_capture_slot` reads through `wm-read.sh`, which needs a live daemon and
is out of reach of a hermetic test. So: everything up to and including "the
reducer holds it in a shape its consumer accepts" is pinned here; "the consumer
process actually read it" is not.

Iterates `wm.CAPTURE_SLOTS` rather than naming four lanes, so a fifth is covered
by being REGISTERED, not by someone remembering to edit this file.

Run:
  py -3 -m pytest core/scripts/tests/test_capture_lane_chain.py -q
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

CORE_ROOT = Path(__file__).resolve().parent.parent.parent
CORE_SCRIPTS = CORE_ROOT / "scripts"
sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402


def _load(modname: str, filename: str):
    """Load a hyphenated script (not importable by name) by path."""
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge", "body-merge.py")
retro = _load("worker_retrospective_mod", "worker_retrospective.py")

SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"


def _mk_agent(tmp_path: Path, reducer_wm: dict | None = None, name="alpha") -> Path:
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    if reducer_wm is not None:
        (state / "working-memory.yaml").write_text(
            yaml.dump(reducer_wm, default_flow_style=False, sort_keys=False),
            encoding="utf-8")
    return tmp_path


def _stage(pr: Path, sid: str, body_wm: dict, name="alpha") -> Path:
    staged = pr / "agents" / name / "session" / "pending-body-merges"
    staged.mkdir(parents=True, exist_ok=True)
    p = staged / f"{sid}-wm.yaml"
    p.write_text(yaml.dump(body_wm, default_flow_style=False, sort_keys=False),
                 encoding="utf-8")
    return p


def _read_reducer(pr: Path, name="alpha") -> dict:
    return yaml.safe_load(
        (pr / "agents" / name / "session" / "working-memory.yaml")
        .read_text(encoding="utf-8")) or {}


def _entry(slot: str, goal_id: str) -> dict:
    """One capture entry. `goal_id` is REQUIRED on every lane by schema — see
    test_entries_without_a_goal_id_are_dropped_by_the_consumer for why.
    """
    return {"goal_id": goal_id, "slot": slot, "note": f"payload for {slot}"}


# THE MERGE HAS TWO PATHS INTO A CAPTURE LANE AND THEY ARE DIFFERENT CODE.
# body-merge._merge_value applies an ABSENCE RULE first (`if r_val is None:
# return b_val`, line ~191), so a lane the reducer does not yet have is copied
# wholesale and NEVER reaches `_dedup_append`. Only a lane the reducer ALREADY
# holds takes the union path. Both are pinned below, separately (guard-4374).
#
# This split is not tidiness — it was forced. The first version of this file
# tested only the empty-reducer case, and mutation-proof-test.sh certified it
# VACUOUS: sabotaging `_dedup_append` to drop all body items left it GREEN,
# because the test never executed that line. The union case is also the more
# realistic one (a reducer accumulates captures from many Bodies over a
# session), so testing only the absence path tested mainly the path that
# matters least.

def test_every_capture_lane_survives_merge_into_an_empty_reducer_lane(tmp_path):
    """Absence path: reducer has no such lane yet -> body value copied wholesale."""
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {"active_context": {"a": 1}}})
    body = {"slots": {s: [_entry(s, "g-1-01")] for s in wm.CAPTURE_SLOTS}}
    staged = _stage(pr, SID_A, body)

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID_A in summary["staged_merged"], summary

    red = _read_reducer(pr)["slots"]
    for slot in wm.CAPTURE_SLOTS:
        assert red.get(slot) == [_entry(slot, "g-1-01")], (
            f"capture lane {slot} did not survive write->stage->merge "
            f"(reducer holds {red.get(slot)!r}) — a worker's payload in this "
            f"lane reaches no consumer"
        )
    # The reducer's own unrelated slot is untouched, and the staged file is
    # consumed exactly once.
    assert red.get("active_context") == {"a": 1}
    assert not staged.exists()


def test_every_capture_lane_unions_into_a_non_empty_reducer_lane(tmp_path):
    """Union path — the common case, and the one the absence test cannot reach.

    The reducer already holds an entry in every lane; the Body's new entry must
    be APPENDED, not dropped and not overwriting what is already there. Losing
    either direction loses a whole Body's learning: dropping the incoming entry
    silently discards the worker's payload, and replacing the existing list
    discards every other Body's.
    """
    prior = {s: [_entry(s, "g-0-99")] for s in wm.CAPTURE_SLOTS}
    pr = _mk_agent(tmp_path, reducer_wm={"slots": dict(prior)})
    body = {"slots": {s: [_entry(s, "g-1-01")] for s in wm.CAPTURE_SLOTS}}
    _stage(pr, SID_A, body)

    merge.generalize_down("alpha", project_root=pr)

    red = _read_reducer(pr)["slots"]
    for slot in wm.CAPTURE_SLOTS:
        got = red.get(slot) or []
        assert _entry(slot, "g-1-01") in got, (
            f"{slot}: the Body's incoming entry was DROPPED on the union path "
            f"(reducer holds {got!r}) — this is the path a real reducer takes "
            f"once any Body has already contributed to the lane"
        )
        assert _entry(slot, "g-0-99") in got, (
            f"{slot}: the reducer's pre-existing entry was LOST (holds {got!r})"
        )
        assert len(got) == 2, f"{slot}: expected exactly 2 entries, got {got!r}"


def test_identical_entries_from_two_bodies_collapse_by_content_hash(tmp_path):
    """The edge case  names: 'identical observations from 2 workers'.

    Two Bodies stage byte-identical entries; the union must keep ONE.
    """
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {}})
    same = _entry("spark_capture", "g-2-01")
    _stage(pr, SID_A, {"slots": {"spark_capture": [same]}})
    _stage(pr, SID_B, {"slots": {"spark_capture": [same]}})

    merge.generalize_down("alpha", project_root=pr)
    got = _read_reducer(pr)["slots"].get("spark_capture")
    assert got == [same], (
        f"identical entries from two Bodies did not collapse: {got!r}"
    )


def test_distinct_goal_ids_do_not_collapse(tmp_path):
    """The complement, and the reason every lane's schema REQUIRES goal_id.

    Pinned separately because the dedup sorts into two buckets and a test of
    only the collapsing one passes for an implementation that collapses
    EVERYTHING (guard-4374). Two workers whose observations happen to read
    identically would then lose one goal's learning silently — the goal_id is
    what makes the content hashes differ.
    """
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {}})
    a = _entry("spark_capture", "g-3-01")
    b = _entry("spark_capture", "g-3-02")
    _stage(pr, SID_A, {"slots": {"spark_capture": [a]}})
    _stage(pr, SID_B, {"slots": {"spark_capture": [b]}})

    merge.generalize_down("alpha", project_root=pr)
    got = _read_reducer(pr)["slots"].get("spark_capture") or []
    assert len(got) == 2, f"distinct goals collapsed into one entry: {got!r}"
    assert a in got and b in got, got


def test_merged_entries_index_by_goal_id_for_the_consumer(tmp_path):
    """CONSUME half (minus the daemon hop): what the merge leaves in the slot
    must be a shape the consumer's own transform accepts and buckets by goal.
    """
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {}})
    _stage(pr, SID_A, {"slots": {"exp_capture": [
        _entry("exp_capture", "g-4-01"), _entry("exp_capture", "g-4-02")]}})
    merge.generalize_down("alpha", project_root=pr)

    idx = retro.index_captures(_read_reducer(pr)["slots"].get("exp_capture"))
    assert set(idx) == {"g-4-01", "g-4-02"}, idx
    assert len(idx["g-4-01"]) == 1, idx


def test_entries_without_a_goal_id_are_dropped_by_the_consumer(tmp_path):
    """Why goal_id is mandatory on every lane, pinned rather than asserted in
    prose: the consumer DROPS an unjoinable row. A lane schema that let goal_id
    be optional would merge fine and then vanish at consumption — a silent loss
    with a green merge test.
    """
    pr = _mk_agent(tmp_path, reducer_wm={"slots": {}})
    _stage(pr, SID_A, {"slots": {"exp_capture": [
        {"slot": "exp_capture", "note": "no goal_id"},
        _entry("exp_capture", "g-5-01"),
    ]}})
    merge.generalize_down("alpha", project_root=pr)

    merged = _read_reducer(pr)["slots"].get("exp_capture") or []
    assert len(merged) == 2, f"merge should carry both rows: {merged!r}"
    idx = retro.index_captures(merged)
    assert set(idx) == {"g-5-01"}, (
        f"the id-less row must be dropped at consumption, not bucketed: {idx!r}"
    )
