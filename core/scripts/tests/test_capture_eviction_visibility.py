""" — capture-lane eviction must be COUNTED, not silent.

THE DEFECT. Both append paths enforced the array cap with a bare
`while len(arr) > limit: arr.pop(0)` and returned `{"ok": true}` / rc=0.
Nothing recorded the drop. Measured on one live worker Body (alpha, cc-07,
SID c4d40b86):

    exp_capture       135 appends,  20 kept  -> 115 destroyed
    spark_capture     136 appends,  50 kept  ->  86 destroyed
    hyp_capture        24 appends,  10 kept  ->  14 destroyed
    encoding_capture   52 appends,  52 kept  ->   0  (UNCAPPED)

215 entries gone. The uncapped lane is the positive control: appends ==
surviving EXACTLY, so appends are 1:1 with entries and the subtraction is
arithmetic rather than inference. This matters more than an ordinary counter
because the capture lanes are the ONLY channel a worker Body's execution
learning has -- a worker skips every reducer-only phase, so Phase 6.5 never
runs there.

WHY THE COUNTER IS TOP-LEVEL AND NOT slot_meta -- the pin this file exists for.
`body-merge.py::merge_wm` merges `slot_meta` REDUCER-WINS (only Body-only metas
are added), so a counter parked there is DISCARDED at generalize-down: the same
silent loss, one layer up, in the exact code path that is supposed to carry a
worker's findings home. Top-level keys instead route through `_merge_value`,
where a nested int gets the 3-way-delta SUM. test_slot_meta_counter_is_discarded
is the negative control that keeps that reasoning falsifiable rather than
asserted -- if body-merge ever changes policy, THAT test fails and tells the next
reader the design premise moved.

The caps are deliberately NOT raised here. Raising them trades a silent data
loss for a silent unbounded growth (the reset-surviving reason in
memory-pipeline.yaml is real). Counting first is what makes any future cap
choice measurable instead of estimated -- which is what failed the first time.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "exp_capture"
CAP = 20  # memory-pipeline.yaml working_memory_pruning.array_limits.exp_capture


def _entry(n: int) -> dict:
    """One capture item. `_item_ts` is present because the append path stamps
    every dict item -- a fixture without it exercises a shape production never
    produces (guard-920)."""
    return {
        "goal_id": f"g-306-289-{n:03d}",
        "category": "cross-box-bodies",
        "execution_summary": f"entry {n}",
        "outcome_class": "routine",
        "key_decisions": [],
        "surprise_level": 0,
        "verbatim_anchors": [],
        "_item_ts": f"2026-08-14T00:{n // 60:02d}:{n % 60:02d}",
    }


def _append(item: dict) -> None:
    """Invoke the real cmd_append against the redirected WM.

    The item arrives on STDIN in production ("JSON from stdin" per the parser),
    so the fixture feeds stdin rather than passing the item as an attribute --
    a different call shape would exercise a branch production never takes.
    """
    saved = sys.stdin
    sys.stdin = io.StringIO(json.dumps(item))
    try:
        wm.cmd_append(SimpleNamespace(slot=SLOT))
    finally:
        sys.stdin = saved


class _TempWM:
    """BODY_WM_PATH is the ONLY correct redirect -- patching wm.WM_PATH is a
    no-op for I/O and would target the live agent's WM (guard-862)."""

    def __enter__(self):
        self._orig = os.environ.get("BODY_WM_PATH")
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["BODY_WM_PATH"] = str(Path(self._tmp.name) / "working-memory.yaml")
        wm.cmd_init(SimpleNamespace())
        return self

    def __exit__(self, *exc):
        if self._orig is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = self._orig
        self._tmp.cleanup()
        return False


def test_eviction_increments_a_top_level_counter():
    """Appending past the cap records how many entries were destroyed."""
    with _TempWM():
        for i in range(CAP + 3):
            _append(_entry(i))
        data = wm.read_wm()

        assert len(data["slots"][SLOT]) == CAP, "cap no longer enforced"
        ev = data.get("capture_evictions")
        assert isinstance(ev, dict), (
            "capture_evictions missing -- eviction is silent again, which is the "
            "whole defect g-306-289 was filed for")
        assert ev.get(SLOT) == 3, (
            f"expected 3 evictions (23 appends, cap {CAP}), got {ev.get(SLOT)}")


def test_counter_is_top_level_not_slot_meta():
    """The design pin. slot_meta is reducer-wins in body-merge, so a counter
    there never reaches the reducer."""
    with _TempWM():
        for i in range(CAP + 1):
            _append(_entry(i))
        data = wm.read_wm()

        assert "capture_evictions" in data, "counter is not at the top level"
        meta = (data.get("slot_meta") or {}).get(SLOT) or {}
        assert "evicted" not in json.dumps(meta), (
            "eviction count is being written into slot_meta, which body-merge "
            "merges reducer-wins -- it would be discarded at generalize-down")


def test_no_eviction_leaves_the_counter_clean():
    """A lane under its cap must not inflate the counter. Without this, a
    non-zero reading could not be trusted to mean real loss."""
    with _TempWM():
        for i in range(CAP - 1):
            _append(_entry(i))
        data = wm.read_wm()

        assert len(data["slots"][SLOT]) == CAP - 1
        ev = data.get("capture_evictions") or {}
        assert not ev.get(SLOT), f"counter inflated with no eviction: {ev}"


def test_eviction_counter_accumulates_across_appends():
    """Each overflow adds to the running total rather than overwriting it --
    otherwise the figure reads as 1 forever and understates the loss."""
    with _TempWM():
        for i in range(CAP + 5):
            _append(_entry(i))
        first = (wm.read_wm().get("capture_evictions") or {}).get(SLOT)
        assert first == 5, f"expected 5, got {first}"

        for i in range(CAP + 5, CAP + 9):
            _append(_entry(i))
        second = (wm.read_wm().get("capture_evictions") or {}).get(SLOT)
        assert second == 9, f"expected cumulative 9, got {second}"


def test_body_merge_sums_eviction_counters_across_bodies():
    """The reason the counter is top-level: it must AGGREGATE, so a reducer sees
    total loss across every Body rather than one Body's slice."""
    sys.path.insert(0, str(CORE_SCRIPTS))
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "body_merge_mod", CORE_SCRIPTS / "body-merge.py")
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)

    reducer = {"capture_evictions": {SLOT: 10}, "slots": {}, "slot_meta": {}}
    body = {"capture_evictions": {SLOT: 4}, "slots": {}, "slot_meta": {}}
    merged = bm.merge_wm(reducer, body)
    assert merged["capture_evictions"][SLOT] == 14, (
        "top-level counters must SUM at generalize-down; got "
        f"{merged['capture_evictions'][SLOT]}")


def test_slot_meta_counter_is_discarded_negative_control():
    """Proves WHY the counter is not in slot_meta, instead of asserting it.

    If body-merge ever stops being reducer-wins on slot_meta, this test fails and
    tells the next reader the design premise moved -- rather than leaving a
    rationale comment that quietly stopped being true.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "body_merge_mod2", CORE_SCRIPTS / "body-merge.py")
    bm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bm)

    reducer = {"slots": {}, "slot_meta": {SLOT: {"update_count": 1}}}
    body = {"slots": {}, "slot_meta": {SLOT: {"update_count": 99, "evicted": 115}}}
    merged = bm.merge_wm(reducer, body)
    assert "evicted" not in merged["slot_meta"][SLOT], (
        "slot_meta is no longer reducer-wins -- the g-306-289 placement "
        "rationale needs re-deriving, not just this test updating")
