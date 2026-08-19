#!/usr/bin/env python3
"""Pins array_limits enforcement on the GENERALIZE-DOWN path ().

WHY THIS FILE EXISTS. Cap enforcement lived only on the APPEND path — wm.py
``cmd_append`` and its daemon mirror ``wm_write.py::append_slot``. But a worker
Body's capture entries reach a reducer's working memory through
``body-merge.py::merge_wm``, not through append, and that function contained
ZERO references to ``array_limits``, ``_eviction_sort_key`` or ``limit``. So
every cap declared in ``core/config/memory-pipeline.yaml`` was simply not
applied to the traffic that fills these lanes. Measured 2026-08-17 on the
reducer (cc-04): ``spark_capture`` 69 against cap 50, ``exp_capture`` 40
against cap 20 — two independent lanes over, same mechanism.

NOT A REGRESSION, and that was worth checking before writing a word of it:
``git log -S array_limits -- core/scripts/body-merge.py`` and the same for
``_eviction_sort_key`` both return NOTHING, so the merge path never enforced.
The goal record listed the regression-vs-omission question as UNMEASURED and
asked not to assert a cause without checking.

WHAT THE CAPS ARE FOR (g-306-176): these slots SURVIVE ``wm-reset``. Without a
cap they grow without bound across every session the reducer ever runs. The
harm being fixed here is unbounded growth of a reset-surviving slot — NOT data
loss. Nothing was being destroyed on this path precisely because nothing
evicted, and inferring loss from an over-cap count is the unprobed positive
claim ``verify-before-assuming.md`` governs.

THE OTHER HALF, so a reader does not mistake this for the whole story. The two
paths fail in OPPOSITE directions, measured the same day on the same fleet:
the reducer sat OVER cap because merge never evicts, while a worker Body
(cc-08) sat exactly AT cap — spark 50/50, exp 20/20, every entry
``load_bearing`` — where an unflagged append sorts to index 0 and is popped by
the very write that created it (g-306-308 / g-115-6541). Restoring enforcement
here makes that selection defect start to bite on this path too. These tests
pin the enforcement, not the selection policy; ``load_bearing`` ordering is
asserted below only to prove the shared key is actually being consulted.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import wm  # noqa: E402


def _load_body_merge():
    spec = importlib.util.spec_from_file_location(
        "body_merge_under_test", SCRIPTS / "body-merge.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def bmg():
    return _load_body_merge()


@pytest.fixture(scope="module")
def limits():
    """The REAL configured caps, read the way the code reads them.

    Deliberately not a hardcoded dict: a test that re-types the caps stops
    agreeing with config the first time either side moves, and agrees loudly
    in the meantime.
    """
    lim = wm.get_pruning_config(wm.read_config()).get("array_limits", {}) or {}
    assert lim, "CONTROL FAILED: array_limits is empty — config did not load"
    return lim


def entry(i: int, load_bearing: bool = False) -> dict:
    d = {"goal_id": f"g-{i:04d}", "_item_ts": f"2026-08-17T{i // 60:02d}:{i % 60:02d}:00"}
    if load_bearing:
        d["load_bearing"] = True
    return d


# ── the defect itself ───────────────────────────────────────────────────────

def test_merged_capped_slot_is_brought_back_under_cap(bmg, limits):
    """THE LOAD-BEARING ASSERTION. Reducer 45 + Body 30 = 75 spark entries
    against cap 50. Before this fix the merged slot came out at 75."""
    cap = limits["spark_capture"]
    reducer = {"slots": {"spark_capture": [entry(i) for i in range(cap - 5)]}}
    body = {"slots": {"spark_capture": [entry(100 + i) for i in range(30)]}}

    out = bmg.merge_wm(reducer, body)

    merged = out["slots"]["spark_capture"]
    assert len(merged) == cap, (
        f"merge_wm left spark_capture at {len(merged)} against cap {cap} — "
        "array_limits is not being applied on the generalize-down path, which "
        "is the g-306-309 defect"
    )


def test_every_configured_cap_is_enforced_not_just_the_capture_lanes(bmg, limits):
    """The defect is general: the goal was written about the capture lanes, but
    NOTHING in array_limits was enforced here. knowledge_debt, known_blockers,
    micro_hypotheses, recent_violations and sensory_buffer are equally
    reset-surviving and equally uncapped on this path. Driven off the config so
    a newly-added cap joins this test by existing."""
    reducer = {"slots": {slot: [entry(i) for i in range(cap + 7)]
                         for slot, cap in limits.items()}}
    out = bmg.merge_wm(reducer, {"slots": {}})

    over = {slot: len(out["slots"][slot])
            for slot, cap in limits.items() if len(out["slots"][slot]) > cap}
    assert not over, f"slots still over cap after merge: {over} (caps {limits})"


def test_uncapped_array_slot_is_left_alone(bmg, limits):
    """encoding_capture is in wm.ARRAY_SLOTS but carries NO array_limits entry —
    it is uncapped on purpose. Enforcement must not invent a cap for it, or the
    lane that currently absorbs worker encoding hand-offs starts silently
    dropping them."""
    assert "encoding_capture" not in limits, (
        "encoding_capture gained a cap — that is a real decision, but this test "
        "encoded the opposite; re-read the intent before changing the assertion"
    )
    reducer = {"slots": {"encoding_capture": [entry(i) for i in range(300)]}}
    out = bmg.merge_wm(reducer, {"slots": {}})
    assert len(out["slots"]["encoding_capture"]) == 300


def test_non_list_slot_values_are_untouched(bmg):
    """A capped NAME whose value is not a list (a scalar left by an older
    schema, or a dict) must pass through rather than raise."""
    reducer = {"slots": {"spark_capture": None, "known_blockers": {"a": 1}}}
    out = bmg.merge_wm(reducer, {"slots": {}})
    assert out["slots"]["spark_capture"] is None
    assert out["slots"]["known_blockers"] == {"a": 1}


# ── the shared policy is genuinely shared ───────────────────────────────────

def test_eviction_consults_the_load_bearing_key(bmg, limits):
    """Proves merge-time eviction goes through wm's key rather than a local
    FIFO. Every load-bearing entry must survive while unflagged ones absorb the
    cap pressure. A plain `del arr[:n]` or a naive timestamp sort passes the
    count assertions above and FAILS this one."""
    cap = limits["hyp_capture"]
    flagged = 3
    reducer = {"slots": {"hyp_capture":
                         [entry(i, load_bearing=i < flagged) for i in range(cap - 1)]}}
    body = {"slots": {"hyp_capture": [entry(50 + i) for i in range(6)]}}

    out = bmg.merge_wm(reducer, body)

    merged = out["slots"]["hyp_capture"]
    assert len(merged) == cap
    kept = sum(1 for x in merged if x.get("load_bearing"))
    assert kept == flagged, (
        f"{flagged - kept} load-bearing entries were evicted before unflagged "
        "ones — merge-time eviction is not using wm._eviction_sort_key"
    )


def test_policy_is_imported_not_reimplemented():
    """Structural pin (rb-8183 shape). The eviction sort must have exactly ONE
    definition; body-merge must call it, not carry a copy. A second copy agrees
    on the day it is written and stops agreeing silently — which is the whole
    reason this file's subject was broken for its entire lifetime."""
    src = (SCRIPTS / "body-merge.py").read_text(encoding="utf-8")
    assert "wm.enforce_slot_limit" in src, \
        "body-merge no longer calls the shared limiter"
    assert "def _eviction_sort_key" not in src, \
        "body-merge grew its own copy of the eviction key — import it instead"
    assert "load_bearing" not in src.split("def merge_wm", 1)[1].split("\ndef ", 1)[0], \
        "merge_wm inlined the load_bearing rule instead of delegating to wm.py"


# ── the eviction tally ──────────────────────────────────────────────────────

def test_evictions_are_tallied_at_top_level(bmg, limits):
    """capture_evictions is TOP-LEVEL and not slot_meta on purpose: slot_meta is
    merged reducer-wins immediately below the enforcement point, so a counter
    parked there is dropped at exactly this step. Mirrors cmd_append's key."""
    cap = limits["exp_capture"]
    reducer = {"slots": {"exp_capture": [entry(i) for i in range(cap + 4)]}}
    out = bmg.merge_wm(reducer, {"slots": {}})
    assert out["capture_evictions"]["exp_capture"] == 4


def test_tally_accumulates_onto_an_existing_count(bmg, limits):
    """A Body arrives carrying its OWN append-path eviction tally. Merge-time
    evictions must ADD to it. Overwriting would silently erase the write-path
    count the moment the Body lands — and that count is the only evidence any
    reader has that the append path is destroying entries (g-306-308)."""
    cap = limits["spark_capture"]
    reducer = {"slots": {"spark_capture": [entry(i) for i in range(cap + 2)]},
               "capture_evictions": {"spark_capture": 7}}
    body = {"slots": {}, "capture_evictions": {"spark_capture": 5}}

    out = bmg.merge_wm(reducer, body)

    total = out["capture_evictions"]["spark_capture"]
    assert total >= 12 + 2, (
        f"tally is {total}: merge-time evictions (2) must ADD to the merged "
        "pre-existing counts (7 reducer + 5 body), never replace them"
    )


def test_no_tally_key_is_created_when_nothing_is_evicted(bmg, limits):
    """A merge that evicts nothing must not stamp an empty counter — an
    always-present `capture_evictions: {}` reads as 'measured zero' rather than
    'never fired', which is the ambiguity guard-1641 is about."""
    reducer = {"slots": {"spark_capture": [entry(i) for i in range(3)]}}
    out = bmg.merge_wm(reducer, {"slots": {}})
    assert "capture_evictions" not in out


# ── the shared helper, directly ─────────────────────────────────────────────

@pytest.mark.parametrize("limit", [0, None, ""])
def test_enforce_slot_limit_is_a_noop_without_a_limit(limit):
    arr = [entry(i) for i in range(5)]
    assert wm.enforce_slot_limit(arr, limit) == 0
    assert len(arr) == 5


def test_enforce_slot_limit_is_a_noop_when_already_fitting():
    arr = [entry(i) for i in range(5)]
    assert wm.enforce_slot_limit(arr, 5) == 0
    assert len(arr) == 5


def test_enforce_slot_limit_mutates_in_place_and_returns_the_count():
    """In-place is the contract both callers rely on: cmd_append holds `arr` as
    a reference into the WM dict, and merge_wm holds it inside m_slots."""
    arr = [entry(i) for i in range(12)]
    same = arr
    assert wm.enforce_slot_limit(arr, 4) == 8
    assert len(arr) == 4
    assert same is arr
