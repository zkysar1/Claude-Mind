"""Tests for the pipeline merge of a member's forget and undo (, unit u7a).

A hypothesis record is merged by stage and then by content, and neither knows that a member's
forget blanked its statement. Measured before this unit, with the stale copy one stage further
along, 2 of 4 probe statements came back under the forget marker, and a copy whose marker an undo
had cleared lost to a stale copy that still held the forget. ``_merge_pipeline_record`` now orders
a forget and an undo by their own stamps (``forgotten_at``, ``restored_at``) and takes the
statement and both stamps whole from the copy whose event is later.

The records these tests merge are NOT hand-built. A resolver tier is a reader of a field, and a
reader nobody writes is a no-op under a passing suite (rb-5493), so the other half of the fix is
the writers, and what feeds the merge here is what they produced. ``_Trace`` stands in for the
daemon transport only: every write goes through the REAL ``pipeline_write.update_field`` handler,
driven by the REAL forget and undo of ``knowledge-edit-apply.py``, and the record is kept as it
stands after EACH write. That gives the states a merge can meet in the field: the stale copy from
before the forget, a copy caught between any two writes, and the finished ones. The few
hand-built records say so, and exist to make an adverse case a lifecycle cannot.
"""

from __future__ import annotations

import datetime
import importlib.util
import itertools
import json
import sys
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[1]
_ROOT = _SCRIPTS.parents[1]
for _p in (str(_SCRIPTS), str(_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _rt  # noqa: E402
import coordination_merge as cm  # noqa: E402
import knowledge_retention  # noqa: E402
from knowledge_projection import (  # noqa: E402
    FORGOTTEN_FIELD,
    RESTORED_FIELD,
    is_forgotten,
    item_handle,
    item_text_fields,
)
from mind_api.src.world import pipeline_write  # noqa: E402


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply_mod = _load("knowledge_edit_apply_forget_merge_tests", "knowledge-edit-apply.py")
export_mod = _load("knowledge_export_forget_merge_tests", "knowledge-export.py")

SECRET = "handle-secret-for-tests"
ENV_ID = "env-under-test"
HEAD = apply_mod._FORGOTTEN_HEAD
T0 = datetime.datetime(2026, 10, 3, 12, 0, 0, tzinfo=datetime.timezone.utc)
HOUR = datetime.timedelta(hours=1)
STAGES = tuple(cm._PIPELINE_STAGE_RANK)

TITLE = "Green widgets outsell blue ones"
A = "2026-01-02_green-widgets"
B = "2026-01-03_gear-demand"
C = "2026-01-04_resolved-widgets"

#: Two original statements, one that sorts below the tombstone's first letter and one above. The
#: base rule's content tiebreak flips with it, so a merge that happened to be right for one of them
#: is caught by the other.
CLAIMS = ["Apples ripen sooner on cool hillsides.", "Widgets sell better in green than in blue."]

GROUP = ("claim", "title", FORGOTTEN_FIELD, RESTORED_FIELD)


def _hyp(item_id: str, **extra) -> dict:
    rec = {"id": item_id, "title": TITLE, "stage": "active", "horizon": "session",
           "type": "calibration", "confidence": 0.6,
           "position": "YES green widgets outsell blue in every market tested",
           "formed_date": "2026-01-02", "category": "acme", "claim": CLAIMS[1],
           "rationale": "Observed across three markets in 2025."}
    rec.update(extra)
    return rec


@pytest.fixture(params=CLAIMS, ids=["claim-below-tombstone", "claim-above-tombstone"])
def claim(request) -> str:
    return request.param


@pytest.fixture(params=[False, True], ids=["same-second", "an-hour-apart"])
def spaced(request) -> bool:
    return request.param


@pytest.fixture
def world(tmp_path, monkeypatch, claim):
    w = tmp_path / "world"
    tree = w / "knowledge" / "tree"
    (tree / "acme").mkdir(parents=True)
    (tree / "acme" / "acme-widgets.md").write_text(
        "---\ntopic: Acme widgets\n---\n\nWidgets are blue.\n", encoding="utf-8")
    index = {"last_updated": "2026-01-01", "nodes": {
        "acme-widgets": {"file": "world/knowledge/tree/acme/acme-widgets.md",
                         "summary": "Widgets.", "last_updated": "2026-01-01"}}}
    (tree / "_tree.yaml").write_text(yaml.safe_dump(index, sort_keys=False), encoding="utf-8")
    records = [
        _hyp(A, claim=claim),
        _hyp(B, title="Gear demand is flat", claim="Demand for gears did not move in 2025."),
        _hyp(C, stage="resolved", outcome="CONFIRMED", resolved_date="2026-02-01",
             title="Widgets resolved title", claim="Resolved widgets claim, long enough to keep."),
    ]
    (w / "pipeline.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    (w / "guardrails.jsonl").write_text(
        json.dumps({"id": "guard-1", "category": "acme", "rule": "r", "status": "active"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WORLD_PATH", str(w))
    monkeypatch.setenv(export_mod._GOAL_HANDLE_SECRET_VAR, SECRET)
    monkeypatch.setenv("ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("MIND_SID", "sid-under-test")
    return w


@pytest.fixture
def retention(tmp_path):
    return tmp_path / "spool" / ENV_ID / "retention"


def _store_path(world: Path) -> Path:
    return world / "pipeline.jsonl"


def _stored_all(world: Path) -> list[dict]:
    return [json.loads(ln) for ln in _store_path(world).read_text(encoding="utf-8").splitlines() if ln]


def _stored(world: Path, item_id: str) -> dict:
    return next(r for r in _stored_all(world) if r["id"] == item_id)


def _handle(item_id: str) -> str:
    return item_handle("hypothesis", item_id, SECRET, ENV_ID)


class _Trace:
    """Stands in for the daemon transport of POST /v1/pipeline/update-field.

    Every call runs the real handler against the world's store, and the record as it stands AFTER
    each write is kept with the field that write set, in order.
    """

    def __init__(self, world):
        self.world = world
        self.snaps: list[tuple[str, dict]] = []

    def __call__(self, method, path, query=None, body=None, headers=None):
        assert (method, path) == ("POST", "/v1/pipeline/update-field")
        params = {k: v[0] for k, v in urllib.parse.parse_qs(query, keep_blank_values=True).items()}
        ctx = SimpleNamespace(query=params, paths=SimpleNamespace(world=self.world), headers={})
        resp = pipeline_write.update_field(ctx)
        if resp.status >= 400:
            raise _rt.RtError(f"daemon HTTP {resp.status}: {resp.body.decode('utf-8')}")
        if params["id"] == A:
            self.snaps.append((params["field"], _stored(self.world, A)))
        return resp.body.decode("utf-8")


def _apply(op: str, retention: Path) -> int:
    return apply_mod.main(["--handle", _handle(A), "--op", op, f"--retention-dir={retention}", "--apply"], {})


#: What each write of a forget, an undo and a second forget leaves, named, and the field it sets.
#: The fixture checks the fields against what the writers actually did, so a reordering of the
#: writers fails there instead of silently relabelling the states.
STEPS = (
    ("marked", FORGOTTEN_FIELD), ("claim_blanked", "claim"), ("forgotten", "title"),
    ("undo_stamped", RESTORED_FIELD), ("claim_restored", "claim"), ("text_restored", "title"),
    ("restored", FORGOTTEN_FIELD),
    ("reforget_marked", FORGOTTEN_FIELD), ("reforget_claim_blanked", "claim"), ("reforgotten", "title"),
    ("undo2_stamped", RESTORED_FIELD), ("claim_restored2", "claim"), ("text_restored2", "title"),
    ("restored_again", FORGOTTEN_FIELD),
)

#: Which event a state belongs to. Copies of one event differ only in how far a writer got.
EPOCH = {"original": 0, "marked": 1, "claim_blanked": 1, "forgotten": 1,
         "undo_stamped": 2, "claim_restored": 2, "text_restored": 2, "restored": 2,
         "reforget_marked": 3, "reforget_claim_blanked": 3, "reforgotten": 3,
         "undo2_stamped": 4, "claim_restored2": 4, "text_restored2": 4, "restored_again": 4}


@pytest.fixture
def states(world, retention, monkeypatch, spaced) -> dict[str, dict]:
    """A hypothesis taken through forget, undo, a second forget and a second undo by the real
    writers, and the record after every write. ``spaced`` moves the clock an hour between the four
    operations; without it all four run in one second, which is the case the stamps must still
    order. Two undos, because a group that dropped ``restored_at`` is only wrong once a second one
    has to outrank the first."""
    clock = {"now": T0}
    monkeypatch.setattr(knowledge_retention, "now_utc", lambda: clock["now"])
    trace = _Trace(world)
    monkeypatch.setattr(_rt, "rt_call", trace)
    original = _stored(world, A)
    for op in ("forget", "undo", "forget", "undo"):
        assert _apply(op, retention) == 0, op
        if spaced:
            clock["now"] += HOUR
    assert [field for field, _ in trace.snaps] == [field for _, field in STEPS]
    out = {"original": original}
    out.update({name: rec for (name, _), (_, rec) in zip(STEPS, trace.snaps)})
    assert set(out) == set(EPOCH)
    return out


def _group(rec: dict) -> dict:
    return {k: rec[k] for k in GROUP if k in rec}


def _dump(rec: dict) -> str:
    """The record as the store writes it: key order counts."""
    return json.dumps(rec, ensure_ascii=True)


def _merge(x: dict, y: dict) -> dict:
    return cm._merge_pipeline_record(x, y)


def _rb(records) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=True) + "\n" for r in records).encode()


def _recs(blob: bytes) -> list[dict]:
    return [json.loads(line) for line in blob.decode().splitlines() if line.strip()]


# --- the writers: what feeds the merge ----------------------------------------


def test_the_writers_stamp_each_event_after_the_one_before_it(states):
    s = states
    assert FORGOTTEN_FIELD not in s["original"] and RESTORED_FIELD not in s["original"]
    first, undone, second, again = s["forgotten"][FORGOTTEN_FIELD], s["restored"][RESTORED_FIELD], \
        s["reforgotten"][FORGOTTEN_FIELD], s["restored_again"][RESTORED_FIELD]
    assert first < undone < second < again, "ordered even when all four run in one second"
    assert s["restored_again"][FORGOTTEN_FIELD] is None
    assert s["restored_again"]["claim"] == s["original"]["claim"]
    assert s["restored"][FORGOTTEN_FIELD] is None, "an undo clears the marker and keeps the key"
    assert (s["restored"]["claim"], s["restored"]["title"]) == (s["original"]["claim"], s["original"]["title"])
    assert s["forgotten"]["claim"].startswith(HEAD) and s["forgotten"]["title"].startswith(HEAD)
    assert s["reforgotten"][RESTORED_FIELD] == undone, "a second forget keeps the undo's stamp"
    # What a writer caught part way leaves.
    assert (s["marked"]["claim"], s["marked"][FORGOTTEN_FIELD]) == (s["original"]["claim"], first)
    assert s["undo_stamped"]["claim"].startswith(HEAD)
    assert (s["undo_stamped"][FORGOTTEN_FIELD], s["undo_stamped"][RESTORED_FIELD]) == (first, undone)


# --- the merge: the later event wins ------------------------------------------


def test_the_later_event_wins_the_statement_and_the_stamps_in_either_merge_order(states):
    for earlier, later in itertools.product(states, states):
        if EPOCH[earlier] >= EPOCH[later]:
            continue
        for x, y in ((earlier, later), (later, earlier)):
            merged = _merge(states[x], states[y])
            assert _group(merged) == _group(states[later]), (earlier, later, x, y)


@pytest.mark.parametrize("stale_stage", STAGES)
def test_the_later_event_wins_whatever_stage_the_stale_copy_is_at(states, stale_stage):
    """A stale copy is further along when a peer resolved or archived the hypothesis after the
    forgetting box last pulled. The stage is still the base rule's; the statement is not."""
    for earlier, later in itertools.product(states, states):
        if EPOCH[earlier] >= EPOCH[later]:
            continue
        stale, fresh = dict(states[earlier], stage=stale_stage), states[later]
        further = max((stale_stage, fresh["stage"]), key=cm._PIPELINE_STAGE_RANK.get)
        for x, y in ((stale, fresh), (fresh, stale)):
            merged = _merge(x, y)
            assert _group(merged) == _group(fresh), (earlier, later, stale_stage)
            assert merged["stage"] == further, (earlier, later, stale_stage)


def test_copies_of_one_event_converge_on_one_of_them(states):
    for x, y in itertools.combinations_with_replacement(states, 2):
        if EPOCH[x] != EPOCH[y]:
            continue
        xy, yx = _merge(states[x], states[y]), _merge(states[y], states[x])
        assert _dump(xy) == _dump(yx), (x, y)
        assert _group(xy) in (_group(states[x]), _group(states[y])), (x, y)


def test_a_finished_undo_beats_a_copy_caught_part_way_through_it(states):
    for midway in ("undo_stamped", "claim_restored", "text_restored"):
        for x, y in ((midway, "restored"), ("restored", midway)):
            assert _group(_merge(states[x], states[y])) == _group(states["restored"]), (x, y)


def test_equal_events_are_ordered_by_the_groups_content_alone(states):
    """Two copies of one event differ in how far a writer got. Which one stands is a function of
    the statement and stamps they hold, never of the rest of the record: the merged record's
    order key must be the winner's, or the fold would depend on how it is grouped."""
    x = dict(states["claim_blanked"], rationale="aaa")
    y = dict(states["forgotten"], rationale="zzz")
    assert cm._canon(_group(x)) > cm._canon(_group(y)), "premise: the group says x"
    assert cm._canon(x) < cm._canon(y), "adverse: the rest of the record says y"

    for p, q in ((x, y), (y, x)):
        assert _group(_merge(p, q)) == _group(x)


def test_a_merge_only_ever_produces_a_state_a_writer_passed_through(states):
    """The statement and both stamps are taken whole from one copy, so a merge cannot mix a
    cleared marker with blanked text, which would show the member a tombstone."""
    for x, y in itertools.product(states, states):
        merged = _merge(states[x], states[y])
        assert _group(merged) in (_group(states[x]), _group(states[y])), (x, y)
        shown_blank = not is_forgotten(merged) and any(
            str(merged[name]).startswith(HEAD) for name in ("claim", "title"))
        assert not shown_blank, (x, y)


def test_three_copies_fold_to_one_record_in_every_order_and_every_grouping(states):
    for trio in itertools.combinations_with_replacement(states, 3):
        outcomes = set()
        for p, q, r in itertools.permutations(trio):
            sp, sq, sr = states[p], states[q], states[r]
            outcomes.add(_dump(_merge(_merge(sp, sq), sr)))
            outcomes.add(_dump(_merge(sp, _merge(sq, sr))))
        assert len(outcomes) == 1, trio


# --- the merge: what the group is, and what it is not -------------------------


@pytest.mark.parametrize("field", item_text_fields("hypothesis"))
def test_every_field_a_forget_blanks_comes_from_the_forgotten_copy(states, field):
    stale = dict(states["original"], stage="resolved")
    for x, y in ((stale, states["forgotten"]), (states["forgotten"], stale)):
        assert _merge(x, y)[field] == states["forgotten"][field]


def test_fields_outside_the_group_follow_the_base_rule(states):
    stale = dict(states["original"], stage="resolved", rationale="The stale copy's rationale.")
    forgotten = dict(states["forgotten"], rationale="The forgotten copy's rationale.")
    for x, y in ((stale, forgotten), (forgotten, stale)):
        merged = _merge(x, y)
        assert (merged["stage"], merged["rationale"]) == ("resolved", "The stale copy's rationale.")
        assert merged["claim"] == forgotten["claim"]


def test_the_inputs_are_adverse_without_the_stamps_the_base_rule_brings_the_stale_statement_back(states):
    """The positive control for the tests above: strip the stamps and the SAME pairs merge by stage
    and then by content, and the stale statement wins for some of them. A merge test the old rule
    could pass would prove nothing about this one."""
    bare = {k: v for k, v in states["forgotten"].items() if k not in (FORGOTTEN_FIELD, RESTORED_FIELD)}
    stale_won = [stage for stage in STAGES for x, y in ((dict(states["original"], stage=stage), bare),
                                                        (bare, dict(states["original"], stage=stage)))
                 if _merge(x, y)["claim"] == states["original"]["claim"]]
    assert stale_won, "no pair brought the stale statement back, so these inputs are not adverse"


# --- the merge: hand-built records, for the cases a lifecycle cannot make -----

FIRST, LATER = "2026-10-03T12:00:00+00:00", "2026-10-03T12:00:01+00:00"
TOMBSTONE = HEAD + "2026-10-03."


def _rec(**extra) -> dict:
    return _hyp(A, **extra)


def test_a_finished_undo_beats_an_unfinished_one_whatever_their_content_says():
    done = _rec(claim=CLAIMS[0], **{FORGOTTEN_FIELD: None, RESTORED_FIELD: LATER})
    midway = _rec(claim=TOMBSTONE, title=TOMBSTONE, **{FORGOTTEN_FIELD: FIRST, RESTORED_FIELD: LATER})
    assert cm._canon(_group(midway)) > cm._canon(_group(done)), "adverse: content alone picks the unfinished copy"

    for x, y in ((done, midway), (midway, done)):
        assert _group(_merge(x, y)) == _group(done)


def test_a_tie_between_a_forget_and_an_undo_hides():
    forgotten = _rec(claim=TOMBSTONE, title=TOMBSTONE, **{FORGOTTEN_FIELD: FIRST})
    restored = _rec(claim=CLAIMS[1], **{FORGOTTEN_FIELD: None, RESTORED_FIELD: FIRST})
    assert cm._canon(_group(restored)) > cm._canon(_group(forgotten)), "adverse: content alone shows it"

    for x, y in ((forgotten, restored), (restored, forgotten)):
        assert _group(_merge(x, y)) == _group(forgotten)


def test_a_copy_whose_two_stamps_tie_counts_as_hidden():
    """No writer stamps both in one instant, but a hand edit or a clock fault can. The marker is
    still set, so the exposure hides the record, and the merge ranks it hidden too: a finished
    undo at the same stamp does not win it back."""
    tied = _rec(claim=TOMBSTONE, title=TOMBSTONE, **{FORGOTTEN_FIELD: FIRST, RESTORED_FIELD: FIRST})
    done = _rec(claim=CLAIMS[1], **{FORGOTTEN_FIELD: None, RESTORED_FIELD: FIRST})
    assert is_forgotten(tied) and not is_forgotten(done)

    for x, y in ((tied, done), (done, tied)):
        assert _group(_merge(x, y)) == _group(tied)


def test_a_copy_that_never_saw_an_undo_does_not_inherit_its_stamp():
    """A box that forgot after the undo, without having seen it, holds no ``restored_at``. The
    group is one state: the merge does not carry the other copy's stamp across into it."""
    forgotten = _rec(claim=TOMBSTONE, title=TOMBSTONE, **{FORGOTTEN_FIELD: "2026-10-03T13:00:00+00:00"})
    restored = _rec(claim=CLAIMS[0], **{FORGOTTEN_FIELD: None, RESTORED_FIELD: LATER})

    for x, y in ((forgotten, restored), (restored, forgotten)):
        merged = _merge(x, y)
        assert _group(merged) == _group(forgotten)
        assert RESTORED_FIELD not in merged


def test_a_stamp_the_undo_cleared_is_not_brought_back_by_the_side_only_union():
    """The base rule unions a field one side lacks. A cleared marker is a key set to null, and
    the stale copy's stamp would otherwise ride back onto the restored one."""
    restored = _rec(claim=CLAIMS[0], **{FORGOTTEN_FIELD: None, RESTORED_FIELD: LATER})
    stale = _rec(claim=TOMBSTONE, title=TOMBSTONE, stage="resolved", **{FORGOTTEN_FIELD: FIRST})

    for x, y in ((restored, stale), (stale, restored)):
        merged = _merge(x, y)
        assert merged[FORGOTTEN_FIELD] is None and not is_forgotten(merged)
        assert merged["claim"] == CLAIMS[0]


def test_records_nobody_forgot_merge_by_stage_then_content_as_before():
    low = {"id": A, "stage": "active", "claim": "zzz sorts last", "title": "t"}
    high = {"id": A, "stage": "resolved", "claim": "aaa sorts first", "title": "t"}
    for x, y in ((low, high), (high, low)):
        assert _merge(x, y)["claim"] == "aaa sorts first", "the further-along copy is the base"
    equal_a = {"id": A, "stage": "active", "claim": "aaa", "title": "t"}
    equal_z = {"id": A, "stage": "active", "claim": "zzz", "title": "t"}
    for x, y in ((equal_a, equal_z), (equal_z, equal_a)):
        assert _merge(x, y)["claim"] == "zzz", "equal stage: the greater content stands"


# --- whole stores: bytes, fixpoints and what the member sees ------------------


def test_merged_stores_are_byte_commutative_stable_and_leave_other_records_alone(states, world):
    others = [_dump(r) for r in _stored_all(world) if r["id"] != A]
    for x, y in itertools.product(states, states):
        local, remote = _rb([states[x]] + [json.loads(o) for o in others]), \
            _rb([states[y]] + [json.loads(o) for o in others])
        merged = cm.merge_pipeline(local, remote)
        assert merged == cm.merge_pipeline(remote, local), (x, y)
        assert cm.merge_pipeline(merged, merged) == merged, (x, y)
        assert cm.merge_pipeline(merged, local) == merged and cm.merge_pipeline(merged, remote) == merged, (x, y)
        assert merged.decode().splitlines()[1:] == others, "the other records are byte-identical"


@pytest.mark.parametrize("ours, theirs", [("forgotten", "original"), ("restored", "forgotten"),
                                          ("reforgotten", "restored"), ("restored_again", "reforgotten")])
def test_boxes_that_keep_exchanging_copies_settle_on_the_later_event(states, ours, theirs):
    local_a, local_b = _rb([states[ours]]), _rb([states[theirs]])
    settled = cm.merge_pipeline(local_a, local_b)
    for _ in range(4):
        from_b = cm.merge_pipeline(local_b, settled)
        from_a = cm.merge_pipeline(local_a, from_b)
        assert from_a == from_b, "not converged: the boxes disagree"
        settled = from_a
    assert _group(_recs(settled)[0]) == _group(states[ours])


def _published(world: Path) -> set:
    bundle = export_mod.build_bundle(world, _ROOT, extra_paths=(), env=None)
    return {row.get("handle") for row in bundle.hypotheses}


def test_the_member_sees_what_the_merge_kept(states, world):
    others = [r for r in _stored_all(world) if r["id"] != A]
    cases = [("forgotten", "original", False), ("marked", "original", False),
             ("restored", "forgotten", True), ("restored", "undo_stamped", True),
             ("reforgotten", "restored", False), ("restored_again", "reforgotten", True)]
    for ours, theirs, shown in cases:
        for local, remote in ((ours, theirs), (theirs, ours)):
            merged = cm.merge_pipeline(_rb([states[local]] + others), _rb([states[remote]] + others))
            _store_path(world).write_bytes(merged)
            published = _published(world)
            assert (_handle(A) in published) is shown, (ours, theirs, local)
            assert {_handle(B), _handle(C)} <= published, "the control records stay shown"
