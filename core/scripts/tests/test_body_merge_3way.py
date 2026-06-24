"""Phase 2B (): body-merge.py 3-way delta merge using baseline content.

The Phase-1C merge was 2-way (reducer + body). For NUMERIC counters that
double-counts the fork baseline B each side inherited: with reducer=r and
body=b both forked from B, `r + b` counts B twice. The 3-way delta merges by
the body's NET divergence from the common ancestor: `r + (b - B)`, counting B
once (the reducer already carries it). Arrays (union+dedup) and timestamps
(latest-wins) are baseline-immune and unchanged. When no baseline content is
available (dormant single-runner, or a staged orphan that carries only the
hash) the merge degrades to the original 2-way SUM (backward-compatible).

Daemon-safe (no daemon_integration marker -- pure dict arithmetic; the
generalize_down case uses a tmp project_root override, the body-merge.py pattern).

Run:
  python -m pytest core/scripts/tests/test_body_merge_3way.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

import yaml

CORE_SCRIPTS = Path(__file__).resolve().parent.parent      # core/scripts/
if str(CORE_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(CORE_SCRIPTS))


def _load(modname: str, filename: str):
    spec = importlib.util.spec_from_file_location(modname, CORE_SCRIPTS / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load("body_merge", "body-merge.py")

SID = "33333333-3333-4333-8333-333333333333"


# ───────────────────────── merge_wm 3-way unit ─────────────────────────

def test_3way_numeric_counter_uses_net_delta():
    # The canonical bug: 2-way SUM double-counts the shared baseline.
    baseline = {"slots": {"loop_state": {"goals_completed": 5}}}
    reducer = {"slots": {"loop_state": {"goals_completed": 8}}}   # reducer +3
    body = {"slots": {"loop_state": {"goals_completed": 7}}}      # body +2
    merged = merge.merge_wm(reducer, body, baseline)
    # 3-way: 8 + (7 - 5) = 10. (2-way 8 + 7 = 15 would double-count the 5.)
    assert merged["slots"]["loop_state"]["goals_completed"] == 10


def test_2way_fallback_when_no_baseline():
    reducer = {"slots": {"loop_state": {"goals_completed": 8}}}
    body = {"slots": {"loop_state": {"goals_completed": 7}}}
    merged = merge.merge_wm(reducer, body)  # baseline=None -> 2-way SUM (unchanged)
    assert merged["slots"]["loop_state"]["goals_completed"] == 15


def test_3way_no_divergence_yields_reducer():
    # A Body that never advanced the counter (body == baseline) contributes 0.
    baseline = {"slots": {"c": 5}}
    reducer = {"slots": {"c": 9}}
    body = {"slots": {"c": 5}}
    merged = merge.merge_wm(reducer, body, baseline)
    assert merged["slots"]["c"] == 9  # 9 + (5 - 5)


def test_3way_nested_signals_counter():
    # The baseline threads recursively into loop_state.signals.*.
    baseline = {"slots": {"loop_state": {"signals": {"productive_streak": 2}}}}
    reducer = {"slots": {"loop_state": {"signals": {"productive_streak": 4}}}}
    body = {"slots": {"loop_state": {"signals": {"productive_streak": 3}}}}
    merged = merge.merge_wm(reducer, body, baseline)
    assert merged["slots"]["loop_state"]["signals"]["productive_streak"] == 5  # 4+(3-2)


def test_3way_arrays_still_union_dedup_baseline_immune():
    baseline = {"slots": {"encoding_queue": ["a"]}}
    reducer = {"slots": {"encoding_queue": ["a", "r1"]}}
    body = {"slots": {"encoding_queue": ["a", "b1"]}}
    merged = merge.merge_wm(reducer, body, baseline)
    # union+dedup: the shared baseline "a" is deduped, both deltas kept.
    assert merged["slots"]["encoding_queue"] == ["a", "r1", "b1"]


def test_3way_body_only_counter_carried_without_baseline_entry():
    # A counter the baseline lacks: body value carried (no delta to subtract).
    baseline = {"slots": {"loop_state": {"goals_completed": 5}}}
    reducer = {"slots": {"loop_state": {"goals_completed": 8}}}
    body = {"slots": {"loop_state": {"goals_completed": 7, "new_counter": 3}}}
    merged = merge.merge_wm(reducer, body, baseline)
    assert merged["slots"]["loop_state"]["goals_completed"] == 10
    assert merged["slots"]["loop_state"]["new_counter"] == 3


def test_3way_float_baseline_does_not_treat_bool_as_number():
    # Bools must stay reducer-wins even with a numeric-looking baseline.
    baseline = {"slots": {"flag": False}}
    reducer = {"slots": {"flag": True}}
    body = {"slots": {"flag": False}}
    merged = merge.merge_wm(reducer, body, baseline)
    assert merged["slots"]["flag"] is True  # reducer-wins, never True+False arithmetic


# ─────────────── generalize_down end-to-end with a baseline file ───────────────

def _mk_agent_with_wm(tmp_path: Path, reducer_wm: dict, name: str = "alpha") -> Path:
    state = tmp_path / "agents" / name / "session"
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "working-memory.yaml", "w", encoding="utf-8") as f:
        yaml.dump(reducer_wm, f, default_flow_style=False, sort_keys=False)
    return tmp_path


def test_generalize_down_reads_baseline_file_for_3way(tmp_path):
    pr = _mk_agent_with_wm(tmp_path, {"slots": {"loop_state": {"goals_completed": 8}}})
    bd = pr / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    # body WM diverged to 7 (NOT equal to the fork baseline 5 -> not a no-op).
    (bd / "working-memory.yaml").write_text(
        "slots:\n  loop_state:\n    goals_completed: 7\n", encoding="utf-8")
    # immutable fork-time baseline snapshot (5).
    (bd / "forked-wm-baseline.yaml").write_text(
        "slots:\n  loop_state:\n    goals_completed: 5\n", encoding="utf-8")
    # forked_wm_hash = hash of the baseline bytes; the diverged body won't match,
    # so the sessions-pass no-op short-circuit is skipped and the body merges.
    bh = hashlib.sha256((bd / "forked-wm-baseline.yaml").read_bytes()).hexdigest()
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nbody_state: closed-pending-merge\nforked_wm_hash: {bh}\n",
        encoding="utf-8")

    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["merged"]
    red = yaml.safe_load(
        (pr / "agents" / "alpha" / "session" / "working-memory.yaml").read_text(encoding="utf-8"))
    # 3-way: 8 + (7 - 5) = 10 (NOT the 2-way 15).
    assert red["slots"]["loop_state"]["goals_completed"] == 10


def test_generalize_down_2way_when_baseline_file_absent(tmp_path):
    # No forked-wm-baseline.yaml -> generalize_down falls back to the 2-way SUM.
    pr = _mk_agent_with_wm(tmp_path, {"slots": {"loop_state": {"goals_completed": 8}}})
    bd = pr / "agents" / "alpha" / "sessions" / SID
    bd.mkdir(parents=True, exist_ok=True)
    (bd / "working-memory.yaml").write_text(
        "slots:\n  loop_state:\n    goals_completed: 7\n", encoding="utf-8")
    (bd / "body-manifest.yaml").write_text(
        f"unitKey: {SID}\nbody_state: closed-pending-merge\n", encoding="utf-8")
    summary = merge.generalize_down("alpha", project_root=pr)
    assert SID in summary["merged"]
    red = yaml.safe_load(
        (pr / "agents" / "alpha" / "session" / "working-memory.yaml").read_text(encoding="utf-8"))
    assert red["slots"]["loop_state"]["goals_completed"] == 15  # 2-way fallback


if __name__ == "__main__":
    sys.exit(__import__("pytest").main([__file__, "-q"]))
