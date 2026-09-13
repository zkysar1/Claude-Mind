""" — the consumed-watermark must suppress re-delivery on the
GENERALIZE-DOWN path, not only on the fast lane.

THE DEFECT. `capture_consumed_hashes` (g-306-311) is the durable record of every
capture entry a reducer has ALREADY merged. It was wired into exactly one of the
two delivery paths: `capture_fast_lane.py` passes it as `extra_seen`, while
`body-merge.py` referenced it ZERO times. `merge_wm` -> `_merge_value` ->
`_dedup_append(r_val, b_val)` with no `extra_seen` is, per that helper's own
docstring, "the previous behaviour byte-for-byte" — i.e. the exact bug g-306-310
measured. Generalize-down is the path `aspirations-spark` names as how a worker
Body's captures reach the reducer, so on that path clearing a consumed slot is
precisely what re-delivers the full set.

WHY DEDUP ALONE CANNOT CATCH IT. `_dedup_append` keys on the reducer's CURRENT
slot contents. Consumption CLEARS the slot, so the basis is empty and the source
Body — which retains its own entries indefinitely — re-offers everything. The
crash case (reducer still holds the items) dedups correctly, which is why closed
drain trackers kept missing this.

BUG-FIRST (guard-385). Each assertion below was shown to FAIL before the fix by
disabling it in place; see the goal's outcome_note for the recorded run.
"""
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    """Import a hyphenated CLI module by path (the house idiom)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

bmg = _load("body_merge_watermark_tests", "body-merge.py")
import wm  # noqa: E402

@pytest.fixture(autouse=True)
def _hermetic_world_staged(tmp_path, monkeypatch):
    """Point the staged-WM root at a TMP world for every test in this file.

    Load-bearing, not tidiness (g-115-9750, same reasoning as the g-306-420
    carrier fixture). `_consume_staged` now resolves the world-rooted staging
    dir through `_paths.WORLD_DIR`, so without this fixture a test would scan —
    and a producer test would WRITE into — the LIVE `world/`, which on an
    own-cloud box is the guard-955 production-key collision class. MEASURED,
    not hypothetical: the first run of this change (before this fixture
    existed) left three synthetic files in the live
    `world/body-staged-wm/test_stage_and_push_writes_tri0/`.

    Patching the module ATTRIBUTE works because `world_staged_dir` does its
    `from _paths import WORLD_DIR` inside the function body.
    """
    import _paths
    w = tmp_path / "world"
    w.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_paths, "WORLD_DIR", w, raising=False)
    return w



CONSUMED_SLOT = "capture_consumed_hashes"


def _entry(goal_id: str, text: str) -> dict:
    """A capture entry shaped like the ones worker-loop Phase 3.5/3.6 append."""
    return {"goal_id": goal_id, "observation": text}


def _wm(slots: dict) -> dict:
    return {"slots": dict(slots)}


def test_consumed_slot_name_matches_capture_fast_lane():
    """The twin literal is pinned, not trusted to a comment.

    body-merge.py spells "capture_consumed_hashes" rather than importing it,
    because capture_fast_lane.py imports body-merge as `bmg` and the reverse
    import would be circular. That is a deliberate duplication, so the agreement
    needs a test or it can drift silently in either direction.
    """
    cfl = _load("capture_fast_lane_watermark_tests", "capture_fast_lane.py")
    assert cfl.CONSUMED_HASHES_SLOT == CONSUMED_SLOT


@pytest.mark.parametrize("slot", list(wm.CAPTURE_SLOTS))
def test_consumed_entry_is_not_redelivered_when_slot_was_cleared_to_empty(slot):
    """THE REGRESSION. Reducer consumed the entry and cleared the slot to [].

    Before the fix `_dedup_append([], [entry])` saw an empty basis and appended
    the entry straight back.
    """
    consumed = _entry("g-1", "already merged once")
    h = bmg._content_hash(consumed)

    reducer = _wm({slot: [], CONSUMED_SLOT: {slot: [h]}})
    body = _wm({slot: [consumed]})

    merged = bmg.merge_wm(reducer, body)
    assert merged["slots"][slot] == [], (
        f"{slot}: a consumed entry was re-delivered on generalize-down"
    )


@pytest.mark.parametrize("slot", list(wm.CAPTURE_SLOTS))
def test_consumed_entry_is_not_redelivered_when_slot_key_was_removed(slot):
    """The SECOND clear-shape, and the one the absent-key branch hid.

    `merge_wm`'s slot loop assigns `b_val` WHOLESALE when the key is missing
    from the reducer — no dedup runs at all. A fix applied only to the
    `sk in r_slots` branch passes the test above and still re-delivers here, so
    both shapes are pinned.
    """
    consumed = _entry("g-2", "merged once, key then removed")
    h = bmg._content_hash(consumed)

    reducer = _wm({CONSUMED_SLOT: {slot: [h]}})  # note: no `slot` key at all
    body = _wm({slot: [consumed]})

    merged = bmg.merge_wm(reducer, body)
    assert merged["slots"][slot] == [], (
        f"{slot}: absent-key branch re-delivered a consumed entry unfiltered"
    )


def test_unconsumed_entries_still_arrive():
    """The fix must not become a blanket suppressor.

    An entry the reducer has never seen has no hash in the watermark and must
    still be delivered — otherwise the 'fix' silently drops all new captures,
    which is a strictly worse defect than the one being closed.
    """
    slot = "spark_capture"
    old = _entry("g-1", "already merged")
    fresh = _entry("g-2", "brand new observation")

    reducer = _wm({slot: [], CONSUMED_SLOT: {slot: [bmg._content_hash(old)]}})
    body = _wm({slot: [old, fresh]})

    merged = bmg.merge_wm(reducer, body)
    assert merged["slots"][slot] == [fresh]


def test_non_capture_array_slots_keep_the_no_extra_seen_default():
    """Outcome 3: the opt-in property `_dedup_append` documents is preserved.

    A non-capture array slot must merge exactly as before even when a watermark
    dict happens to carry an entry under its name — `_watermark_for` gates on
    `wm.CAPTURE_SLOTS` membership, not on the watermark's contents, so a stray
    key cannot start suppressing an unrelated lane.
    """
    slot = "known_blockers"
    assert slot not in wm.CAPTURE_SLOTS, "fixture slot must not be a capture slot"

    item = {"blocker_id": "b-1"}
    h = bmg._content_hash(item)

    reducer = _wm({slot: [], CONSUMED_SLOT: {slot: [h]}})
    body = _wm({slot: [item]})

    merged = bmg.merge_wm(reducer, body)
    assert merged["slots"][slot] == [item], (
        "a non-capture slot was suppressed by the capture watermark"
    )


def test_missing_or_malformed_watermark_is_inert():
    """Fail-open: an absent or wrong-typed watermark restores prior behaviour.

    The watermark lives in the reducer's own WM and can be absent (a fresh
    reducer), or a non-dict if something upstream wrote it wrong. Neither may
    raise, and neither may suppress — dropping captures on a malformed watermark
    would lose work silently.
    """
    slot = "spark_capture"
    item = _entry("g-3", "never merged")
    body = _wm({slot: [item]})

    for bad in (None, [], "not-a-dict", {slot: "not-a-list"}):
        reducer = _wm({slot: []} if bad is None else {slot: [], CONSUMED_SLOT: bad})
        merged = bmg.merge_wm(reducer, body)
        assert merged["slots"][slot] == [item], f"watermark {bad!r} was not inert"
