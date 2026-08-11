"""Pins for the worker->reducer spark-capture bridge ().

A WORKER Body skips every reducer-only phase, so six learning lanes are
structurally unreachable on the worker path (rb-creation, guardrail-extraction,
gotcha-detection, forge-skill, pattern-outcome, experience-file-loading).
Specimen g-315-518: worker executed, hypothesis resolved, commit pushed, ZERO
learning artifacts. The bridge is: worker APPENDS to the `spark_capture` WM
slot -> body-merge.merge_wm carries it at generalize-down -> aspirations-spark
Phase 6.5 replays it in the reducer.

The transport itself needed no body-merge code change (merge_wm's generic
array/absent-slot handling already covers it), which is exactly why it needs
pinning: a future refactor of `_merge_value` or of the body-only-slot branch
would break this bridge with nothing naming it. Three surfaces are pinned:

  1. TRANSPORT   -- merge_wm carries the slot in both the absent-on-reducer and
                    present-on-both shapes, and does NOT collapse entries that
                    differ only by goal_id (the review's dedup-collision risk).
  2. SURVIVAL    -- spark_capture is in RESET_SURVIVING_SLOTS, so consolidate
                    Step-5 wm-reset does not wipe what Step -1 just delivered
                    (delivery and reset are in the SAME consolidation run).
  3. NON-EVICTION-- spark_capture is in ARRAY_SLOTS, so wm-prune's scalar
                    eviction does not null a populated slot at the 120-minute
                    evict threshold while its Body waits for consolidation.

Constant PARITY with the daemon mirror is owned by
test_wm_reset_cadence.py::test_shared_wm_constants_parity_with_daemon; this
file pins MEMBERSHIP on both sides so a one-sided deletion fails here with a
message naming the bridge, not just "sets differ".

Run:
  STORAGE_BACKEND=local python3 -m pytest core/scripts/tests/test_spark_capture_bridge.py -q
"""
from __future__ import annotations

import ast
import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

CORE_SCRIPTS = Path(__file__).resolve().parent.parent          # core/scripts/
PROJECT_ROOT = CORE_SCRIPTS.parent.parent
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))

import wm  # noqa: E402

SLOT = "spark_capture"


def _load(modname: str, filename: str):
    """Import a hyphenated core/scripts module by path (the test_two_body_parity
    pattern -- `body-merge.py` is not a legal module name)."""
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge_spark", "body-merge.py")


def _entry(goal_id: str, observation: str, category: str = "cross-box-bodies",
           sq_trigger=None, ts: str = "2026-08-04T00:00:00") -> dict:
    """One spark_capture item in its documented shape (working-memory.md).

    `_item_ts` is present because the append endpoint stamps every dict item --
    a fixture without it would exercise a shape production never produces
    (guard-920: replicate the literal production arg shape).
    """
    return {
        "goal_id": goal_id,
        "category": category,
        "observation": observation,
        "sq_trigger": sq_trigger,
        "_item_ts": ts,
    }


# ---------------------------------------------------------------------------
# 1. TRANSPORT (goal verification outcome 2)
# ---------------------------------------------------------------------------

def test_merge_carries_body_only_spark_capture():
    """The reducer has never seen the slot -- the whole payload must survive.

    This is the FIRST-worker case and the common one: the reducer's WM has no
    `spark_capture` key at all, so `_merge_value` is never called and the
    body-only branch of merge_wm is what carries it.
    """
    body_items = [_entry("g-315-518", "worker path produced no learning artifacts")]
    reducer = {"slots": {"sensory_buffer": []}, "slot_meta": {}}
    body = {"slots": {SLOT: body_items}, "slot_meta": {}}

    merged = merge.merge_wm(reducer, body)

    assert merged["slots"][SLOT] == body_items, (
        "body-only spark_capture was not carried into the reducer WM -- the "
        "worker->reducer spark bridge (g-306-176) is severed")


def test_merge_unions_when_both_sides_hold_capture():
    """Second worker merging into a reducer that already holds a first batch."""
    first = _entry("g-306-001", "first observation", ts="2026-08-04T00:00:01")
    second = _entry("g-306-002", "second observation", ts="2026-08-04T00:00:02")
    reducer = {"slots": {SLOT: [first]}, "slot_meta": {}}
    body = {"slots": {SLOT: [second]}, "slot_meta": {}}

    merged = merge.merge_wm(reducer, body)

    assert merged["slots"][SLOT] == [first, second], (
        "union lost an entry or reordered the reducer's items")


def test_identical_observations_from_distinct_goals_both_survive():
    """THE dedup-collision risk the design review flagged.

    `_dedup_append` unions by CONTENT HASH. Two workers that phrase the same
    lesson identically would collapse to one entry -- and the second goal's
    learning would vanish with no error anywhere. `goal_id` inside each entry is
    what makes the hashes differ. Held identical on every other field (including
    `_item_ts`) so goal_id is the ONLY thing under test.
    """
    text = "bare open(..., 'a') corrupts under concurrent writes"
    a = _entry("g-306-101", text)
    b = _entry("g-306-102", text)
    assert a["_item_ts"] == b["_item_ts"], "fixture must isolate goal_id"

    merged = merge.merge_wm({"slots": {SLOT: [a]}, "slot_meta": {}},
                            {"slots": {SLOT: [b]}, "slot_meta": {}})

    assert merged["slots"][SLOT] == [a, b], (
        "distinct-goal entries with identical observation text were deduped -- "
        "one goal's learning is silently lost (g-306-176 risk register)")


def test_byte_identical_entry_is_deduped():
    """The other half of the contract: a genuine re-merge of the SAME staged WM
    must not duplicate. Without this, the goal_id defense above could be
    'satisfied' by disabling dedup, which would re-encode every replay."""
    e = _entry("g-306-103", "same entry, merged twice")

    merged = merge.merge_wm({"slots": {SLOT: [e]}, "slot_meta": {}},
                            {"slots": {SLOT: [dict(e)]}, "slot_meta": {}})

    assert merged["slots"][SLOT] == [e], "re-merge of an identical entry duplicated it"


def test_spark_capture_is_not_reducer_wins():
    """A REDUCER_WINS_KEYS member would drop the Body's items outright. The
    bridge depends on this slot NOT being on that list."""
    assert SLOT not in merge.REDUCER_WINS_KEYS, (
        "spark_capture became reducer-wins -- worker captures would be discarded "
        "at generalize-down while the merge still reported success")


def test_end_to_end_fork_capture_generalize_down(tmp_path):
    """The REAL entry point, not the pure function (goal outcomes 1 + 2).

    Reuses the two-Body harness from test_two_body_parity rather than
    hand-rolling an Nth copy of it (the g-306-178 lesson: 7 duplicated
    fake-backend helpers already exist and nobody wants an 8th). This exercises
    fork -> worker append -> genuine close -> merge.generalize_down, so a
    regression in manifest enumeration, baseline handling, or the body-only-slot
    branch fails here even though merge_wm alone would still pass.

    The append goes through wm.cmd_append with BODY_WM_PATH pointed at the
    FORKED Body WM -- the same resolution wm-append.sh gets from the injected
    env -- so this also pins lazy creation of an absent array slot.
    """
    from test_two_body_parity import (REDUCER_SID, WORKER_SID, _body_wm,
                                      _mk_mind, _reducer_wm, bm)
    from test_two_body_parity import merge as tb_merge

    pr = _mk_mind(tmp_path, {"slots": {"encoding_queue": []}})
    bm.write_manifest(REDUCER_SID, "alpha", role="worker", project_root=pr)
    bm.write_manifest(WORKER_SID, "alpha", role="worker", project_root=pr)
    worker_wm = _body_wm(pr, WORKER_SID)
    assert worker_wm.exists(), "worker did not fork -- harness precondition"

    # The reducer has no spark_capture at all, which is the first-worker shape.
    assert SLOT not in (_reducer_wm(pr).get("slots") or {})

    # cmd_append takes its item on STDIN (not args.value) -- same as the
    # `echo '<json>' | wm-append.sh <slot>` shape the worker skill uses.
    import io
    original = os.environ.get("BODY_WM_PATH")
    original_stdin = sys.stdin
    os.environ["BODY_WM_PATH"] = str(worker_wm)
    sys.stdin = io.StringIO(
        '{"goal_id":"g-315-518","category":"cross-box-bodies",'
        '"observation":"worker path produced zero learning artifacts",'
        '"sq_trigger":null}')
    try:
        wm.cmd_append(SimpleNamespace(slot=SLOT))
    finally:
        sys.stdin = original_stdin
        if original is None:
            os.environ.pop("BODY_WM_PATH", None)
        else:
            os.environ["BODY_WM_PATH"] = original

    import yaml
    body_slots = yaml.safe_load(worker_wm.read_text(encoding="utf-8"))["slots"]
    assert len(body_slots[SLOT]) == 1, (
        "append did not land in the forked Body WM (goal outcome 1)")
    captured = body_slots[SLOT][0]
    assert captured["goal_id"] == "g-315-518"

    bm.set_state(WORKER_SID, "alpha", "closed-pending-merge", project_root=pr)
    summary = tb_merge.generalize_down("alpha", project_root=pr)
    assert WORKER_SID in summary["merged"]

    merged = _reducer_wm(pr)["slots"]
    assert merged.get(SLOT) == [captured], (
        "generalize_down did not deliver spark_capture to the reducer WM -- the "
        "bridge is severed at the real entry point (goal outcome 2)")


# ---------------------------------------------------------------------------
# 2. SURVIVAL across consolidate Step-5 wm-reset (goal verification outcome 3)
# ---------------------------------------------------------------------------

def test_reset_preserves_spark_capture():
    """body-merge delivers at consolidate Step -1; wm-reset runs at Step 5 of the
    SAME run. Without the RESET_SURVIVING_SLOTS exemption the payload is wiped
    ~5 steps after arriving, before any Phase 6.5 could consume it."""
    items = [_entry("g-306-201", "must outlive the Step-5 reset")]
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpdir:
        # BODY_WM_PATH is the ONLY correct redirect -- patching wm.WM_PATH is a
        # no-op for I/O and would target the live agent's WM (guard-862).
        os.environ["BODY_WM_PATH"] = str(Path(tmpdir) / "working-memory.yaml")
        try:
            wm.cmd_init(SimpleNamespace())
            data = wm.read_wm()
            data["slots"][SLOT] = items
            wm.write_wm(data)

            wm.cmd_reset(SimpleNamespace())

            after = wm.read_wm()
            assert after["slots"].get(SLOT) == items, (
                "wm-reset wiped spark_capture -- consolidate Step 5 would destroy "
                "what Step -1 delivered in the same run (g-306-176)")
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


# ---------------------------------------------------------------------------
# 3. NON-EVICTION by wm-prune
# ---------------------------------------------------------------------------

def test_prune_does_not_evict_stale_populated_capture():
    """The scalar-eviction predicate is `slot_name not in ARRAY_SLOTS and ...
    slot_val is not None`, and a non-empty list is not None. A Body that
    captures and then waits >120min for consolidation would lose the slot."""
    items = [_entry("g-306-301", "captured long before the reducer consolidated")]
    original = os.environ.get("BODY_WM_PATH")
    with tempfile.TemporaryDirectory() as tmpdir:
        os.environ["BODY_WM_PATH"] = str(Path(tmpdir) / "working-memory.yaml")
        try:
            wm.cmd_init(SimpleNamespace())
            data = wm.read_wm()
            data["slots"][SLOT] = items
            # Stamp the slot far past evict_threshold_minutes (120).
            data.setdefault("slot_meta", {})[SLOT] = {
                "updated_at": "2020-01-01T00:00:00",
                "accessed_at": "2020-01-01T00:00:00",
                "update_count": 1,
            }
            wm.write_wm(data)

            wm.cmd_prune(SimpleNamespace(dry_run=False, json=True))

            after = wm.read_wm()
            assert after["slots"].get(SLOT) == items, (
                "wm-prune evicted a populated spark_capture -- ARRAY_SLOTS "
                "membership is what prevents this (g-306-176)")
        finally:
            if original is None:
                os.environ.pop("BODY_WM_PATH", None)
            else:
                os.environ["BODY_WM_PATH"] = original


# ---------------------------------------------------------------------------
# 4. MEMBERSHIP on both sides of the hand-mirror (guard-2323 / guard-1189)
# ---------------------------------------------------------------------------

def _daemon_constant(name: str) -> set:
    """AST-read a constant from the daemon endpoint without importing it (the
    module pulls in server-side deps this test does not need)."""
    src = (PROJECT_ROOT / "mind_api" / "src" / "endpoints" / "wm_write.py").read_text(
        encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == name):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in the daemon mirror")


def test_membership_present_on_both_sides():
    """wm-append.sh / wm-reset.sh are DAEMON-ONLY, so the daemon copy is the LIVE
    path -- a CLI-only edit here would leave the bridge inert in production while
    every CLI-level test above still passed (guard-2323)."""
    for const in ("ARRAY_SLOTS", "RESET_SURVIVING_SLOTS"):
        assert SLOT in getattr(wm, const), f"wm.py {const} lost {SLOT} (g-306-176)"
        assert SLOT in _daemon_constant(const), (
            f"daemon wm_write.py {const} lost {SLOT} -- the LIVE wm-append/"
            f"wm-reset path would diverge from the CLI (g-306-176)")


def test_array_limit_is_configured():
    """The slot is reset-surviving, so a window where the reducer never runs
    Phase 6.5 would otherwise grow it without bound."""
    import yaml
    cfg = yaml.safe_load(
        (PROJECT_ROOT / "core" / "config" / "memory-pipeline.yaml").read_text(
            encoding="utf-8"))
    limits = (cfg.get("working_memory_pruning") or {}).get("array_limits") or {}
    assert isinstance(limits.get(SLOT), int) and limits[SLOT] > 0, (
        "spark_capture has no array_limits entry -- an unconsumed slot survives "
        "every reset and grows without bound (g-306-176)")
